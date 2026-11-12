"""Defect 1 — the render chain never normalised loudness.

Nothing in the pipeline applied ``loudnorm``, so ten clips cut from one source
landed between -23.4 and -17.5 LUFS (measured on storage/jobs/110) — a 5.9 LU
spread, i.e. every clip plays at a different perceived volume.

The fix is the two-pass EBU R128 recipe: measure the window with
``loudnorm=print_format=json``, then render with those ``measured_*`` values and
``linear=true`` so ffmpeg applies one fixed gain when it can, falling back to
its limiter only when the true-peak ceiling forbids linear gain. The filter
resamples internally to 192 kHz, so ``aresample`` must come AFTER it, and the
deliverable is stereo 48 kHz.

These tests assert the emitted filter string and, where ffmpeg is available,
the loudness actually measured on a rendered file — never that a mock ran.
"""
import re
import shutil
import subprocess

import pytest

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="requires ffmpeg and ffprobe on PATH")

TIMEOUT = 120


def _run(cmd, timeout=TIMEOUT):
    return subprocess.run(cmd, capture_output=True, text=True, errors="ignore", timeout=timeout)


def _make_audio_clip(path, level_db, freq=1000, duration=5.0, video=True):
    """A constant-tone clip at a known level (a 1 kHz tone has ~0 LU
    loudness range, so linear normalisation is possible and the target is
    reached exactly — which is what makes the assertion tight)."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={duration}",
    ]
    if video:
        cmd += ["-f", "lavfi", "-i", f"color=c=blue:s=320x180:d={duration}",
                "-map", "1:v", "-map", "0:a"]
    cmd += ["-af", f"volume={level_db}dB", "-c:a", "aac", "-shortest", path]
    r = _run(cmd)
    assert r.returncode == 0, r.stderr[-500:]
    return path


def _integrated_lufs(path):
    """Independent R128 measurement (ebur128, not loudnorm's own report)."""
    r = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
              "-filter_complex", "ebur128=peak=true", "-f", "null", "-"])
    m = re.search(r"Integrated loudness:\s*\n\s*I:\s*(-?[\d.]+)\s*LUFS", r.stderr)
    assert m, r.stderr[-800:]
    p = re.search(r"True peak:\s*\n\s*Peak:\s*(-?[\d.]+)\s*dBFS", r.stderr)
    return float(m.group(1)), (float(p.group(1)) if p else None)


# ---------------------------------------------------------------------------
# pass 2: the filter string the chain must emit
# ---------------------------------------------------------------------------
MEASURED = {
    "input_i": "-23.35",
    "input_tp": "-2.74",
    "input_lra": "2.50",
    "input_thresh": "-33.53",
    "target_offset": "2.79",
}


def test_loudnorm_filter_is_two_pass_linear():
    from services.audio_polish import build_loudnorm_filter

    chain = build_loudnorm_filter(MEASURED)

    assert chain.startswith("loudnorm="), chain
    assert "I=-14" in chain and "TP=-1.5" in chain, chain
    # the measured values of THIS clip are what makes it two-pass
    for pair in ("measured_I=-23.35", "measured_TP=-2.74", "measured_LRA=2.50",
                 "measured_thresh=-33.53", "offset=2.79"):
        assert pair in chain, f"{pair} missing from {chain}"
    assert "linear=true" in chain, chain
    # loudnorm works at 192 kHz internally: resampling has to come after it,
    # and the deliverable is stereo 48 kHz.
    assert chain.index("aresample=48000") > chain.index("loudnorm="), chain
    assert "channel_layouts=stereo" in chain, chain
    assert chain.index("aresample=48000") > chain.index("channel_layouts=stereo"), chain


def test_loudnorm_filter_is_empty_without_usable_measurement():
    """A failed/unusable measurement must not inject a broken filter."""
    from services.audio_polish import build_loudnorm_filter

    assert build_loudnorm_filter(None) == ""
    assert build_loudnorm_filter({}) == ""
    # silent media measures -inf; feeding that to loudnorm yields garbage
    assert build_loudnorm_filter({**MEASURED, "input_i": "-inf"}) == ""
    assert build_loudnorm_filter({**MEASURED, "input_tp": "nan"}) == ""


