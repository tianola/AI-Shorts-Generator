"""Diagnostic: What did job 93's pipeline ACTUALLY produce?"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

JOB_DIR = "E:/01-AI-PROJECTS/ai-shorts-generator/storage/jobs/93"

def main():
    print("=" * 80)
    print("JOB 93 PIPELINE DIAGNOSTIC")
    print("=" * 80)

    # 1. Temporal localization
    temporal_file = os.path.join(JOB_DIR, "temporal_events.json")
    print("\n[1] TEMPORAL LOCALIZATION")
    if os.path.exists(temporal_file):
        with open(temporal_file, encoding='utf-8') as f:
            events = json.load(f)
        print(f"  [OK] {len(events)} events")
    else:
        print(f"  [FAIL] NO temporal_events.json - worker used OLD code")

    # 2. Transcription
    segments_file = os.path.join(JOB_DIR, "segments.json")
    print("\n[2] TRANSCRIPTION")
    if os.path.exists(segments_file):
        with open(segments_file, encoding='utf-8') as f:
            segments = json.load(f)
        print(f"  [OK] {len(segments)} segments")

    # 3. Scene attention
    print("\n[3] SCENE ATTENTION")
    for clip_idx in range(10):
        clip_dir = os.path.join(JOB_DIR, f"clip_{clip_idx}")
        attention_file = os.path.join(clip_dir, "attention.json")
        if os.path.exists(attention_file):
            with open(attention_file, encoding='utf-8') as f:
                attention = json.load(f)
            scenes = attention.get("scenes", [])
            print(f"  Clip {clip_idx}: {len(scenes)} scenes")

    # 4. YOLO test on source
    print("\n[4] YOLO DETECTION (source video)")
    try:
        from services.focus_tracking import sample_frames
        source_video = os.path.join(JOB_DIR, "source.mp4")
        if os.path.exists(source_video):
            samples = sample_frames(source_video, sample_interval=5.0)
            print(f"  [OK] Sampled {len(samples)} frames (every 5s)")
            person_total = sum(len([d for d in dets if d['cls'] == 0]) for _, dets in samples)
            object_total = sum(len([d for d in dets if d['cls'] != 0]) for _, dets in samples)
            print(f"  Persons detected: {person_total}")
            print(f"  Objects detected: {object_total}")
            for ts, dets in samples[:5]:
                if dets:
                    print(f"    t={ts:.0f}s: {len(dets)} detections")
        else:
            print(f"  [FAIL] source.mp4 not found")
    except Exception as e:
        print(f"  [FAIL] {e}")

    # 5. Trajectory test on clip_0
    print("\n[5] TRAJECTORY (clip_0)")
    try:
        from services.focus_tracking import compute_smooth_yolo_trajectory
        from services.reframe_plan import analyse_clip
        clip_0_video = os.path.join(JOB_DIR, "clip_0", "short.mp4")
        if os.path.exists(clip_0_video):
            analysis = analyse_clip(clip_0_video)
            samples = analysis.get("samples", [])
            tracks = analysis.get("tracks", [])
            print(f"  [OK] {len(samples)} samples, {len(tracks)} tracks")
            if samples:
                src_w = analysis.get("src_w", 1920)
                src_h = analysis.get("src_h", 1080)
                trajectory = compute_smooth_yolo_trajectory(samples, src_w, src_h)
                print(f"  [OK] {len(trajectory)} trajectory points")
                if trajectory:
                    print(f"    First: t={trajectory[0][0]:.1f}s")
                    print(f"    Last: t={trajectory[-1][0]:.1f}s")
            else:
                print(f"  [FAIL] NO samples")
        else:
            print(f"  [FAIL] clip_0/short.mp4 not found")
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()

    print("\n" + "=" * 80)

if __name__ == "__main__":
    main()
