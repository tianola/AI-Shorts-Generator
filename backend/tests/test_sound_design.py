"""Sound design: speech gaps, placement, planned sound events, and the SFX sidechain mix.

Run from backend/:  python -m pytest tests/test_sound_design.py -q
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from config import settings  # noqa: E402
from services.editing.models import AudioTrackItem, Timeline  # noqa: E402
from services.editing.sound import place_in_gap, plan_sound_events, speech_gaps  # noqa: E402
from services.render import (  # noqa: E402
    _sidechain_settings,
    build_single_pass_filter,
    render_single_pass,
)

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="requires ffmpeg and ffprobe on PATH")

TIMEOUT = 120
SIDECHAIN = {"threshold": 0.02, "ratio": 6.0, "attack_ms": 8.0, "release_ms": 250.0}
STATIC_FRAG = "[0:v]crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920[vsingle]"


def _run(cmd, timeout=TIMEOUT):
    return subprocess.run(cmd, capture_output=True, text=True, errors="ignore", timeout=timeout)


def _timeline(tracks, duration=10.0):
    """tracks: [(kind, start, gain_db)]"""
    return Timeline(
        clip_duration=duration,
        profile="test",
        audio_tracks=[AudioTrackItem(kind=k, start=s, gain_db=g, event_id=f"ev{i}")
                      for i, (k, s, g) in enumerate(tracks)],
    )


@pytest.fixture(scope="session")
def sfx_wav(tmp_path_factory):
    if not FFMPEG:
        pytest.skip("requires ffmpeg and ffprobe on PATH")
    path = str(tmp_path_factory.mktemp("sfx") / "hit.wav")
    r = _run(["ffmpeg", "-y", "-f", "lavfi", "-i",
              "sine=frequency=1000:sample_rate=48000:duration=0.2", "-ac", "2", path])
    assert r.returncode == 0, r.stderr[-400:]
    return path


# ---------------------------------------------------------------------------
# 1. speech_gaps
# ---------------------------------------------------------------------------
def test_speech_gaps_pause_of_0_4s_is_a_gap():
    words = [{"start": 0.0, "end": 1.0}, {"start": 1.4, "end": 2.0}]
    gaps = speech_gaps(words, 2.0)
    assert len(gaps) == 1
    assert gaps[0][0] == pytest.approx(1.0)
    assert gaps[0][1] == pytest.approx(1.4)


def test_speech_gaps_shorter_than_0_25s_is_not_a_gap():
    words = [{"start": 0.0, "end": 1.0}, {"start": 1.2, "end": 2.0}]
    assert speech_gaps(words, 2.0) == []


def test_speech_gaps_include_before_first_and_after_last_word():
    words = [{"start": 0.5, "end": 1.0}, {"start": 1.5, "end": 2.0}]
    gaps = [(round(a, 3), round(b, 3)) for a, b in speech_gaps(words, 3.0)]
    assert gaps == [(0.0, 0.5), (1.0, 1.5), (2.0, 3.0)]


def test_speech_gaps_no_words_is_one_gap_over_whole_clip():
    assert speech_gaps([], 10.0) == [(0.0, 10.0)]


# ---------------------------------------------------------------------------
# 2. place_in_gap
# ---------------------------------------------------------------------------
def test_place_in_gap_start_already_inside_is_kept():
    assert place_in_gap(1.5, [(1.0, 2.0)]) == (1.5, True)


def test_place_in_gap_start_inside_word_moves_to_nearby_gap_start():
    # 0.8 is inside speech; the gap starts 0.2 s later, within the 0.5 s window
    assert place_in_gap(0.8, [(1.0, 1.5)]) == (1.0, True)


def test_place_in_gap_nothing_close_is_unchanged_and_false():
    assert place_in_gap(1.0, [(5.0, 6.0)]) == (1.0, False)


def test_place_in_gap_last_tenth_of_a_gap_is_not_inside():
    gaps = [(1.0, 2.0)]
    assert place_in_gap(1.95, gaps) == (1.95, False)
    # just before the last 0.1 s still counts as inside
    assert place_in_gap(1.9, gaps) == (1.9, True)


# ---------------------------------------------------------------------------
# 3. plan_sound_events
# ---------------------------------------------------------------------------
FIND_SET = {"whoosh_1", "impact_1", "pop_1", "sub_boom_1"}


def _find(names):
    return lambda name: f"/x/{name}.wav" if name in names else None


# Speech 0.5-2.6 and 2.9-9.0: gaps at (0, 0.5), (2.6, 2.9) and (9.0, 10.0).
WORDS = [{"start": 0.5, "end": 2.6}, {"start": 2.9, "end": 9.0}]


def test_plan_sound_events_main_timeline():
    tl = _timeline([("whoosh", 0.0, -8.0), ("impact", 3.0, -6.0),
                    ("pop", 6.0, -10.0), ("sub_boom", 8.0, -6.0)])
    events = plan_sound_events(tl, WORDS, _find(FIND_SET))

    # sub_boom: strong sound, no gap within 0.5 s -> dropped
    assert [e["kind"] for e in events] == ["whoosh", "impact", "pop"]
    assert [e["start"] for e in events] == sorted(e["start"] for e in events)

    whoosh, impact, pop = events
    assert whoosh["path"] == "/x/whoosh_1.wav"
    assert whoosh["start"] == 0.0 and whoosh["note"] == "in a gap"
    assert whoosh["gain_db"] == -8.0

    assert impact["path"] == "/x/impact_1.wav"
    assert impact["start"] == pytest.approx(2.6)
    assert impact["note"] == "moved into a gap"

    # pop: light sound, no gap -> kept 6 dB lower
    assert pop["path"] == "/x/pop_1.wav"
    assert pop["start"] == 6.0
    assert pop["note"] == "turned down: no gap"
    assert pop["gain_db"] == pytest.approx(-16.0)


def test_plan_sound_events_rotates_files_and_never_repeats_back_to_back():
    tl = _timeline([("impact", 1.0, -6.0), ("impact", 4.0, -6.0)])
    events = plan_sound_events(tl, [], _find({"impact_1", "impact_2", "thump"}))
    assert len(events) == 2
    assert events[0]["path"].endswith("impact_1.wav")
    assert events[1]["path"].endswith("impact_2.wav")
    assert events[0]["path"] != events[1]["path"]


def test_plan_sound_events_same_file_not_played_twice_in_a_row():
    # Only one pop file exists: the second pop has nothing different to play, so it is dropped.
    tl = _timeline([("pop", 1.0, -6.0), ("pop", 4.0, -6.0)])
    events = plan_sound_events(tl, [], _find({"pop_1"}))
    assert len(events) == 1
    assert events[0]["path"] == "/x/pop_1.wav"


def test_plan_sound_events_skips_kind_whose_names_have_no_file():
    tl = _timeline([("riser", 2.0, -6.0), ("whoosh", 4.0, -6.0)])
    events = plan_sound_events(tl, [], _find({"whoosh_1"}))
    assert [e["kind"] for e in events] == ["whoosh"]


def test_plan_sound_events_documentary_timeline_is_empty():
    tl = Timeline(clip_duration=10.0, profile="documentary")
    assert plan_sound_events(tl, WORDS, _find(FIND_SET)) == []


# ---------------------------------------------------------------------------
# 4. build_single_pass_filter with sfx_events / sidechain (graph strings)
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_sfx_events_graph_with_sidechain(sfx_wav):
    events = [{"path": sfx_wav, "start": 0.5, "gain_db": -12.0},
              {"path": sfx_wav, "start": 2.0, "gain_db": -6.0}]
    extra, fc, vmap, amap = build_single_pass_filter(
        STATIC_FRAG, has_audio=True, sfx_events=events, sidechain=SIDECHAIN)

    assert len(extra) == 2
    assert "[0:a]aformat=sample_rates=48000:channel_layouts=stereo,volume=1.0[orig]" in fc
    assert "asplit=2[vmix][vkey]" in fc
    assert ("sidechaincompress=threshold=0.02:ratio=6.0:attack=8.0:release=250.0") in fc
    assert "amix=inputs=2:duration=first:dropout_transition=0:normalize=0" in fc
    assert "adelay=500|500" in fc
    assert "adelay=2000|2000" in fc
    assert "volume=-12.0dB" in fc
    assert "volume=-6.0dB" in fc
    # the compressor is keyed by the voice but only compresses the SFX bus
    assert "[sfxbus][vkey]sidechaincompress" in fc
    assert "[orig][vkey]" not in fc
    assert "[vmix][sfxduck]" in fc
    assert vmap == "[vout]"
    assert amap == "[aout]"


@needs_ffmpeg
def test_sfx_events_graph_without_sidechain(sfx_wav):
    events = [{"path": sfx_wav, "start": 0.5, "gain_db": -12.0},
              {"path": sfx_wav, "start": 2.0, "gain_db": -6.0}]
    _, fc, _, amap = build_single_pass_filter(
        STATIC_FRAG, has_audio=True, sfx_events=events, sidechain=None)
    assert "sidechaincompress" not in fc
    assert "[orig][sfxbus]" in fc
    assert "amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]" in fc
    assert amap == "[aout]"


@needs_ffmpeg
def test_sfx_events_missing_file_is_ignored(sfx_wav):
    events = [{"path": "/definitely/not/here.wav", "start": 1.0, "gain_db": -3.0},
              {"path": sfx_wav, "start": 2.0, "gain_db": -12.0}]
    extra, fc, _, amap = build_single_pass_filter(
        STATIC_FRAG, has_audio=True, sfx_events=events, sidechain=SIDECHAIN)
    assert len(extra) == 1
    assert extra[0]["path"] == sfx_wav
    assert "adelay=1000" not in fc
    assert "adelay=2000|2000" in fc
    assert amap == "[aout]"


@needs_ffmpeg
def test_sfx_events_all_missing_or_empty_leaves_original_audio(sfx_wav):
    _, fc, _, amap = build_single_pass_filter(
        STATIC_FRAG, has_audio=True, sfx_events=[], sfx_files=None)
    assert amap == "0:a?"
    assert "amix" not in fc

    _, fc, _, amap = build_single_pass_filter(
        STATIC_FRAG, has_audio=True,
        sfx_events=[{"path": "/nope.wav", "start": 1.0, "gain_db": -3.0}], sfx_files=None)
    assert amap == "0:a?"
    assert "amix" not in fc


@needs_ffmpeg
def test_legacy_sfx_files_path_unchanged(sfx_wav):
    extra, fc, _, amap = build_single_pass_filter(
        STATIC_FRAG, sfx_files=["whoosh_1.wav", "pop_1.wav"],
        sfx_timestamps=[0.3, 2.5], has_audio=True)
    assert len(extra) == 2
    assert "adelay=300|300" in fc
    assert "adelay=2500|2500" in fc
    assert "volume=0.25" in fc
    assert "[0:a]volume=1.0[orig]" in fc
    assert amap == "[aout]"


# ---------------------------------------------------------------------------
# 5. Ducking with real ffmpeg
# ---------------------------------------------------------------------------
def _astats(path, start, end, key):
    """Peak or RMS level (dB) of a window of an audio file, from ffmpeg's astats."""
    r = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
              "-af", f"atrim=start={start}:end={end},astats=metadata=0",
              "-f", "null", "-"])
    assert r.returncode == 0, r.stderr[-400:]
    vals = re.findall(rf"{key} level dB:\s*(-?inf|-?[\d.]+)", r.stderr)
    assert vals, r.stderr[-600:]
    return float(vals[-1])  # the Overall section prints last


