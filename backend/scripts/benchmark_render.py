"""Time the single-pass renderer on a clip cut from a source video.

Usage:
    python scripts/benchmark_render.py --source SRC.mp4 [--start 0] [--duration 30] [--runs 3] [--json out.json]

The source is cut once (re-encoded, so every run starts from identical input)
into a temp folder. Each run then calls services.render.render_single_pass in a
fresh child process, which keeps the timing and resource numbers clean of this
script's own imports. Wall time is measured around the render call inside the
child. CPU seconds and peak RSS come from sampling the child and every
descendant (the ffmpeg processes) with psutil while the render runs; a process
that lives less than one sample tick (0.05 s) can be missed.

The renderer's storage root and app log are redirected into the temp folder, so
nothing is written under backend/storage. The temp folder is deleted at the end.
"""
import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

import psutil

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BACKEND_DIR, "scripts"))

CUT_TIMEOUT_S = 600
RUN_TIMEOUT_S = 3600
SAMPLE_S = 0.05

ASS_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Montserrat,64,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,3,0,2,40,40,200,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
{events}
"""


def _ass_time(seconds):
    cs = int(round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def write_captions(path, duration):
    """A minimal .ass file: one caption every two seconds across the clip."""
    lines = []
    t = 0.0
    n = 0
    while t < duration:
        end = min(t + 2.0, duration)
        lines.append(f"Dialogue: 0,{_ass_time(t)},{_ass_time(end)},Default,,0,0,0,,Benchmark caption {n}")
        t += 2.0
        n += 1
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(ASS_TEMPLATE.format(events="\n".join(lines)))
    return path


def cut_clip(source, start, duration, out_path):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-i", source, "-t", f"{duration:.3f}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="ignore", timeout=CUT_TIMEOUT_S)
    if r.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"cutting the clip failed: {r.stderr[-400:]}")
    return out_path


def _worker(args):
    """Child process: run one render and record its wall time to a JSON file."""
    sys.path.insert(0, BACKEND_DIR)
    os.chdir(BACKEND_DIR)
    from services.render import render_single_pass
    from config import settings

    # Marker tells the parent the render itself has started, so the CPU
    # baseline excludes this process's imports.
    with open(args.marker, "w", encoding="utf-8") as fh:
        fh.write("start")
    t0 = time.perf_counter()
    render_single_pass(args.clip, 0.0, args.duration, args.out, captions_path=args.ass)
    wall = time.perf_counter() - t0
    with open(args.result, "w", encoding="utf-8") as fh:
        json.dump({"wall_s": wall,
                   "ffmpeg_preset": settings.FFMPEG_PRESET,
                   "ffmpeg_crf": settings.FFMPEG_CRF,
                   "ffmpeg_threads": settings.FFMPEG_THREADS}, fh)
    return 0


class _TreeSampler:
    """Tracks CPU seconds and RSS of a process and all its live descendants."""

    def __init__(self, root_pid):
        self.root_pid = root_pid
        self.cpu_by_proc = {}   # (pid, create_time) -> latest user+system seconds
        self.peak_rss = 0
        self.base_cpu = None
        self.recording = False

    def sample(self):
        try:
            root = psutil.Process(self.root_pid)
            procs = [root] + root.children(recursive=True)
        except psutil.Error:
            return
        rss_total = 0
        for p in procs:
            try:
                ct = p.cpu_times()
                key = (p.pid, p.create_time())
                self.cpu_by_proc[key] = ct.user + ct.system
                rss_total += p.memory_info().rss
            except psutil.Error:
                continue
        if self.recording:
            self.peak_rss = max(self.peak_rss, rss_total)

    def start_recording(self):
        self.base_cpu = sum(self.cpu_by_proc.values())
        self.recording = True

    def cpu_seconds(self):
        return sum(self.cpu_by_proc.values()) - (self.base_cpu or 0.0)


def _run_once(index, clip, duration, ass, tmp):
    out = os.path.join(tmp, f"out_run{index}.mp4")
    result = os.path.join(tmp, f"result_run{index}.json")
    marker = os.path.join(tmp, f"marker_run{index}.txt")
    log_path = os.path.join(tmp, f"worker_run{index}.log")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["STORAGE_ROOT"] = os.path.join(tmp, "storage")
    env["APP_LOG_PATH"] = os.path.join(tmp, "app.log")
    cmd = [sys.executable, os.path.abspath(__file__), "--_worker",
           "--clip", clip, "--duration", str(duration), "--ass", ass,
           "--out", out, "--result", result, "--marker", marker]

    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, cwd=BACKEND_DIR, env=env, stdout=log, stderr=subprocess.STDOUT)
        sampler = _TreeSampler(proc.pid)
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while True:
            try:
                proc.wait(timeout=SAMPLE_S)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait(timeout=30)
                raise RuntimeError(f"run {index} exceeded {RUN_TIMEOUT_S}s")
            if not sampler.recording and os.path.exists(marker):
                sampler.start_recording()
            sampler.sample()

    if proc.returncode != 0 or not os.path.exists(result):
        with open(log_path, encoding="utf-8", errors="ignore") as fh:
            tail = fh.read()[-1500:]
        raise RuntimeError(f"render run {index} failed (exit {proc.returncode}):\n{tail}")

    with open(result, encoding="utf-8") as fh:
        info = json.load(fh)
    return {
        "run": index,
        "wall_s": info["wall_s"],
        "cpu_s": sampler.cpu_seconds(),
        "peak_rss_mb": sampler.peak_rss / (1024 * 1024),
        "output": out,
        "settings": {k: info[k] for k in ("ffmpeg_preset", "ffmpeg_crf", "ffmpeg_threads")},
    }


def _stats(values):
    return {"min": min(values), "mean": statistics.fmean(values), "max": max(values)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Benchmark the single-pass render.")
    parser.add_argument("--source", help="source video to cut the clip from")
    parser.add_argument("--start", type=float, default=0.0, help="clip start in the source (s)")
    parser.add_argument("--duration", type=float, default=30.0, help="clip length (s)")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--json", dest="json_out", help="also write the results here")
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--clip", help=argparse.SUPPRESS)
    parser.add_argument("--ass", help=argparse.SUPPRESS)
    parser.add_argument("--out", help=argparse.SUPPRESS)
    parser.add_argument("--result", help=argparse.SUPPRESS)
    parser.add_argument("--marker", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args._worker:
        return _worker(args)

    if not args.source:
        parser.error("--source is required")
    if not os.path.exists(args.source):
        parser.error(f"source not found: {args.source}")
    if args.runs < 1 or args.duration <= 0:
        parser.error("--runs and --duration must be positive")

    tmp = tempfile.mkdtemp(prefix="bench_render_")
    try:
        clip = cut_clip(args.source, args.start, args.duration, os.path.join(tmp, "clip.mp4"))
        ass = write_captions(os.path.join(tmp, "captions.ass"), args.duration)

        runs = []
        for i in range(1, args.runs + 1):
            res = _run_once(i, clip, args.duration, ass, tmp)
            runs.append(res)
            print(f"run {i}: wall {res['wall_s']:.2f} s  cpu {res['cpu_s']:.2f} s  "
                  f"peak RSS {res['peak_rss_mb']:.0f} MB")

        walls = [r["wall_s"] for r in runs]
        cpus = [r["cpu_s"] for r in runs]
        rss = [r["peak_rss_mb"] for r in runs]
        summary = {"wall_s": _stats(walls), "cpu_s": _stats(cpus), "peak_rss_mb": _stats(rss)}
        print("")
        print(f"{'':<14}{'min':>10}{'mean':>10}{'max':>10}")
        for label, key in (("wall s", "wall_s"), ("cpu s", "cpu_s"), ("peak RSS MB", "peak_rss_mb")):
            s = summary[key]
            print(f"{label:<14}{s['min']:>10.2f}{s['mean']:>10.2f}{s['max']:>10.2f}")

        import compare_media
        last_output = runs[-1]["output"]
        probed = compare_media.probe(last_output)
        print("")
        print(f"output ({runs[-1]['settings']['ffmpeg_preset']} preset, "
              f"CRF {runs[-1]['settings']['ffmpeg_crf']}, "
              f"threads {runs[-1]['settings']['ffmpeg_threads']}):")
        for key in ("width", "height", "duration_s", "video_bitrate_bps", "audio_bitrate_bps",
                    "fps", "audio_sample_rate", "av_gap_s"):
            print(f"  {key:<18}{probed.get(key)}")
        print(f"  {'size_bytes':<18}{os.path.getsize(last_output)}")

        if args.json_out:
            payload = {"source": args.source, "start": args.start, "duration": args.duration,
                       "runs": [{k: v for k, v in r.items() if k != "output"} for r in runs],
                       "summary": summary, "output_probe": probed}
            with open(args.json_out, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            print(f"\nJSON written to {args.json_out}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
