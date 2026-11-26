"""Live Studio: watch a live stream, spot highlights as they happen, cut them out.

The concept (ring buffer of the last minutes, live transcript, AI highlight detection,
instant clip) follows Clipify AI's Live Studio
(github.com/sherlock-dev-ai/Clipify-AI-Video-Clipper-FREE, CC BY-NC 4.0,
non-commercial); the implementation is our own.

Capture: ffmpeg writes the stream as short .ts segments into a buffer folder; segments
older than the buffer length are deleted. Every new segment is transcribed; a rolling
window of the transcript is scored for a highlight (LLM, with a keyword/energy
fallback); a hit, or the operator's "clip that", is cut from the buffered segments.
"""

import json
import re
import shutil
import sys
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from loguru import logger

from services.title_style import fix_title

from config import settings

SEGMENT_SECONDS = 5
YOUTUBE_RE = re.compile(r"^(https?://)?(www\.|m\.)?(youtube\.com/|youtu\.be/)", re.I)
TWITCH_RE = re.compile(r"^(https?://)?(www\.)?twitch\.tv/\w+", re.I)
TIKTOK_RE = re.compile(r"^(https?://)?(www\.)?tiktok\.com/@[\w.-]+", re.I)
OBS_URL = "rtmp://127.0.0.1:1935/live/stream"

_HEURISTICS = {
    "Funny": re.compile(r"\b(haha+|lmao|lol|rofl|hilarious|joke)\b", re.I),
    "Exciting": re.compile(r"\b(oh my god|omg|let'?s go|insane|unbelievable|no way|yooo+)\b", re.I),
    "Surprising": re.compile(r"\b(wait what|are you serious|what just happened|no shot)\b", re.I),
    "Gaming": re.compile(r"\b(clutch|ace|headshot|pentakill|boss fight|victory)\b", re.I),
    "Announcement": re.compile(r"\b(big news|announcement|breaking|officially|launching)\b", re.I),
}


# ---------------------------------------------------------------- the buffer

@dataclass
class Segment:
    path: Path
    index: int
    start: float     # seconds since the session began


class RollingBuffer:
    """The last `max_seconds` of the stream as numbered segments on disk."""

    def __init__(self, folder: Path, max_seconds: int):
        self.folder = Path(folder)
        self.max_seconds = max_seconds
        self.folder.mkdir(parents=True, exist_ok=True)

    def clear(self) -> None:
        for item in self.folder.glob("seg_*.ts"):
            item.unlink(missing_ok=True)

    def segments(self) -> List[Segment]:
        out = []
        for item in sorted(self.folder.glob("seg_*.ts")):
            try:
                index = int(item.stem.split("_")[1])
            except (IndexError, ValueError):
                continue
            out.append(Segment(item, index, index * float(SEGMENT_SECONDS)))
        return out

    def prune(self, now_seconds: float) -> int:
        """Delete segments that ended before the buffer window. Returns how many."""
        removed = 0
        for seg in self.segments():
            if seg.start + SEGMENT_SECONDS < now_seconds - self.max_seconds:
                seg.path.unlink(missing_ok=True)
                removed += 1
        return removed

    def covering(self, start: float, end: float) -> List[Segment]:
        """Segments overlapping [start, end], oldest first. Keeps only completed ones
        (the newest file is still being written)."""
        segs = self.segments()[:-1]
        return [s for s in segs if s.start + SEGMENT_SECONDS > start and s.start < end]


# ---------------------------------------------------------------- highlights

@dataclass
class Highlight:
    start: float
    end: float
    category: str
    title: str
    score: float
    reason: str
    description: str = ""


def _subject(context: str) -> str:
    """A short name for what the stream is about, from its title/context (first words)."""
    words = re.sub(r"[^\w\s'&.-]", " ", context or "").split()
    return " ".join(words[:4]) if words else "Live"


def _keep_topic(title: str, topic: str) -> str:
    """Make sure the title names the stream's topic (e.g. "GTA 6"), so the clip stays on it."""
    title = title.strip()
    if not topic or topic.lower() in title.lower():
        return title[:80]
    return f"{topic}: {title}"[:80]