# ---------------------------------------------------------------------------
# pass 1: the measurement
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_measure_loudness_returns_the_two_pass_values(tmp_path):
    from services.audio_polish import measure_loudness

    clip = _make_audio_clip(str(tmp_path / "quiet.mp4"), -23)
    before, _ = _integrated_lufs(clip)
    measured = measure_loudness(clip)

    assert measured is not None, "measurement pass produced nothing usable"
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        assert key in measured, measured
        float(measured[key])  # must be a finite number, not "-inf"/"nan"
    # loudnorm's gated measurement vs ebur128 — same standard, small drift ok
    assert abs(float(measured["input_i"]) - before) < 1.0, (measured, before)


@needs_ffmpeg
def test_measure_loudness_only_covers_the_requested_window(tmp_path):
    """-ss/-t must scope the measurement to the rendered window.

    The file is quiet for its first 3 s and loud for the next 6, so the two
    windows read ~17 LU apart. Measuring the whole file instead of the window —
    which is what happens if the seek flags are dropped — makes them equal and
    feeds pass 2 the wrong numbers.
    """
    from services.audio_polish import measure_loudness

    path = str(tmp_path / "quiet-then-loud.wav")
    r = _run(["ffmpeg", "-y", "-f", "lavfi", "-i",
              "sine=frequency=1000:sample_rate=48000:duration=9",
              "-af", "volume='if(lt(t,3),0.0708,0.501)':eval=frame", path])
    assert r.returncode == 0, r.stderr[-400:]

    first = float(measure_loudness(path, start=0.0, duration=3.0)["input_i"])
    rest = float(measure_loudness(path, start=3.0, duration=6.0)["input_i"])
    assert rest - first > 5, (first, rest)


@needs_ffmpeg
def test_measure_loudness_of_silence_returns_none(tmp_path):
    from services.audio_polish import measure_loudness

    silent = str(tmp_path / "silent.wav")
    _run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
          "-t", "3", "-q:a", "9", "-acodec", "libmp3lame", silent])
    assert measure_loudness(silent) is None


def test_measure_loudness_gives_up_instead_of_hanging(tmp_path, monkeypatch):
    """The probe pass is an ffmpeg run like any other: it must be bounded."""
    import services.audio_polish as audio_polish

    monkeypatch.setattr(audio_polish, "ANALYSIS_TIMEOUT_S", 1)
    monkeypatch.setattr(audio_polish.subprocess, "run", _wedged_run)
    assert audio_polish.measure_loudness("whatever.mp4") is None


def _wedged_run(cmd, **kwargs):
    """Stand in for an ffmpeg that never returns (a wedged decoder)."""
    assert kwargs.get("timeout"), f"unbounded subprocess call: {cmd[:3]}"
    raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])


# ---------------------------------------------------------------------------
# wiring: the chain has to sit at the end of the render audio graph
# ---------------------------------------------------------------------------
def test_single_pass_filter_ends_audio_in_loudnorm():
    from services.audio_polish import build_loudnorm_filter
    from services.render import build_single_pass_filter

    STATIC = "[0:v]crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920[vsingle]"
    chain = build_loudnorm_filter(MEASURED)
    _, fc, _, amap = build_single_pass_filter(
        STATIC,
        sfx_files=["whoosh_1.wav"],
        sfx_timestamps=[0.3],
        has_audio=True,
        loudnorm_chain=chain,
    )
    assert amap == "[aout]", amap
    assert fc.count("loudnorm=") == 1, fc
    assert fc.index("amix=") < fc.index("loudnorm="), (
        "normalising before the SFX mix leaves the delivered level dependent "
        "on how much effect audio was added")
    norm_chain = [c for c in fc.split(";") if "loudnorm=" in c]
    assert len(norm_chain) == 1 and norm_chain[0].endswith("[aout]"), fc
    # the mix must still hand the voice through at unity (defect 2)
    assert "normalize=0" in fc, fc

    from test_filtergraphs import _assert_valid
    _assert_valid(fc, "single pass with loudnorm")


def test_single_pass_filter_normalises_a_voice_only_clip_too():
    """No SFX used to mean the audio was mapped straight through, so those
    clips stayed un-normalised; they must go through the chain as well."""
    from services.audio_polish import build_loudnorm_filter
    from services.render import build_single_pass_filter

    STATIC = "[0:v]crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920[vsingle]"
    chain = build_loudnorm_filter(MEASURED)
    _, fc, _, amap = build_single_pass_filter(
        STATIC, has_audio=True, loudnorm_chain=chain,
    )
    assert amap == "[aout]", amap
    assert "[0:a]" + chain + "[aout]" in fc, fc


