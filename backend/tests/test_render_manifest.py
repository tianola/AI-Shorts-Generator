"""Render manifest: what was rendered is recorded, fingerprinted and comparable."""
import hashlib
import json

from config import settings
from services import render_manifest as rm


def _make_files(tmp_path):
    """Create the fixture files once; _build never rewrites them."""
    files = {
        "source": tmp_path / "source_video.mp4",
        "sticker": tmp_path / "sticker.png",
        "sfx": tmp_path / "hit.wav",
        "font": tmp_path / "Font.ttf",
        "timeline": tmp_path / "timeline.json",
    }
    files["source"].write_bytes(b"source-bytes")
    files["sticker"].write_bytes(b"sticker")
    files["sfx"].write_bytes(b"sfx")
    files["font"].write_bytes(b"font")
    files["timeline"].write_text('{"cuts": []}', encoding="utf-8")
    return files


def _build(tmp_path, **overrides):
    files = _make_files(tmp_path) if not (tmp_path / "source_video.mp4").exists() else {
        "source": tmp_path / "source_video.mp4",
        "sticker": tmp_path / "sticker.png",
        "sfx": tmp_path / "hit.wav",
        "font": tmp_path / "Font.ttf",
        "timeline": tmp_path / "timeline.json",
    }
    kwargs = dict(
        job_id="job-1",
        clip_index=2,
        clip_start=10.0,
        clip_end=40.5,
        source_path=str(files["source"]),
        layout="split",
        caption_style="bold",
        caption_motion="pop",
        edit_profile="fast",
        tone="energetic",
        timeline_path=str(files["timeline"]),
        sticker_paths=[str(files["sticker"])],
        sfx_paths=[str(files["sfx"])],
        font_paths=[str(files["font"])],
        filtergraph="[0:v]scale=1080:1920[v]",
        anti_fingerprint={"hflip": False},
        extra={"note": "x"},
    )
    kwargs.update(overrides)
    return rm.build_manifest(**kwargs)


def test_file_sha256_matches_hashlib(tmp_path):
    p = tmp_path / "f.bin"
    data = b"hello world" * 1000
    p.write_bytes(data)
    assert rm.file_sha256(p) == hashlib.sha256(data).hexdigest()
    assert rm.file_sha256(p, chunk=7) == hashlib.sha256(data).hexdigest()


def test_file_sha256_missing_is_none(tmp_path):
    assert rm.file_sha256(tmp_path / "nope.bin") is None


def test_ffmpeg_version_is_none_or_ffmpeg_line():
    v = rm.ffmpeg_version()
    assert v is None or v.startswith("ffmpeg")


def test_build_manifest_has_all_keys_and_is_serialisable(tmp_path):
    m = _build(tmp_path)
    expected = {
        "manifest_version", "renderer_version", "git_commit", "ffmpeg_version",
        "job_id", "clip_index", "window", "source", "layout", "caption", "edit",
        "settings", "loudnorm", "assets", "filtergraph", "anti_fingerprint", "extra",
    }
    assert expected <= set(m)
    assert m["manifest_version"] == 1
    assert m["renderer_version"] == rm.RENDERER_VERSION == "2026.11.0"
    assert m["window"] == {"start": 10.0, "end": 40.5}
    assert set(m["caption"]) == {"style", "motion"}
    assert set(m["edit"]) == {"profile", "tone", "timeline_sha256"}
    assert m["edit"]["timeline_sha256"] == rm.file_sha256(tmp_path / "timeline.json")
    assert set(m["settings"]) == set(rm._SETTINGS_KEYS)
    assert set(m["loudnorm"]) == {"I", "TP", "LRA"}
    assert set(m["assets"]) == {"stickers", "sfx", "fonts"}
    assert m["extra"] == {"note": "x"}
    json.dumps(m)


def test_source_path_never_serialised_in_full(tmp_path):
    m = _build(tmp_path)
    text = json.dumps(m)
    assert str(tmp_path) not in text
    assert m["source"]["name"] == "source_video.mp4"
    assert m["source"]["size"] == len(b"source-bytes")
    assert m["source"]["mtime"] is not None


