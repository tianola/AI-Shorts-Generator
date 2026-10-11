"""
Video rendering service.
Burns captions into videos using FFmpeg and libass.
"""
import subprocess
import json
import os
from config import settings
from utils.logger import job_info, job_error, job_warning

def get_video_size(video_path: str) -> tuple:
    """
    Get video dimensions using ffprobe.

    Args:
        video_path: Path to video file

    Returns:
        Tuple of (width, height)

    Raises:
        Exception: If video size cannot be determined
    """
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        video_path
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True
        )
        data = json.loads(result.stdout)

        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                return (stream.get("width"), stream.get("height"))

        # No video stream found — return fallback vertical size
        job_error(0, "ffprobe returned no video streams; using fallback size 1080x1920")
        return (1080, 1920)

    except Exception as e:
        # On probe failure, return fallback size rather than raising so the pipeline can continue
        job_error(0, f"Failed to get video size: {str(e)}; using fallback 1080x1920")
        return (1080, 1920)

def mux_tts(
    video_path: str,
    tts_path: str,
    output_path: str
) -> str:
    """
    Mix TTS voice-over into the video while keeping the full clip length.

    The TTS (a short hook, typically a few seconds) is layered on top of the
    original clip audio (ducked) so the finished short keeps its intended
    duration instead of collapsing to the length of the voice file.

    Args:
        video_path: Path to input video
        tts_path: Path to tts audio (mp3)
        output_path: Path to output video
    Returns:
        Path to muxed output video
    """
    # Ensure output dir exists
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    if not os.path.exists(tts_path):
        raise Exception(f"TTS audio not found: {tts_path}")

    has_audio = _has_audio_stream(video_path)

    if has_audio:
        # Layer the hook voice-over on top of the ducked original audio.
        # amix duration=longest keeps the output as long as the video's own
        # audio, so the clip is never truncated to the TTS length.
        cmd = [
            "ffmpeg",
            "-y",
            "-i", video_path,
            "-i", tts_path,
            "-map", "0:v",
            "-filter_complex",
            "[1:a]adelay=600|600,volume=1.0[t];[0:a]volume=0.35[orig];"
            "[t][orig]amix=inputs=2:duration=longest:dropout_transition=0[a]",
            "-map", "[a]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            output_path
        ]
    else:
        # No original audio: pad the TTS with silence to match the video so
        # the output keeps its full length.
        video_duration = _probe_duration(video_path)
        cmd = [
            "ffmpeg",
            "-y",
            "-i", video_path,
            "-i", tts_path,
            "-map", "0:v",
            "-map", "1:a",
            "-af", f"apad=whole_dur={max(video_duration - 0.6, 1):.3f},adelay=600|600",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            output_path
        ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        job_info(0, f"Muxed TTS into video: {output_path}")
        return output_path
    except subprocess.CalledProcessError as e:
        err = e.stderr
        if isinstance(err, (bytes, bytearray)):
            error_msg = err.decode(errors='ignore')
        else:
            error_msg = str(err) if err else str(e)
        job_error(0, f"TTS mux failed: {error_msg}")
        raise Exception(f"TTS mux failed: {error_msg}")


def _has_audio_stream(video_path: str) -> bool:
    """Return True when the video file contains at least one audio stream."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        video_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # Unknown — guessing "no audio" would strip the voice from the
            # finished short, which is far worse than an empty audio map.
            return True
        return bool(result.stdout.strip())
    except Exception:
        return True  # Assume audio exists; the amix path is the common case.


def _probe_duration(video_path: str) -> float:
    """Return the video duration in seconds (0.0 on failure)."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def add_title_overlay(
    video_path: str,
    title: str,
    output_path: str,
    font_size: int = 52
) -> str:
    """Add a professional motion-style top title overlay (neutral gradient band,
    clean white uppercase text — no yellow lines). Used only as a fallback when
    the motion_title PNG path is unavailable."""
    from services.font_resolve import find_font, ffmpeg_font_path

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    display_title = str(title or "Viral Moment")[:50].upper()

    # Word-wrap into at most 2 clean lines with max ~16-18 chars per line to prevent horizontal overflow
    words = display_title.split()
    lines = []
    current_line = []
    for word in words:
        current_line.append(word)
        if len(" ".join(current_line)) > 16:
            lines.append(" ".join(current_line))
            current_line = []
    if current_line:
        lines.append(" ".join(current_line))

    display_text = "\n".join(lines[:2])

    # Adaptive font size — safe and perfectly scaled for 1080x1920
    length = len(display_title)
    if length < 20:
        fs = 64
    elif length < 35:
        fs = 56
    else:
        fs = 48

    # Write title to temp file — avoids ALL drawtext inline escaping issues
    title_file = output_path + ".title.txt"
    with open(title_file, "w", encoding="utf-8") as f:
        f.write(display_text)

    title_file_ff = ffmpeg_font_path(title_file)
    font = find_font(bold=True)
    font_ff = ffmpeg_font_path(font) if font else ""
    font_opt = f"fontfile='{font_ff}':" if font_ff else ""

    # Professional clean fallback title overlay (no dark band across video):
    subtitle_filter = (
        f"drawtext=textfile='{title_file_ff}':"
        f"{font_opt}"
        f"fontcolor=white:fontsize={fs}:"
        f"x=(w-text_w)/2:y=210:"
        f"borderw=4:bordercolor=0x000000:"
        f"shadowx=3:shadowy=3:shadowcolor=0x000000@0.5"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-vf", subtitle_filter,
        "-map", "0:v",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-r", "30",
        "-g", "60",
        "-fps_mode", "cfr",
        "-preset", settings.FFMPEG_PRESET,
        "-crf", str(settings.FFMPEG_CRF),
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        output_path,
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        job_info(0, f"Title overlay added: {output_path}")
        return output_path
    except subprocess.CalledProcessError as e:
        err = e.stderr
        if isinstance(err, (bytes, bytearray)):
            error_msg = err.decode(errors='ignore')
        else:
            error_msg = str(err) if err else str(e)
        job_error(0, f"Title overlay failed: {error_msg}")
        raise Exception(f"Title overlay failed: {error_msg}")
    finally:
        # Clean up temp title file
        try:
            os.remove(title_file)
        except OSError:
            pass


def burn_captions(
    video_path: str,
    captions_path: str,
    output_path: str,
) -> str:
    """
    Burn ASS captions into a video using FFmpeg and libass.
    Uses the embedded styles from the .ass file for exact positioning and font sizing.
    """
    # Ensure output directory exists
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    # Build subtitle filter — normalize path for ffmpeg and escape single quotes
    captions_escaped = captions_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    subtitle_filter = f"subtitles='{captions_escaped}'"

    # FFmpeg command with settings from config
    cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-vf", subtitle_filter,
        "-map", "0:v",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-fps_mode", "cfr",
        "-preset", settings.FFMPEG_PRESET,
        "-crf", str(settings.FFMPEG_CRF),
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        output_path
    ]

    creationflags = 0
    if os.name == 'nt':
        creationflags = 0x08000000 # subprocess.CREATE_NO_WINDOW

    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            creationflags=creationflags
        )
        job_info(0, f"Captions burned in: {output_path}")
        return output_path
    except subprocess.CalledProcessError as e:
        err = e.stderr
        if isinstance(err, (bytes, bytearray)):
            error_msg = err.decode(errors='ignore')
        else:
            error_msg = str(err) if err else str(e)
        job_error(0, f"Caption burn-in failed: {error_msg}")
        raise Exception(f"Caption burn-in failed: {error_msg}")

