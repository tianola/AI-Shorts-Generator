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
    with pytest.raises(ValueError):          # unreadable: the caller falls back to keywords
        await live.llm_highlight("garbage", 0, 20)


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


def test_captions_use_the_script_words_and_leave_no_gap_where_recognition_failed():
    script = "On August 15, 1977, a dish in Ohio picked up a signal."
    heard = [{"word": " On", "start": 0.0, "end": 0.3}, {"word": " August", "start": 0.3, "end": 0.66},
             {"word": " 15,", "start": 0.66, "end": 1.28},
             # "1977," came back with no length and "a dish" as "addition"
             {"word": " 1977,", "start": 2.68, "end": 2.68}, {"word": " addition", "start": 3.54, "end": 3.86},
             {"word": " in", "start": 3.86, "end": 4.0}, {"word": " Ohio", "start": 4.0, "end": 4.36},
             {"word": " picked", "start": 4.36, "end": 4.64}, {"word": " up", "start": 4.64, "end": 4.9},
             {"word": " a", "start": 4.9, "end": 5.02}, {"word": " signal.", "start": 5.02, "end": 5.5}]
    out = narrate.align_script_to_words(script, heard, 5.8)
    assert [w["word"].strip() for w in out] == script.split()           # the script's words, all of them
    assert all(a["end"] <= b["start"] + 1e-6 for a, b in zip(out, out[1:]))
    assert all(b["start"] - a["end"] < 0.6 for a, b in zip(out, out[1:])), "a stretch of speech lost its caption"
    assert out[-1]["end"] <= 5.8


def test_alignment_with_nothing_matching_still_covers_the_whole_voice():
    out = narrate.align_script_to_words("one two three four", [{"word": " xyz", "start": 0.5, "end": 0.9}], 8.0)
    assert [w["word"].strip() for w in out] == ["one", "two", "three", "four"]
    assert out[0]["start"] == 0 and out[-1]["end"] <= 8.0


# ------------------------------------------------------------- stock footage

def test_keyword_fallback_prefers_frequent_long_words():
    from services import stock_footage as sf
    q = sf.fallback_queries("The telescope heard a signal. The signal came from the telescope array.", 2)
    assert q == ["telescope", "signal"] and sf.fallback_queries("a an the", 2) == ["abstract background"]


def test_the_sharpest_portrait_file_is_chosen():
    from services import stock_footage as sf
    video = {"video_files": [
        {"link": "a", "width": 540, "height": 960, "file_type": "video/mp4"},
        {"link": "b", "width": 1080, "height": 1920, "file_type": "video/mp4"},
        {"link": "c", "width": 2160, "height": 3840, "file_type": "video/mp4"},   # wider than 1080
        {"link": "d", "width": 1920, "height": 1080, "file_type": "video/mp4"}]}
    assert sf.pick_file(video)["link"] == "b"
    assert sf.pick_file({"video_files": []}) is None


@pytest.mark.asyncio
async def test_no_key_means_no_footage_and_no_network_call(monkeypatch, tmp_path):
    from services import stock_footage as sf
    monkeypatch.setattr(sf.settings, "PEXELS_API_KEY", None, raising=False)
    called = []
    monkeypatch.setattr(sf.httpx, "get", lambda *a, **k: called.append(1))
    assert await sf.footage_for("any script", tmp_path) == [] and not called


