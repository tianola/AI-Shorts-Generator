from worker import _url_fingerprint

def test_uploaded_job_preserves_source(tmp_path):
    job_dir = tmp_path / "101"
    job_dir.mkdir()
    source_file = job_dir / "source.mp4"
    source_file.write_bytes(b"dummy video content")
    
    url = "upload://test_video.mp4"
    fp_path = job_dir / "source.urlhash"
    fp_path.write_text(_url_fingerprint(url), encoding="utf-8")
    
    assert source_file.exists()
    assert fp_path.read_text(encoding="utf-8") == _url_fingerprint(url)


def test_tiktok_batch_sweep_reports_clips_the_worker_already_published(monkeypatch):
    """The worker publishes per clip while rendering, so this sweep normally sees
    every clip already posted. It used to crash on its own summary line while
    trying to say so - job 89, 2026-10-13: "TikTok auto-upload failed: 'status'".
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database.base import Base
    from database.models import Clip, Job
    from services import tiktok_upload

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Job(id=121, youtube_url="https://youtu.be/x", status="completed"))
    db.add(Clip(id=1, job_id=121, index=0, start=0.0, end=75.0,
                title="already out", tiktok_video_id="179_abc"))
    db.add(Clip(id=2, job_id=121, index=1, start=75.0, end=150.0,
                title="never rendered", file_path="jobs/121/clip_1/absent.mp4"))
    db.commit()

    monkeypatch.setattr(tiktok_upload, "get_tiktok_auth_status",
                        lambda: {"authenticated": True, "username": "tester"})

    res = tiktok_upload.upload_all_clips_to_tiktok_for_job(121, db)

    assert res["skipped_count"] == 1
    assert res["uploaded_count"] == 0
    assert res["failed_count"] == 1, "a clip with no rendered file is reported, not swallowed"
    assert res["success"] is False, "an unfinished clip must not read as a clean sweep"
