from services.focus_tracking import compute_smooth_yolo_trajectory, build_split_screen_filter

def test_ema_trajectory_smoothing():
    # Simulate jittery detections alternating slightly
    samples = [(0.0 + i * 0.5, [{"cls": 0, "conf": 0.9, "box": (400 + (i % 2) * 20, 200, 200, 400)}]) for i in range(10)]
    traj = compute_smooth_yolo_trajectory(samples, src_w=1920, src_h=1080)
    assert len(traj) > 0
    x_positions = [t[1] for t in traj]
    deltas = [abs(x_positions[i] - x_positions[i - 1]) for i in range(1, len(x_positions))]
    # With deadband + EMA smoothing, deltas should be very smooth (< 15 px)
    assert max(deltas) < 15

def test_split_screen_filtergraph_builder():
    fg = build_split_screen_filter(
        box_top=(100, 50, 400, 800),
        box_bottom=(1200, 50, 400, 800),
        src_w=1920,
        src_h=1080
    )
    assert "vstack" in fg or "vstack" in fg.lower()
    assert "scale=1080:960" in fg