def test_missing_source_gives_none_values(tmp_path):
    m = _build(tmp_path, source_path=str(tmp_path / "missing.mp4"))
    assert m["source"]["size"] is None
    assert m["source"]["mtime"] is None


def test_assets_keyed_by_basename_sorted_and_deduplicated(tmp_path):
    b = tmp_path / "b"
    a = tmp_path / "a"
    b.mkdir()
    a.mkdir()
    (a / "z.png").write_bytes(b"1")
    (b / "z.png").write_bytes(b"2")  # same basename, different folder
    (a / "m.png").write_bytes(b"3")
    m = rm.build_manifest(
        job_id="j", clip_index=0, clip_start=0, clip_end=1,
        sticker_paths=[str(b / "z.png"), str(a / "m.png"), str(a / "z.png")],
    )
    stickers = m["assets"]["stickers"]
    assert list(stickers) == ["m.png", "z.png"]
    assert stickers["m.png"] == hashlib.sha256(b"3").hexdigest()
    assert stickers["z.png"] == hashlib.sha256(b"2").hexdigest()  # first occurrence wins


def test_same_inputs_same_fingerprint(tmp_path):
    _make_files(tmp_path)
    m1 = _build(tmp_path)
    m2 = _build(tmp_path)
    assert rm.manifest_fingerprint(m1) == rm.manifest_fingerprint(m2)
    assert len(rm.manifest_fingerprint(m1)) == 64


def test_changing_crf_changes_fingerprint_and_diff(tmp_path, monkeypatch):
    _make_files(tmp_path)
    before = _build(tmp_path)
    new_crf = 24 if settings.FFMPEG_CRF == 23 else 23
    monkeypatch.setattr(settings, "FFMPEG_CRF", new_crf)
    after = _build(tmp_path)
    assert rm.manifest_fingerprint(before) != rm.manifest_fingerprint(after)
    assert "settings.FFMPEG_CRF" in rm.diff_manifests(before, after)


def test_changing_asset_bytes_changes_fingerprint(tmp_path):
    _make_files(tmp_path)
    before = _build(tmp_path)
    (tmp_path / "hit.wav").write_bytes(b"different sfx bytes")
    after = _build(tmp_path)
    assert rm.manifest_fingerprint(before) != rm.manifest_fingerprint(after)
    assert rm.diff_manifests(before, after) == ["assets.sfx.hit.wav"]


def test_write_and_load_round_trip(tmp_path):
    _make_files(tmp_path)
    clip_dir = tmp_path / "clip_2"
    m = _build(tmp_path)
    path = rm.write_manifest(clip_dir, m)
    assert path == str(clip_dir / "render_manifest.json")
    assert not [p for p in clip_dir.iterdir() if p.suffix == ".tmp"]
    loaded = rm.load_manifest(clip_dir)
    assert loaded == m
    assert rm.manifest_fingerprint(loaded) == rm.manifest_fingerprint(m)


def test_load_manifest_corrupt_json_is_none(tmp_path):
    (tmp_path / "render_manifest.json").write_text("{not json", encoding="utf-8")
    assert rm.load_manifest(tmp_path) is None


def test_load_manifest_non_dict_is_none(tmp_path):
    (tmp_path / "render_manifest.json").write_text("[]", encoding="utf-8")
    assert rm.load_manifest(tmp_path) is None


def test_load_manifest_missing_is_none(tmp_path):
    assert rm.load_manifest(tmp_path / "absent") is None


def test_diff_identical_is_empty(tmp_path):
    _make_files(tmp_path)
    m = _build(tmp_path)
    assert rm.diff_manifests(m, json.loads(json.dumps(m))) == []


def test_diff_reports_added_and_nested_keys():
    a = {"x": {"y": 1, "z": 2}, "only_a": 1}
    b = {"x": {"y": 1, "z": 3}, "only_b": 1}
    assert rm.diff_manifests(a, b) == ["only_a", "only_b", "x.z"]
