"""Shared pytest setup for import-time Telegram configuration."""

import os

import pytest

os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")
os.environ.setdefault("TELEGRAM_SESSION_NAME", "test_session")


@pytest.fixture
def transcript_cache_dir(tmp_path, monkeypatch):
    """Isolated, per-test SQLite transcript cache directory."""
    d = tmp_path / "transcripts"
    monkeypatch.setenv("TELEGRAM_TRANSCRIPT_CACHE_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def isolated_reply_store(tmp_path, monkeypatch):
    # Never let mocked incoming handlers write to the owner's real subscription DB.
    monkeypatch.setenv("TELEGRAM_REPLY_DB", str(tmp_path / "replies.db"))