def heuristic_highlight(text: str, start: float, end: float, context: str = "",
                        topic: str = "") -> Optional[Highlight]:
    """Cheap offline detector: keyword families and exclamation density.

    Its title is generic on purpose: the first words of the speech make a cut-off, meaningless
    title, so a hit found this way is named after the topic (or the stream) and the kind of moment."""
    best, weight = None, 0
    for category, pattern in _HEURISTICS.items():
        hits = len(pattern.findall(text))
        if hits * 2 > weight:
            best, weight = category, hits * 2
    if text.count("!") + text.count("?") >= 3:
        weight += 2
        best = best or "High-energy"
    if not best or weight < 2:
        return None
    return Highlight(start, end, best, f"{topic or _subject(context)}: {best.lower()} moment",
                     min(10.0, 5.0 + weight), "keyword match")


async def llm_highlight(text: str, start: float, end: float, context: str = "",
                        topic: str = "") -> Optional[Highlight]:
    """Ask the model whether the recent speech is a clip-worthy moment, and to name it.

    `context` is what is known about the stream (its title, what is happening around it), so
    the title can say what the clip is about instead of repeating a line of chatter. `topic` is
    the name every title must carry (e.g. "GTA 6"), which keeps the clip on the hype."""
    from services.llm_router import call_llm_json

    about = f"What is known about this stream: {context}\n" if context else ""
    keep = (f'The stream is about "{topic}". The title MUST contain "{topic}" exactly, and it must '
            "name what happens in this moment, not generic chatter. ") if topic else ""
    prompt = (
        "You clip live streams for YouTube Shorts.\n"
        f"{about}Recent speech from the stream (automatic transcript, may contain mistakes):\n"
        f'"{text[-1800:]}"\n\n'
        "Is it an engaging moment worth a vertical short (funny, exciting, surprising, emotional, "
        "an announcement, a great play, an insight)? Reply JSON only: "
        '{"is_highlight": true/false, "category": "one word", "score": 1-10, '
        '"title": "a headline of 5-9 words that says what happens, the surprising or funny part first; '
        "a complete phrase, never cut off, no quotes, no clickbait that the clip does not deliver. "
        f'{keep}", '
        '"description": "one sentence for the video description", "reason": "short"}'
    )
    raw = await call_llm_json(prompt=prompt, system="You are a sharp live-stream clipper. JSON only.",
                              model=settings.OMNIROUTE_MODEL, temperature=0.3, max_tokens=600)
    data = json.loads(raw)          # unreadable reply: raises, so the caller falls back
    if not data.get("is_highlight"):
        return None                 # the model's verdict: not worth a clip
    try:
        score = max(1.0, min(10.0, float(data.get("score", 7))))
    except (TypeError, ValueError):
        score = 7.0
    title = _keep_topic(str(data.get("title") or "").strip().strip('"'), topic)
    # The shared checker: hype and extra emoji go, the length is cut, the topic is kept, and a
    # title with nothing the stream actually says is replaced by the stream's own words.
    title, _fixed = fix_title(title, text, topic)
    if len(title.split()) < 3:        # nothing usable to publish under
        return None
    return Highlight(start, end, str(data.get("category") or "Highlight")[:24], title, score,
                     str(data.get("reason") or "")[:200], str(data.get("description") or "")[:300])


def threshold_for(sensitivity: int) -> float:
    """Score a highlight must reach: sensitivity 1 (picky) .. 10 (eager)."""
    return max(3.0, 9.5 - max(1, min(10, sensitivity)) * 0.7)


# ---------------------------------------------------------------- the session

def validate_source(kind: str, url: str) -> Optional[str]:
    """An error message, or None when the source is acceptable."""
    kind = (kind or "").lower()
    if kind == "obs":
        return None
    if kind == "youtube":
        return None if YOUTUBE_RE.match(url or "") else "Enter a YouTube live link"
    if kind == "twitch":
        return None if TWITCH_RE.match(url or "") else "Enter a Twitch channel link (twitch.tv/name)"
    if kind == "tiktok":
        return None if TIKTOK_RE.match(url or "") else "Enter a TikTok live link (tiktok.com/@name/live)"
    return "Source must be youtube, twitch, tiktok or obs"


# H.264 first: yt-dlp can offer AV1 video, which the MPEG-TS buffer cannot hold, and the buffer then
# records audio only. The first address is the video, the second the audio.
LIVE_FORMAT = "bv*[height<=1080][vcodec^=avc1]+ba/bv*[height<=1080]+ba/b[height<=1080]/b"


