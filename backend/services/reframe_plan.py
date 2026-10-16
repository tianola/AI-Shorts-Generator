"""What a clip should be framed on, decided from the clip itself.

The old adaptive rule asked one question per sampled frame — "are there two
people 200 px apart?" — and answered split-screen for any video with a
passer-by, a camera operator, or a three-person shot. A stacked 9:16 two-shot
is a *choice a human editor makes for a conversation*, so this module asks the
questions an editor implicitly asks:

  * do exactly two people stay in shot, at comparable size, most of the clip?
  * is anyone actually speaking, and is one of them the one doing it?
  * who does the diarization say is talking — a balanced conversation, or one
    voice carrying the clip? (when speaker turns are available)
  * or is the real subject a thing on screen (a screen, a ball, a product)?
  * and while the narration is describing that thing, should the crop move onto
    it for those seconds? (when a scene read is available)

Only the first two together justify stacking two people; everything else
follows one subject, and the crop follows the one that is talking rather than
whichever box happened to be biggest.
"""
import os
import subprocess
import threading
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from utils.logger import logger

# Two people only make a two-shot if both stay, at a comparable size.
# 0.45 is measured, not guessed: on job 90's clips the longest track topped out
# at 0.44 (crowds and hard cuts fragment association), while a genuine
# side-by-side talking pair reached 0.56. The crowd cases are rejected by
# co-visibility anyway — this only has to keep a handheld two-shot in.
MIN_TRACK_COVERAGE = 0.45        # present in most of the sampled frames
MIN_TRACK_HEIGHT_FRAC = 0.22     # head-to-toe box at least this tall, of frame height
SIZE_PARITY_MAX_RATIO = 1.9      # 3 m foreground guy vs 20 px background guy is not a two-shot
MIN_COVISIBILITY = 0.55          # both present together in this fraction of samples
MIN_SEPARATION_FRAC = 0.12       # horizontal centres this far apart (of source width)
MIN_SEPARATION_BOX_RATIO = 1.2   # ...and clearly further apart than they are wide
MIN_SEPARATED_SAMPLES = 0.60     # in this fraction of the co-visible samples
SPEECH_RATIO_MIN = 0.10          # a conversation is mostly talking
VOICE_FLOOR_RMS = 0.02           # ≈ -34 dBFS: below this nobody is audible
MOTION_DOMINANCE_RATIO = 1.7     # head motion of the talker over the listener
MIN_HEAD_MOTION = 0.004          # below this both are just JPEG/cut noise
MIN_SPEAKER_SHARE = 0.30         # both voices of a conversation carry this much talk time
MIN_BINDING_PRESENCE = 0.50      # a voice's own picture is on screen most of its turns
BINDING_DOMINANCE = 1.3          # ...and clearly ahead of the runner-up track
ATTENTION_MIN_IMPORTANCE = 0.6   # the narration must be clearly about the thing
ATTENTION_MIN_VISIBILITY = 0.34  # ...and the camera must show it this often in that window
ATTENTION_MIN_IMPORTANCE = 0.6   # narration must be clearly about the thing to move the crop
ATTENTION_MIN_VISIBILITY = 0.34  # ...and the camera must show it this often in that window

_CACHE: "Dict[str, Dict[str, Any]]" = {}
_CACHE_ORDER: List[str] = []
_CACHE_MAX = 24
_LOCK = threading.Lock()


def reset_cache() -> None:
    """Drop memoised analyses (tests, and a clip re-rendered in place)."""
    with _LOCK:
        _CACHE.clear()
        _CACHE_ORDER.clear()


