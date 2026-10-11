import os
import pytest
from pathlib import Path
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