class LiveSession:
    def __init__(self, root: Path, kind: str, url: str, sensitivity: int = 5,
                 buffer_minutes: int = 10, auto_clip: bool = True, min_gap: float = 30.0,
                 layout: str = "auto", context: str = "", topic: str = "",
                 facecam: Optional[tuple] = None):
        self.root = Path(root)
        self.kind, self.url = kind.lower(), url.strip()
        self.sensitivity = max(1, min(10, sensitivity))
        self.auto_clip = auto_clip
        self.layout = layout or "auto"      # vertical layout of the clips: auto, gaming, fit_blur...
        self.facecam = facecam              # fixed facecam box as fractions of the frame (x, y, w, h), if known
        self.context = context.strip()      # what the stream is about; the stream's own title is added
        self.topic = topic.strip()          # the name every title must carry, e.g. "GTA 6"
        self.min_gap = min_gap
        self.buffer = RollingBuffer(self.root / "buffer", max(3, min(30, buffer_minutes)) * 60)
        self.clips_dir = self.root / "clips"
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.status, self.detail = "idle", "Ready"
        self.transcript: List[dict] = []
        self.started: Optional[float] = None
        self._proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._last_hit = -1e9
        self._lock = threading.Lock()
        self._cam_votes: List[Tuple[float, float, float]] = []   # (centre x, centre y, hit rate) per clip

    # -- control
    def start(self) -> None:
        self.buffer.clear()
        self._stop.clear()
        self.started = time.time()
        self.status, self.detail = "connecting", "Opening the stream"
        threading.Thread(target=self._capture, daemon=True, name="live-capture").start()
        threading.Thread(target=self._analyse, daemon=True, name="live-analyse").start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self.status, self.detail = "idle", "Stopped"

    @property
    def elapsed(self) -> float:
        return time.time() - self.started if self.started else 0.0

    # -- capture
    def _stream_url(self) -> List[str]:
        """ffmpeg input arguments for the stream. A YouTube live offers video and audio as
        separate streams, so yt-dlp may print two addresses (video first, then audio)."""
        if self.kind == "obs":
            return ["-listen", "1", "-i", OBS_URL]
        out = subprocess.run([sys.executable, "-m", "yt_dlp", "-g", "--no-warnings",
                              "-f", LIVE_FORMAT, self.url],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        lines = [ln.strip() for ln in out.stdout.splitlines() if ln.startswith("http")]
        if out.returncode or not lines:
            reason = [ln for ln in (out.stderr or "").splitlines() if ln.startswith("ERROR")]
            raise RuntimeError((reason[-1] if reason else "yt-dlp could not open the stream")[:300])
        args: List[str] = []
        for address in lines[:2]:
            args += ["-i", address]
        return args

    def _learn_stream_title(self) -> None:
        """Add the stream's own title to the context the model names clips from."""
        if self.kind not in ("youtube", "twitch"):
            return
        try:
            out = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", "--skip-download",
                                  "--print", "%(title)s", self.url],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            title = (out.stdout or "").strip().splitlines()[-1:] or [""]
            if title[0] and title[0] not in self.context:
                self.context = f"{self.context} Stream title: {title[0]}".strip()
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _capture(self) -> None:
        try:
            self._learn_stream_title()
            source = self._stream_url()
            inputs = source.count("-i")
            maps = ["-map", "0:v:0?", "-map", "1:a:0?"] if inputs == 2 else ["-map", "0:v:0?", "-map", "0:a:0?"]
            cmd = ["ffmpeg", "-y", *source, *maps, "-c", "copy", "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
                   "-reset_timestamps", "1", str(self.buffer.folder / "seg_%06d.ts")]
            self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.status, self.detail = "live", "Recording the live buffer"
            while not self._stop.is_set() and self._proc.poll() is None:
                self.buffer.prune(self.elapsed)
                time.sleep(2)
            if not self._stop.is_set():
                self.status, self.detail = "disconnected", "The stream ended"
        except Exception as exc:
            logger.warning(f"Live capture failed: {exc}")
            self.status, self.detail = "disconnected", str(exc)[:200]

    # -- analysis
    def _transcribe(self, seg: Segment) -> str:
        """The speech in one segment, or "" when there is none or recognition fails.

        The audio is decoded here with ffmpeg and handed over as samples: faster-whisper's
        own file decoder breaks against some PyAV versions."""
        try:
            import numpy as np
            from services.whisper import get_whisper_model
            raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(seg.path), "-vn", "-ac", "1", "-ar", "16000",
                                  "-f", "s16le", "-"], capture_output=True, timeout=60).stdout
            if len(raw) < 16000:   # under half a second of audio
                return ""
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            model = get_whisper_model("tiny")
            segments, _info = model.transcribe(samples, vad_filter=True, language="en")
            return " ".join(s.text.strip() for s in segments).strip()
        except Exception as exc:
            logger.warning(f"Live transcription failed: {type(exc).__name__}: {exc}")
            return ""

    def _analyse(self) -> None:
        import asyncio
        seen = -1
        window: List[dict] = []
        while not self._stop.is_set():
            ready = [s for s in self.buffer.segments()[:-1] if s.index > seen]
            if not ready:
                time.sleep(1.5)
                continue
            for seg in ready:
                seen = seg.index
                text = self._transcribe(seg)
                if not text:
                    continue
                entry = {"start": seg.start, "end": seg.start + SEGMENT_SECONDS, "text": text}
                with self._lock:
                    self.transcript.append(entry)
                    self.transcript[:] = self.transcript[-60:]
                window.append(entry)
                window[:] = window[-4:]              # about 20 s of speech
                joined = " ".join(w["text"] for w in window)
                if not self.auto_clip or len(joined.split()) < 8:
                    continue
                if window[0]["start"] - self._last_hit < self.min_gap:
                    continue
                # The keyword detector only stands in when the model could not answer. When
                # the model answered "not a highlight" that is the verdict; second-guessing
                # it produced clips with made-up generic titles.
                try:
                    hit = asyncio.run(llm_highlight(joined, window[0]["start"], window[-1]["end"], self.context, self.topic))
                except Exception:
                    hit = heuristic_highlight(joined, window[0]["start"], window[-1]["end"], self.context, self.topic)
                if hit and hit.score >= threshold_for(self.sensitivity):
                    self._last_hit = hit.end
                    try:
                        self.cut(max(0.0, hit.start - 2.0), hit.end + 3.0, hit.title, hit.category, hit.reason,
                                 hit.description)
                    except Exception as exc:
                        logger.warning(f"Live clip failed: {exc}")

    # -- clipping
    def _find_facecam(self, raw: Path) -> Optional[tuple]:
        """The webcam panel (fractions of the frame) for this clip, or None.

        facecam_finder looks for a face that the detector hits on most sampled frames in one
        corner or side edge. The webcam is locked for the whole session once the same spot is
        found in two clips, or in a clip where it is hit on 80% of the frames. Until then each
        clip uses the webcam it shows itself."""
        from services.facecam_finder import find_webcams
        from services.gaming_layout import panel_for_face

        cams = find_webcams(str(raw))
        if not cams:
            return None
        cam = cams[0]
        w, h = cam.src_w, cam.src_h
        x, y, bw, bh = cam.box
        cx, cy = (x + bw / 2) / w, (y + bh / 2) / h
        self._cam_votes.append((cx, cy, cam.hit_rate))
        agree = [v for v in self._cam_votes if abs(v[0] - cx) < 0.03 and abs(v[1] - cy) < 0.03]
        px, py, pw, ph = panel_for_face(cam.box, w, h)
        panel = (px / w, py / h, pw / w, ph / h)
        if len(agree) >= 2 or cam.hit_rate >= 0.8:
            self.facecam = panel
            logger.info(f"Live webcam locked at {panel} ({cam.region}, hit rate {cam.hit_rate})")
        return panel

    def _crop_with_facecam(self, raw: Path, out: Path) -> None:
        """The streamer's webcam fixed on top, the gameplay cropped below.

        The webcam box is the one given for the session, or found in the first clip and then kept."""
        from services.gaming_layout import build_gaming_filter
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height", "-of", "csv=p=0", str(raw)],
                               capture_output=True, text=True).stdout.strip().split(",")
        w, h = int(probe[0]), int(probe[1])
        cam = self.facecam or self._find_facecam(raw)
        if not cam:
            raise LookupError("no facecam in this stream")
        fx, fy, fw, fh = cam
        graph = build_gaming_filter(w, h, (int(fx * w), int(fy * h), int(fw * w), int(fh * h)), cover=True)
        proc = subprocess.run(["ffmpeg", "-y", "-i", str(raw), "-filter_complex", graph, "-map", "[outv]", "-map", "0:a?",
                               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "copy",
                               "-movflags", "+faststart", str(out)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode:
            raise RuntimeError(proc.stderr[-300:])

    def cut(self, start: float, end: float, title: str = "Live moment", category: str = "",
            reason: str = "", description: str = "") -> dict:
        """Cut [start, end] (seconds since session start) from the buffer into a clip."""
        segs = self.buffer.covering(start, end)
        if not segs:
            raise RuntimeError("That moment is no longer in the buffer")
        clip_id = uuid.uuid4().hex[:8]
        folder = self.clips_dir / clip_id
        folder.mkdir(parents=True, exist_ok=True)
        listing = folder / "parts.txt"
        listing.write_text("".join(f"file '{s.path.as_posix()}'\n" for s in segs), encoding="utf-8")
        raw, vertical = folder / "raw.mp4", folder / "clip.mp4"
        proc = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-af", "aresample=async=1:first_pts=0",
                               "-c:a", "aac", str(raw)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode:
            raise RuntimeError(f"ffmpeg concat failed: {proc.stderr[-300:]}")
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                                "stream=codec_type", "-of", "csv=p=0", str(raw)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
        if "video" not in probe.stdout:
            raise RuntimeError("The buffer has no video for this moment (stream format not recorded)")
        try:
            done = False
            if self.facecam or self.layout in ("auto", "gaming"):
                try:
                    self._crop_with_facecam(raw, vertical)
                    done = True
                except LookupError:
                    pass            # no webcam overlay: not a gaming stream, frame it normally
            if not done:
                from services.smart_crop import smart_crop_video
                smart_crop_video(str(raw), str(vertical), mode=self.layout)
        except Exception as exc:
            logger.warning(f"Live clip kept horizontal ({exc})")
            shutil.copy2(raw, vertical)
        listing.unlink(missing_ok=True)
        if title and title != "Instant clip":
            # The headline stays on screen for the whole clip, like the clips the main pipeline makes.
            titled = folder / "titled.mp4"
            try:
                from services.motion_title import add_motion_title
                add_motion_title(str(vertical), title, str(titled))
                titled.replace(vertical)
            except Exception as exc:
                logger.warning(f"Live clip has no title overlay ({exc})")
                titled.unlink(missing_ok=True)
        meta = {"id": clip_id, "title": title, "category": category, "reason": reason, "description": description,
                "duration": round(sum(1 for _ in segs) * float(SEGMENT_SECONDS), 1),
                "created_at": time.time(),
                "path": str(vertical.relative_to(settings.STORAGE_ROOT)).replace("\\", "/")}
        (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return meta

    def instant_clip(self, seconds: int = 30) -> dict:
        """Cut the last `seconds` of the stream: the "clip that" button."""
        now = self.elapsed
        return self.cut(max(0.0, now - max(10, min(120, seconds))), now, "Instant clip", "Manual")

    def clips(self) -> List[dict]:
        out = []
        for meta in self.clips_dir.glob("*/meta.json"):
            try:
                out.append(json.loads(meta.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda c: c.get("created_at", 0), reverse=True)

    def delete_clip(self, clip_id: str) -> bool:
        if not clip_id.isalnum():
            return False
        folder = self.clips_dir / clip_id
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
            return True
        return False


_session: Optional[LiveSession] = None


def get_session() -> Optional[LiveSession]:
    return _session


def start_session(kind: str, url: str, **options) -> LiveSession:
    global _session
    error = validate_source(kind, url)
    if error:
        raise ValueError(error)
    if _session and _session.status in ("connecting", "live"):
        raise RuntimeError("A live session is already running; stop it first")
    _session = LiveSession(Path(settings.STORAGE_ROOT) / "live_studio", kind, url, **options)
    _session.start()
    return _session


def stop_session() -> None:
    if _session:
        _session.stop()
