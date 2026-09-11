"""Tests for local-time rendering of datetimes in tool results.  [fork]"""

from datetime import datetime, timezone

import pytest

import sanitize
from telegram_mcp import timefmt


@pytest.fixture(autouse=True)
def _clear_tz_cache():
    timefmt._cached_tz = None
    timefmt._cached_tz_name = None
    yield
    timefmt._cached_tz = None
    timefmt._cached_tz_name = None


def test_utc_datetime_is_rendered_in_configured_timezone(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MCP_TZ", "Europe/Moscow")
    dt = datetime(2026, 9, 4, 12, 7, 35, tzinfo=timezone.utc)
    assert timefmt.to_local_iso(dt) == "2026-09-04T15:07:35+03:00"


def test_naive_datetime_is_treated_as_utc(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MCP_TZ", "Europe/Moscow")
    assert timefmt.to_local_iso(datetime(2026, 9, 4, 12, 7, 35)) == "2026-09-04T15:07:35+03:00"


def test_invalid_timezone_falls_back_to_machine_local(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MCP_TZ", "Not/AZone")
    dt = datetime(2026, 9, 4, 12, 7, 35, tzinfo=timezone.utc)
    expected = dt.astimezone(datetime.now().astimezone().tzinfo).isoformat()
    assert timefmt.to_local_iso(dt) == expected


def test_timezone_change_is_picked_up_without_restart(monkeypatch):
    dt = datetime(2026, 9, 4, 12, 7, 35, tzinfo=timezone.utc)
    monkeypatch.setenv("TELEGRAM_MCP_TZ", "Europe/Moscow")
    assert timefmt.to_local_iso(dt) == "2026-09-04T15:07:35+03:00"
    monkeypatch.setenv("TELEGRAM_MCP_TZ", "Asia/Yekaterinburg")
    assert timefmt.to_local_iso(dt) == "2026-09-04T17:07:35+05:00"


def test_tool_result_payload_carries_local_time(monkeypatch):
    """The serializer behind format_tool_result (get_history & co) must convert too."""
    monkeypatch.setenv("TELEGRAM_MCP_TZ", "Europe/Moscow")
    payload = sanitize.format_tool_result(
        [{"date": datetime(2026, 9, 4, 12, 7, 35, tzinfo=timezone.utc)}]
    )
    assert '"date": "2026-09-04T15:07:35+03:00"' in payload
