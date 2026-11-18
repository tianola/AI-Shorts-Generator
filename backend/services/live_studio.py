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
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from loguru import logger

from config import settings

SEGMENT_SECONDS = 5
YOUTUBE_RE = re.compile(r"^(https?://)?(www\.|m\.)?(youtube\.com/|youtu\.be/)", re.I)
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


def heuristic_highlight(text: str, start: float, end: float) -> Optional[Highlight]:
    """Cheap offline detector: keyword families and exclamation density."""
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
    title = " ".join(text.split()[:6]).strip().title()
    return Highlight(start, end, best, title or best, min(10.0, 5.0 + weight), "keyword match")


async def llm_highlight(text: str, start: float, end: float) -> Optional[Highlight]:
    """Ask the model whether the recent speech is a clip-worthy moment."""
    from services.llm_router import call_llm_json

    prompt = (
        "You clip live streams. Recent speech from the stream:\n"
        f'"{text[-1800:]}"\n\n'
        "Is it an engaging moment worth a vertical short (funny, exciting, surprising, emotional, "
        "an announcement, a great play, an insight)? Reply JSON only: "
        '{"is_highlight": true/false, "category": "one word", "score": 1-10, '
        '"title": "catchy 4-7 words in the spoken language", "reason": "short"}'
    )
    raw = await call_llm_json(prompt=prompt, system="You are a sharp live-stream clipper. JSON only.",
                              model=settings.OMNIROUTE_MODEL, temperature=0.3, max_tokens=600)
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not data.get("is_highlight"):
        return None
    try:
        score = max(1.0, min(10.0, float(data.get("score", 7))))
    except (TypeError, ValueError):
        score = 7.0
    return Highlight(start, end, str(data.get("category") or "Highlight")[:24],
                     str(data.get("title") or "Live moment").strip()[:60], score,
                     str(data.get("reason") or "")[:200])


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
    if kind == "tiktok":
        return None if TIKTOK_RE.match(url or "") else "Enter a TikTok live link (tiktok.com/@name/live)"
    return "Source must be youtube, tiktok or obs"


class LiveSession:
    def __init__(self, root: Path, kind: str, url: str, sensitivity: int = 5,
                 buffer_minutes: int = 10, auto_clip: bool = True, min_gap: float = 30.0):
        self.root = Path(root)
        self.kind, self.url = kind.lower(), url.strip()
        self.sensitivity = max(1, min(10, sensitivity))
        self.auto_clip = auto_clip
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
        if self.kind == "obs":
            return ["-listen", "1", "-i", OBS_URL]
        out = subprocess.run(["yt-dlp", "-g", "-f", "best[height<=1080]/best", self.url],
                             capture_output=True, text=True, timeout=60)
        lines = [ln for ln in out.stdout.splitlines() if ln.startswith("http")]
        if out.returncode or not lines:
            raise RuntimeError((out.stderr or "yt-dlp could not open the stream").strip()[-300:])
        return ["-i", lines[0]]

    def _capture(self) -> None:
        try:
            source = self._stream_url()
            cmd = ["ffmpeg", "-y", *source, "-c", "copy", "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
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
        try:
            from services.whisper import get_whisper_model
            model = get_whisper_model("tiny")
            segments, _info = model.transcribe(str(seg.path), vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()
        except Exception as exc:
            logger.debug(f"Live transcription skipped: {exc}")
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
                try:
                    hit = asyncio.run(llm_highlight(joined, window[0]["start"], window[-1]["end"]))
                except Exception:
                    hit = None
                hit = hit or heuristic_highlight(joined, window[0]["start"], window[-1]["end"])
                if hit and hit.score >= threshold_for(self.sensitivity):
                    self._last_hit = hit.end
                    try:
                        self.cut(max(0.0, hit.start - 2.0), hit.end + 3.0, hit.title, hit.category, hit.reason)
                    except Exception as exc:
                        logger.warning(f"Live clip failed: {exc}")

    # -- clipping
    def cut(self, start: float, end: float, title: str = "Live moment", category: str = "",
            reason: str = "") -> dict:
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
                               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
                               str(raw)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode:
            raise RuntimeError(f"ffmpeg concat failed: {proc.stderr[-300:]}")
        try:
            from services.smart_crop import smart_crop_video
            smart_crop_video(str(raw), str(vertical), mode="auto")
        except Exception as exc:
            logger.warning(f"Live clip kept horizontal ({exc})")
            shutil.copy2(raw, vertical)
        listing.unlink(missing_ok=True)
        meta = {"id": clip_id, "title": title, "category": category, "reason": reason,
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