def test_a_found_video_is_downloaded_once_and_reused(monkeypatch, tmp_path):
    from services import stock_footage as sf
    monkeypatch.setattr(sf.settings, "PEXELS_API_KEY", "k", raising=False)

    class Resp:
        status_code = 200
        def json(self):
            return {"videos": [{"id": 7, "duration": 3, "video_files": []},          # too short
                               {"id": 9, "duration": 20, "video_files": [
                                   {"link": "http://x/v.mp4", "width": 1080, "height": 1920, "file_type": "video/mp4"}]}]}

    class Stream:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_bytes(self, n): yield b"x" * 20000

    downloads = []
    monkeypatch.setattr(sf.httpx, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(sf.httpx, "stream", lambda *a, **k: downloads.append(1) or Stream())
    first = sf.fetch("night sky", tmp_path)
    again = sf.fetch("night sky", tmp_path)
    assert first == again and first.name == "pexels_9.mp4" and len(downloads) == 1


def test_a_youtube_live_with_separate_video_and_audio_gets_two_inputs(monkeypatch, tmp_path):
    """The real Al Jazeera live offers video-only and audio-only HLS; yt-dlp prints two addresses."""
    session = live.LiveSession(tmp_path / "live", "youtube", "https://youtube.com/@x/live")

    class Done:
        returncode = 0
        stdout = "https://v.example/video.m3u8\nhttps://a.example/audio.m3u8\n"
        stderr = ""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return Done()
    monkeypatch.setattr(live.subprocess, "run", fake_run)
    args = session._stream_url()
    assert args == ["-i", "https://v.example/video.m3u8", "-i", "https://a.example/audio.m3u8"]
    assert "-m" in seen["cmd"] and "yt_dlp" in seen["cmd"] and "bv*[height<=720]+ba/b[height<=720]/b" in seen["cmd"]


def test_a_refused_stream_reports_the_reason_not_a_wall_of_warnings(monkeypatch, tmp_path):
    session = live.LiveSession(tmp_path / "live", "youtube", "https://youtube.com/@x/live")

    class Fail:
        returncode = 1
        stdout = ""
        stderr = "WARNING: lots of noise\nERROR: [youtube] abc: Requested format is not available"
    monkeypatch.setattr(live.subprocess, "run", lambda *a, **k: Fail())
    with pytest.raises(RuntimeError, match="Requested format is not available"):
        session._stream_url()


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")
def test_a_segment_is_transcribed_from_decoded_samples(monkeypatch, tmp_path):
    """faster-whisper's own file decoder breaks on some PyAV versions, so it gets samples."""
    import services.whisper as w
    seen = {}

    class Model:
        def transcribe(self, audio, **kw):
            seen["type"] = type(audio).__name__
            seen["language"] = kw.get("language")
            return [type("S", (), {"text": " hello world "})()], None
    monkeypatch.setattr(w, "get_whisper_model", lambda name=None: Model())
    seg_path = tmp_path / "seg_000000.ts"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=duration=3", "-f", "mpegts", str(seg_path)],
                   check=True, capture_output=True)
    session = live.LiveSession(tmp_path / "live", "obs", "")
    assert session._transcribe(live.Segment(seg_path, 0, 0.0)) == "hello world"
    assert seen == {"type": "ndarray", "language": "en"}


@pytest.mark.asyncio
async def test_the_stream_context_reaches_the_model_and_titles_are_complete(monkeypatch):
    import services.llm_router as router
    seen = {}

    async def fake(**kw):
        seen["prompt"] = kw["prompt"]
        return ('{"is_highlight": true, "category": "excitement", "score": 8, '
                '"title": "IShowSpeed reacts to his first GTA 6 gameplay", "description": "He is shocked."}')
    monkeypatch.setattr(router, "call_llm_json", fake)
    hit = await live.llm_highlight("no way this is real", 0, 20, "GTA 6 releases on November 19")
    assert "GTA 6 releases on November 19" in seen["prompt"]
    assert hit.title.startswith("IShowSpeed") and hit.description == "He is shocked."

    async def cut_off(**_kw):
        return '{"is_highlight": true, "score": 9, "title": "Well, Yall"}'
    monkeypatch.setattr(router, "call_llm_json", cut_off)
    assert await live.llm_highlight("x", 0, 20) is None          # two words is not a title


def test_a_keyword_hit_is_named_after_the_stream_not_after_a_line_of_chatter():
    hit = live.heuristic_highlight("OH MY GOD no way that was insane!!", 0, 20, "GTA 6 gameplay")
    assert hit.title.startswith("GTA 6 gameplay:") and "OH MY GOD" not in hit.title.upper()
