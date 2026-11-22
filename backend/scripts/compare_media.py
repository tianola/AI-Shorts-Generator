"""Measure two rendered videos and compare them: timing, loudness, colour,
similarity, tone frequency and detector counts.

Usage:
    python scripts/compare_media.py A.mp4 B.mp4
    python scripts/compare_media.py A.mp4 B.mp4 --reference SRC.mp4
    python scripts/compare_media.py A.mp4 B.mp4 --json out.json

For each file it reports ffprobe timing and stream facts, EBU R128 integrated
loudness / loudness range / true peak (ebur128), mean HUEMED / SATAVG / YAVG
over five evenly spaced frames (signalstats), blackdetect and freezedetect
counts, and the dominant frequency of the first three seconds (numpy FFT).
For the pair it reports SSIM (All) and PSNR (average) of B against A over the
common duration, with B scaled to A's size when they differ. With --reference,
each file also gets a speed ratio of reference duration / file duration.

Every subprocess call carries an explicit timeout. Nothing is written to disk
except the optional --json file and short-lived temp files that are removed.
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from fractions import Fraction

PROBE_TIMEOUT_S = 60
ANALYSIS_TIMEOUT_S = 900
PIPE_TIMEOUT_S = 120
COLOUR_FRAMES = 5
TONE_SECONDS = 3
TONE_RATE = 48000


def _run(cmd, timeout, cwd=None, binary=False):
    """Run a command, returning CompletedProcess. Never raises on non-zero exit."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=not binary,
        errors=None if binary else "ignore",
        timeout=timeout,
        cwd=cwd,
    )


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fraction(text):
    try:
        f = Fraction(text)
        return float(f) if f else None
    except (ValueError, ZeroDivisionError, TypeError):
        return None


def probe(path):
    """ffprobe facts for one file. Missing values come back as None."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    r = _run(cmd, PROBE_TIMEOUT_S)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {r.stderr[-400:]}")
    data = json.loads(r.stdout or "{}")
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    vid = next((s for s in streams if s.get("codec_type") == "video"), None)
    aud = next((s for s in streams if s.get("codec_type") == "audio"), None)

    fmt_duration = _float_or_none(fmt.get("duration"))
    video_duration = _float_or_none(vid.get("duration")) if vid else None
    audio_duration = _float_or_none(aud.get("duration")) if aud else None
    if video_duration is None and vid is not None:
        video_duration = fmt_duration
    if audio_duration is None and aud is not None:
        audio_duration = fmt_duration

    audio_bitrate = _float_or_none(aud.get("bit_rate")) if aud else None
    video_bitrate = _float_or_none(vid.get("bit_rate")) if vid else None
    if video_bitrate is None and vid is not None and fmt_duration:
        # Container bitrate minus audio gives a usable estimate for mp4 files
        # whose video stream does not carry its own bit_rate field.
        total = _float_or_none(fmt.get("bit_rate"))
        if total:
            video_bitrate = total - (audio_bitrate or 0.0)

    fps = None
    if vid:
        fps = _fraction(vid.get("avg_frame_rate")) or _fraction(vid.get("r_frame_rate"))

    gap = None
    if video_duration is not None and audio_duration is not None:
        gap = video_duration - audio_duration

    return {
        "path": path,
        "duration_s": fmt_duration,
        "video_duration_s": video_duration,
        "audio_duration_s": audio_duration,
        "av_gap_s": gap,
        "width": int(vid["width"]) if vid and "width" in vid else None,
        "height": int(vid["height"]) if vid and "height" in vid else None,
        "video_codec": vid.get("codec_name") if vid else None,
        "video_bitrate_bps": video_bitrate,
        "audio_bitrate_bps": audio_bitrate,
        "fps": fps,
        "audio_sample_rate": _float_or_none(aud.get("sample_rate")) if aud else None,
        "has_audio": aud is not None,
    }


def loudness(path):
    """EBU R128 integrated loudness (LUFS), loudness range (LU) and true peak (dBTP)."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
           "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"]
    r = _run(cmd, ANALYSIS_TIMEOUT_S)
    text = r.stderr or ""
    summary = text[text.rfind("Summary:"):] if "Summary:" in text else ""
    i_m = re.search(r"Integrated loudness:\s*\n\s*I:\s*(-?[\d.]+|-inf)\s*LUFS", summary)
    lra_m = re.search(r"Loudness range:\s*\n\s*LRA:\s*(-?[\d.]+)\s*LU", summary)
    tp_m = re.search(r"True peak:\s*\n\s*Peak:\s*(-?[\d.]+|-inf)\s*dBFS", summary)
    return {
        "integrated_lufs": _float_or_none(i_m.group(1)) if i_m else None,
        "loudness_range_lu": _float_or_none(lra_m.group(1)) if lra_m else None,
        "true_peak_dbtp": _float_or_none(tp_m.group(1)) if tp_m else None,
    }