def _iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix0 = max(ax, bx)
    iy0 = max(ay, by)
    ix1 = min(ax + aw, bx + bw)
    iy1 = min(ay + ah, by + bh)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def associate_person_tracks(samples: List[Tuple[float, list]], iou_threshold: float = 0.30) -> List[Dict[str, Any]]:
    """Greedy nearest-box tracking of person detections across sampled frames.

    Returns [{'id', 'timeline': [(ts, box)], ...}] with boxes as (x, y, w, h).
    Sampling at 0.5 s means a walking subject jumps more than an IoU of 0.3
    between two samples, so a track breaks at a hard cut and at a long jump —
    which is the behaviour we want: a passer-by is not a subject.
    """
    tracks: List[Dict[str, Any]] = []
    for ts, dets in samples:
        persons = [d for d in dets if d.get("cls") == 0]
        boxes = [d["box"] for d in persons]
        used = set()
        for tr in tracks:
            last_box = tr["timeline"][-1][1]
            last_ts = tr["timeline"][-1][0]
            # A track only continues within one sampling gap after its last hit.
            if ts - last_ts > 1.6 or not boxes:
                continue
            best_i, best_io = -1, 0.0
            for i, box in enumerate(boxes):
                if i in used:
                    continue
                io = _iou(last_box, box)
                if io > best_io:
                    best_i, best_io = i, io
            if best_i >= 0 and best_io >= iou_threshold:
                used.add(best_i)
                tr["timeline"].append((ts, boxes[best_i]))
        for i, box in enumerate(boxes):
            if i not in used:
                tracks.append({"id": len(tracks), "timeline": [(ts, box)]})
    return tracks


def track_metrics(track: Dict[str, Any], n_samples: int, src_w: int, src_h: int) -> Dict[str, float]:
    """Coverage, mean box height (fraction of frame) and mean centre x."""
    timeline = track["timeline"]
    heights = [b[3] for _ts, b in timeline]
    centres = [(b[0] + b[2] / 2.0) for _ts, b in timeline]
    return {
        "coverage": (len(timeline) / n_samples) if n_samples else 0.0,
        "height_frac": (float(np.mean(heights)) / src_h) if heights and src_h else 0.0,
        "mean_cx": float(np.mean(centres)) if centres else src_w / 2.0,
        "area": float(np.mean([b[2] * b[3] for _ts, b in timeline])) if timeline else 0.0,
    }


