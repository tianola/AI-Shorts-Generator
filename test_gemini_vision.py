"""Test Google Gemini Vision API with new SDK"""
import os
import sys
from pathlib import Path

# Add backend to path
backend_dir = Path(__file__).parent / "backend"
sys.path.insert(0, str(backend_dir))
os.chdir(backend_dir)

def test_gemini_vision():
    """Test if Gemini Vision API works"""
    try:
        from google import genai
        from google.genai import types
        from config import settings

        if not settings.GOOGLE_AI_STUDIO_KEY:
            print("[FAIL] No GOOGLE_AI_STUDIO_KEY configured")
            return False

        client = genai.Client(api_key=settings.GOOGLE_AI_STUDIO_KEY)
        model = settings.GOOGLE_AI_STUDIO_MODEL

        # Test with a simple text prompt first
        print(f"Testing with model: {model}")
        response = client.models.generate_content(
            model=model,
            contents="Say 'API working' if you can read this."
        )
        print(f"[OK] Text API: {response.text}")

        # Test with an image from job 93
        job_dir = Path("E:/01-AI-PROJECTS/ai-shorts-generator/storage/jobs/93")
        source_video = job_dir / "source.mp4"

        if source_video.exists():
            # Extract a frame
            import cv2
            cap = cv2.VideoCapture(str(source_video))
            cap.set(cv2.CAP_PROP_POS_MSEC, 10000)  # 10s in
            ret, frame = cap.read()
            cap.release()

            if ret:
                # Save frame
                frame_path = job_dir / "test_frame.jpg"
                cv2.imwrite(str(frame_path), frame)
                print(f"Extracted frame: {frame_path}")

                # Test vision
                from PIL import Image
                img = Image.open(frame_path)
                response = client.models.generate_content(
                    model=model,
                    contents=[
                        "Describe what you see in this video frame in 2 sentences.",
                        img
                    ]
                )
                print(f"[OK] Vision API: {response.text}")
                return True
        else:
            print(f"[FAIL] source.mp4 not found")
            return False

    except Exception as e:
        print(f"[FAIL] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_gemini_vision()
    sys.exit(0 if success else 1)