@needs_ffmpeg
def test_sidechain_ducks_effects_under_speech_not_in_gaps(tmp_path):
    # Speech 0-2 s and 4-6 s, silence 2-4 s. The commas inside the expression are escaped
    # for the filtergraph parser.
    voice_expr = r"0.3*sin(2*PI*220*t)*(lt(t\,2)+gt(t\,4))"
    video = str(tmp_path / "clip.mp4")
    r = _run(["ffmpeg", "-y",
              "-f", "lavfi", "-i", "color=c=black:s=320x180:d=6",
              "-f", "lavfi", "-i", f"aevalsrc={voice_expr}:s=48000:d=6",
              "-map", "0:v", "-map", "1:a",
              "-c:v", "libx264", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "128k", "-ac", "2", video])
    assert r.returncode == 0, r.stderr[-500:]

    impact = os.path.join(settings.STORAGE_ROOT, "assets", "sfx", "impact", "impact_1.wav")
    if not os.path.isfile(impact):
        impact = str(tmp_path / "impact.wav")
        r = _run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                  "sine=frequency=80:sample_rate=48000:duration=0.3,afade=t=out:st=0:d=0.3",
                  "-ac", "2", impact])
        assert r.returncode == 0, r.stderr[-400:]

    events = [{"path": impact, "start": 1.0, "gain_db": -6.0},   # during speech
              {"path": impact, "start": 3.0, "gain_db": -6.0}]   # in the gap
    extra, fc, vmap, amap = build_single_pass_filter(
        "[0:v]scale=320:180[vsingle]", has_audio=True,
        sfx_events=events, sidechain=SIDECHAIN)
    assert amap == "[aout]"
    assert len(extra) == 2

    # Probe: split the compressed SFX bus so it can be written out on its own.
    probed = fc.replace("[sfxduck]", "[sfxduck0];[sfxduck0]asplit=2[sfxduck][sfxprobe]", 1)
    probe_wav = str(tmp_path / "probe.wav")
    mix_wav = str(tmp_path / "mix.wav")
    vid_out = str(tmp_path / "video_out.mp4")

    cmd = ["ffmpeg", "-y", "-i", video]
    for ei in extra:
        cmd += ["-i", ei["path"]]
    cmd += ["-filter_complex", probed,
            "-map", "[sfxprobe]", "-c:a", "pcm_s16le", probe_wav,
            "-map", vmap, "-frames:v", "5", "-c:v", "libx264", "-pix_fmt", "yuv420p", vid_out,
            "-map", amap, "-c:a", "pcm_s16le", mix_wav]
    r = _run(cmd)
    assert r.returncode == 0, r.stderr[-800:]

    speech_peak = _astats(probe_wav, 0.95, 1.45, "Peak")
    gap_peak = _astats(probe_wav, 2.95, 3.45, "Peak")
    assert gap_peak - speech_peak >= 10.0, (
        f"effect not ducked under speech: speech peak {speech_peak} dB, gap peak {gap_peak} dB")

    mix_rms = _astats(mix_wav, 2.95, 3.45, "RMS")
    probe_rms = _astats(probe_wav, 2.95, 3.45, "RMS")
    assert abs(mix_rms - probe_rms) <= 3.0, (
        f"mix in gap {mix_rms} dB RMS vs effect alone {probe_rms} dB RMS: voice not silent?")


