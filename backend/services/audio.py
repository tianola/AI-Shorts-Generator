import json
import subprocess
import os


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

    output_path = video_path.replace("source.mp4", "audio.wav")

    creationflags = 0
    if os.name == 'nt':
        creationflags = 0x08000000

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
        subprocess.run(silent_command, check=True, creationflags=creationflags)
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

    subprocess.run(command, check=True, creationflags=creationflags)

    print("END extract_audio", flush=True)

    return output_path


TRANSCRIPTION_SAMPLE_RATE = 16000


def decode_audio_for_transcription(audio_path: str, start: float = 0.0,
                                   duration: float = None):
    """Decode any media file to mono float32 PCM at 16 kHz, via ffmpeg.

    faster-whisper 1.2.1 always passes metadata_errors= to av.open(), an
    argument PyAV 19 removed, so its own decoder raises TypeError before a
    single sample is read - every local transcription job failed. Handing the
    model a decoded array keeps transcription on the ffmpeg binary the rest of
    the pipeline already depends on, and works with any PyAV version.
    """
    import numpy as np

    command = ["ffmpeg", "-v", "error"]
    if start:
        command += ["-ss", str(start)]
    command += ["-i", audio_path]
    if duration is not None:
        command += ["-t", str(duration)]
    command += ["-vn", "-ac", "1", "-ar", str(TRANSCRIPTION_SAMPLE_RATE), "-f", "s16le", "-"]

    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    blocks = []
    try:
        while True:
            chunk = process.stdout.read(8 * 1024 * 1024)
            if not chunk:
                break
            blocks.append(np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0)
    finally:
        stderr = process.stderr.read()
        process.stderr.close()
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()

    if process.returncode != 0:
        message = (stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"ffmpeg could not decode {audio_path}: {message[:400]}")

    if not blocks:
        return np.zeros(0, dtype=np.float32)
    return blocks[0] if len(blocks) == 1 else np.concatenate(blocks)
