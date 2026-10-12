import subprocess
import os
from utils.logger import job_info, job_error

def generate_storyboard(video_path: str, output_dir: str) -> str:
    """
    Generate a 2x2 storyboard grid for a video clip.
    """
    os.makedirs(output_dir, exist_ok=True)
    storyboard_path = os.path.join(output_dir, "storyboard.jpg")
    
    # Extract 4 frames across the clip (time-based, works for any duration)
    # fps=0.25 grabs 1 frame per 4s; tile=2x2 builds the grid.
    # For clips <4s ffmpeg tiles the available frames (repeats last if fewer than 4).
    cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-vf", "fps=0.25,tile=2x2",
        "-frames:v", "1",
        storyboard_path
    ]
    
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        job_info(0, f"Storyboard generated: {storyboard_path}")
        return storyboard_path
    except Exception as e:
        job_error(0, f"Storyboard generation failed: {str(e)}")
        return ""
