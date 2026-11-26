"""Tests for the technical quality checks (services/quality/technical.py), the report models,
the pipeline wrapper (services/quality/pipeline.py), the worker's publish gate, the API summary
and the GET /clips/{id}/quality endpoint.

The media fixtures are tiny ffmpeg-generated files (1080x1920, 15 fps, 6 s) built once per module.
Tests that need ffmpeg are skipped with a reason when it is missing.

Run from backend/:  python -m pytest tests/test_quality_technical.py -q
"""
import json
import os
import re
import shutil
import subprocess
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.quality import technical
from services.quality import pipeline
from services.quality.models import Evidence, Finding, QualityReport, has_evidence, status_for

TIMEOUT = 120
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg and ffprobe on PATH")

SPEED = 1.02


def _ff(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True, timeout=TIMEOUT,
                   capture_output=True)


def _finding(severity="warning", confidence=0.9, category="visual"):
    return Finding(
        id="f", category=category, severity=severity, issue="x", explanation="y",
        confidence=confidence, evidence=Evidence(kind="measurement", ref="ref", value=1),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def media(tmp_path_factory):
    if not HAS_FFMPEG:
        pytest.skip("requires ffmpeg and ffprobe on PATH")
    d = tmp_path_factory.mktemp("quality_media")
    video = "testsrc2=size=1080x1920:rate=15:duration=6"
    # Amplitude 0.3 as specified; the sine source's own gain is 1/8, so it is written out directly.
    tone = "aevalsrc=0.3*sin(2*PI*220*t):s=48000:d=6"
    enc = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", "15"]
    aenc = ["-c:a", "aac", "-ar", "48000"]

    paths = {}
    paths["good"] = str(d / "good.mp4")
    _ff("-f", "lavfi", "-i", video, "-f", "lavfi", "-i", tone,
        *enc, *aenc, "-shortest", paths["good"])

    paths["no_video"] = str(d / "no_video.m4a")
    _ff("-f", "lavfi", "-i", tone, "-vn", "-c:a", "aac", "-ar", "48000", paths["no_video"])

    paths["no_audio"] = str(d / "no_audio.mp4")
    _ff("-f", "lavfi", "-i", video, "-an", *enc, paths["no_audio"])

    paths["wrong_size"] = str(d / "wrong_size.mp4")
    _ff("-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=15:duration=6", "-f", "lavfi", "-i", tone,
        *enc, *aenc, "-shortest", paths["wrong_size"])

    paths["all_black"] = str(d / "all_black.mp4")
    _ff("-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=15:d=6", "-f", "lavfi", "-i", tone,
        *enc, *aenc, "-shortest", paths["all_black"])

    still = str(d / "still.png")
    _ff("-f", "lavfi", "-i", "testsrc2=size=1080x1920", "-frames:v", "1", still)
    paths["frozen"] = str(d / "frozen.mp4")
    _ff("-loop", "1", "-i", still, "-f", "lavfi", "-i", tone,
        "-t", "6", *enc, *aenc, "-shortest", paths["frozen"])

    paths["silent"] = str(d / "silent.mp4")
    _ff("-f", "lavfi", "-i", video, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=6",
        *enc, *aenc, "-shortest", paths["silent"])

    paths["clipping"] = str(d / "clipping.mp4")
    _ff("-f", "lavfi", "-i", video, "-f", "lavfi", "-i",
        "aevalsrc=2*sin(2*PI*220*t):s=48000:d=6", *enc, *aenc, "-shortest", paths["clipping"])

    paths["corrupt"] = str(d / "corrupt.mp4")
    with open(paths["corrupt"], "wb") as fh:
        fh.write(os.urandom(4096))

    paths["missing"] = str(d / "does_not_exist.mp4")
    return paths


SILENT_CAPTIONS = [{"start": 0.5, "end": 5.5}]


def _report(path, **expect):
    return technical.evaluate_clip(path, expect)


def _findings(report, severity=None, category=None):
    return [f for f in report.findings
            if (severity is None or f.severity == severity)
            and (category is None or f.category == category)]


# ---------------------------------------------------------------------------
# 1. GOOD clip passes
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_good_clip_passes(media):
    report = _report(media["good"], expect_audio=True, expected_duration=6.0, size=(1080, 1920))
    assert report.status == "passed", [
        (f.severity, f.confidence, f.issue, f.evidence.value) for f in report.findings
    ]


# ---------------------------------------------------------------------------
# 2-4. Structure
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_no_video_fails(media):
    report = _report(media["no_video"], expect_audio=True)
    assert report.status == "failed"
    crit = _findings(report, "critical")
    assert any("no video" in f.issue for f in crit), [f.issue for f in report.findings]


@needs_ffmpeg
def test_no_audio_fails_when_audio_expected(media):
    report = _report(media["no_audio"], expect_audio=True)
    assert report.status == "failed"
    assert any("no audio" in f.issue for f in _findings(report, "critical"))


@needs_ffmpeg
def test_no_audio_is_fine_when_audio_not_expected(media):
    report = _report(media["no_audio"], expect_audio=False)
    assert not any(f.category == "audio" and f.severity == "critical" and "no audio" in f.issue
                   for f in report.findings)


@needs_ffmpeg
def test_wrong_size_fails(media):
    report = _report(media["wrong_size"], expect_audio=True)
    assert report.status == "failed"
    assert any("720x1280" in f.issue and f.severity == "critical" for f in report.findings), \
        [f.issue for f in report.findings]


# ---------------------------------------------------------------------------
# 5-7. Content scans
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_all_black_fails_with_blackdetect_evidence(media):
    report = _report(media["all_black"], expect_audio=True)
    assert report.status == "failed"
    black = [f for f in report.findings if f.evidence.ref == "blackdetect"]
    assert black, [(f.issue, f.evidence.ref) for f in report.findings]
    assert any(f.severity == "critical" for f in black)
    value = black[0].evidence.value
    assert isinstance(value, dict) and "start" in value and "duration" in value


@needs_ffmpeg
def test_frozen_frame_warns_with_freezedetect_evidence(media):
    report = _report(media["frozen"], expect_audio=True)
    freeze = [f for f in report.findings if f.evidence.ref == "freezedetect"]
    assert any(f.severity == "warning" for f in freeze), [f.issue for f in report.findings]


@needs_ffmpeg
def test_silence_under_captions_warns(media):
    report = _report(media["silent"], expect_audio=True, caption_events=SILENT_CAPTIONS)
    hits = [f for f in report.findings if f.evidence.ref == "silencedetect"]
    assert any(f.severity == "warning" and f.category == "audio" and "captions" in f.issue
               for f in hits), [(f.category, f.issue, f.evidence.ref) for f in report.findings]


@needs_ffmpeg
def test_clipping_audio_is_not_passed(media):
    report = _report(media["clipping"], expect_audio=True)
    loudness = [f for f in report.findings if f.evidence.ref.startswith("ebur128")]
    if report.checks.get("loudness") == "skipped":
        pytest.skip("loudness measurement unavailable: " + json.dumps(
            [f.issue for f in report.findings if f.evidence.ref == "loudness"]))
    assert report.status != "passed", [(f.severity, f.issue) for f in loudness]
    assert any("clips" in f.issue or "dBTP" in f.issue for f in loudness)


# ---------------------------------------------------------------------------
# 8. Unreadable files never raise
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["corrupt", "missing"])
@needs_ffmpeg
def test_unreadable_file_is_failed_not_raised(media, key):
    report = _report(media[key], expect_audio=True)
    assert report.status == "failed"
    assert any(f.severity == "critical" and "cannot be read" in f.issue for f in report.findings)


