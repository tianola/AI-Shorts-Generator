"""yt-dlp's EJS n-solver needs a JS runtime; yt-dlp only enables deno by default
and this box has node, not deno - so the web client returns "The page needs to be
reloaded" (job 104). The resolver must find node's absolute path even when it is
not on PATH (the SYSTEM service PATH has no node dir), so a hardcoded fallback
location is part of the contract.
"""
import os

import services.youtube as yt


def test_explicit_node_path_wins(monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: p == r"C:\custom\node.exe")
    assert yt._resolve_js_runtime(r"C:\custom\node.exe") == r"C:\custom\node.exe"


def test_falls_back_to_which(monkeypatch):
    monkeypatch.setattr(yt.shutil, "which", lambda name: r"C:\Program Files\nodejs\node.exe")
    # exists() true only for the which() result
    monkeypatch.setattr(os.path, "exists",
                        lambda p: p == r"C:\Program Files\nodejs\node.exe")
    assert yt._resolve_js_runtime("") == r"C:\Program Files\nodejs\node.exe"


def test_falls_back_to_known_location_when_not_on_path(monkeypatch):
    # Simulate the SYSTEM service: node not on PATH (which -> None), but the
    # standard install dir exists on disk.
    monkeypatch.setattr(yt.shutil, "which", lambda name: None)
    monkeypatch.setattr(os.path, "exists",
                        lambda p: p == r"C:\Program Files\nodejs\node.exe")
    assert yt._resolve_js_runtime("") == r"C:\Program Files\nodejs\node.exe"


def test_returns_empty_when_no_runtime_anywhere(monkeypatch):
    monkeypatch.setattr(yt.shutil, "which", lambda name: None)
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    assert yt._resolve_js_runtime("") == ""
