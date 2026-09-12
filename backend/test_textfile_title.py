"""Quick test: textfile approach for title overlay."""
import os
import subprocess
import tempfile

video = r"E:\01-AI-PROJECTS\ai-shorts-generator\storage\jobs\28\clip_0\final_short.mp4"
tmpdir = tempfile.gettempdir()

title = "The $3,200 iPhone is REAL"
title_file = os.path.join(tmpdir, "test_title.txt")
with open(title_file, "w", encoding="utf-8") as f:
    f.write(title)

# Use forward slashes for ALL paths — no colon escaping needed
tf = title_file.replace("\\", "/")
font = "C:/Windows/Fonts/arialbd.ttf"

print(f"title_file (ffmpeg): {tf}")
print(f"font (ffmpeg): {font}")

# Test 1: Simple drawtext with textfile
vf = f"drawtext=textfile='{tf}':fontcolor=white:fontsize=64:x=10:y=10:borderw=3:bordercolor=black"
cmd = [
    "ffmpeg", "-y", "-i", video,
    "-vf", vf,
    "-map", "0:v", "-map", "0:a?",
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
    "-c:a", "aac", "-pix_fmt", "yuv420p",
    os.path.join(tmpdir, "test_simple.mp4"),
]
result = subprocess.run(cmd, capture_output=True, text=True)
if result.returncode == 0:
    print("SIMPLE TEST OK!")
else:
    print(f"SIMPLE TEST FAIL: {result.stderr[-400:]}")

# Test 2: Full TikTok chain
vf2 = (
    f"drawbox=x=0:y=0:w=iw:h=220:color=black@0.6:t=fill,"
    f"drawtext=textfile='{tf}':"
    f"fontfile='{font}':"
    f"fontcolor=0xFFE500:fontsize=64:"
    f"x=(w-text_w)/2:y=80:borderw=5:bordercolor=0x000000,"
    f"drawtext=textfile='{tf}':"
    f"fontfile='{font}':"
    f"fontcolor=white:fontsize=64:"
    f"x=(w-text_w)/2:y=80:borderw=3:bordercolor=0x000000,"
    f"drawbox=x=(iw/2-100):y=160:w=200:h=4:color=0xFFE500:t=fill"
)
cmd2 = [
    "ffmpeg", "-y", "-i", video,
    "-vf", vf2,
    "-map", "0:v", "-map", "0:a?",
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
    "-c:a", "aac", "-pix_fmt", "yuv420p",
    os.path.join(tmpdir, "test_tiktok.mp4"),
]
result2 = subprocess.run(cmd2, capture_output=True, text=True)
if result2.returncode == 0:
    print("FULL TIKTOK TEST OK!")
else:
    print(f"FULL TIKTOK TEST FAIL: {result2.stderr[-500:]}")

try:
    os.remove(title_file)
except OSError:
    pass