# ---------------------------------------------------------------------------
# 9. Evidence contract
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_every_finding_carries_evidence_and_consistent_times(media):
    keys = ["good", "no_video", "no_audio", "wrong_size", "all_black", "frozen", "silent",
            "corrupt", "missing"]
    reports = []
    for key in keys:
        reports.append((key, technical.evaluate_clip(media[key], {
            "expect_audio": True, "expected_duration": 6.0, "speed": SPEED,
            "caption_events": SILENT_CAPTIONS if key == "silent" else [],
        })))
    problems = []
    for key, report in reports:
        for f in report.findings:
            if not has_evidence(f.evidence):
                problems.append((key, f.issue, "no evidence"))
            if not 0.0 <= f.confidence <= 1.0:
                problems.append((key, f.issue, "confidence out of range"))
            if (f.timestamp_clip is None) != (f.timestamp_final is None):
                problems.append((key, f.issue, "one timestamp missing"))
            if f.timestamp_final is not None:
                expected = round(f.timestamp_final * SPEED, 3)
                if f.timestamp_clip != pytest.approx(expected, abs=0.001):
                    problems.append((key, f.issue, f.timestamp_clip, expected))
    assert not problems, problems


# ---------------------------------------------------------------------------
# 10. Evidence-less findings are dropped
# ---------------------------------------------------------------------------
def test_finding_without_evidence_is_dropped():
    c = technical._Collector(1.0)
    c.add("check", "file", "critical", "issue", "why", 1.0, Evidence(kind="measurement"))
    assert c.findings == []


