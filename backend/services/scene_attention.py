"""What each scene of a clip is about, so the crop can centre the right thing.

:mod:`services.reframe_plan` decides *whom* to follow; this answers the other
half of the question an editor asks — *what* are they talking about right now.
Three tiers, each one soft-failing into the one below it:

  1. keyword — a spoken word names an object YOLO saw on screen (always local);
  2. text LLM — one call per clip reads the transcript against the visible
     inventory and names the subject of each stretch of speech;
  3. vision — off unless ``SCENE_ATTENTION_VLM`` is on and a model is set: a
     few frames ask which inventory object the frame is really about.

Geometry never comes from a language model. Every scene this module returns
carries boxes lifted from the YOLO samples the person tracks already use, keyed
to the same timestamps, so the crop below it does not change.
"""
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from utils.logger import logger

from config import settings

# A mention rarely starts on the exact word, and the camera usually holds on the
# thing for a beat after it is named.
PAD_BEFORE = 1.0
PAD_AFTER = 2.5

# A keyword plus a fully visible object is a solid claim but not proof; a
# mention of something the camera shows twice is nothing.
KEYWORD_BASE_IMPORTANCE = 0.5
KEYWORD_VISIBILITY_SPAN = 0.5
KEYWORD_MAX_IMPORTANCE = 0.9
VLM_IMPORTANCE = 0.75

# COCO names are terse; people say other things. Only synonyms that unambiguously
# mean the class belong here — "device" or "gadget" would be guesswork.
SYNONYMS = {
    "television": "tv",
    "screen": "tv",
    "monitor": "tv",
    "computer": "laptop",
    "notebook": "laptop",
    "macbook": "laptop",
    "pc": "laptop",
    "phone": "cell phone",
    "cellphone": "cell phone",
    "smartphone": "cell phone",
    "iphone": "cell phone",
    "android": "cell phone",
    "ball": "sports ball",
    "mug": "cup",
    "espresso": "cup",
    "coffee": "cup",
    "soda": "bottle",
    "beer": "bottle",
}

OBJECT_TOKENS: Dict[str, str] = {}
for _name in list(SYNONYMS.values()):
    OBJECT_TOKENS[_name] = _name
for _synonym, _name in SYNONYMS.items():
    OBJECT_TOKENS.setdefault(_synonym, _name)


def _class_names(samples: List[Tuple[float, list]]) -> List[str]:
    """Non-person object names the detector actually saw in this clip."""
    seen: List[str] = []
    for _ts, dets in samples:
        for det in dets:
            if det.get("cls") == 0:
                continue
            name = str(det.get("name") or "").lower().strip()
            if name and name not in seen:
                seen.append(name)
    return sorted(seen)


def _read_words(clip_words_path: str) -> List[Dict[str, Any]]:
    try:
        with open(clip_words_path, "r", encoding="utf-8") as f:
            words = json.load(f)
    except Exception:
        return []
    return words if isinstance(words, list) else []


def _clean_token(word: Any) -> str:
    return re.sub(r"[^a-z0-9']", "", str(word or "").lower().strip())


def _target_for_word(word: Any, inventory: List[str]) -> Optional[str]:
    """The inventory object this word names, or None.

    The class name's own tokens count ("phone" reaches 'cell phone'), and so do
    the synonym map — but only ever to something the detector saw.
    """
    token = _clean_token(word)
    if not token:
        return None
    mapped = OBJECT_TOKENS.get(token)
    if mapped and mapped in inventory:
        return mapped
    if token in inventory:
        return token
    for name in inventory:
        if token in name.split():
            return name
    return None


def _cuts(analysis: Dict[str, Any]) -> List[float]:
    """Timestamps where a new subject appears, taken from the person tracks.

    Only structural evidence counts. The obvious extra signal — a sampled frame
    with nothing in it — is a trap: on a real clip the detector misses its target
    on most samples (one job's talking-head footage had 70 of 80 frames empty),
    and reading those as cuts trimmed every attention window down to a single
    sample, which is how this tier died on the first smoke run.
    """
    samples = analysis.get("samples") or []
    if len(samples) < 2:
        return []
    first = float(samples[0][0])
    cuts = set()
    for track in analysis.get("tracks") or []:
        timeline = track.get("timeline") or []
        if timeline and float(timeline[0][0]) > first:
            cuts.add(round(float(timeline[0][0]), 3))
    return sorted(cuts)