def test_single_pass_filter_without_chain_is_unchanged():
    """Callers that pass no chain (and silent sources) must get today's graph
    back — normalisation is added, not imposed on every entry point."""
    from services.render import build_single_pass_filter

    STATIC = "[0:v]crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920[vsingle]"
    _, fc, _, amap = build_single_pass_filter(STATIC, has_audio=True)
    assert amap == "0:a?", amap
    assert "loudnorm" not in fc, fc

    _, fc2, _, amap2 = build_single_pass_filter(
        STATIC, sfx_files=["whoosh_1.wav"], sfx_timestamps=[0.3], has_audio=True)
    assert "loudnorm" not in fc2, fc2
    assert amap2 == "[aout]"

    _, fc3, _, amap3 = build_single_pass_filter(STATIC, has_audio=False)
    assert amap3 is None and "loudnorm" not in fc3, fc3


# ---------------------------------------------------------------------------
# measured behaviour on a real render
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_render_single_pass_delivers_minus_14_lufs(tmp_path):
    from services.render import render_single_pass
    from services.vertical import build_vertical_filter

    src = _make_audio_clip(str(tmp_path / "quiet.mp4"), -23)
    out = str(tmp_path / "final.mp4")
    render_single_pass(src, 0.0, 5.0, out,
                       vertical_frag=build_vertical_filter(src, mode="static"))

    lufs, peak = _integrated_lufs(out)
    assert abs(lufs - (-14.0)) <= 0.8, f"expected -14 LUFS, measured {lufs}"
    assert peak is not None and peak <= 0.0, f"clipping: {peak} dBFS"

    # loudnorm works at 192 kHz internally; the deliverable must come back as
    # stereo 48 kHz.
    probe = _run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                  "-show_entries", "stream=sample_rate,channels", "-of", "csv=p=0", out])
    rate, channels = probe.stdout.strip().split(",")[:2]
    assert int(rate) == 48000 and int(channels) == 2, probe.stdout


@needs_ffmpeg
def test_consecutive_clips_from_one_source_match_in_loudness(tmp_path):
    """The defect itself: two clips cut from the same source 7 LU apart must
    leave the renderer within 1 LU of each other."""
    from services.render import render_single_pass
    from services.vertical import build_vertical_filter

    loud = _make_audio_clip(str(tmp_path / "loud.mp4"), -16, freq=1000)
    quiet = _make_audio_clip(str(tmp_path / "quiet.mp4"), -23, freq=900)

    spread_before = abs(_integrated_lufs(loud)[0] - _integrated_lufs(quiet)[0])
    assert spread_before > 4, "fixture is not a valid repro of the defect"

    levels = []
    for i, src in enumerate((loud, quiet)):
        out = str(tmp_path / f"final_{i}.mp4")
        render_single_pass(src, 0.0, 5.0, out,
                           vertical_frag=build_vertical_filter(src, mode="static"))
        levels.append(_integrated_lufs(out)[0])

    assert abs(levels[0] - levels[1]) < 1.0, f"after render: {levels}"


@needs_ffmpeg
def test_sfx_mixed_clip_is_still_normalised(tmp_path):
    """The normaliser runs on the mixed graph output, so an SFX layer cannot
    push the deliverable off target."""
    from services.render import render_single_pass
    from services.vertical import build_vertical_filter

    sfx = str(tmp_path / "whoosh.wav")
    _run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anoisesrc=color=white:duration=0.4",
          "-af", "volume=-25dB", sfx])

    src = _make_audio_clip(str(tmp_path / "quiet.mp4"), -23)
    out = str(tmp_path / "final.mp4")
    render_single_pass(src, 0.0, 5.0, out,
                       vertical_frag=build_vertical_filter(src, mode="static"),
                       sfx_files=[sfx], sfx_timestamps=[1.0])

    lufs, peak = _integrated_lufs(out)
    assert abs(lufs - (-14.0)) <= 1.2, f"expected ~-14 LUFS, measured {lufs}"
