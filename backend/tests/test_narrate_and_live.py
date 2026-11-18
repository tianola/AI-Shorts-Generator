"""Narrate Studio and Live Studio."""
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings  # noqa: E402
from services import live_studio as live  # noqa: E402
from services import narrate  # noqa: E402

FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path), raising=False)
    return tmp_path


# ------------------------------------------------------------------ narrate

def test_clean_script_keeps_only_spoken_text():
    raw = "# Title\n**Narrator:** The door creaked open. [pause]\n- (music swells) Nobody came.\n---\nScene 2: It was gone."
    assert narrate.clean_script(raw) == "The door creaked open. Nobody came. It was gone."


def test_scripts_are_cut_on_whole_sentences_to_the_target_length():
    sentence = "This sentence has exactly seven words in it. "
    script = narrate.fit_script_to_duration(sentence * 20, 20)   # ~50 words
    assert script.endswith("it.") and 30 <= len(script.split()) <= 56


def test_even_words_cover_the_whole_narration():
    words = narrate.even_words("one two three four", 8.0)
    assert [w["word"].strip() for w in words] == ["one", "two", "three", "four"]
    assert words[0]["start"] == 0 and words[-1]["end"] <= 8.0
    assert all(a["end"] <= b["start"] for a, b in zip(words, words[1:]))


def test_library_names_cannot_escape_the_folder():
    for bad in ("../x.mp4", "a/b.mp4", "..", "", ".hidden.mp4", "missing.mp4"):
        with pytest.raises(narrate.NarrateError):
            narrate.safe_library_file(bad)
    (narrate.library_dir() / "bg.mp4").write_bytes(b"x")
    assert narrate.safe_library_file("bg.mp4").name == "bg.mp4"
    assert [v["name"] for v in narrate.list_library()] == ["bg.mp4"]


@pytest.mark.asyncio
async def test_generate_script_returns_clean_fitted_text(monkeypatch):
    async def fake(**_kw):
        return '{"script": "# Hook\\nNobody believed her. The house had no door. Then it opened."}'
    import services.llm_router as router
    monkeypatch.setattr(router, "call_llm_json", fake)
    script = await narrate.generate_script("a house with no door", "horror", 30)
    assert script.startswith("Nobody believed her.") and "#" not in script


@pytest.mark.asyncio
async def test_an_empty_idea_is_refused():
    with pytest.raises(narrate.NarrateError):
        await narrate.generate_script("  ")


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_a_narrated_short_renders_end_to_end_without_a_library(tmp_path, monkeypatch):
    """Voice -> captions -> gradient background -> burned video, with a stub voice."""
    def fake_voice(text, voice, out_path, rate=0):
        target = out_path.with_suffix(".wav")
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=3", str(target)],
                       check=True, capture_output=True)
        return target
    monkeypatch.setattr(narrate, "synthesize", fake_voice)
    def fake_timings(audio, script, dur, job_dir):
        path = job_dir / "words.json"
        path.write_text(__import__("json").dumps(narrate.even_words(script, dur)))
        return path
    monkeypatch.setattr(narrate, "_word_timings", fake_timings)
    steps = []
    final = narrate.render(tmp_path / "job", "Nobody believed her. The house had no door.", "sapi:x",
                           progress=lambda m, p: steps.append(p))
    assert final.is_file() and final.stat().st_size > 1024
    assert steps[-1] == 100


# ---------------------------------------------------------------------- live

def test_sources_are_validated():
    assert live.validate_source("youtube", "https://youtube.com/watch?v=abc") is None
    assert live.validate_source("youtube", "https://example.com") is not None
    assert live.validate_source("tiktok", "https://www.tiktok.com/@name/live") is None
    assert live.validate_source("obs", "") is None
    assert live.validate_source("twitch", "x") is not None


def test_the_buffer_keeps_the_window_and_hides_the_segment_being_written(tmp_path):
    buf = live.RollingBuffer(tmp_path, max_seconds=20)
    for i in range(8):
        (tmp_path / f"seg_{i:06d}.ts").write_bytes(b"x")
    assert buf.prune(now_seconds=40) == 3             # segments 0-2 ended before 20 s ago
    assert [s.index for s in buf.segments()] == [3, 4, 5, 6, 7]
    assert [s.index for s in buf.covering(20, 30)] == [4, 5]   # 7 is still being written
    assert buf.covering(500, 600) == []


def test_keyword_highlights_and_sensitivity():
    hit = live.heuristic_highlight("OH MY GOD no way, that was insane!! what?!", 10, 30)
    assert hit and hit.category in ("Exciting", "Surprising") and hit.score >= 7
    assert live.heuristic_highlight("so today we are going over the schedule", 0, 10) is None
    assert live.threshold_for(10) < live.threshold_for(1)


@pytest.mark.asyncio
async def test_an_llm_verdict_becomes_a_highlight_or_nothing(monkeypatch):
    import services.llm_router as router
    replies = iter(['{"is_highlight": true, "category": "Funny", "score": 8, "title": "He fell off the chair"}',
                    '{"is_highlight": false}', "not json"])

    async def fake(**_kw):
        return next(replies)
    monkeypatch.setattr(router, "call_llm_json", fake)
    first = await live.llm_highlight("he fell", 0, 20)
    assert first.title == "He fell off the chair" and first.score == 8
    assert await live.llm_highlight("nothing", 0, 20) is None
    assert await live.llm_highlight("garbage", 0, 20) is None


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_a_clip_is_cut_from_buffered_segments(tmp_path, monkeypatch):
    session = live.LiveSession(tmp_path / "live", "obs", "", buffer_minutes=3)
    folder = session.buffer.folder
    for i in range(6):
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=10:duration={live.SEGMENT_SECONDS}",
                        "-f", "lavfi", "-i", "sine=duration=5", "-shortest", "-c:v", "libx264", "-c:a", "aac",
                        "-f", "mpegts", str(folder / f"seg_{i:06d}.ts")], check=True, capture_output=True)
    import services.smart_crop as sc
    monkeypatch.setattr(sc, "smart_crop_video", lambda src, dst, mode="auto": shutil.copy2(src, dst))
    meta = session.cut(5.0, 20.0, "Great moment", "Funny", "test")
    assert meta["title"] == "Great moment"
    assert (settings.STORAGE_ROOT and os.path.isfile(os.path.join(settings.STORAGE_ROOT, meta["path"])))
    assert [c["id"] for c in session.clips()] == [meta["id"]]
    assert session.delete_clip(meta["id"]) and session.clips() == []
    assert not session.delete_clip("../etc")
    with pytest.raises(RuntimeError):
        session.cut(900, 920)                 # nothing buffered there
