"""The API service runs as LocalSystem without the user's WinGet folder on PATH.

It logged "ffmpeg is not on PATH" on every start while FFmpeg sat installed under
the user's WinGet folder. `ensure_media_tools_on_path` finds such a folder and
puts it on the process PATH so later subprocess calls see it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import env_check


def _fake_tools(folder):
    for tool in ("ffmpeg", "ffprobe"):
        name = tool + (".exe" if os.name == "nt" else "")
        path = folder / name
        path.write_text("")
        path.chmod(0o755)


@pytest.mark.skipif(os.name != "nt", reason="the fallback folders are Windows folders")
def test_a_missing_ffmpeg_is_found_in_ffmpeg_dir(monkeypatch, tmp_path):
    _fake_tools(tmp_path)
    monkeypatch.setenv("PATH", r"C:\Windows\system32")
    monkeypatch.setenv("FFMPEG_DIR", str(tmp_path))

    assert env_check.ensure_media_tools_on_path() == str(tmp_path)
    assert os.environ["PATH"].split(os.pathsep)[0] == str(tmp_path)
    assert env_check.missing_media_tools() == []


def test_nothing_changes_when_ffmpeg_is_already_on_path(monkeypatch, tmp_path):
    _fake_tools(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".EXE")

    assert env_check.ensure_media_tools_on_path() == ""
    assert os.environ["PATH"] == str(tmp_path)