def _clamp_to_shot(start: float, end: float, cuts: List[float]) -> Tuple[float, float]:
    """Trim a window so it never crosses the cut that ends its shot.

    The margin is a hundredth of a second: far below the half-second sampling
    interval, so the frame on the other side of the cut is excluded, and far
    above float noise, so it is not accidentally re-included by one.
    """
    before = [c for c in cuts if c <= start]
    if before:
        start = max(start, before[-1] + 1e-3)
    after = [c for c in cuts if c > start]
    if after:
        end = min(end, after[0] - 1e-3)
    return start, end


def _boxes_for(samples: List[Tuple[float, list]], target: str, start: float, end: float) -> Tuple[List[Tuple[float, tuple]], float]:
    """The largest box of ``target`` at each sampled instant inside the window."""
    boxes: List[Tuple[float, tuple]] = []
    expected = 0
    for ts, dets in samples:
        ts = float(ts)
        if ts < start - 1e-6 or ts > end + 1e-6:
            continue
        expected += 1
        candidates = [det["box"] for det in dets
                      if str(det.get("name") or "").lower().strip() == target]
        if candidates:
            boxes.append((ts, max(candidates, key=lambda box: box[2] * box[3])))
    visibility = (len(boxes) / expected) if expected else 0.0
    return boxes, visibility


def _keyword_claims(analysis: Dict[str, Any], words: List[Dict[str, Any]], inventory: List[str], cuts: List[float]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Spoken word + visible object, merged across one breath."""
    samples = analysis.get("samples") or []
    if not samples or not words:
        return [], []
    last_ts = float(samples[-1][0])
    claims: List[Dict[str, Any]] = []
    for word in words:
        target = _target_for_word(word.get("word"), inventory)
        if target is None:
            continue
        try:
            start = max(0.0, float(word["start"]) - PAD_BEFORE)
            end = min(last_ts, float(word["end"]) + PAD_AFTER)
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        start, end = _clamp_to_shot(start, end, cuts)
        if end <= start:
            continue
        match = next((c for c in claims
                      if c["target"] == target and c["start"] <= end and start <= c["end"]), None)
        if match:
            match["start"] = min(match["start"], start)
            match["end"] = max(match["end"], end)
        else:
            # 0.0 is a placeholder: a keyword's real claim strength is how much
            # of its own window the camera actually showed, computed once the
            # final window is known.
            claims.append({"target": target, "start": start, "end": end,
                           "importance": 0.0, "source": "keyword"})
    return claims, []


def _segments_from_words(words: List[Dict[str, Any]], max_span: float = 4.0, pause: float = 0.8,
                         max_segments: int = 24) -> List[Dict[str, Any]]:
    """The transcript in stretchable bites the model can reason about."""
    segments: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for entry in words:
        try:
            start = float(entry["start"])
            end = float(entry["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(entry.get("word") or "").strip()
        if not text:
            continue
        if current is None:
            current = {"start": start, "end": end, "text": text}
        elif start - current["end"] > pause or current["end"] - current["start"] >= max_span:
            segments.append(current)
            current = {"start": start, "end": end, "text": text}
        else:
            current["end"] = max(current["end"], end)
            current["text"] = f"{current['text']} {text}"
    if current:
        segments.append(current)
    return segments[:max_segments]


LLM_SYSTEM = (
    "You decide what a short video should be cropped on. Answer in JSON only, "
    "with no commentary."
)


def _llm_prompt(segments: List[Dict[str, Any]], inventory: List[str], transcript: str) -> str:
    rows = [{"i": index, "start": round(segment["start"], 2), "end": round(segment["end"], 2),
             "text": segment["text"]} for index, segment in enumerate(segments)]
    return (
        f"Objects visible in this clip: {json.dumps(inventory)}\n"
        f"Transcript segments (clip-relative seconds): {json.dumps(rows, ensure_ascii=False)}\n"
        f"Full transcript: {transcript[:1200]}\n\n"
        "For every segment whose speech is clearly ABOUT one of the visible objects — the "
        "narrator describing it, demonstrating it, or pointing at it — return an entry:\n"
        '{"scenes": [{"i": <segment index>, "target": "<object exactly as listed>", '
        '"importance": <0.0 to 1.0>}]}\n'
        "importance is how sure you are that this object is what the viewer should be looking "
        "at during that segment. Return an empty list when nothing in the speech is about a "
        "visible object. Never name an object that is not in the list."
    )


def _match_target(text: Any, inventory: List[str]) -> Optional[str]:
    """Pin a model's answer to a class the detector actually saw."""
    clean = re.sub(r"^\s*(?:a|an|the)\s+", "", str(text or "").lower()).strip(" .!?,;:")
    if not clean:
        return None
    if clean in inventory:
        return clean
    tokens = set(re.findall(r"[a-z0-9]+", clean))
    for name in sorted(inventory):
        if name in tokens or tokens & set(name.split()):
            return name
    return None


async def _llm_claims(segments: List[Dict[str, Any]], inventory: List[str],
                      transcript: str, cuts: List[float], llm_call: Optional[Callable]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """One text-model pass over the clip's speech."""
    if not segments or not inventory:
        return [], ["llm tier skipped: nothing to read or no object on screen"]
    call = llm_call
    if call is None:
        from services.llm_router import call_llm_json
        call = call_llm_json
    try:
        raw = call(prompt=_llm_prompt(segments, inventory, transcript), system=LLM_SYSTEM,
                   temperature=0.2, max_tokens=800)
        if hasattr(raw, "__await__"):
            raw = await raw
    except Exception as exc:
        logger.warning(f"scene attention: llm tier failed ({type(exc).__name__}: {exc})")
        return [], [f"llm tier skipped: {type(exc).__name__}"]
    try:
        payload = json.loads(raw)
    except Exception:
        logger.warning("scene attention: llm tier returned unreadable JSON")
        return [], ["llm tier skipped: unreadable JSON"]

    claims: List[Dict[str, Any]] = []
    dropped = 0
    entries = payload.get("scenes") if isinstance(payload, dict) else None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        target = _match_target(entry.get("target"), inventory)
        try:
            index = int(entry.get("i"))
            importance = min(1.0, max(0.0, float(entry.get("importance", 0.0))))
        except (TypeError, ValueError):
            continue
        if target is None or index < 0 or index >= len(segments):
            dropped += 1
            continue
        segment = segments[index]
        start, end = _clamp_to_shot(float(segment["start"]), float(segment["end"]), cuts)
        if end <= start:
            dropped += 1
            continue
        claims.append({"target": target, "start": start, "end": end,
                       "importance": importance, "source": "llm"})
    notes = [f"llm tier: {len(claims)} scene(s)"]
    if dropped:
        notes.append(f"llm tier: {dropped} claim(s) dropped (unknown object or segment)")
    return claims, notes


def _grab_frame(video_path: str, ts: float):
    """One decoded frame, or None — the vision tier probes, it does not analyse."""
    import cv2

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(round(float(ts) * fps))))
        ok, frame = cap.read()
        return frame if ok else None
    finally:
        cap.release()


