"""Disk preflight and transcription-input cleanup.

E: sits at 100% full, and job 80 alone is 19 GB of source video plus audio. The
worker has to notice that before it claims a job, and the wav that only the
transcriber reads has to go once words.json exists.
"""
import os

import pytest

from config import settings
from utils import disk


@pytest.fixture
def job_dir(tmp_path):
    d = tmp_path / "jobs" / "9901"
    d.mkdir(parents=True)
    return d


def _write(path, content="x", size=0):
    path.write_text(content)
    if size:
        with open(path, "ab") as f:
            f.truncate(size)
    return str(path)


def test_free_gb_matches_shutil_for_a_path(tmp_path):
    import shutil

    assert disk.free_gb(str(tmp_path)) == pytest.approx(shutil.disk_usage(str(tmp_path)).free / 1024**3)


def test_headroom_is_refused_below_the_margin(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MIN_FREE_DISK_GB", 10.0)
    monkeypatch.setattr(disk, "free_gb", lambda path=None: 9.0)
    assert disk.has_headroom(str(tmp_path)) is False


def test_headroom_allows_a_volume_with_room(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MIN_FREE_DISK_GB", 10.0)
    monkeypatch.setattr(disk, "free_gb", lambda path=None: 40.0)
    assert disk.has_headroom(str(tmp_path)) is True


def test_reclaim_counts_bytes_and_tolerates_missing_files(tmp_path):
    kept = _write(tmp_path / "audio.wav", size=5000)
    gone = str(tmp_path / "nope.wav")
    assert disk.reclaim([kept, gone]) == 5000
    assert not os.path.exists(kept)


def test_reclaim_survives_a_path_it_cannot_remove(tmp_path):
    # A directory where a file is expected: removing it must not raise, the
    # worker is halfway through a job and a stale leftover is not fatal.
    blocked = tmp_path / "audio.wav"
    blocked.mkdir()

    assert disk.reclaim([str(blocked)]) == 0
    assert blocked.exists()


def test_audio_survives_until_the_transcript_exists(job_dir):
    import worker

    audio = _write(job_dir / "audio.wav", size=4000)
    _write(job_dir / "words.json", "[]")

    assert worker._drop_transcription_input(job_dir, 9901) == 0
    assert os.path.exists(audio)


def test_audio_is_dropped_once_the_transcript_is_written(job_dir, monkeypatch):
    import worker

    audio = _write(job_dir / "audio.wav", size=4000)
    groq = _write(job_dir / "audio_groq.mp3", size=900)
    _write(job_dir / "segments.json", "[{}]", size=10)
    _write(job_dir / "words.json", "[{}]", size=10)
    monkeypatch.setattr(worker.settings, "DELETE_AUDIO_AFTER_TRANSCRIPTION", True)

    freed = worker._drop_transcription_input(job_dir, 9901)

    assert freed == 4900
    assert not os.path.exists(audio)
    assert not os.path.exists(groq)


def test_audio_is_kept_when_the_setting_is_off(job_dir, monkeypatch):
    import worker

    audio = _write(job_dir / "audio.wav", size=4000)
    _write(job_dir / "segments.json", "[{}]", size=10)
    _write(job_dir / "words.json", "[{}]", size=10)
    monkeypatch.setattr(worker.settings, "DELETE_AUDIO_AFTER_TRANSCRIPTION", False)

    assert worker._drop_transcription_input(job_dir, 9901) == 0
    assert os.path.exists(audio)


def test_download_aborts_once_the_volume_is_full(tmp_path, monkeypatch):
    """The guard fires mid-download and is not retried with other clients."""
    from services import youtube

    attempts = []

    class FakeYdl:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            attempts.append(self.opts.get("extractor_args"))
            for hook in self.opts["progress_hooks"]:
                hook({"status": "downloading", "downloaded_bytes": 500, "total_bytes": 1000})
            return {"title": "x"}

    monkeypatch.setattr(youtube.yt_dlp, "YoutubeDL", FakeYdl)
    monkeypatch.setattr(youtube, "free_gb", lambda path=None: 0.5)
    monkeypatch.setattr(settings, "MIN_FREE_DISK_GB", 10.0)
    monkeypatch.setattr(type(settings), "JOBS_DIR", property(lambda self: str(tmp_path)))

    with pytest.raises(disk.NotEnoughSpaceError):
        youtube.download_youtube("https://youtu.be/abcdef", 9902)

    assert len(attempts) == 1
