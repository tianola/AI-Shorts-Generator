"""What each scene of a clip is about — the tier that decides WHERE to crop.

Fabricated analyses and word files: no YOLO, no network. The tiers are pure
functions over the sampled detections and the clip's words, and the model calls
are injected, so what is pinned here is the contract the crop consumes — a
scene names a target YOLO actually saw, carries an importance the consumer can
gate on, and brings boxes keyed to the same sampled timestamps the person
tracks use.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import scene_attention as sa

SRC_W, SRC_H = 1920, 1080

LAPTOP_BOX = (900, 300, 500, 350)


def _det(cls, name, box, conf=0.8):
    return {"cls": cls, "name": name, "conf": conf, "box": box}


def _person(box, conf=0.8):
    return _det(0, "person", box, conf)


def _samples(per_frame, interval=0.5):
    return [(i * interval, list(dets)) for i, dets in enumerate(per_frame)]


def _analysis(samples, tracks=None):
    return {
        "samples": samples,
        "tracks": tracks or [],
        "n_samples": len(samples),
        "src_w": SRC_W,
        "src_h": SRC_H,
        "two_shot": None,
        "speech": 0.6,
        "motion": {},
        "focus": None,
    }


def _laptop_frames(n=20):
    return _samples([[_det(63, "laptop", LAPTOP_BOX)] for _ in range(n)])


def _words(tmp_path, *specs):
    payload = [{"word": word, "start": start, "end": end} for word, start, end in specs]
    path = tmp_path / "words.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _build(analysis, words_file, **kwargs):
    """Run attention with the network tiers silenced unless a test asks for one.

    Tier 1 is pure arithmetic over the samples; leaving the real router wired in
    a unit test would make the suite depend on a gateway being up.
    """
    kwargs.setdefault("llm_call", _fake_llm([]))
    return asyncio.run(sa.build_attention(analysis, words_file, **kwargs))


def _fake_llm(scenes):
    """A canned LLM answer, in the shape the prompt asks for."""
    payload = json.dumps({"scenes": scenes})

    async def call(**kwargs):
        return payload

    return call


# ---------------------------------------------------------------------------
# Tier 1 — the local keyword read
# ---------------------------------------------------------------------------

class TestKeywordTier:
    def test_a_word_about_a_visible_object_becomes_a_scene(self, tmp_path):
        samples = _laptop_frames(20)
        words = _words(tmp_path, ("the", 4.0, 4.2), ("laptop", 4.3, 4.9), ("is", 5.0, 5.2))
        attention = _build(_analysis(samples), words)

        assert len(attention["scenes"]) == 1
        scene = attention["scenes"][0]
        assert scene["target"] == "laptop"
        assert scene["source"] == "keyword"
        assert scene["start"] <= 4.0 and scene["end"] >= 4.9
        assert scene["importance"] >= 0.6
        assert scene["boxes"] and scene["boxes"][0][1] == LAPTOP_BOX

    def test_a_synonym_reaches_the_same_object(self, tmp_path):
        samples = _laptop_frames(20)
        words = _words(tmp_path, ("this", 2.0, 2.3), ("computer", 2.4, 3.0))
        attention = _build(_analysis(samples), words)
        assert [s["target"] for s in attention["scenes"]] == ["laptop"]

    def test_an_unmentioned_visible_object_is_not_a_scene(self, tmp_path):
        samples = _laptop_frames(20)
        words = _words(tmp_path, ("nothing", 2.0, 2.4), ("special", 2.5, 3.0), ("here", 3.1, 3.4))
        attention = _build(_analysis(samples), words)
        assert attention["scenes"] == []

    def test_an_object_that_is_never_on_screen_is_not_a_scene(self, tmp_path):
        samples = _samples([[_det(62, "tv", (400, 200, 900, 600))] for _ in range(20)])
        words = _words(tmp_path, ("my", 4.0, 4.2), ("laptop", 4.3, 4.9))
        attention = _build(_analysis(samples), words)
        assert attention["scenes"] == []

    def test_two_mentions_in_one_breath_are_one_scene(self, tmp_path):
        samples = _laptop_frames(20)
        words = _words(tmp_path, ("laptop", 4.0, 4.5), ("laptop", 5.0, 5.5))
        attention = _build(_analysis(samples), words)
        assert len(attention["scenes"]) == 1

    def test_a_hard_cut_ends_a_scene(self, tmp_path):
        """A mention in shot one must not hold the crop on shot two's laptop."""
        samples = _laptop_frames(20)
        person = (200, 200, 400, 700)
        tracks = [{"id": 0, "timeline": [(5.0, person), (5.5, person)]}]  # the shot changes at 5.0
        words = _words(tmp_path, ("laptop", 4.0, 4.5))

        scene = _build(_analysis(samples, tracks), words)["scenes"][0]

        assert scene["end"] <= 5.0
        assert all(ts < 5.0 for ts, _box in scene["boxes"])

    def test_a_missed_detection_inside_a_shot_is_not_a_cut(self, tmp_path):
        """Real footage blinks: the detector loses the object for a sample, and
        that is not a scene change. Reading it as one trims every window into a
        sliver, which is how the tier died on the first smoke clip."""
        laptop = _det(63, "laptop", LAPTOP_BOX)
        frames = []
        for i in range(20):
            ts = i * 0.5
            frames.append([] if ts == 5.5 else [laptop])
        words = _words(tmp_path, ("laptop", 4.5, 5.0))

        scene = _build(_analysis(_samples(frames)), words)["scenes"][0]

        assert scene["end"] - scene["start"] >= 4.0
        assert len(scene["boxes"]) >= 7


