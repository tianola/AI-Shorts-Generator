"""Test VLM scene attention fallback on clip_0"""
import asyncio
import sys
from pathlib import Path

backend_dir = Path(__file__).parent / "backend"
sys.path.insert(0, str(backend_dir))

async def test_vlm_fallback():
    """Test VLM scene attention on clip_0 from job 93"""
    from services import reframe_plan
    from services.vlm_scene_attention import build_vlm_attention

    # Use final_video.mp4 since short.mp4 was deleted after render
    clip_0_video = "E:/01-AI-PROJECTS/ai-shorts-generator/storage/jobs/93/clip_0/final_video.mp4"
    clip_0_words = "E:/01-AI-PROJECTS/ai-shorts-generator/storage/jobs/93/clip_0/words.json"

    print(f"Testing VLM scene attention on {clip_0_video}")

    # Load transcript
    import json
    with open(clip_0_words, encoding='utf-8') as f:
        words = json.load(f)
    transcript = " ".join(w.get("word", "") for w in words[:50])

    # Analyse clip
    analysis = reframe_plan.analyse_clip(clip_0_video)
    print(f"Samples: {len(analysis.get('samples', []))}")
    print(f"Tracks: {len(analysis.get('tracks', []))}")

    # Test VLM attention
    attention = await build_vlm_attention(
        analysis, clip_0_words, transcript, video_path=clip_0_video
    )

    print(f"\nVLM Attention result:")
    print(f"  Scenes: {len(attention.get('scenes', []))}")
    for note in attention.get("notes", []):
        print(f"  Note: {note}")

    for scene in attention.get("scenes", []):
        print(f"\n  Scene:")
        print(f"    Time: {scene.get('start'):.1f}-{scene.get('end'):.1f}s")
        print(f"    Target: {scene.get('target')}")
        print(f"    Importance: {scene.get('importance'):.2f}")
        print(f"    Action: {scene.get('action')}")
        print(f"    Objects: {scene.get('objects', [])}")
        print(f"    Source: {scene.get('source')}")

    return attention

if __name__ == "__main__":
    attention = asyncio.run(test_vlm_fallback())
    print(f"\n{'='*60}")
    print(f"Total scenes found: {len(attention.get('scenes', []))}")