def colour_stats(path, duration, frames=COLOUR_FRAMES):
    """Mean signalstats HUEMED / SATAVG / YAVG over evenly spaced frames."""
    if not duration or duration <= 0:
        return {"huemed": None, "satavg": None, "yavg": None, "frames": 0}
    samples = {"HUEMED": [], "SATAVG": [], "YAVG": []}
    with tempfile.TemporaryDirectory(prefix="cmp_colour_") as tmp:
        for idx in range(frames):
            # Centre of each of N equal slices, so the first and last frames
            # are not taken from the very first or last frame of the file.
            t = duration * (idx + 0.5) / frames
            stats_name = f"stats_{idx}.txt"
            cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{t:.3f}",
                   "-i", path, "-frames:v", "1",
                   "-vf", f"signalstats,metadata=mode=print:file={stats_name}",
                   "-f", "null", "-"]
            # cwd=tmp so the relative file name has no drive-letter colon to escape.
            _run(cmd, PROBE_TIMEOUT_S, cwd=tmp)
            stats_path = os.path.join(tmp, stats_name)
            if not os.path.exists(stats_path):
                continue
            with open(stats_path, encoding="utf-8", errors="ignore") as fh:
                for key, val in re.findall(r"lavfi\.signalstats\.(HUEMED|SATAVG|YAVG)=(-?[\d.]+)", fh.read()):
                    samples[key].append(float(val))

    def mean(vals):
        return sum(vals) / len(vals) if vals else None

    return {
        "huemed": mean(samples["HUEMED"]),
        "satavg": mean(samples["SATAVG"]),
        "yavg": mean(samples["YAVG"]),
        "frames": len(samples["YAVG"]),
    }


def similarity(b_path, a_path, a_info, b_info, duration):
    """SSIM (All) and PSNR (average) of B against A, B scaled to A's size."""
    if not duration or duration <= 0:
        return {"ssim_all": None, "psnr_avg": None, "compared_seconds": 0.0}
    width, height = a_info.get("width"), a_info.get("height")
    if not width or not height:
        return {"ssim_all": None, "psnr_avg": None, "compared_seconds": 0.0}
    scale = f"scale={width}:{height}:flags=bicubic,setsar=1"
    out = {"compared_seconds": duration}

    # Both inputs are cut to the common duration. The first input is the
    # distorted (B) stream and the second the reference (A), as ssim/psnr expect.
    for name, filt in (("ssim", "ssim"), ("psnr", "psnr")):
        fc = f"[0:v]{scale}[m];[1:v]setsar=1[r];[m][r]{filt}"
        cmd = ["ffmpeg", "-hide_banner", "-nostats",
               "-t", f"{duration:.3f}", "-i", b_path,
               "-t", f"{duration:.3f}", "-i", a_path,
               "-filter_complex", fc, "-f", "null", "-"]
        r = _run(cmd, ANALYSIS_TIMEOUT_S)
        text = r.stderr or ""
        if name == "ssim":
            m = re.findall(r"SSIM .*?All:([\d.]+|inf)", text)
            out["ssim_all"] = _float_or_none(m[-1]) if m else None
        else:
            m = re.findall(r"PSNR .*?average:([\d.]+|inf)", text)
            out["psnr_avg"] = _float_or_none(m[-1]) if m else None
    return out


