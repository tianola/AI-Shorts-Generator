"""The layout gate: a stacked two-shot has to be earned.

Pure-function tests over fabricated YOLO sample lists — no model, no ffmpeg —
because the whole point of the rewrite is that the decision is a handful of
comparisons an editor makes in their head, and those comparisons are what
should be pinned. The ffmpeg-backed speech probe is exercised separately.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from services import reframe_plan as plan

SRC_W, SRC_H = 1920, 1080


def _person(x, y, w, h, conf=0.8):
    return {"cls": 0, "name": "person", "conf": conf, "box": (x, y, w, h)}


def _obj(cls, name, x, y, w, h, conf=0.8):
    return {"cls": cls, "name": name, "conf": conf, "box": (x, y, w, h)}


def _samples(per_frame, interval=0.5):
    return [(i * interval, list(dets)) for i, dets in enumerate(per_frame)]


def _two_person_frames(n=20, gap=700, h_a=420, h_b=420):
    """A steady interview two-shot: left third, right third, both full height."""
    left = (250, 300, 300, h_a)
    right = (250 + gap + 300, 300, 300, h_b)
    return _samples([[_person(*left), _person(*right)] for _ in range(n)])


# ---------------------------------------------------------------------------
# tracking
# ---------------------------------------------------------------------------

class TestTrackAssociation:
    def test_a_steady_person_is_one_track(self):
        samples = _two_person_frames(20)
        tracks = plan.associate_person_tracks(samples)
        assert len(tracks) == 2
        assert all(len(t["timeline"]) == 20 for t in tracks)

    def test_a_jumping_box_starts_a_new_track(self):
        """A hard cut is not the same person, so it must not extend the track."""
        samples = _samples([
            [_person(100, 300, 300, 400)],
            [_person(100, 300, 300, 400)],
            [_person(1500, 300, 300, 400)],  # different shot
        ])
        tracks = plan.associate_person_tracks(samples)
        assert [len(t["timeline"]) for t in tracks] == [2, 1]

    def test_a_track_waits_only_one_gap_before_giving_up(self):
        samples = _samples([[_person(100, 300, 300, 400)], [], [_person(120, 300, 300, 400)]])
        tracks = plan.associate_person_tracks(samples)
        # 1.0 s between hits is within the 1.6 s window, so it stays one subject.
        assert len(tracks) == 1
        assert len(tracks[0]["timeline"]) == 2


# ---------------------------------------------------------------------------
# the two-shot gate
# ---------------------------------------------------------------------------

class TestTwoShotGate:
    def test_a_real_two_shot_qualifies(self):
        samples = _two_person_frames(20)
        tracks = plan.associate_person_tracks(samples)
        pair = plan.find_two_shot(tracks, samples, SRC_W, SRC_H)
        assert pair is not None
        left, right = pair
        # The camera's left goes on top, not whichever box the detector emitted first.
        assert left["timeline"][0][1][0] < right["timeline"][0][1][0]

    def test_three_persistent_people_are_not_a_two_shot(self):
        """The job 90 case: a group shot is not a podcast, stacked or otherwise."""
        samples = _samples([
            [_person(150, 300, 300, 420), _person(800, 300, 300, 420), _person(1450, 300, 300, 420)]
            for _ in range(20)
        ])
        tracks = plan.associate_person_tracks(samples)
        assert plan.find_two_shot(tracks, samples, SRC_W, SRC_H) is None

    def test_two_people_in_one_lucky_frame_are_not_a_two_shot(self):
        per_frame = [[_person(250, 300, 300, 420)] for _ in range(19)]
        per_frame[7] = [_person(250, 300, 300, 420), _person(1200, 300, 300, 420)]
        samples = _samples(per_frame)
        tracks = plan.associate_person_tracks(samples)
        assert plan.find_two_shot(tracks, samples, SRC_W, SRC_H) is None

    def test_a_background_extra_is_not_a_co_host(self):
        samples = _samples([
            [_person(250, 200, 500, 700), _person(1300, 700, 120, 160)] for _ in range(20)
        ])
        tracks = plan.associate_person_tracks(samples)
        assert plan.find_two_shot(tracks, samples, SRC_W, SRC_H) is None

    def test_two_people_standing_side_by_side_are_one_frame_not_two(self):
        samples = _two_person_frames(20, gap=40)
        tracks = plan.associate_person_tracks(samples)
        assert plan.find_two_shot(tracks, samples, SRC_W, SRC_H) is None


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------

def _analysis(tracks, samples, two_shot, speech=0.55, motion=None, src_w=SRC_W, src_h=SRC_H):
    return {
        "samples": samples,
        "tracks": tracks,
        "n_samples": len(samples),
        "src_w": src_w,
        "src_h": src_h,
        "two_shot": two_shot,
        "speech": speech,
        "motion": motion if motion is not None else {},
        "focus": None,
    }


class TestDecideLayout:
    def test_talking_balanced_two_shot_splits(self):
        samples = _two_person_frames(20)
        tracks = plan.associate_person_tracks(samples)
        pair = plan.find_two_shot(tracks, samples, SRC_W, SRC_H)
        analysis = _analysis(tracks, samples, pair, speech=0.6, motion={0: 0.05, 1: 0.045})
        mode, _focus = plan.decide_layout(analysis)
        assert mode == "split_screen"

    def test_a_silent_two_shot_does_not_split(self):
        samples = _two_person_frames(20)
        tracks = plan.associate_person_tracks(samples)
        pair = plan.find_two_shot(tracks, samples, SRC_W, SRC_H)
        analysis = _analysis(tracks, samples, pair, speech=0.02, motion={0: 0.05, 1: 0.045})
        mode, _focus = plan.decide_layout(analysis)
        assert mode != "split_screen"

    def test_one_person_talking_does_not_split(self):
        """Two in frame, one carrying the clip: follow the talker, do not stack."""
        samples = _two_person_frames(20)
        tracks = plan.associate_person_tracks(samples)
        pair = plan.find_two_shot(tracks, samples, SRC_W, SRC_H)
        analysis = _analysis(tracks, samples, pair, speech=0.6, motion={0: 0.06, 1: 0.008})
        mode, focus = plan.decide_layout(analysis)
        assert mode == "single_speaker"
        assert focus["kind"] == "person"
        assert focus["track_id"] == 0

    def test_an_unmeasurable_soundtrack_does_not_volunteer_a_split(self):
        samples = _two_person_frames(20)
        tracks = plan.associate_person_tracks(samples)
        pair = plan.find_two_shot(tracks, samples, SRC_W, SRC_H)
        analysis = _analysis(tracks, samples, pair, speech=None, motion={0: 0.05, 1: 0.05})
        mode, _focus = plan.decide_layout(analysis)
        assert mode != "split_screen"


class TestChooseFocus:
    def test_the_talker_beats_the_largest_person(self):
        """A presenter leaning into camera is big; the one talking is the subject."""
        big_but_still = _person(100, 100, 700, 900)
        small_but_talking = _person(1300, 250, 300, 500)
        samples = _samples([[big_but_still, small_but_talking] for _ in range(20)])
        tracks = plan.associate_person_tracks(samples)
        analysis = _analysis(tracks, samples, None, speech=0.7, motion={t["id"]: 0.006 for t in tracks})
        analysis["motion"][tracks[0]["id"]] = 0.07   # the smaller one
        analysis["motion"][tracks[1]["id"]] = 0.007
        focus = plan.choose_focus(analysis)
        assert focus["kind"] == "person"
        assert focus["reason"].startswith("dominant speaker")

    def test_a_persistent_object_outranks_every_face(self):
        """'Someone talks while the attention is on this' — frame the this."""
        person = _person(200, 300, 260, 380)
        screen = _obj(62, "tv", 500, 150, 1300, 800)
        samples = _samples([[person, screen] for _ in range(20)])
        tracks = plan.associate_person_tracks(samples)
        analysis = _analysis(tracks, samples, None, speech=0.7, motion={0: 0.001})
        focus = plan.choose_focus(analysis)
        assert focus["kind"] == "object"
        assert len(focus["boxes"]) == 20

    def test_without_speech_evidence_motion_is_not_invented(self):
        person_a = _person(200, 300, 300, 500)
        person_b = _person(1200, 300, 300, 500)
        samples = _samples([[person_a, person_b] for _ in range(20)])
        tracks = plan.associate_person_tracks(samples)
        analysis = _analysis(tracks, samples, None, speech=0.0, motion={0: 0.09, 1: 0.002})
        focus = plan.choose_focus(analysis)
        # No voice → the loud-motion claim is void; the largest person wins.
        assert focus["reason"] == "largest person"


# ---------------------------------------------------------------------------
# the follow-crop must honour the focus
# ---------------------------------------------------------------------------

class TestFocusTrajectory:
    def test_focus_boxes_beat_the_largest_box(self):
        big = (100, 100, 700, 900)      # centre x = 450
        small = (1300, 250, 300, 500)   # centre x = 1450
        samples = _samples([[{"cls": 0, "name": "person", "conf": 0.8, "box": big},
                             {"cls": 0, "name": "person", "conf": 0.8, "box": small}] for _ in range(10)])
        focus = [(ts, small) for ts, _dets in samples]
        from services.focus_tracking import compute_smooth_yolo_trajectory
        plain = compute_smooth_yolo_trajectory(samples, SRC_W, SRC_H)
        pinned = compute_smooth_yolo_trajectory(samples, SRC_W, SRC_H, focus_boxes=focus)
        assert plain and pinned
        # Unfocused follows the biggest box (left); focused follows the talker (right).
        assert plain[0][1] < pinned[0][1]

    def test_a_sample_missing_the_focus_is_skipped_not_reassigned(self):
        from services.focus_tracking import compute_smooth_yolo_trajectory
        target = (1300, 250, 300, 500)
        other = (100, 100, 700, 900)
        samples = _samples([[{"cls": 0, "conf": 0.8, "name": "person", "box": other},
                             {"cls": 0, "conf": 0.8, "name": "person", "box": target}] for _ in range(6)])
        focus = [(ts, target) for ts, _d in samples[:3]]  # loses the subject mid-clip
        traj = compute_smooth_yolo_trajectory(samples, SRC_W, SRC_H, focus_boxes=focus)
        assert len(traj) == 3
        assert {round(ts, 2) for ts, *_rest in traj} == {round(ts, 2) for ts, _b in focus}


# ---------------------------------------------------------------------------
# the speech probe (real ffmpeg, cheap synthetic files)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="requires ffmpeg on PATH")
class TestSpeechRatio:
    def _with_audio(self, tmp_path, audio_source: str, name: str) -> str:
        import subprocess
        out = str(tmp_path / name)
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=black:size=320x240:rate=25",
             "-f", "lavfi", "-i", audio_source, "-t", "3",
             "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast",
             "-c:a", "aac", "-shortest", out],
            check=True, capture_output=True,
        )
        return out

    def test_silence_reads_as_no_speech(self, tmp_path):
        path = self._with_audio(tmp_path, "anullsrc=r=16000:cl=mono", "silent.mp4")
        assert plan.speech_ratio(path) < 0.05

    def test_continuous_sound_reads_as_speech_bearing(self, tmp_path):
        path = self._with_audio(tmp_path, "sine=frequency=440:sample_rate=16000", "loud.mp4")
        assert plan.speech_ratio(path) > 0.5

    def test_a_video_without_audio_track_is_unknown_not_zero(self, tmp_path):
        import subprocess
        out = str(tmp_path / "noaudio.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", "color=black:size=320x240:duration=2", "-an", "-c:v", "libx264", out],
            check=True, capture_output=True,
        )
        assert plan.speech_ratio(out) is None
