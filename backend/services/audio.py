import json
import subprocess


def _video_has_audio(video_path):
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "json",
            video_path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return False
    try:
        payload = json.loads(probe.stdout or "{}")
        streams = payload.get("streams", [])
        return any(stream.get("codec_type") == "audio" for stream in streams)
    except Exception:
        return False


def _video_duration_seconds(video_path):
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        value = float((probe.stdout or "").strip())
        return max(1.0, value)
    except Exception:
        return 1.0


def extract_audio(video_path):

    print("START extract_audio", flush=True)

    output_path = video_path.replace(
        "source.mp4",
        "audio.wav"
    )

    if not _video_has_audio(video_path):
        duration = _video_duration_seconds(video_path)
        silent_command = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=mono:sample_rate=16000",
            "-t",
            str(duration),
            "-ac",
            "1",
            "-ar",
            "16000",
            output_path,
        ]
        print("Running silent-audio fallback command:", silent_command, flush=True)
        subprocess.run(silent_command, check=True)
        print("END extract_audio", flush=True)
        return output_path

    command = [
        "ffmpeg",
        "-y",
        "-i",
        video_path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        output_path,
    ]

    print("Running command:", command, flush=True)

    subprocess.run(
        command,
        check=True
    )

    print("END extract_audio", flush=True)

    return output_path