def dominant_frequency(path, seconds=TONE_SECONDS):
    """Dominant audio frequency (Hz) of the first `seconds`, or None if silent/absent."""
    import numpy as np

    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path,
           "-t", str(seconds), "-vn", "-ac", "1", "-ar", str(TONE_RATE),
           "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
    r = _run(cmd, PIPE_TIMEOUT_S, binary=True)
    if r.returncode != 0 or len(r.stdout) < 2048:
        return None
    samples = np.frombuffer(r.stdout[: len(r.stdout) // 2 * 2], dtype="<i2").astype(np.float64)
    if not np.any(samples):
        return None
    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window))
    freqs = np.fft.rfftfreq(len(samples), d=1.0 / TONE_RATE)
    spectrum[freqs < 20.0] = 0.0  # ignore DC and sub-audible drift
    peak = int(np.argmax(spectrum))
    if peak <= 0 or peak >= len(spectrum) - 1:
        return float(freqs[peak])
    # Parabolic interpolation between the peak bin and its neighbours.
    a, b, c = spectrum[peak - 1], spectrum[peak], spectrum[peak + 1]
    denom = a - 2 * b + c
    offset = 0.5 * (a - c) / denom if denom != 0 else 0.0
    return float((peak + offset) * TONE_RATE / len(samples))


def detector_counts(path):
    """Counts of blackdetect and freezedetect events on the video stream."""
    counts = {}
    for name, filt in (("black_segments", "blackdetect=d=0.1:pix_th=0.10"),
                       ("freeze_segments", "freezedetect=n=0.003:d=0.5")):
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
               "-an", "-vf", filt, "-f", "null", "-"]
        r = _run(cmd, ANALYSIS_TIMEOUT_S)
        text = r.stderr or ""
        key = "black_start" if name == "black_segments" else "freeze_start"
        counts[name] = len(re.findall(rf"{key}:", text))
    return counts


def analyse_file(path, reference=None, ref_duration=None):
    """Everything measured for one file."""
    info = probe(path)
    report = dict(info)
    report["loudness"] = loudness(path) if info["has_audio"] else None
    report["colour"] = colour_stats(path, info["video_duration_s"] or info["duration_s"])
    report["tone_hz_first_3s"] = dominant_frequency(path) if info["has_audio"] else None
    report["detectors"] = detector_counts(path)
    if reference is not None and ref_duration and info["duration_s"]:
        report["speed_ratio_vs_reference"] = ref_duration / info["duration_s"]
    else:
        report["speed_ratio_vs_reference"] = None
    return report


def compare(a_path, b_path, reference=None):
    """Measure A and B and the similarity of B to A. Returns one dict."""
    ref_duration = None
    if reference:
        ref_duration = probe(reference)["duration_s"]
    a = analyse_file(a_path, reference, ref_duration)
    b = analyse_file(b_path, reference, ref_duration)
    common = min(a["duration_s"] or 0.0, b["duration_s"] or 0.0)
    sim = similarity(b_path, a_path, a, b, common)
    return {"reference": reference, "reference_duration_s": ref_duration,
            "a": a, "b": b, "b_vs_a": sim}


