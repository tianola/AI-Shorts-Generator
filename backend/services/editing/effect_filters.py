"""ffmpeg filters for the visual effects in a timeline: zoom punch, camera shake, flash, red tint.

Everything is a pure function of time, so the same timeline always renders the same frames (no
random()). The fragment is authored against [0:v] and ends in [vfx]; the single-pass renderer
rewires it into the chain right after the vertical crop and before the captions, so the captions,
sticker emoji, popup and title stay still while the footage moves.

Camera movement is a time-varying scale laid over the original frame, which acts as a crop. The
picture is only enlarged while an effect is running (the scale is 1.0 at rest), and a shake always
enlarges it a little more than it moves, so the moving copy always covers the frame and no black
edge can show.
"""

from typing import Dict, Iterable, List, Optional

# Zoom punch: peak enlargement 5% (weak) to 14% (strong), a smooth rise and fall over the effect.
ZOOM_BASE = 0.05
ZOOM_RANGE = 0.09
# Shake: the extra enlargement that gives the crop room to move, and the share of it used as travel.
SHAKE_HEADROOM_BASE = 0.03
SHAKE_HEADROOM_RANGE = 0.04
SHAKE_TRAVEL_SHARE = 0.8
# Different frequencies on x and y make the shake look hand-held rather than a circle.
SHAKE_FREQ_X = 17.0
SHAKE_FREQ_Y = 23.0
# A brief lift, not a white-out: 0.32 doubled the frame's mean brightness on a real clip.
FLASH_BRIGHTNESS = 0.2
TINT_RED_GAIN = 0.16
TINT_OTHER_GAIN = 0.14


def _num(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def _bump(start: float, end: float) -> str:
    """0 before start, rises to 1 at the middle and returns to 0 at end (a half sine)."""
    length = max(end - start, 0.05)
    return f"sin(PI*clip((t-{_num(start)})/{_num(length)},0,1))"


def _camera_terms(tracks: Iterable[Dict], width: int, height: int):
    """(zoom_expr, dx_expr, dy_expr) for the zoom and shake tracks, or None when there are none."""
    zoom_terms: List[str] = []
    dx_terms: List[str] = []
    dy_terms: List[str] = []
    for track in tracks:
        kind = track["kind"]
        start, end = float(track["start"]), float(track["end"])
        strength = max(0.0, min(1.0, float(track.get("intensity", 0.5))))
        if end <= start:
            continue
        if kind == "zoom_punch":
            zoom_terms.append(f"{_num(ZOOM_BASE + ZOOM_RANGE * strength)}*{_bump(start, end)}")
        elif kind == "shake":
            headroom = SHAKE_HEADROOM_BASE + SHAKE_HEADROOM_RANGE * strength
            travel_x = headroom * width / 2.0 * SHAKE_TRAVEL_SHARE
            travel_y = headroom * height / 2.0 * SHAKE_TRAVEL_SHARE
            envelope = _bump(start, end)
            zoom_terms.append(f"{_num(headroom)}*{envelope}")
            dx_terms.append(f"{_num(travel_x)}*{envelope}*sin(2*PI*{_num(SHAKE_FREQ_X)}*t)")
            dy_terms.append(f"{_num(travel_y)}*{envelope}*cos(2*PI*{_num(SHAKE_FREQ_Y)}*t)")
    if not zoom_terms:
        return None
    return "+".join(zoom_terms), ("+".join(dx_terms) or "0"), ("+".join(dy_terms) or "0")


def _window(tracks: Iterable[Dict]) -> str:
    return "+".join(f"between(t,{_num(float(t['start']))},{_num(float(t['end']))})" for t in tracks)


def build_effects_fragment(tracks: Optional[Iterable[Dict]], width: int = 1080, height: int = 1920) -> Optional[str]:
    """A `[0:v]...[vfx]` filter fragment for the visual tracks, or None when there is nothing to draw."""
    tracks = [dict(t) for t in (tracks or [])]
    steps: List[str] = []
    head = "[0:v]"
    prefix = ""

    camera = _camera_terms([t for t in tracks if t["kind"] in ("zoom_punch", "shake")], width, height)
    if camera:
        zoom, dx, dy = camera
        # The enlarged copy is laid over the original, centred and moved by dx, dy. The overlay only
        # shows the original's area, which is exactly a crop, and it takes the changing size of the
        # scaled copy frame by frame (a crop filter reads its input size once, at start-up).
        # Even sizes only: odd widths break yuv420 chroma.
        prefix = (
            "[0:v]split=2[fxa][fxb];"
            f"[fxb]scale=w='trunc({width}*(1+{zoom})/2)*2':h='trunc({height}*(1+{zoom})/2)*2'"
            ":eval=frame:flags=bicubic[fxz];"
            f"[fxa][fxz]overlay=x='(W-w)/2+({dx})':y='(H-h)/2+({dy})':eval=frame:format=auto[fxc];"
        )
        head = "[fxc]"

    flashes = [t for t in tracks if t["kind"] == "flash"]
    if flashes:
        strongest = max(float(t.get("intensity", 0.5)) for t in flashes)
        steps.append(f"eq=brightness={_num(FLASH_BRIGHTNESS * (0.6 + 0.4 * strongest))}:enable='{_window(flashes)}'")

    tints = [t for t in tracks if t["kind"] == "tint_red"]
    if tints:
        strongest = max(float(t.get("intensity", 0.5)) for t in tints)
        steps.append(
            f"colorchannelmixer=rr={_num(1 + TINT_RED_GAIN * strongest)}"
            f":gg={_num(1 - TINT_OTHER_GAIN * strongest)}:bb={_num(1 - TINT_OTHER_GAIN * strongest)}"
            f":enable='{_window(tints)}'"
        )

    if not steps and not camera:
        return None
    if not steps:
        # Camera movement only: relabel the overlay output.
        return prefix + "[fxc]null[vfx]"
    return prefix + head + ",".join(steps) + "[vfx]"