async def _default_vision_call(frame, prompt: str) -> str:
    """Ask a gateway vision model about one frame (OpenAI-compatible content part)."""
    import base64

    import cv2
    import httpx

    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if not ok:
        raise RuntimeError("frame could not be jpeg-encoded")
    api_key = settings.OMNIROUTE_API_KEY or ""
    if not api_key:
        raise RuntimeError("OMNIROUTE_API_KEY is not configured")
    payload = {
        "model": settings.SCENE_ATTENTION_VLM_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode("ascii")}},
            ],
        }],
        "temperature": 0.1,
        "max_tokens": 40,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{settings.OMNIROUTE_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


def _vision_enabled() -> bool:
    return bool(settings.SCENE_ATTENTION_VLM) and bool((settings.SCENE_ATTENTION_VLM_MODEL or "").strip())


async def _vision_claims(analysis: Dict[str, Any], inventory: List[str], cuts: List[float],
                         video_path: Optional[str], vision_call: Optional[Callable],
                         frame_source: Optional[Callable]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Ask a few frames what they are about, when the tier is switched on."""
    if not _vision_enabled():
        return [], []
    samples = analysis.get("samples") or []
    path = video_path or analysis.get("video_path")
    if not samples or not inventory or not path:
        return [], ["vision tier skipped: no frames or no objects to choose from"]

    probe_count = max(1, min(4, len(samples)))
    indices = [int(round(k * (len(samples) - 1) / max(1, probe_count - 1))) for k in range(probe_count)] if probe_count > 1 else [0]
    span = max(1.0, (float(samples[-1][0]) - float(samples[0][0])) / max(1, probe_count))
    grab = frame_source or _grab_frame
    call = vision_call or _default_vision_call
    prompt = (f"Which of these objects is the main subject of this frame? Answer with exactly one of: "
              f"{', '.join(inventory)}. Answer 'none' if it is not one of them.")

    claims: List[Dict[str, Any]] = []
    failures = 0
    for index in dict.fromkeys(indices):
        ts = float(samples[index][0])
        try:
            frame = grab(path, ts)
            if frame is None:
                continue
            answer = call(frame, prompt)
            if hasattr(answer, "__await__"):
                answer = await answer
        except Exception as exc:
            failures += 1
            logger.warning(f"scene attention: vision probe failed ({type(exc).__name__}: {exc})")
            continue
        target = _match_target(answer, inventory)
        if target is None:
            continue
        start, end = _clamp_to_shot(ts, min(float(samples[-1][0]), ts + span), cuts)
        if end <= start:
            continue
        claims.append({"target": target, "start": start, "end": end,
                       "importance": VLM_IMPORTANCE, "source": "vlm"})
    notes = [f"vision tier: {len(claims)} scene(s)"]
    if failures:
        notes.append(f"vision tier: {failures} probe(s) failed")
    return claims, notes


def _materialise(analysis: Dict[str, Any], claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fold overlapping claims and drop the ones with no pixels behind them."""
    samples = analysis.get("samples") or []
    merged: List[Dict[str, Any]] = []
    for claim in sorted(claims, key=lambda c: (c["start"], c["end"])):
        match = next((scene for scene in merged
                      if scene["target"] == claim["target"]
                      and scene["start"] <= claim["end"] and claim["start"] <= scene["end"]), None)
        if match:
            match["start"] = min(match["start"], claim["start"])
            match["end"] = max(match["end"], claim["end"])
            match["importance"] = max(match["importance"], claim["importance"])
            if claim["source"] not in match["source"].split("+"):
                # Sorted so the same pair of tiers always prints the same label.
                match["source"] = "+".join(sorted({*match["source"].split("+"), claim["source"]}))
        else:
            merged.append({"target": claim["target"], "start": claim["start"], "end": claim["end"],
                           "importance": claim.get("importance", 0.0), "source": claim["source"]})

    scenes: List[Dict[str, Any]] = []
    for scene in merged:
        boxes, visibility = _boxes_for(samples, scene["target"], scene["start"], scene["end"])
        if not boxes:
            continue
        if scene["source"] == "keyword":
            scene["importance"] = min(KEYWORD_MAX_IMPORTANCE,
                                      KEYWORD_BASE_IMPORTANCE + KEYWORD_VISIBILITY_SPAN * visibility)
        scene["visibility"] = round(visibility, 3)
        scene["boxes"] = boxes
        scenes.append(scene)
    return sorted(scenes, key=lambda scene: scene["start"])


async def build_attention(analysis: Dict[str, Any], clip_words_path: str, transcript: str = "",
                          video_path: Optional[str] = None, llm_call: Optional[Callable] = None,
                          vision_call: Optional[Callable] = None,
                          frame_source: Optional[Callable] = None) -> Dict[str, Any]:
    """Scenes of one clip: what the speech is about, and where it is on screen.

    Times are clip-relative, to match the sampled analysis the crop reads. The
    keyword tier always runs; the text tier is skipped when there is nothing to
    read or nothing object-like on screen (``llm_call=False`` skips it outright)
    ; the vision tier is off unless both ``SCENE_ATTENTION_VLM`` and
    ``SCENE_ATTENTION_VLM_MODEL`` are set. A failure in any remote tier costs one
    note, never the clip.
    """
    samples = analysis.get("samples") or []
    inventory = _class_names(samples)
    notes: List[str] = []
    claims: List[Dict[str, Any]] = []

    if not inventory:
        return {"scenes": [], "notes": ["no focus-worthy object detected in this clip"]}

    cuts = _cuts(analysis)
    words = _read_words(clip_words_path)
    keyword_claims, keyword_notes = _keyword_claims(analysis, words, inventory, cuts)
    claims += keyword_claims
    notes += keyword_notes or [f"keyword tier: {len(keyword_claims)} scene(s)"]

    if llm_call is not False:
        segments = _segments_from_words(words)
        llm_claims, llm_notes = await _llm_claims(segments, inventory, transcript, cuts, llm_call)
        claims += llm_claims
        notes += llm_notes

    vision_claims, vision_notes = await _vision_claims(analysis, inventory, cuts, video_path,
                                                       vision_call, frame_source)
    claims += vision_claims
    notes += vision_notes

    return {"scenes": _materialise(analysis, claims), "notes": notes}