# ---------------------------------------------------------------------------
# Table output
# ---------------------------------------------------------------------------
ROWS = [
    ("video duration (s)", lambda f: f["video_duration_s"], "{:.3f}"),
    ("audio duration (s)", lambda f: f["audio_duration_s"], "{:.3f}"),
    ("A/V gap (s)", lambda f: f["av_gap_s"], "{:+.3f}"),
    ("resolution", lambda f: (f"{f['width']}x{f['height']}" if f["width"] else None), None),
    ("fps", lambda f: f["fps"], "{:.3f}"),
    ("video bitrate (kb/s)", lambda f: (f["video_bitrate_bps"] / 1000 if f["video_bitrate_bps"] else None), "{:.1f}"),
    ("audio bitrate (kb/s)", lambda f: (f["audio_bitrate_bps"] / 1000 if f["audio_bitrate_bps"] else None), "{:.1f}"),
    ("audio sample rate (Hz)", lambda f: f["audio_sample_rate"], "{:.0f}"),
    ("speed ratio vs reference", lambda f: f["speed_ratio_vs_reference"], "{:.4f}"),
    ("integrated loudness (LUFS)", lambda f: (f["loudness"] or {}).get("integrated_lufs"), "{:.2f}"),
    ("loudness range (LU)", lambda f: (f["loudness"] or {}).get("loudness_range_lu"), "{:.2f}"),
    ("true peak (dBTP)", lambda f: (f["loudness"] or {}).get("true_peak_dbtp"), "{:.2f}"),
    ("HUEMED (mean)", lambda f: f["colour"]["huemed"], "{:.2f}"),
    ("SATAVG (mean)", lambda f: f["colour"]["satavg"], "{:.2f}"),
    ("YAVG (mean)", lambda f: f["colour"]["yavg"], "{:.2f}"),
    ("tone, first 3 s (Hz)", lambda f: f["tone_hz_first_3s"], "{:.1f}"),
    ("black segments", lambda f: f["detectors"]["black_segments"], "{:d}"),
    ("freeze segments", lambda f: f["detectors"]["freeze_segments"], "{:d}"),
]


def _fmt(value, fmt):
    if value is None:
        return "n/a"
    if fmt is None:
        return str(value)
    try:
        return fmt.format(value)
    except (ValueError, TypeError):
        return str(value)


def format_table(report):
    a, b = report["a"], report["b"]
    lines = []
    header = f"{'metric':<28}{'A':>16}{'B':>16}{'B - A':>14}"
    lines.append(f"A = {a['path']}")
    lines.append(f"B = {b['path']}")
    if report["reference"]:
        lines.append(f"reference = {report['reference']} ({_fmt(report['reference_duration_s'], '{:.3f}')} s)")
    lines.append("")
    lines.append(header)
    lines.append("-" * len(header))
    for label, getter, fmt in ROWS:
        av, bv = getter(a), getter(b)
        diff = "n/a"
        if isinstance(av, (int, float)) and isinstance(bv, (int, float)) and fmt:
            diff = _fmt(bv - av + 0.0, fmt.replace("+", "") if fmt.startswith("{:+") else fmt)
        elif av is not None and bv is not None and av != bv:
            diff = "differs"
        elif av is not None and av == bv:
            diff = "same"
        lines.append(f"{label:<28}{_fmt(av, fmt):>16}{_fmt(bv, fmt):>16}{diff:>14}")
    lines.append("")
    sim = report["b_vs_a"]
    lines.append(f"B vs A over {_fmt(sim['compared_seconds'], '{:.3f}')} s common duration:")
    lines.append(f"  SSIM All        : {_fmt(sim['ssim_all'], '{:.4f}')}")
    lines.append(f"  PSNR average dB : {_fmt(sim['psnr_avg'], '{:.2f}')}")
    return "\n".join(lines)


def _jsonable(obj):
    if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    return obj


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compare two rendered videos.")
    parser.add_argument("a", help="file A (the reference for similarity)")
    parser.add_argument("b", help="file B (compared against A)")
    parser.add_argument("--reference", help="source clip for speed ratios")
    parser.add_argument("--json", dest="json_out", help="also write the full report here")
    args = parser.parse_args(argv)

    for p in (args.a, args.b) + ((args.reference,) if args.reference else ()):
        if not os.path.exists(p):
            parser.error(f"file not found: {p}")

    report = compare(args.a, args.b, args.reference)
    print(format_table(report))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(_jsonable(report), fh, indent=2)
        print(f"\nJSON written to {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
