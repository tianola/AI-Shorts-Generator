"""Render manifest: a record of exactly how a clip was rendered.

The manifest captures every input that can change the output of a render
(renderer and ffmpeg versions, the source file's identity, layout, caption and
edit choices, the relevant settings, the asset files and the filtergraph) so a
render can be reproduced and two renders compared. ``manifest_fingerprint``
gives a stable SHA-256 over the whole manifest.

Pure helpers: this module does not import ``worker.py`` and only touches
``config`` and ``services.audio_polish`` lazily inside ``build_manifest``.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

RENDERER_VERSION = '2026.11.0'
MANIFEST_VERSION = 1
MANIFEST_FILENAME = 'render_manifest.json'

_SETTINGS_KEYS = (
    'FFMPEG_PRESET',
    'FFMPEG_CRF',
    'FFMPEG_THREADS',
    'SINGLE_PASS_RENDER',
    'EFFECTS_ENABLED',
    'EFFECTS_DISABLED_KINDS',
    'SOUND_DESIGN_ENABLED',
    'SFX_SIDECHAIN_ENABLED',
    'SFX_DUCK_THRESHOLD',
    'SFX_DUCK_RATIO',
    'SFX_DUCK_ATTACK_MS',
    'SFX_DUCK_RELEASE_MS',
    'CAPTION_EMOJI_MODE',
)


def file_sha256(path, chunk: int = 1 << 20) -> Optional[str]:
    """Hex SHA-256 of a file, or None if it is missing or unreadable."""
    try:
        digest = hashlib.sha256()
        with open(path, 'rb') as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest()
    except (OSError, TypeError, ValueError):
        return None


@functools.lru_cache(maxsize=1)
def ffmpeg_version() -> Optional[str]:
    """First line of ``ffmpeg -version``, or None if ffmpeg is unavailable."""
    try:
        proc = subprocess.run(
            ['ffmpeg', '-version'],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    first = (proc.stdout or '').strip().splitlines()
    return first[0].strip() if first else None


def git_commit(repo_dir=None) -> Optional[str]:
    """``git rev-parse HEAD`` for the repo root, or None on failure."""
    if repo_dir is None:
        repo_dir = Path(__file__).resolve().parents[2]
    try:
        proc = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(repo_dir),
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    commit = (proc.stdout or '').strip()
    return commit or None


def _settings_snapshot() -> dict:
    try:
        from config import settings
    except Exception:  # config may fail to load in a bare environment
        return {key: None for key in _SETTINGS_KEYS}
    return {key: getattr(settings, key, None) for key in _SETTINGS_KEYS}


def _loudnorm_snapshot() -> dict:
    try:
        from services.audio_polish import (
            LOUDNORM_TARGET_I, LOUDNORM_TARGET_TP, LOUDNORM_TARGET_LRA,
        )
    except Exception:
        return {'I': None, 'TP': None, 'LRA': None}
    return {'I': LOUDNORM_TARGET_I, 'TP': LOUDNORM_TARGET_TP, 'LRA': LOUDNORM_TARGET_LRA}


def _asset_hashes(paths: Iterable) -> dict:
    """{basename: sha256} sorted by basename; duplicate basenames collapse."""
    out: dict = {}
    for p in paths or ():
        name = os.path.basename(str(p))
        if name in out:
            continue
        out[name] = file_sha256(p)
    return dict(sorted(out.items()))


def build_manifest(
    *,
    job_id,
    clip_index,
    clip_start,
    clip_end,
    source_path=None,
    layout=None,
    caption_style=None,
    caption_motion=None,
    edit_profile=None,
    tone=None,
    timeline_path=None,
    sticker_paths=(),
    sfx_paths=(),
    font_paths=(),
    filtergraph=None,
    anti_fingerprint=None,
    extra=None,
) -> dict:
    """Assemble the JSON-serialisable manifest for one rendered clip."""
    source = {'name': None, 'size': None, 'mtime': None}
    if source_path is not None:
        source['name'] = os.path.basename(str(source_path))
        try:
            st = os.stat(source_path)
            source['size'] = st.st_size
            source['mtime'] = st.st_mtime
        except OSError:
            pass

    return {
        'manifest_version': MANIFEST_VERSION,
        'renderer_version': RENDERER_VERSION,
        'git_commit': git_commit(),
        'ffmpeg_version': ffmpeg_version(),
        'job_id': job_id,
        'clip_index': clip_index,
        'window': {'start': clip_start, 'end': clip_end},
        'source': source,
        'layout': layout,
        'caption': {'style': caption_style, 'motion': caption_motion},
        'edit': {
            'profile': edit_profile,
            'tone': tone,
            'timeline_sha256': file_sha256(timeline_path) if timeline_path else None,
        },
        'settings': _settings_snapshot(),
        'loudnorm': _loudnorm_snapshot(),
        'assets': {
            'stickers': _asset_hashes(sticker_paths),
            'sfx': _asset_hashes(sfx_paths),
            'fonts': _asset_hashes(font_paths),
        },
        'filtergraph': filtergraph,
        'anti_fingerprint': anti_fingerprint,
        'extra': dict(extra) if extra else {},
    }


def manifest_fingerprint(manifest: dict) -> str:
    """SHA-256 hex of the canonical JSON form of the whole manifest."""
    canonical = json.dumps(
        manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def write_manifest(clip_dir, manifest: dict) -> str:
    """Atomically write ``render_manifest.json`` into ``clip_dir``; returns its path."""
    clip_dir = str(clip_dir)
    os.makedirs(clip_dir, exist_ok=True)
    final_path = os.path.join(clip_dir, MANIFEST_FILENAME)
    fd, tmp_path = tempfile.mkstemp(dir=clip_dir, prefix='.render_manifest.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, final_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return final_path


def load_manifest(clip_dir) -> Optional[dict]:
    """Read ``render_manifest.json``; None if missing, invalid, or not an object."""
    path = os.path.join(str(clip_dir), MANIFEST_FILENAME)
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def diff_manifests(a: dict, b: dict) -> list[str]:
    """Sorted dotted key paths whose values differ between two manifests."""
    diffs: list[str] = []

    def _walk(x: Any, y: Any, prefix: str) -> None:
        if isinstance(x, dict) and isinstance(y, dict):
            for key in set(x) | set(y):
                path = f'{prefix}.{key}' if prefix else str(key)
                if key not in x or key not in y:
                    diffs.append(path)
                else:
                    _walk(x[key], y[key], path)
        elif x != y:
            diffs.append(prefix)

    _walk(a, b, '')
    return sorted(diffs)