# ---------------------------------------------------------------------------
# 11. Status rules
# ---------------------------------------------------------------------------
def test_status_rules():
    assert status_for([_finding("info", 1.0)]) == "passed"
    assert status_for([_finding("warning", 0.5)]) == "passed"
    assert status_for([_finding("warning", 0.7)]) == "review"
    assert status_for([_finding("warning", 0.9), _finding("critical", 0.1)]) == "failed"
    assert status_for([]) == "passed"


# ---------------------------------------------------------------------------
# 12. One crashing check does not stop the others
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_crashing_check_is_isolated(media, monkeypatch):
    def boom(c, path):
        raise RuntimeError("simulated loudness crash")

    monkeypatch.setattr(technical, "_check_loudness", boom)
    report = technical.evaluate_clip(media["good"], {"expect_audio": True, "expected_duration": 6.0})
    assert isinstance(report, QualityReport)
    assert report.checks["loudness"] == "skipped"
    assert report.checks.get("freeze") == "passed"
    assert report.checks.get("audio") == "passed"


# ---------------------------------------------------------------------------
# 13. Pipeline wrapper
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_assess_clip_writes_files_and_returns_fingerprint(media, tmp_path):
    clip_dir = tmp_path / "clip"
    clip_dir.mkdir()
    final = clip_dir / "final_video.mp4"
    shutil.copy(media["good"], final)
    words = tmp_path / "words.json"
    words.write_text(json.dumps({"words": [
        {"word": "hello", "start": 0.5, "end": 1.0},
        {"word": "world", "start": 1.2, "end": 1.9},
    ]}), encoding="utf-8")

    result = pipeline.assess_clip(
        str(final), str(clip_dir), job_id=1, clip_index=0, clip_start=0, clip_end=6,
        words_file=str(words), expect_audio=True,
    )
    assert result is not None
    assert set(result) >= {"report", "manifest", "fingerprint"}
    assert re.fullmatch(r"[0-9a-f]{64}", result["fingerprint"])
    assert (clip_dir / "quality.json").exists()
    assert (clip_dir / "render_manifest.json").exists()


def test_assess_clip_missing_dir_returns_none(media, tmp_path):
    missing_dir = str(tmp_path / "no_such_dir")
    result = pipeline.assess_clip(
        media["good"] if HAS_FFMPEG else __file__, missing_dir, job_id=1, clip_index=0,
        clip_start=0, clip_end=6, words_file=str(tmp_path / "none.json"), expect_audio=True,
    )
    assert result is None


