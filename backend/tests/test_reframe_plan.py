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
# diarization: the voice picks the subject
# ---------------------------------------------------------------------------


def _turns(*specs):
    return [{"speaker": speaker, "start": a, "end": b} for speaker, a, b in specs]


def _shot_reverse_shot(switch_at: float = 5.0, n: int = 20):
    """Only the speaker's own shot is on screen — a camera that cuts on turns."""
    frames = []
    for i in range(n):
        ts = i * 0.5
        frames.append([_person(300, 250, 400, 600)] if ts < switch_at else [_person(1200, 250, 400, 600)])
    samples = _samples(frames)
    tracks = plan.associate_person_tracks(samples)
    return samples, tracks


class TestVoiceChoosesTheSubject:
    def test_alternating_voices_switch_the_crop_between_their_tracks(self):
        samples, tracks = _shot_reverse_shot()
        diarization = _turns(("A", 0.0, 5.0), ("B", 5.0, 10.0))
        # Head motion alone would pin the whole clip on the left figure.
        analysis = _analysis(tracks, samples, None, speech=0.7, motion={0: 0.062, 1: 0.01})
        focus = plan.choose_focus(analysis, diarization=diarization)
        assert focus["kind"] == "person"
        assert focus["reason"] == "speaker turns bound 2 voices to their tracks"
        # The left voice owns the first half, the right voice the second.
        assert focus["boxes"][0][1][0] < 600
        assert focus["boxes"][-1][1][0] > 1100

    def test_one_voice_on_camera_outweighs_head_motion(self):
        """The camera stays mostly on the talker; the listener fidgets."""
        frames = []
        for i in range(20):
            ts = i * 0.5
            frames.append([_person(300, 250, 400, 600)] if ts < 7.0 else [_person(1200, 250, 400, 600)])
        samples = _samples(frames)
        tracks = plan.associate_person_tracks(samples)
        # Motion claims the right-hand figure; the voice says the left one talks.
        analysis = _analysis(tracks, samples, None, speech=0.7, motion={0: 0.01, 1: 0.06})
        diarization = _turns(("A", 0.0, 9.5))
        focus = plan.choose_focus(analysis, diarization=diarization)
        assert focus["reason"] == "dominant speaker by voice"
        assert focus["track_id"] == 0
        assert focus["boxes"][0][1][0] < 600

    def test_two_always_visible_people_leave_the_read_to_motion(self):
        """Presence is tied in a wide two-shot — no coin-flip bindings."""
        samples = _two_person_frames(20)
        tracks = plan.associate_person_tracks(samples)
        analysis = _analysis(tracks, samples, None, speech=0.7, motion={0: 0.06, 1: 0.008})
        diarization = _turns(("A", 0.0, 9.5))
        focus = plan.choose_focus(analysis, diarization=diarization)
        assert focus["reason"] == "dominant speaker by head motion"
        assert focus["track_id"] == 0

    def test_a_thing_the_clip_is_about_survives_the_voices(self):
        person = _person(200, 300, 260, 380)
        screen = _obj(62, "tv", 500, 150, 1300, 800)
        samples = _samples([[person, screen] for _ in range(20)])
        tracks = plan.associate_person_tracks(samples)
        analysis = _analysis(tracks, samples, None, speech=0.7, motion={0: 0.001})
        diarization = _turns(("A", 0.0, 9.5))
        focus = plan.choose_focus(analysis, diarization=diarization)
        assert focus["kind"] == "object"


class TestDecideLayoutWithVoices:
    def test_balanced_voices_force_the_split_despite_motion_dominance(self):
        samples = _two_person_frames(20)
        tracks = plan.associate_person_tracks(samples)
        pair = plan.find_two_shot(tracks, samples, SRC_W, SRC_H)
        diarization = _turns(("A", 0.0, 2.5), ("B", 2.5, 5.0), ("A", 5.0, 7.5), ("B", 7.5, 10.0))
        analysis = _analysis(tracks, samples, pair, speech=0.6, motion={0: 0.09, 1: 0.005})
        # Motion alone reads one gesturing talker; the voices say a conversation.
        assert plan.decide_layout(analysis)[0] == "single_speaker"
        assert plan.decide_layout(analysis, diarization=diarization)[0] == "split_screen"

    def test_one_voice_carrying_the_clip_vetoes_a_motion_split(self):
        samples = _two_person_frames(20)
        tracks = plan.associate_person_tracks(samples)
        pair = plan.find_two_shot(tracks, samples, SRC_W, SRC_H)
        diarization = _turns(("A", 0.0, 9.5), ("B", 9.5, 10.0))
        analysis = _analysis(tracks, samples, pair, speech=0.6, motion={0: 0.05, 1: 0.045})
        assert plan.decide_layout(analysis)[0] == "split_screen"
        mode, focus = plan.decide_layout(analysis, diarization=diarization)
        assert mode == "single_speaker"
        assert focus is not None

    def test_alternating_voices_without_a_two_shot_follow_the_talker(self):
        samples, tracks = _shot_reverse_shot()
        diarization = _turns(("A", 0.0, 5.0), ("B", 5.0, 10.0))
        analysis = _analysis(tracks, samples, None, speech=0.7, motion={})
        mode, focus = plan.decide_layout(analysis, diarization=diarization)
        assert mode == "single_speaker"
        assert focus["boxes"][0][1][0] < 600
        assert focus["boxes"][-1][1][0] > 1100


