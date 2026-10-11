"""Tests for the windowless-daemon stdio redirect.

The bug this guards: the old code appended to storage/logs/worker_error.log
forever, and it reached 1.2 GB.
"""
import os
import sys

import pytest

from utils import daemon_streams
from config import settings


@pytest.fixture
def daemon_logs_dir(tmp_path, request):
    storage = tmp_path / "storage"
    original = type(settings).LOGS_DIR
    type(settings).LOGS_DIR = property(lambda self: str(storage / "logs"))
    yield storage / "logs"
    type(settings).LOGS_DIR = original


def test_no_redirect_when_streams_exist(daemon_logs_dir):
    daemon_streams.redirect_daemon_streams("worker")
    assert not daemon_logs_dir.exists()


def test_missing_streams_are_redirected_to_files(daemon_logs_dir, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None, raising=False)
    monkeypatch.setattr(sys, "stderr", None, raising=False)

    daemon_streams.redirect_daemon_streams("worker")
    try:
        print("hello stdout")
        print("hello stderr", file=sys.stderr)
        sys.stderr.flush()
        sys.stdout.flush()
    finally:
        sys.stdout.close()
        sys.stderr.close()

    assert (daemon_logs_dir / "worker.log").read_text(encoding="utf-8").strip() == "hello stdout"
    assert (daemon_logs_dir / "worker_error.log").read_text(encoding="utf-8").strip() == "hello stderr"


def test_oversized_log_is_rotated_instead_of_growing(daemon_logs_dir, monkeypatch):
    daemon_logs_dir.mkdir(parents=True)
    big = daemon_logs_dir / "worker_error.log"
    big.write_text("x" * (daemon_streams.MAX_DAEMON_LOG_BYTES + 10), encoding="utf-8")

    monkeypatch.setattr(sys, "stderr", None, raising=False)
    daemon_streams.redirect_daemon_streams("worker")
    try:
        print("fresh", file=sys.stderr)
        sys.stderr.flush()
    finally:
        sys.stderr.close()

    assert big.read_text(encoding="utf-8").strip() == "fresh"
    assert (daemon_logs_dir / "worker_error.log.1").stat().st_size == daemon_streams.MAX_DAEMON_LOG_BYTES + 10


def test_unwritable_log_dir_falls_back_to_devnull(monkeypatch, tmp_path):
    # A path that cannot be created: a file where a directory must be.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    original = type(settings).LOGS_DIR
    type(settings).LOGS_DIR = property(lambda self: str(blocker / "logs"))
    monkeypatch.setattr(sys, "stderr", None, raising=False)
    try:
        daemon_streams.redirect_daemon_streams("api")
        assert sys.stderr is not None
        print("goes nowhere", file=sys.stderr)
    finally:
        type(settings).LOGS_DIR = original
