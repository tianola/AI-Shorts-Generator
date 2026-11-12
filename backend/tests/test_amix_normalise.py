"""Defect 2 — ``amix`` without ``:normalize=0`` ducked the voice on every mix.

``amix`` scales each input by 1/n by default (that is its whole point as a
*averaging* mixer), so mixing one SFX hit under the dialogue quietly halves the
speech, and the render's own ``volume=1.0``/"voice preserved at 100%" comment was
a lie the moment an effect was added. ``normalize=0`` asks for a straight sum
and leaves the levels the caller set.

Three call sites were reported. Only two of them were still un-normalised;
``build_single_pass_filter``'s mixer already carried ``normalize=0`` and
``test_filtergraphs.py:148`` pins it — that assertion is repeated here so the
regression cannot come back through this door either.
"""
import re
import shutil
import subprocess

import pytest

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="requires ffmpeg and ffprobe on PATH")

TIMEOUT = 120


def _amix_argument(filter_string):
    """The argument string of the single amix in this graph."""
    found = re.findall(r"amix=[^;\[]+", filter_string)
    assert len(found) == 1, found
    return found[0]


def _run(cmd, timeout=TIMEOUT):
    return subprocess.run(cmd, capture_output=True, text=True, errors="ignore", timeout=timeout)


def _tone(path, freq, level_db="-20dB", duration=5.0, video=False):
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i",
           f"sine=frequency={freq}:sample_rate=48000:duration={duration}"]
    if video:
        cmd += ["-f", "lavfi", "-i", f"color=c=teal:s=320x180:d={duration}",
                "-map", "1:v", "-map", "0:a"]
    cmd += ["-af", f"volume={level_db}", "-c:a", "aac", "-shortest", path]
    r = _run(cmd)
    assert r.returncode == 0, r.stderr[-400:]
    return path


def _integrated_lufs(path):
    r = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
              "-filter_complex", "ebur128=peak=true", "-f", "null", "-"])
    m = re.search(r"Integrated loudness:\s*\n\s*I:\s*(-?[\d.]+)\s*LUFS", r.stderr)
    assert m, r.stderr[-600:]
    return float(m.group(1))


class _Recorder:
    """Captures the mix/render command instead of running it.

    ``subprocess`` is one shared module object, so the patch is global while the
    test runs: ffprobe and the read-only loudnorm measurement pass are delegated
    to the real binaries (faking them would make the code take a different
    branch and record a different graph), and only the encode is captured.
    """

    def __init__(self, real_run):
        self.real = real_run
        self.commands = []

    def __call__(self, cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if str(cmd[0]).startswith("ffprobe") or "print_format=json" in joined:
            return self.real(cmd, **kwargs)
        self.commands.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def filter_of(self, needle):
        for cmd in self.commands:
            joined = " ".join(cmd)
            if needle in joined:
                return joined
        raise AssertionError(f"no ffmpeg call containing {needle!r}; saw {self.commands}")


# ---------------------------------------------------------------------------
# render.mux_tts — the title/voice-over mix
# ---------------------------------------------------------------------------
def test_mux_tts_amix_is_not_normalised(monkeypatch, tmp_path):
    import subprocess as _sp
    from services import render

    if not FFMPEG:
        pytest.skip("needs ffmpeg to build the fixture")
    video = _tone(str(tmp_path / "bed.mp4"), 440, video=True)
    tts = _tone(str(tmp_path / "voice.m4a"), 1000)

    rec = _Recorder(_sp.run)
    monkeypatch.setattr(render.subprocess, "run", rec)
    render.mux_tts(video, tts, str(tmp_path / "out.mp4"))

    fc = rec.filter_of("amix=")
    assert "normalize=0" in _amix_argument(fc), fc


@needs_ffmpeg
def test_mux_tts_keeps_the_bed_where_the_caller_put_it(tmp_path):
    """Measured, not string-matched.

    ``volume=0.35`` is the duck mux_tts asks for. With amix's normalising the
    bed arrives at 0.35 * 0.5 and the short loses 6 dB of its own audio, so the
    delivered file cannot be predicted from the filter the caller wrote. The
    reference render below is that same mix written out by hand; the pipeline
    output has to land on it.
    """
    from services import render

    video = _tone(str(tmp_path / "bed.mp4"), 440, video=True)
    tts = _tone(str(tmp_path / "voice.m4a"), 1000)
    out = str(tmp_path / "out.mp4")
    render.mux_tts(video, tts, out)

    reference = str(tmp_path / "reference.mp4")
    r = _run([
        "ffmpeg", "-y", "-i", video, "-i", tts, "-map", "0:v",
        "-filter_complex",
        "[1:a]adelay=600|600,volume=1.0[t];[0:a]volume=0.35[orig];"
        "[t][orig]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[a]",
        "-map", "[a]", "-c:v", "copy", "-c:a", "aac", reference,
    ])
    assert r.returncode == 0, r.stderr[-500:]

    got, want = _integrated_lufs(out), _integrated_lufs(reference)
    assert abs(got - want) < 0.5, (
        f"mux_tts delivered {got} LUFS, the un-normalised mix measures {want} LUFS "
        f"— amix is scaling the inputs (missing normalize=0?)")


# ---------------------------------------------------------------------------
# sfx.mix_sfx_into_video — the SFX mix of the sequential pipeline
# ---------------------------------------------------------------------------
def test_sfx_mix_amix_is_not_normalised(monkeypatch, tmp_path):
    import subprocess as _sp
    from services import sfx

    if not FFMPEG:
        pytest.skip("needs ffmpeg to build the fixture")
    bed = _tone(str(tmp_path / "bed.mp4"), 440, video=True)
    hit = _tone(str(tmp_path / "hit.wav"), 2000, "10dB", 0.3)

    monkeypatch.setattr(sfx, "_find_sfx_file", lambda name: hit)
    rec = _Recorder(_sp.run)
    monkeypatch.setattr(sfx.subprocess, "run", rec)
    sfx.mix_sfx_into_video(bed, str(tmp_path / "out.mp4"), ["whoosh_1"])

    fc = rec.filter_of("amix=")
    arg = _amix_argument(fc)
    assert "normalize=0" in arg, arg
    assert "inputs=2" in arg, arg
    # the mix feeds the loudness pass, not the encoder directly (defect 1)
    assert "loudnorm=" in fc, fc


# ---------------------------------------------------------------------------
# render.build_single_pass_filter — already fixed upstream; pinned, not patched
# ---------------------------------------------------------------------------
def test_single_pass_amix_stays_un_normalised():
    from services.render import build_single_pass_filter

    STATIC = "[0:v]crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920[vsingle]"
    _, fc, _, _ = build_single_pass_filter(
        STATIC, sfx_files=["whoosh_1.wav", "pop_1.wav"], sfx_timestamps=[0.3, 2.5],
        has_audio=True,
    )
    arg = _amix_argument(fc)
    assert "normalize=0" in arg, arg
    assert "inputs=3" in arg, arg
    assert "[0:a]volume=1.0[orig]" in fc, fc