# ---------------------------------------------------------------------------
# Tier 2 — one text-LLM call per clip, soft-failing into Tier 1
# ---------------------------------------------------------------------------

class TestLlmTier:
    def test_the_llm_names_a_scene_the_keywords_missed(self, tmp_path):
        samples = _laptop_frames(20)
        words = _words(tmp_path, ("this", 0.0, 0.3), ("is", 0.3, 0.6), ("great", 0.6, 1.0))
        attention = _build(_analysis(samples), words,
                           llm_call=_fake_llm([{"i": 0, "target": "laptop", "importance": 0.85}]))

        scene = attention["scenes"][0]
        assert scene["target"] == "laptop"
        assert scene["source"] == "llm"
        assert scene["importance"] == 0.85
        assert scene["boxes"][0][1] == LAPTOP_BOX

    def test_the_llm_cannot_invent_an_object_that_is_not_on_screen(self, tmp_path):
        samples = _laptop_frames(20)
        words = _words(tmp_path, ("this", 0.0, 0.3), ("is", 0.3, 0.6))
        attention = _build(_analysis(samples), words,
                           llm_call=_fake_llm([{"i": 0, "target": "pizza", "importance": 0.9}]))
        assert attention["scenes"] == []

    def test_invalid_json_keeps_the_keyword_scenes(self, tmp_path):
        samples = _laptop_frames(20)
        words = _words(tmp_path, ("laptop", 4.0, 4.5))

        async def garbage(**kwargs):
            return "sorry, I cannot answer in JSON"

        attention = _build(_analysis(samples), words, llm_call=garbage)

        assert [s["target"] for s in attention["scenes"]] == ["laptop"]
        assert any("llm" in note.lower() for note in attention["notes"])

    def test_a_broken_llm_never_breaks_attention(self, tmp_path):
        samples = _laptop_frames(20)
        words = _words(tmp_path, ("laptop", 4.0, 4.5))

        async def explode(**kwargs):
            raise RuntimeError("all LLM backends failed")

        attention = _build(_analysis(samples), words, llm_call=explode)
        assert [s["target"] for s in attention["scenes"]] == ["laptop"]

    def test_exactly_one_llm_call_per_clip(self, tmp_path):
        samples = _laptop_frames(20)
        words = _words(tmp_path, ("this", 0.0, 0.3), ("laptop", 0.4, 1.0))
        calls = []

        async def counting(**kwargs):
            calls.append(kwargs)
            return json.dumps({"scenes": []})

        _build(_analysis(samples), words, llm_call=counting)
        assert len(calls) == 1
        assert "laptop" in calls[0]["prompt"]

    def test_the_llm_is_not_asked_when_nothing_object_like_is_visible(self, tmp_path):
        person = (200, 200, 400, 700)
        samples = _samples([[_person(person)] for _ in range(20)])
        words = _words(tmp_path, ("hello", 0.0, 0.5))
        calls = []

        async def counting(**kwargs):
            calls.append(kwargs)
            return json.dumps({"scenes": []})

        attention = _build(_analysis(samples), words, llm_call=counting)
        assert calls == []
        assert attention["scenes"] == []

    def test_the_llm_answer_can_lift_a_weak_keyword_scene(self, tmp_path):
        """The word was there and the model agrees it matters — keep the higher claim."""
        samples = _laptop_frames(20)
        words = _words(tmp_path, ("laptop", 0.0, 0.5))
        attention = _build(_analysis(samples), words,
                           llm_call=_fake_llm([{"i": 0, "target": "laptop", "importance": 0.95}]))
        assert len(attention["scenes"]) == 1
        assert attention["scenes"][0]["importance"] == 0.95
        assert attention["scenes"][0]["source"] == "keyword+llm"