# ---------------------------------------------------------------------------
# Single-pass unified render pipeline (Task D)
# ---------------------------------------------------------------------------
def _esc_filter_path(path: str) -> str:
    """Escape a filesystem path for use inside an ffmpeg filter argument."""
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def build_single_pass_filter(
    vertical_frag: str,
    captions_path: str = None,
    title_png: str = None,
    sfx_files: list = None,
    sfx_timestamps: list = None,
    has_audio: bool = True,
    smart_blur_frag: str = None,
    popup_png: str = None,
    popup_start: float = None,
    popup_duration: float = 2.0,
    popup_position: str = "upper_third",
    popups: list = None,
) -> tuple:
    """
    Construct an all-in-one FFmpeg filter_complex that combines all visual
    and audio transformations into a SINGLE pass.

    Args:
        vertical_frag: The `[0:v] ... [vsingle]` vertical-crop fragment.
        captions_path: Optional .ass subtitle file to burn.
        title_png: Optional motion-title PNG path (already-styled overlay).
        sfx_files / sfx_timestamps: Optional SFX audio list + start offsets.
        has_audio: Whether the source video has an audio stream.
        smart_blur_frag: Optional `[0:v] ... [outv]` fragment that blurs a
            tracked unsafe region. When provided it is applied BEFORE the
            vertical crop (it consumes [0:v] and re-emits [outv]).
        popup_png: Optional path to prepared popup card PNG.
        popup_start: Timestamp (in seconds) when the visual popup appears.
        popup_duration: Duration (in seconds) the popup stays visible (default 2.0).
        popup_position: Popup placement ('upper_third' or 'upper_right').
        popups: Optional list of popup dicts (each with png, start, duration, position).

    Returns:
        (extra_inputs, filter_complex, vmap, amap)
          - extra_inputs: list of dicts for extra -i inputs (PNG, SFX files)
          - filter_complex: full filtergraph string ending in [vout]/[aout]
          - vmap: "-map" argument for video stream ('[vout]')
          - amap: "-map" argument for audio stream ('[aout]') or "0:a?" or None
    """
    extra_inputs = []
    fc_parts = []
    next_idx = 1  # input 0 = main video
    cur_v = "[0:v]"

    def _wire(frag: str, out_label: str):
        """Append a builder fragment rewired to read from cur_v and emit out_label.

        Fragments are authored against [0:v]. A pad may only be consumed once,
        so a fragment that references its source twice needs an explicit split;
        and two stages both emitting [vsingle] is a duplicate-label graph error.
        """
        nonlocal cur_v
        text = frag.strip()
        refs = text.count("[0:v]")
        if refs == 0:
            raise ValueError(f"Filter fragment has no [0:v] input: {text[:80]}")
        if refs > 1:
            pads = "".join(f"[wiresplit{i}]" for i in range(refs))
            fc_parts.append(f"{cur_v}split={refs}{pads}")
            for i in range(refs):
                text = text.replace("[0:v]", f"[wiresplit{i}]", 1)
        else:
            text = text.replace("[0:v]", cur_v, 1)

        text = text.rstrip()
        if text.endswith("]"):
            text = text[:text.rfind("[")]
        fc_parts.append(text + out_label)
        cur_v = out_label

    # ---- Stage 0: optional smart blur (applied on the source frame) ----
    if smart_blur_frag:
        _wire(smart_blur_frag, "[vblur]")

    # ---- Stage 1: vertical crop (always required) ----
    _wire(vertical_frag, "[vsingle]")

    # ---- Stage 2: captions (optional) ----
    if captions_path:
        cap_esc = _esc_filter_path(captions_path)
        fc_parts.append(cur_v + "subtitles='" + cap_esc + "'[vsub]")
        cur_v = "[vsub]"

    # ---- Stage 3: Contextual visual popup card overlay (optional) ----
    popup_list = []
    if popups:
        popup_list.extend(popups)
    elif popup_png:
        popup_list.append({
            "png": popup_png,
            "start": popup_start if popup_start is not None else 2.5,
            "duration": popup_duration if popup_duration is not None else 2.0,
            "position": popup_position or "upper_third",
        })

    if popup_list:
        from services.visual_popup import build_popup_filtergraph_fragment
        for i, pop in enumerate(popup_list):
            p_png = pop.get("png") or pop.get("path")
            if not p_png:
                continue
            p_start = float(pop.get("start", 2.5))
            p_dur = float(pop.get("duration", 2.0))
            p_pos = pop.get("position", "upper_third")

            extra_inputs.append({"path": p_png, "kind": "popup"})
            p_idx = next_idx
            next_idx += 1

            prep, anim_label, overlay_str = build_popup_filtergraph_fragment(
                input_idx=p_idx,
                start_time=p_start,
                duration=p_dur,
                position=p_pos,
            )
            fc_parts.append(prep)
            out_label = f"[vpop{i}]" if i < len(popup_list) - 1 else "[vpop]"
            fc_parts.append(f"{cur_v}[{anim_label}]{overlay_str}{out_label}")
            cur_v = out_label

    # ---- Stage 4: motion title PNG overlay (optional, stays for full clip) ----
    if title_png:
        extra_inputs.append({"path": title_png, "kind": "png"})
        title_idx = next_idx
        next_idx += 1
        scale_expr = (
            "0.9+0.10*clip(t/0.45,0,1)"
            "+0.12*sin(clip(t/0.6,0,1)*PI/1.5)"
            "+0.015*sin(2*PI*t/2.4)"
        )
        y_expr = "36*(1-clip(t/0.45,0,1))"
        # Inside '...' a comma is literal to the graph parser (this is the form
        # ffmpeg's own docs use for scale/select), so the quoted scale factors
        # must NOT be escaped — but the unquoted overlay y= must be.
        escy = y_expr.replace(",", "\\,")
        fc_parts.append("[" + str(title_idx) + ":v]format=rgba[title]")
        fc_parts.append(
            "[title]scale=w='iw*(" + scale_expr
            + ")':h='ih*(" + scale_expr + ")':eval=frame,"
            + "fade=t=in:st=0:d=0.45:alpha=1[title_anim]"
        )
        fc_parts.append(
            cur_v + "[title_anim]overlay=x=(W-w)/2:y=" + escy
            + ":shortest=1:format=auto[vt]"
        )
        cur_v = "[vt]"

    # ---- Stage 4: SFX audio mixing (100% voice preserved) ----
    aout = None
    if sfx_files and has_audio:
        sfx_files = list(sfx_files)
        sfx_timestamps = list(sfx_timestamps or [])
        fc_parts.append("[0:a]volume=1.0[orig]")
        labels = []
        for i, sfx_path in enumerate(sfx_files):
            sfx_idx = next_idx
            next_idx += 1
            extra_inputs.append({"path": sfx_path, "kind": "sfx"})
            delay_ms = int(float(sfx_timestamps[i]) * 1000) if i < len(sfx_timestamps) else 300
            label = "sfx" + str(i)
            fc_parts.append(
                "[" + str(sfx_idx) + ":a]adelay=" + str(delay_ms) + "|" + str(delay_ms)
                + ",volume=0.25[" + label + "]"
            )
            labels.append("[" + label + "]")
        mix_in = "[orig]" + "".join(labels)
        fc_parts.append(
            mix_in + "amix=inputs=" + str(1 + len(labels))
            + ":duration=first:dropout_transition=0:normalize=0[aout]"
        )
        aout = "[aout]"
    elif has_audio:
        aout = "0:a?"

    # ---- Finalize video output ----
    fc_parts.append(cur_v + "format=yuv420p[vout]")
    vmap = "[vout]"

    return extra_inputs, ";".join(fc_parts), vmap, aout