# ---------------------------------------------------------------------------
# 6. render_single_pass end to end, and the sidechain settings switch
# ---------------------------------------------------------------------------
@pytest.fixture
def tiny_source(tmp_path):
    src = str(tmp_path / "src.mp4")
    r = _run(["ffmpeg", "-y",
              "-f", "lavfi", "-i", "color=c=purple:s=320x180:d=1.5",
              "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
              "-shortest", "-c:v", "libx264", "-c:a", "aac", src])
    assert r.returncode == 0, r.stderr[-400:]
    return src


def _probe(path):
    import json
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
              "-of", "json", path])
    assert r.returncode == 0, r.stderr[-400:]
    info = json.loads(r.stdout)
    types = [s.get("codec_type") for s in info.get("streams", [])]
    return float(info["format"]["duration"]), types


@needs_ffmpeg
def test_render_single_pass_with_sfx_events_sidechain_on_and_off(
        tiny_source, sfx_wav, tmp_path, monkeypatch):
    from services.vertical import build_vertical_filter

    frag = build_vertical_filter(tiny_source, mode="static")
    events = [{"path": sfx_wav, "start": 0.3, "gain_db": -12.0}]
    durations = {}
    for enabled in (True, False):
        monkeypatch.setattr(settings, "SFX_SIDECHAIN_ENABLED", enabled)
        out = str(tmp_path / f"render_sidechain_{enabled}.mp4")
        result = render_single_pass(tiny_source, 0.0, 1.5, out,
                                    vertical_frag=frag, sfx_events=events)
        assert os.path.exists(result) and os.path.getsize(result) > 0
        dur, types = _probe(result)
        assert "audio" in types and "video" in types, (enabled, types)
        durations[enabled] = dur
        assert 1.2 <= dur <= 2.0, (enabled, dur)
    assert abs(durations[True] - durations[False]) <= 0.3, durations


def test_sidechain_settings_follow_config_switch(monkeypatch):
    monkeypatch.setattr(settings, "SFX_SIDECHAIN_ENABLED", False)
    assert _sidechain_settings() is None

    monkeypatch.setattr(settings, "SFX_SIDECHAIN_ENABLED", True)
    cfg = _sidechain_settings()
    assert isinstance(cfg, dict)
    assert set(cfg) == {"threshold", "ratio", "attack_ms", "release_ms"}