# ---------------------------------------------------------------------------
# Tier 3 — vision, off unless asked for
# ---------------------------------------------------------------------------

class TestVisionTier:
    def test_the_vision_tier_is_off_by_default(self, tmp_path):
        samples = _laptop_frames(20)
        words = _words(tmp_path, ("this", 0.0, 0.3), ("is", 0.3, 0.6))
        seen = []

        def vision(frame, prompt):
            seen.append(frame)
            return "laptop"

        _build(_analysis(samples), words, llm_call=_fake_llm([]), vision_call=vision)
        assert seen == []

    def test_an_enabled_vision_model_names_what_it_sees(self, tmp_path, monkeypatch):
        from config import settings

        monkeypatch.setattr(settings, "SCENE_ATTENTION_VLM", True)
        monkeypatch.setattr(settings, "SCENE_ATTENTION_VLM_MODEL", "test-vision-model")

        samples = _laptop_frames(20)
        words = _words(tmp_path, ("look", 0.0, 0.3), ("at", 0.3, 0.5), ("this", 0.5, 0.8))
        analysis = _analysis(samples)
        analysis["video_path"] = "unused.mp4"  # the injected vision call never opens it

        def vision(frame, prompt):
            return "a laptop"

        attention = _build(analysis, words, llm_call=_fake_llm([]), vision_call=vision,
                           frame_source=lambda path, ts: "frame")

        scenes = attention["scenes"]
        assert scenes
        assert all(scene["target"] == "laptop" for scene in scenes)
        assert all(scene["source"] == "vlm" for scene in scenes)
        assert scenes[0]["boxes"][0][1] == LAPTOP_BOX

    def test_a_vision_answer_outside_the_inventory_is_dropped(self, tmp_path, monkeypatch):
        from config import settings

        monkeypatch.setattr(settings, "SCENE_ATTENTION_VLM", True)
        monkeypatch.setattr(settings, "SCENE_ATTENTION_VLM_MODEL", "test-vision-model")

        samples = _laptop_frames(20)
        words = _words(tmp_path, ("look", 0.0, 0.3), ("at", 0.3, 0.5))
        analysis = _analysis(samples)
        analysis["video_path"] = "unused.mp4"

        attention = _build(analysis, words, llm_call=_fake_llm([]),
                           vision_call=lambda frame, prompt: "a giraffe",
                           frame_source=lambda path, ts: "frame")
        assert attention["scenes"] == []


# ---------------------------------------------------------------------------
# the shape every consumer relies on
# ---------------------------------------------------------------------------

class TestAttentionShape:
    def test_a_clip_with_no_words_yields_an_empty_but_usable_attention(self):
        attention = _build(_analysis(_laptop_frames(20)), "missing-words.json",
                           llm_call=_fake_llm([]))
        assert attention["scenes"] == []
        assert isinstance(attention["notes"], list)

    def test_scenes_are_ordered_and_clip_relative(self, tmp_path):
        samples = _samples([
            [_det(63, "laptop", LAPTOP_BOX)] if 4.0 <= i * 0.5 <= 6.0
            else [_det(62, "tv", (400, 200, 900, 600))]
            for i in range(20)
        ])
        words = _words(tmp_path, ("laptop", 4.2, 4.8), ("television", 8.2, 8.8))
        attention = _build(_analysis(samples), words)

        targets = [s["target"] for s in attention["scenes"]]
        assert targets == ["laptop", "tv"]
        assert all(s["start"] >= 0.0 for s in attention["scenes"])


# ---------------------------------------------------------------------------
# the object inventory the tiers match against
# ---------------------------------------------------------------------------

class TestFocusClassInventory:
    def test_the_default_inventory_covers_the_common_demo_subjects(self):
        from services.focus_tracking import DEFAULT_FOCUS_CLASSES

        names = set(DEFAULT_FOCUS_CLASSES.values())
        assert {"car", "cat", "dog", "remote", "keyboard", "book"} <= names

    def test_no_override_means_the_default_inventory(self):
        from services.focus_tracking import DEFAULT_FOCUS_CLASSES, resolve_focus_classes

        assert resolve_focus_classes("") == DEFAULT_FOCUS_CLASSES

    def test_an_override_names_classes_and_drops_unknown_ones(self):
        from services.focus_tracking import resolve_focus_classes

        resolved = resolve_focus_classes("laptop, cell phone,unheard-of")
        assert set(resolved.values()) == {"person", "laptop", "cell phone"}

    def test_person_survives_any_override(self):
        """Without it every track, and so every layout decision, stops working."""
        from services.focus_tracking import resolve_focus_classes

        assert resolve_focus_classes("tv")[0] == "person"