def _run_ffmpeg_and_verify(
    cmd: list,
    output_path: str,
    label: str,
    min_duration: float = 0.0,
) -> str:
    """Run an ffmpeg command and verify the output exists and has positive size."""
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    creationflags = 0
    if os.name == 'nt':
        creationflags = 0x08000000 # subprocess.CREATE_NO_WINDOW
        
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, creationflags=creationflags)
    except subprocess.CalledProcessError as e:
        err = e.stderr
        if isinstance(err, (bytes, bytearray)):
            err = err.decode(errors="ignore")
        job_error(0, label + " failed: " + str(err))
        raise Exception(label + " failed: " + str(err))
    if not os.path.exists(output_path) or os.path.getsize(output_path) <= 0:
        raise Exception(label + " produced no output file: " + output_path)
    if min_duration > 0:
        d = _probe_duration(output_path)
        if d > 0 and d < min_duration:
            raise Exception(
                label + " produced too-short duration %.2fs (need >= %.2fs)" % (d, min_duration)
            )
    return output_path


def render_single_pass(
    input_video: str,
    start: float,
    end: float,
    output_path: str,
    vertical_frag: str = None,
    captions_path: str = None,
    title_text: str = None,
    title_png: str = None,
    sfx_files: list = None,
    sfx_timestamps: list = None,
    mode: str = "static",
    smart_blur_frag: str = None,
    popup_png: str = None,
    popup_start: float = None,
    popup_duration: float = 2.0,
    popup_position: str = "upper_third",
    popups: list = None,
    source_channel: str = None,
) -> str:
    """
    Render a clip in a SINGLE ffmpeg pass (Task D).
    Combines: vertical crop + captions + visual popup card + title overlay + SFX mix.
    Voice is preserved at 100% (no ducking).
    """
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    if vertical_frag is None:
        from services.vertical import build_vertical_filter
        vertical_frag = build_vertical_filter(input_video, mode=mode)

    # Build title PNG via PIL when requested and not provided
    if title_text and not title_png:
        try:
            from services.motion_title import render_title_frame
            from PIL import Image
            frame = render_title_frame(title_text, source_channel=source_channel)
            title_png = output_path + ".title.png"
            Image.fromarray(frame, "RGBA").save(title_png)
            job_info(0, "Rendered title PNG: " + title_png)
        except Exception as e:
            job_warning(0, "Title PNG render failed, skipping: " + str(e))
            title_png = None

    has_audio = _has_audio_stream(input_video)

    # Filter out SFX files that don't exist on disk
    if sfx_files:
        kept_sfx = []
        kept_ts = []
        for i, sfx_path in enumerate(sfx_files):
            if os.path.exists(sfx_path):
                kept_sfx.append(sfx_path)
                kept_ts.append(
                    sfx_timestamps[i] if sfx_timestamps and i < len(sfx_timestamps) else 0.3
                )
        sfx_files = kept_sfx
        sfx_timestamps = kept_ts

    extra, fc, vmap, amap = build_single_pass_filter(
            vertical_frag,
            captions_path=captions_path,
            title_png=title_png,
            sfx_files=sfx_files,
            sfx_timestamps=sfx_timestamps,
            has_audio=has_audio,
            smart_blur_frag=smart_blur_frag,
            popup_png=popup_png,
            popup_start=popup_start,
            popup_duration=popup_duration,
            popup_position=popup_position,
            popups=popups,
        )

    # Build ffmpeg command: 0 = main video, then extra inputs
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-t", str(end - start), "-i", input_video]
    for ei in extra:
        # Loop PNG / popup image inputs
        if ei.get("kind") in ("png", "popup"):
            cmd += ["-loop", "1"]
        cmd += ["-i", ei["path"]]

    cmd += ["-map", vmap]
    if amap:
        cmd += ["-map", amap]

    # Encode to a sibling temp file and move it into place only on success:
    # with -y, a crashed ffmpeg used to leave a truncated clip at output_path
    # that the UI happily served as a "completed" short.
    _root, _ext = os.path.splitext(output_path)
    tmp_path = f"{_root}.part{_ext or '.mp4'}"

    cmd += [
        "-filter_complex", fc,
        "-c:v", "libx264",
        "-fps_mode", "cfr",
        "-preset", settings.FFMPEG_PRESET,
        "-crf", str(settings.FFMPEG_CRF),
    ]
    # 0 = let x264 use every logical core. On a 16-thread desktop the old
    # hard cap of 2 cost 7.5s vs 3.4s for a 30s 1080x1920 clip - identical
    # output, 2.2x slower. Lower it only if RAM is the constraint.
    if settings.FFMPEG_THREADS > 0:
        cmd += ["-threads", str(settings.FFMPEG_THREADS)]
    cmd += [
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        tmp_path,
    ]

    try:
        _run_ffmpeg_and_verify(
            cmd, tmp_path, "single-pass render",
            min_duration=max(end - start - 0.3, 0.0),
        )
        os.replace(tmp_path, output_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    # Clean up temp title PNG
    if title_png and title_png.endswith(".title.png") and os.path.exists(title_png):
        try:
            os.remove(title_png)
        except OSError:
            pass

    return output_path