class TestDiarizationReachesTheRenderSeams:
    def test_detect_adaptive_layout_forwards_the_turns(self, monkeypatch):
        from services import reframe_plan, smart_crop

        captured = {}

        def fake_resolve(video_path, sample_interval=0.5, diarization=None, attention=None):
            captured["diarization"] = diarization
            return "single_speaker", None

        monkeypatch.setattr(reframe_plan, "resolve", fake_resolve)
        turns = _turns(("A", 0.0, 1.0))
        mode = smart_crop.detect_adaptive_layout("dummy.mp4", diarization=turns)
        assert mode == "single_speaker"
        assert captured["diarization"] == turns

    def test_a_preset_single_speaker_uses_the_voice_focus(self, monkeypatch):
        import services.focus_tracking as focus_tracking
        from services import reframe_plan, vertical

        analysis = {
            "samples": [(0.0, [_person(1200, 250, 400, 600)])],
            "tracks": [],
            "n_samples": 1,
            "src_w": SRC_W,
            "src_h": SRC_H,
            "two_shot": None,
            "speech": 0.7,
            "motion": {},
            "focus": None,
        }
        monkeypatch.setattr(reframe_plan, "analyse_clip", lambda path, sample_interval=0.5: analysis)

        captured = {}

        def fake_choose_focus(analysis_arg, diarization=None, attention=None):
            captured["diarization"] = diarization
            return {"kind": "person", "boxes": [(0.0, (100, 100, 200, 300))], "reason": "dominant speaker by voice"}

        monkeypatch.setattr(reframe_plan, "choose_focus", fake_choose_focus)
        monkeypatch.setattr(vertical, "_probe_size", lambda path: (SRC_W, SRC_H))
        seen = {}

        def fake_face(samples, src_w, src_h, target_w, target_h, focus_boxes=None):
            seen["focus_boxes"] = focus_boxes
            return "crop=200:300:100:100"

        monkeypatch.setattr(focus_tracking, "build_face_crop_filter", fake_face)
        turns = _turns(("A", 0.0, 1.0))
        frag = vertical.build_vertical_filter("dummy.mp4", mode="single_speaker", diarization=turns)
        assert captured["diarization"] == turns
        assert seen["focus_boxes"] == [(0.0, (100, 100, 200, 300))]
        assert "crop=200:300:100:100" in frag


# ---------------------------------------------------------------------------
# scene attention moves the crop onto the thing being described
# ---------------------------------------------------------------------------


def _attention(target, start, end, boxes, importance=0.8, visibility=1.0):
    return {"scenes": [{
        "start": start, "end": end, "target": target, "boxes": boxes,
        "importance": importance, "visibility": visibility, "source": "keyword",
    }], "notes": []}