def find_two_shot(tracks: List[Dict[str, Any]], samples: List[Tuple[float, list]], src_w: int, src_h: int) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Return the two boxes that justify a stacked layout, or None.

    'Dominant' means persistent and full-height enough to be a subject. Exactly
    two of them qualify — three people in frame is a group shot, and the old
    code read it as a podcast because it looked at the first two it found.
    """
    n_samples = len(samples)
    if n_samples == 0:
        return None
    dominant = []
    for tr in tracks:
        m = track_metrics(tr, n_samples, src_w, src_h)
        if m["coverage"] >= MIN_TRACK_COVERAGE and m["height_frac"] >= MIN_TRACK_HEIGHT_FRAC:
            dominant.append((tr, m))
    if len(dominant) != 2:
        return None
    (t1, m1), (t2, m2) = dominant
    heights = sorted([m1["height_frac"], m2["height_frac"]])
    if heights[0] <= 0 or heights[1] / heights[0] > SIZE_PARITY_MAX_RATIO:
        return None

    ts1 = dict(t1["timeline"])
    ts2 = dict(t2["timeline"])
    common = sorted(set(ts1) & set(ts2))
    if len(common) / n_samples < MIN_COVISIBILITY:
        return None
    separated = 0
    for ts in common:
        a, b = ts1[ts], ts2[ts]
        gap = abs((a[0] + a[2] / 2.0) - (b[0] + b[2] / 2.0))
        # Two people whose boxes nearly touch are one group shot; a stacked
        # layout is for the wide interview where each has their own half.
        needed = max(MIN_SEPARATION_FRAC * src_w, MIN_SEPARATION_BOX_RATIO * max(a[2], b[2]))
        if gap >= needed:
            separated += 1
    if separated / len(common) < MIN_SEPARATED_SAMPLES:
        return None
    # Left goes on top: the stacked layout reads as a conversation in the order
    # the camera met them, not in the order YOLO happened to emit.
    left, right = (t1, t2) if m1["mean_cx"] <= m2["mean_cx"] else (t2, t1)
    return left, right


def _no_window() -> int:
    return 0x08000000 if os.name == "nt" else 0


def speech_ratio(video_path: str) -> Optional[float]:
    """Fraction of the clip's duration that carries an audible voice.

    Mono 16 kHz PCM decoded by ffmpeg, 30 ms RMS windows, thresholded against
    the clip's own noise floor. This is a speech-versus-silence probe, not a
    classifier: a loud continuous music bed reads as speech too, and that is
    deliberate — deciding 'is anyone talking' is all the layout gate needs, and
    *who* is talking comes from head motion, not from the waveform.

    Returns None when the clip has no audio track or ffmpeg cannot read it, so
    the caller treats 'unknown' as 'not proven' rather than 'silent'.
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", video_path, "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
            capture_output=True,
            creationflags=_no_window(),
        )
    except Exception as exc:
        logger.warning(f"speech probe failed to run ffmpeg: {exc}")
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    window = 480  # 30 ms at 16 kHz
    if audio.size < window * 4:
        return None
    trimmed = audio[: (audio.size // window) * window].reshape(-1, window)
    rms = np.sqrt(np.mean(trimmed ** 2, axis=1))
    floor = float(np.percentile(rms, 20))
    ceiling = float(np.max(rms))
    if ceiling <= 0:
        return 0.0
    # Cap the relative threshold by the signal's own level, or a clip that is
    # loud all the way through — one constant tone, or music over B-roll —
    # would be called silent for having no quiet quarter to measure against.
    threshold = max(VOICE_FLOOR_RMS, min(floor * 2.5, ceiling * 0.35))
    return float(np.mean(rms >= threshold))


def _head_band(frame, box):
    """Normalised grayscale crop of the head region of a person box."""
    import cv2

    x, y, w, h = box
    hy0 = max(0, int(y))
    hy1 = min(frame.shape[0], int(y + 0.34 * h))
    hx0 = max(0, int(x + 0.15 * w))
    hx1 = min(frame.shape[1], int(x + 0.85 * w))
    if hy1 - hy0 < 12 or hx1 - hx0 < 12:
        return None
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)[hy0:hy1, hx0:hx1]
    small = cv2.resize(gray, (32, 24), interpolation=cv2.INTER_AREA)
    return small.astype(np.float32) / 255.0


def head_motion_scores(video_path: str, tracks: List[Dict[str, Any]]) -> Dict[int, float]:
    """Median frame-to-frame head change per track — who is moving to talk.

    Sampling is the same 0.5 s grid the tracks were built on. The median, not
    the mean, because a hard cut spikes every track at once and a mean would
    read a fast-cut edit as two people arguing.
    """
    import cv2

    wanted: Dict[float, List[int]] = {}
    for tr in tracks:
        for ts, _box in tr["timeline"]:
            wanted.setdefault(round(ts, 4), []).append(tr["id"])
    if not wanted:
        return {}

    timelines = {tr["id"]: {round(ts, 4): box for ts, box in tr["timeline"]} for tr in tracks}
    caps: Dict[int, np.ndarray] = {}
    diffs: Dict[int, List[float]] = {tr["id"]: [] for tr in tracks}
    stamps = sorted(wanted)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {}
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        for ts in stamps:
            target_frame = int(round(ts * fps))
            if not cap.grab():
                break
            current = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
            if current < target_frame:
                cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
            ok, frame = cap.read()
            if not ok:
                break
            for track_id in wanted[ts]:
                box = timelines[track_id].get(ts)
                if box is None:
                    continue
                band = _head_band(frame, box)
                if band is None:
                    continue
                previous = caps.get(track_id)
                if previous is not None and previous.shape == band.shape:
                    diffs[track_id].append(float(np.mean(np.abs(band - previous))))
                caps[track_id] = band
    finally:
        cap.release()

    return {
        track_id: float(np.median(values)) if values else 0.0
        for track_id, values in diffs.items()
    }


def object_stats(samples: List[Tuple[float, list]], src_w: int, src_h: int) -> Dict[int, Dict[str, float]]:
    """Per non-person class: how often it appears and how big it gets."""
    total = max(1, len(samples))
    stats: Dict[int, Dict[str, float]] = {}
    for _ts, dets in samples:
        for d in dets:
            if d.get("cls") == 0:
                continue
            x, y, w, h = d["box"]
            entry = stats.setdefault(d["cls"], {"coverage": 0.0, "max_area": 0.0})
            entry["coverage"] += 1
            entry["max_area"] = max(entry["max_area"], float(w * h))
    for entry in stats.values():
        entry["coverage"] /= total
        entry["area_frac"] = entry["max_area"] / float(src_w * src_h)
    return stats


def _dominant_object_focus(samples: List[Tuple[float, list]], tracks: List[Dict[str, Any]], src_w: int, src_h: int) -> Optional[List[Tuple[float, tuple]]]:
    """Boxes of the thing the clip is actually about, when it is a thing.

    An operator's demo beats a presenter's face when the object is both
    persistent and clearly larger than anyone in frame — the 'someone talks
    while the attention is on this' case an editor cuts around.
    """
    n_samples = len(samples)
    person_area = 0.0
    for tr in tracks:
        m = track_metrics(tr, n_samples, src_w, src_h)
        person_area = max(person_area, m["area"])
    if person_area <= 0:
        return None
    for cls, entry in object_stats(samples, src_w, src_h).items():
        if entry["coverage"] < 0.35:
            continue
        if entry["max_area"] < 1.5 * person_area:
            continue
        boxes = []
        for ts, dets in samples:
            candidates = [d["box"] for d in dets if d.get("cls") == cls]
            if candidates:
                boxes.append((ts, max(candidates, key=lambda b: b[2] * b[3])))
        if boxes:
            return boxes
    return None


def representative_box(track: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """The box at the middle of a track's own timeline: its typical framing.

    The split-screen builder needs one box per speaker, and the first frame a
    track was ever seen in is usually the worst candidate — half in shot, or a
    different shot entirely.
    """
    timeline = track["timeline"]
    return timeline[len(timeline) // 2][1]


def analyse_clip(video_path: str, sample_interval: float = 0.5) -> Dict[str, Any]:
    """Everything the layout decision needs, sampled once and memoised."""
    key = video_path
    try:
        stat = os.stat(video_path)
        key = f"{video_path}|{stat.st_mtime_ns}|{stat.st_size}"
    except OSError:
        pass
    with _LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached

    from services.focus_tracking import sample_frames

    analysis: Dict[str, Any] = {
        "video_path": video_path,
        "src_w": 1920,
        "src_h": 1080,
        "n_samples": 0,
        "tracks": [],
        "two_shot": None,
        "speech": None,
        "motion": {},
        "focus": None,
    }
    try:
        samples = sample_frames(video_path, sample_interval=sample_interval)
    except Exception as exc:
        logger.warning(f"layout analysis could not sample {video_path}: {exc}")
        return analysis

    import cv2

    cap = cv2.VideoCapture(video_path)
    if cap.isOpened():
        analysis["src_w"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
        analysis["src_h"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
    cap.release()

    tracks = associate_person_tracks(samples)
    analysis["samples"] = samples
    analysis["n_samples"] = len(samples)
    analysis["tracks"] = tracks
    analysis["two_shot"] = find_two_shot(tracks, samples, analysis["src_w"], analysis["src_h"])
    analysis["speech"] = speech_ratio(video_path)
    if tracks:
        analysis["motion"] = head_motion_scores(video_path, tracks)
    analysis["focus"] = choose_focus(analysis)

    with _LOCK:
        _CACHE[key] = analysis
        _CACHE_ORDER.append(key)
        while len(_CACHE_ORDER) > _CACHE_MAX:
            _CACHE.pop(_CACHE_ORDER.pop(0), None)
    return analysis


def _talk_shares(diarization: List[Dict[str, Any]]) -> Dict[str, float]:
    """Fraction of on-mic talk time each voice holds."""
    totals: Dict[str, float] = {}
    for turn in diarization:
        speaker = turn.get("speaker")
        if speaker is None:
            continue
        totals[speaker] = totals.get(speaker, 0.0) + max(
            0.0, float(turn.get("end", 0.0)) - float(turn.get("start", 0.0))
        )
    grand = sum(totals.values())
    if grand <= 0:
        return {}
    return {speaker: value / grand for speaker, value in totals.items()}


def _balanced_conversation(diarization: List[Dict[str, Any]]) -> bool:
    """Exactly two voices that each carry a real share of the clip.

    Two talkers, not 'any two voices anywhere': a 30 s guest answer with a 2 s
    'mm-hm' is an interview, not a conversation, and stacking it would bury
    the person the clip is actually about.
    """
    shares = _talk_shares(diarization)
    return len([s for s, share in shares.items() if share >= MIN_SPEAKER_SHARE]) == 2


def _speaker_track_binding(diarization: List[Dict[str, Any]], tracks: List[Dict[str, Any]], samples: List[Tuple[float, list]]) -> Dict[str, int]:
    """Which on-screen track belongs to each voice, when that is decidable.

    Evidence, not identification: every sampled frame inside a voice's turns is
    a vote for each track visible in it. A voice binds to the track that owns
    most of its talk time — but only when that track clearly leads, because two
    always-visible people tie exactly and a coin-flip binding would crop the
    wrong face for the whole clip.
    """
    if not diarization or not tracks or not samples:
        return {}
    seen_at = {tr["id"]: {ts for ts, _box in tr["timeline"]} for tr in tracks}
    totals: Dict[str, int] = {}
    counts: Dict[str, Dict[int, int]] = {}
    for ts, _dets in samples:
        speaker: Optional[str] = None
        for turn in diarization:
            if float(turn.get("start", 0.0)) <= ts <= float(turn.get("end", 0.0)):
                speaker = turn.get("speaker")
                break
        if speaker is None:
            continue
        totals[speaker] = totals.get(speaker, 0) + 1
        for track_id, seen in seen_at.items():
            if ts in seen:
                bucket = counts.setdefault(speaker, {})
                bucket[track_id] = bucket.get(track_id, 0) + 1
    binding: Dict[str, int] = {}
    for speaker, bucket in counts.items():
        ranked = sorted(bucket.items(), key=lambda item: item[1], reverse=True)
        best_id, best = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0
        total = max(1, totals.get(speaker, 0))
        if best / total >= MIN_BINDING_PRESENCE and best >= BINDING_DOMINANCE * runner_up:
            binding[speaker] = best_id
    return binding


def _speaker_switch_boxes(diarization: List[Dict[str, Any]], bound: Dict[str, Dict[str, Any]]) -> List[Tuple[float, tuple]]:
    """One timeline that cuts to whoever is speaking, from the bound tracks.

    A turn whose voice has no bound track contributes nothing — better to hold
    the last proven face than to guess at a face we never tied to a voice.
    """
    boxes: List[Tuple[float, tuple]] = []
    for turn in diarization:
        track = bound.get(turn.get("speaker"))
        if track is None:
            continue
        start = float(turn.get("start", 0.0))
        end = float(turn.get("end", 0.0))
        for ts, box in track["timeline"]:
            if start <= ts <= end:
                boxes.append((ts, box))
    boxes.sort(key=lambda item: item[0])
    deduped: List[Tuple[float, tuple]] = []
    for ts, box in boxes:
        if deduped and abs(deduped[-1][0] - ts) < 1e-6:
            deduped[-1] = (ts, box)
        else:
            deduped.append((ts, box))
    return deduped


def _voice_focus(analysis: Dict[str, Any], diarization: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The subject the voices choose, when they can prove one."""
    tracks = analysis.get("tracks") or []
    samples = analysis.get("samples") or []
    if not diarization or not tracks or not samples:
        return None
    binding = _speaker_track_binding(diarization, tracks, samples)
    if not binding:
        return None
    by_id = {tr["id"]: tr for tr in tracks}
    bound = {speaker: by_id[track_id] for speaker, track_id in binding.items() if track_id in by_id}
    if not bound:
        return None
    shares = _talk_shares(diarization)
    talkers = [speaker for speaker, share in shares.items() if share >= MIN_SPEAKER_SHARE]
    if len(talkers) == 2 and all(speaker in bound for speaker in talkers) \
            and len({bound[speaker]["id"] for speaker in talkers}) == 2:
        boxes = _speaker_switch_boxes(diarization, bound)
        if boxes:
            return {
                "kind": "person",
                "boxes": boxes,
                "reason": "speaker turns bound 2 voices to their tracks",
            }
    if shares:
        top = max(shares, key=shares.get)
        if top in bound:
            track = bound[top]
            return {
                "kind": "person",
                "track_id": track["id"],
                "boxes": [(ts, box) for ts, box in track["timeline"]],
                "reason": "dominant speaker by voice",
            }
    return None


def _base_focus(analysis: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The motion/object read of the clip, without the voices."""
    samples = analysis.get("samples") or []
    tracks = analysis.get("tracks") or []
    src_w = analysis.get("src_w", 1920)
    src_h = analysis.get("src_h", 1080)
    n_samples = max(1, analysis.get("n_samples", 1))
    if not samples:
        return None

    motion = analysis.get("motion") or {}
    speech = analysis.get("speech")
    if tracks and speech is not None and speech >= SPEECH_RATIO_MIN:
        ranked = sorted(
            ((motion.get(tr["id"], 0.0), tr) for tr in tracks
             if track_metrics(tr, n_samples, src_w, src_h)["coverage"] >= 0.30),
            key=lambda item: item[0],
            reverse=True,
        )
        if ranked:
            top_score, top_track = ranked[0]
            second = ranked[1][0] if len(ranked) > 1 else 0.0
            if top_score >= MIN_HEAD_MOTION and (second <= 0 or top_score >= MOTION_DOMINANCE_RATIO * second):
                return {
                    "kind": "person",
                    "track_id": top_track["id"],
                    "boxes": [(ts, box) for ts, box in top_track["timeline"]],
                    "reason": "dominant speaker by head motion",
                }

    object_boxes = _dominant_object_focus(samples, tracks, src_w, src_h)
    if object_boxes:
        return {"kind": "object", "boxes": object_boxes, "reason": "a persistent object outweighs every person"}

    largest = None
    largest_area = 0.0
    for tr in tracks:
        m = track_metrics(tr, n_samples, src_w, src_h)
        if m["area"] > largest_area:
            largest_area, largest = m["area"], tr
    if largest is not None:
        return {"kind": "person", "track_id": largest["id"], "boxes": largest["timeline"], "reason": "largest person"}
    return None


def _attention_focus(attention: Optional[Dict[str, Any]], focus: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Put the thing the narration is describing where the crop is looking.

    The scenes arriving here were already chosen for meaning — a spoken word, a
    model's reading of the transcript — but their boxes are still the detector's,
    and they replace the box only at the timestamps they cover. Everywhere else
    the crop keeps following the person it was following, which is why an object
    scene can live inside a 'person' focus. A scene that was not gated for
    importance, or whose target the camera barely showed, is skipped; attention
    with nothing to say leaves the focus exactly as it was.
    """
    scenes = (attention or {}).get("scenes") or []
    chosen = []
    for scene in scenes:
        try:
            importance = float(scene.get("importance", 0.0))
            visibility = float(scene.get("visibility", 1.0))
        except (TypeError, ValueError):
            continue
        boxes = scene.get("boxes") or []
        if not boxes or importance < ATTENTION_MIN_IMPORTANCE or visibility < ATTENTION_MIN_VISIBILITY:
            continue
        chosen.append(scene)
    if not chosen:
        return focus

    merged: Dict[float, Any] = {}
    for ts, box in (focus or {}).get("boxes") or []:
        merged[round(float(ts), 4)] = box
    for scene in chosen:
        for ts, box in scene.get("boxes") or []:
            merged[round(float(ts), 4)] = box

    first = min(chosen, key=lambda scene: float(scene.get("start", 0.0)))
    label = (f"attention on '{first.get('target') or 'object'}' "
             f"{float(first.get('start', 0.0)):.1f}-{float(first.get('end', 0.0)):.1f}s")
    merged_focus: Dict[str, Any] = {
        "kind": (focus or {}).get("kind") or "object",
        "boxes": sorted(merged.items()),
        "reason": f"{focus['reason']}; {label}" if focus and focus.get("reason") else label,
    }
    if focus and focus.get("track_id") is not None:
        merged_focus["track_id"] = focus["track_id"]
    return merged_focus


def choose_focus(analysis: Dict[str, Any], diarization: Optional[List[Dict[str, Any]]] = None,
                 attention: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The single subject a follow-crop should hold onto.

    With ``diarization`` (speaker turns from :mod:`services.diarization`) the
    voices pick the subject whenever they can be bound to on-screen tracks; a
    clip whose real subject is a thing on screen keeps that read. With
    ``attention`` (the clip's scenes from :mod:`services.scene_attention`) the
    crop is moved onto the object being described while the description lasts.
    Without either, the decision is exactly what it was before.
    """
    base = _base_focus(analysis)
    focus = base
    if diarization and not (base and base.get("kind") == "object"):
        voice = _voice_focus(analysis, diarization)
        if voice is not None:
            focus = voice
    if attention:
        focus = _attention_focus(attention, focus)
    return focus


def decide_layout(analysis: Dict[str, Any], diarization: Optional[List[Dict[str, Any]]] = None,
                  attention: Optional[Dict[str, Any]] = None) -> Tuple[str, Optional[Dict[str, Any]]]:
    """(mode, focus) for one clip: a two-shot only when two people carry it.

    ``diarization`` is the clip's speaker turns; when present they outrank the
    head-motion read inside a two-shot — a balanced pair of voices stacks the
    frame, a single voice keeps it on that person. ``attention`` is the clip's
    scene read, and moves the follow-crop onto the object being described while
    the description lasts.
    """
    two_shot = analysis.get("two_shot")
    speech = analysis.get("speech")
    motion = analysis.get("motion") or {}
    turns = diarization or []

    if two_shot and speech is not None and speech >= SPEECH_RATIO_MIN:
        if turns:
            if _balanced_conversation(turns):
                return "split_screen", None
            # One voice carries this clip: follow the talker, do not stack the listener.
            return "single_speaker", choose_focus(analysis, diarization=diarization, attention=attention)
        left, right = two_shot
        left_motion = motion.get(left["id"], 0.0)
        right_motion = motion.get(right["id"], 0.0)
        quieter, louder = sorted([left_motion, right_motion])
        both_carry_it = louder <= MOTION_DOMINANCE_RATIO * max(quieter, MIN_HEAD_MOTION)
        if both_carry_it:
            return "split_screen", None
        return "single_speaker", choose_focus(analysis, attention=attention)

    focus = choose_focus(analysis, diarization=diarization, attention=attention)
    if focus is None:
        return "fit_blur", None
    if focus.get("kind") == "object":
        return "fit_blur", focus
    return "single_speaker", focus


def resolve(video_path: str, sample_interval: float = 0.5,
            diarization: Optional[List[Dict[str, Any]]] = None,
            attention: Optional[Dict[str, Any]] = None) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Mode plus the subject to follow — the entry point the render stages use."""
    return decide_layout(analyse_clip(video_path, sample_interval=sample_interval),
                         diarization=diarization, attention=attention)


def describe(video_path: str, sample_interval: float = 0.5) -> str:
    """One-line human account of why a clip got the layout it got."""
    analysis = analyse_clip(video_path, sample_interval=sample_interval)
    mode, focus = decide_layout(analysis)
    two_shot = analysis.get("two_shot")
    return (
        f"mode={mode} speech={analysis.get('speech') if analysis.get('speech') is None else round(analysis['speech'], 2)} "
        f"tracks={len(analysis.get('tracks') or [])} two_shot={'yes' if two_shot else 'no'} "
        f"focus={focus.get('reason') if focus else 'none'}"
    )