# ---------------------------------------------------------------------------
# 14. Caption events from words
# ---------------------------------------------------------------------------
def test_caption_events_dict_form(tmp_path):
    f = tmp_path / "w.json"
    f.write_text(json.dumps({"words": [
        {"start": 0.0, "end": 0.4}, {"start": 0.5, "end": 0.5}, {"start": 1.0, "end": 0.9},
        {"start": 1.1, "end": 1.6},
    ]}), encoding="utf-8")
    assert pipeline.caption_events_from_words(str(f)) == [
        {"start": 0.0, "end": 0.4}, {"start": 1.1, "end": 1.6},
    ]


def test_caption_events_list_form_and_alt_keys(tmp_path):
    f = tmp_path / "w.json"
    f.write_text(json.dumps([
        {"start_time": 2.0, "end_time": 2.5}, {"start": 3.0, "end": 3.5},
    ]), encoding="utf-8")
    assert pipeline.caption_events_from_words(str(f)) == [
        {"start": 2.0, "end": 2.5}, {"start": 3.0, "end": 3.5},
    ]


def test_caption_events_missing_file_is_empty(tmp_path):
    assert pipeline.caption_events_from_words(str(tmp_path / "absent.json")) == []


# ---------------------------------------------------------------------------
# 15. Worker publish gate
# ---------------------------------------------------------------------------
def test_needs_review_before_publish():
    from types import SimpleNamespace
    import worker

    unreviewed_path = next(iter(worker.UNREVIEWED_SELECTION_PATHS))
    assert "llm" not in worker.UNREVIEWED_SELECTION_PATHS

    def clip(path_name, quality):
        return SimpleNamespace(score_breakdown={"selection_path": path_name}, quality_report=quality)

    assert worker._needs_review_before_publish(clip("llm", {"status": "failed"})) is True
    assert worker._needs_review_before_publish(clip("llm", {"status": "review"})) is False
    assert worker._needs_review_before_publish(clip("llm", {"status": "passed"})) is False
    assert worker._needs_review_before_publish(clip("llm", None)) is False
    assert worker._needs_review_before_publish(clip(unreviewed_path, {"status": "passed"})) is True


# ---------------------------------------------------------------------------
# 16. API summary
# ---------------------------------------------------------------------------
def test_quality_summary_none_and_counts():
    from api.common import _quality_summary

    assert _quality_summary(None) == {}
    report = {"status": "failed", "findings": [
        {"severity": "warning"}, {"severity": "warning"}, {"severity": "critical"},
    ]}
    assert _quality_summary(report) == {"status": "failed", "findings": {"warning": 2, "critical": 1}}


# ---------------------------------------------------------------------------
# 17. GET /api/clips/{id}/quality
# ---------------------------------------------------------------------------
@pytest.fixture
def quality_client():
    from database.session import SessionLocal, init_database
    from database.models import Clip, Job
    from api.features import router
    from database.session import get_database

    init_database()
    session = SessionLocal()
    app = FastAPI()
    app.include_router(router, prefix="/api")

    def _db():
        yield session

    app.dependency_overrides[get_database] = _db
    yield TestClient(app), session
    session.query(Clip).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


def test_clip_quality_endpoint_unknown_clip_is_404(quality_client):
    client, _session = quality_client
    response = client.get("/api/clips/987654/quality")
    assert response.status_code == 404


def test_clip_quality_endpoint_returns_stored_records(quality_client):
    from database.models import Clip, Job

    client, session = quality_client
    job = Job(youtube_url="https://example.invalid/watch?v=test")
    session.add(job)
    session.commit()
    report = {"status": "review", "findings": []}
    manifest = {"job_id": job.id, "clip_index": 0}
    clip = Clip(job_id=job.id, index=0, start=0.0, end=6.0, quality_report=report,
                render_manifest=manifest, render_fingerprint="a" * 64)
    session.add(clip)
    session.commit()

    response = client.get(f"/api/clips/{clip.id}/quality")
    assert response.status_code == 200
    body = response.json()
    assert body["report"] == report
    assert body["manifest"] == manifest
    assert body["fingerprint"] == "a" * 64