class TestAttentionMovesTheCrop:
    def _person_laptop_clip(self):
        person = _person(200, 200, 400, 700)
        laptop = _obj(63, "laptop", 900, 300, 500, 350)
        frames = []
        for i in range(30):  # 0 s .. 14.5 s
            ts = i * 0.5
            frames.append([person, laptop] if 10.0 <= ts <= 14.5 else [person])
        samples = _samples(frames)
        tracks = plan.associate_person_tracks(samples)
        return samples, tracks, person["box"], laptop["box"]

    def test_attention_moves_the_crop_onto_the_object_being_described(self):
        samples, tracks, person_box, laptop_box = self._person_laptop_clip()
        analysis = _analysis(tracks, samples, None, speech=0.7, motion={})
        boxes = [(ts, laptop_box) for ts, _d in samples if 10.0 <= ts <= 14.5]
        focus = plan.choose_focus(analysis, attention=_attention("laptop", 10.0, 15.0, boxes))

        assert focus["kind"] == "person"  # still a follow-crop, only re-centred
        at = {round(ts, 1): box for ts, box in focus["boxes"]}
        assert at[12.0] == laptop_box
        assert at[2.0] == person_box
        assert "attention on 'laptop'" in focus["reason"]

    def test_a_scene_that_is_not_important_enough_leaves_the_crop_alone(self):
        samples, tracks, person_box, laptop_box = self._person_laptop_clip()
        analysis = _analysis(tracks, samples, None, speech=0.7, motion={})
        boxes = [(ts, laptop_box) for ts, _d in samples if 10.0 <= ts <= 14.5]
        focus = plan.choose_focus(analysis, attention=_attention("laptop", 10.0, 15.0, boxes, importance=0.4))
        assert focus["reason"] == "largest person"
        assert all(box == person_box for _ts, box in focus["boxes"])

    def test_a_scene_the_camera_rarely_shows_is_not_followed(self):
        samples, tracks, person_box, laptop_box = self._person_laptop_clip()
        analysis = _analysis(tracks, samples, None, speech=0.7, motion={})
        boxes = [(ts, laptop_box) for ts, _d in samples if 10.0 <= ts <= 14.5]
        focus = plan.choose_focus(analysis, attention=_attention("laptop", 10.0, 15.0, boxes, visibility=0.2))
        assert all(box == person_box for _ts, box in focus["boxes"])

    def test_attention_without_any_person_still_frames_the_object(self):
        laptop = _obj(63, "laptop", 900, 300, 500, 350)
        samples = _samples([[laptop] for _ in range(20)])
        tracks = plan.associate_person_tracks(samples)
        analysis = _analysis(tracks, samples, None, speech=0.7, motion={})
        boxes = [(ts, laptop["box"]) for ts, _d in samples]
        focus = plan.choose_focus(analysis, attention=_attention("laptop", 0.0, 10.0, boxes))
        assert focus is not None
        assert focus["kind"] == "object"
        assert "attention on 'laptop'" in focus["reason"]

    def test_an_attended_person_stays_in_the_follow_crop(self):
        laptop = _obj(63, "laptop", 900, 300, 500, 350)
        person = _person(200, 200, 400, 700)
        samples = _samples([[person, laptop] if 10.0 <= i * 0.5 <= 14.5 else [person] for i in range(30)])
        tracks = plan.associate_person_tracks(samples)
        analysis = _analysis(tracks, samples, None, speech=0.7, motion={})
        boxes = [(ts, laptop["box"]) for ts, _d in samples if 10.0 <= ts <= 14.5]
        mode, focus = plan.decide_layout(analysis, attention=_attention("laptop", 10.0, 15.0, boxes))
        assert mode == "single_speaker"
        assert focus["kind"] == "person"

    def test_attention_composes_with_the_voices(self):
        left, right, laptop = (300, 250, 400, 600), (1200, 250, 400, 600), (700, 700, 400, 300)
        frames = []
        for i in range(20):
            ts = i * 0.5
            dets = [_person(*(left if ts < 5.0 else right))]
            if 6.0 <= ts <= 8.5:
                dets.append(_obj(63, "laptop", *laptop))
            frames.append(dets)
        samples = _samples(frames)
        tracks = plan.associate_person_tracks(samples)
        diarization = _turns(("A", 0.0, 5.0), ("B", 5.0, 10.0))
        analysis = _analysis(tracks, samples, None, speech=0.7, motion={})
        boxes = [(ts, laptop) for ts, _d in samples if 6.0 <= ts <= 8.5]

        focus = plan.choose_focus(analysis, diarization=diarization,
                                  attention=_attention("laptop", 6.0, 9.0, boxes))

        at = {round(ts, 1): box for ts, box in focus["boxes"]}
        assert at[0.0] == left
        assert at[6.5] == laptop
        assert at[9.5] == right
        assert "speaker turns" in focus["reason"] and "attention on 'laptop'" in focus["reason"]


class TestAttentionReachesTheRenderSeams:
    def test_detect_adaptive_layout_forwards_the_attention(self, monkeypatch):
        from services import reframe_plan, smart_crop

        captured = {}

        def fake_resolve(video_path, sample_interval=0.5, diarization=None, attention=None):
            captured["attention"] = attention
            return "single_speaker", None

        monkeypatch.setattr(reframe_plan, "resolve", fake_resolve)
        attention = _attention("laptop", 1.0, 2.0, [(1.0, (0, 0, 10, 10))])
        smart_crop.detect_adaptive_layout("dummy.mp4", attention=attention)
        assert captured["attention"] is attention

    def test_build_vertical_filter_forwards_the_attention(self, monkeypatch):
        import services.focus_tracking as focus_tracking
        from services import reframe_plan, vertical

        analysis = {
            "samples": [(0.0, [_person(1200, 250, 400, 600)])],
            "tracks": [], "n_samples": 1, "src_w": SRC_W, "src_h": SRC_H,
            "two_shot": None, "speech": 0.7, "motion": {}, "focus": None,
        }
        monkeypatch.setattr(reframe_plan, "analyse_clip", lambda path, sample_interval=0.5: analysis)

        captured = {}

        def fake_choose_focus(analysis_arg, diarization=None, attention=None):
            captured["attention"] = attention
            return {"kind": "person", "boxes": [(0.0, (100, 100, 200, 300))], "reason": "largest person"}

        monkeypatch.setattr(reframe_plan, "choose_focus", fake_choose_focus)
        monkeypatch.setattr(vertical, "_probe_size", lambda path: (SRC_W, SRC_H))

        def fake_face(samples, src_w, src_h, target_w, target_h, focus_boxes=None):
            return "crop=200:300:100:100"

        monkeypatch.setattr(focus_tracking, "build_face_crop_filter", fake_face)
        attention = _attention("laptop", 0.0, 1.0, [(0.0, (100, 100, 200, 300))])
        vertical.build_vertical_filter("dummy.mp4", mode="single_speaker", attention=attention)
        assert captured["attention"] is attention


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
