"""A resumed render must not lose, or publish over, the operator's manual trim.

``api/features.py`` keeps a trim as ``final_video.trimmed.mp4`` beside the pristine
render (``utils/clip_trim.py``) and records ``trim_start``/``trim_end`` on the row.
Two things in ``worker.py`` used to undo that on the one path that publishes by
itself:

* the render cleanup deleted every ``.mp4`` in the clip directory except
  ``final_video.mp4``, so the trimmed artifact and its manifest entry went first;
* ``process_clip`` re-records the row with ``file_path`` back on the untrimmed
  render, because nothing after the cut ever re-applied the stored trim.

So a job resumed after a restart queued the long version the operator had cut out
of the timeline. Both are asserted here against real files and a real manifest.
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker
from services.trim import TrimValidationError
from utils import clip_trim, derived_cache


@pytest.fixture
def logged(monkeypatch):
    records = []
    monkeypatch.setattr(worker, "job_info",
                        lambda jid, m: records.append(("info", m)))
    monkeypatch.setattr(worker, "job_warning",
                        lambda jid, m: records.append(("warning", m)))
    return records


@pytest.fixture
def clip_dir(tmp_path, monkeypatch):
    """A clip directory holding a render, its trimmed cut, and the stage files."""
    root = tmp_path / "storage"
    d = root / "jobs" / "110" / "clip_0"
    d.mkdir(parents=True)
    (d / clip_trim.BASE_RENDER_NAME).write_bytes(b"the full render")
    monkeypatch.setattr(worker.settings, "STORAGE_ROOT", root)
    monkeypatch.setattr(derived_cache, "MANIFEST_NAME", derived_cache.MANIFEST_NAME)
    return d


class FakeDB:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


# ---------------------------------------------------------------------------
# the cleanup pass
# ---------------------------------------------------------------------------

class TestRenderCleanupKeepsTheTrimmedArtifact:
    def test_the_trimmed_cut_survives_while_the_stage_files_go(
            self, clip_dir, tmp_path):
        (clip_dir / "final_video.trimmed.mp4").write_bytes(b"trimmed")
        for stale in ("short.mp4", "vertical.mp4", "final_short.mp4",
                     "final_with_title.mp4", "captions.ass", "audio.wav"):
            (clip_dir / stale).write_bytes(b"stage")
        (clip_dir / "words.json").write_text("{}", encoding="utf-8")
        (clip_dir / "thumbnail.jpg").write_bytes(b"t")

        worker._prune_render_intermediates(clip_dir)

        left = sorted(os.listdir(str(clip_dir)))
        assert "final_video.trimmed.mp4" in left, (
            f"the resume deleted the only file matching the row's trim: {left}")
        assert "final_video.mp4" in left
        assert not {"short.mp4", "vertical.mp4", "captions.ass", "audio.wav"} & set(left)


# ---------------------------------------------------------------------------
# the re-apply seam
# ---------------------------------------------------------------------------

class TestResumeReappliesTheRecordedTrim:
    def _patch_recut(self, monkeypatch, start, end):
        calls = []

        def fake(base, trim_start, trim_end, output, force_reencode=False):
            calls.append((float(trim_start), float(trim_end)))
            with open(output, "wb") as handle:
                handle.write(b"recut")
            return {"duration": float(trim_end) - float(trim_start),
                    "used_fallback": False, "method": "recoded"}

        import services.trim as trim_module
        monkeypatch.setattr(trim_module, "trim_clip", fake)
        return calls

    def test_the_row_is_repointed_at_a_fresh_trim_of_the_new_render(
            self, clip_dir, monkeypatch, logged):
        calls = self._patch_recut(monkeypatch, 5.0, 20.0)
        row = SimpleNamespace(trim_start=5.0, trim_end=20.0,
                              file_path="jobs/110/clip_0/final_video.mp4")
        db = FakeDB()

        assert worker._reapply_recorded_trim(
            db, 110, row, clip_dir, 3175.0, 3205.0, 0) is True
        assert calls == [(5.0, 20.0)], "the recorded window was not re-cut"
        assert row.file_path.replace("\\", "/") == "jobs/110/clip_0/final_video.trimmed.mp4"
        assert db.commits == 1
        assert any("re-applied its recorded trim" in m for _, m in logged)

    def test_a_row_with_no_recorded_trim_is_left_alone(
            self, clip_dir, monkeypatch, logged):
        calls = self._patch_recut(monkeypatch, 0.0, 1.0)
        row = SimpleNamespace(trim_start=None, trim_end=None,
                             file_path="jobs/110/clip_0/final_video.mp4")

        assert worker._reapply_recorded_trim(
            FakeDB(), 110, row, clip_dir, 3175.0, 3205.0, 0) is False
        assert calls == []
        assert row.file_path == "jobs/110/clip_0/final_video.mp4"

    def test_a_trim_the_new_render_cannot_satisfy_publishes_untrimmed_and_says_so(
            self, clip_dir, monkeypatch, logged):
        def refuse(base, trim_start, trim_end, output, force_reencode=False):
            raise TrimValidationError("trim end 90.0s is past the render's 30.0s")

        import services.trim as trim_module
        monkeypatch.setattr(trim_module, "trim_clip", refuse)
        row = SimpleNamespace(trim_start=5.0, trim_end=90.0,
                              file_path="jobs/110/clip_0/final_video.mp4")

        assert worker._reapply_recorded_trim(
            FakeDB(), 110, row, clip_dir, 3175.0, 3205.0, 0) is False
        assert row.file_path == "jobs/110/clip_0/final_video.mp4", (
            "the row was repointed at a cut that does not exist")
        assert any("will publish untrimmed" in m for _, m in logged), (
            f"a trim that cannot be honoured was swallowed: {logged}")

    def test_the_reapplied_trim_is_reused_rather_than_recut_again(
            self, clip_dir, monkeypatch, logged):
        """A resume whose render did not move must not pay for a second cut."""
        calls = []

        def counted(base, trim_start, trim_end, output, force_reencode=False):
            calls.append(1)
            with open(output, "wb") as handle:
                handle.write(b"recut")
            return {"duration": 15.0, "used_fallback": False, "method": "recoded"}

        import services.trim as trim_module
        monkeypatch.setattr(trim_module, "trim_clip", counted)
        row = SimpleNamespace(trim_start=5.0, trim_end=20.0,
                              file_path="jobs/110/clip_0/final_video.mp4")
        window = (3175.0, 3205.0)

        first = worker._reapply_recorded_trim(FakeDB(), 110, row, clip_dir, *window, 0)
        # The manifest now vouches for that artifact, so the second pass is cached.
        row.file_path = "jobs/110/clip_0/final_video.mp4"
        second = worker._reapply_recorded_trim(FakeDB(), 110, row, clip_dir, *window, 0)

        assert first is True and second is True
        assert len(calls) == 1, "the same window was cut out of the same render twice"
