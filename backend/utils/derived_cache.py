"""Manifest-keyed cache decisions for derived media artifacts.

Why this exists
---------------
The worker used to decide "is this artifact already built?" with
``os.path.exists()`` alone, while storing every per-clip artifact under a
directory named by the clip *index* (``clip_0/``, ``clip_1/``). The index says
nothing about *which* window ended up in slot 0 this time round, and a partial
cleanup made that lethal: ``worker.py`` deletes ``short.mp4``/``captions.ass``
after a render but keeps ``clip_N/words.json``, so a re-run whose selector chose
new windows re-cut the picture and then cropped the *old* windows' transcript
out of the surviving file — job 110 published three shorts with a frame at
t=3175 s carrying a caption from t=0 s.

The rule enforced here
----------------------
An artifact may be reused only when the inputs that produced it are the inputs
this run is asking for. Those inputs (clip window, caption style, source
fingerprint, transcript model, the identity of the file an artifact was derived
from) are recorded in one small JSON manifest per directory —
``<dir>/artifact.json`` — written next to the artifacts it describes:

    {"version": 1,
     "artifacts": {"words.json":   {"kind": "clip_words",
                                    "digest": "9f2c…",
                                    "inputs": {"window": [3175.0, 3202.0], …}},
                   "captions.ass": {"kind": "captions", …}}}

Failures point at rebuilding, never at reusing: a missing manifest, an
unreadable one (the stall watchdog kills the process mid-write), an unknown
artifact, a different kind, a deleted artifact — all answer ``is_current() ==
False``. The one deliberate exception is ``legacy_current=True``, for artifacts
whose rebuild cost is enormous and whose stated invariant is already held
elsewhere (see ``stage_transcribe``).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

MANIFEST_NAME = "artifact.json"
_VERSION = 1


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------

def input_fingerprint(path: Any) -> dict[str, Any]:
    """Cheap identity of an *input* file, for chaining one artifact to another.

    Deliberately size + mtime, not content: hashing a 3-hour mp4 to decide a
    cache skip is worse than the work being skipped. The pair changes whenever
    the file is rewritten, which is exactly the signal wanted when captions
    depend on a words.json that just got re-cropped.
    """
    try:
        stat = os.stat(str(path))
    except OSError:
        return {"absent": True}
    return {
        "name": os.path.basename(os.path.abspath(str(path))),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def source_fingerprint(job_dir: Any, video_path: Any | None = None) -> dict[str, Any]:
    """Identity of the source a job's derived files were built from.

    ``source.urlhash`` is written by ``stage_download_source`` on every path
    through that stage, so it carries the URL the file on disk is claimed to be;
    the stat catches a source.mp4 that was replaced out from under the hash.
    """
    directory = str(job_dir)
    try:
        with open(os.path.join(directory, "source.urlhash"), "r", encoding="utf-8") as handle:
            urlhash = handle.read().strip()
    except OSError:
        urlhash = ""
    return {
        "urlhash": urlhash,
        "file": input_fingerprint(video_path or os.path.join(directory, "source.mp4")),
    }


def _digest(kind: str, inputs: dict[str, Any] | None) -> str:
    payload = json.dumps({"kind": str(kind), "inputs": inputs or {}},
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# manifest I/O
# ---------------------------------------------------------------------------

def manifest_path(artifact_path: Any) -> str:
    absolute = os.path.abspath(str(artifact_path))
    return os.path.join(os.path.dirname(absolute), MANIFEST_NAME)


def _entry_name(artifact_path: Any) -> str:
    # Basename, because the manifest lives in the same directory as its
    # artifacts: clip_0/words.json and clip_1/words.json get their own file and
    # can never describe each other.
    return os.path.basename(os.path.abspath(str(artifact_path)))


def _load_manifest(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        # Absent, unreadable (the worker was killed mid-write) or from a format
        # this build does not know. Every one of those means "no evidence", and
        # no evidence means rebuild.
        return {}
    if not isinstance(data, dict) or data.get("version") != _VERSION:
        return {}
    artifacts = data.get("artifacts")
    return artifacts if isinstance(artifacts, dict) else {}


def _save_manifest(path: str, artifacts: dict[str, Any]) -> bool:
    temporary = f"{path}.tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"version": _VERSION, "artifacts": artifacts},
                      handle, indent=2, sort_keys=True, default=str)
            handle.flush()
        os.replace(temporary, path)
        return True
    except OSError:
        try:
            os.remove(temporary)
        except OSError:
            pass
        # Losing the manifest only costs a rebuild on the next run; it must
        # never cost the render that is in progress.
        return False


# ---------------------------------------------------------------------------
# the two decisions every call site needs
# ---------------------------------------------------------------------------

def is_current(artifact_path: Any, kind: str, inputs: dict[str, Any] | None = None,
               *, legacy_current: bool = False) -> bool:
    """True only when ``artifact_path`` exists *and* was recorded as built from
    exactly ``kind`` + ``inputs``.

    ``legacy_current`` answers "no entry at all" for artifacts written before
    this module existed. Use it only where a wrong guess is both cheap to detect
    later and ruinous to rebuild (Whisper on a three-hour source), never for the
    per-clip text — a legacy ``clip_N/words.json`` is precisely the file job 110
    got wrong.
    """
    if not os.path.exists(str(artifact_path)):
        return False
    entry = _load_manifest(manifest_path(artifact_path)).get(_entry_name(artifact_path))
    if not isinstance(entry, dict):
        return bool(legacy_current)
    return entry.get("digest") == _digest(kind, inputs)


def record(artifact_path: Any, kind: str, inputs: dict[str, Any] | None = None) -> bool:
    """Note that ``artifact_path`` was just produced from ``kind`` + ``inputs``."""
    path = manifest_path(artifact_path)
    artifacts = _load_manifest(path)
    artifacts[_entry_name(artifact_path)] = {
        "kind": str(kind),
        "digest": _digest(kind, inputs),
        "inputs": inputs or {},
    }
    return _save_manifest(path, artifacts)


def forget(artifact_path: Any) -> bool:
    """Drop one artifact's entry.

    Called *before* a rebuild: if the build dies half-way (a watchdog kill, an
    ffmpeg failure, a truncated JSON), what is left on disk must not still carry
    a matching key.
    """
    path = manifest_path(artifact_path)
    artifacts = _load_manifest(path)
    name = _entry_name(artifact_path)
    if name not in artifacts:
        return True
    artifacts.pop(name)
    if not artifacts:
        try:
            os.remove(path)
            return True
        except OSError:
            return False
    return _save_manifest(path, artifacts)
