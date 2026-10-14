import asyncio
import gzip
import os

import pytest

from telegram_mcp import maintenance, runner

HOUR = 3600.0


def _restart(**overrides):
    args = dict(
        now=30 * HOUR,
        started_at=0.0,
        last_activity=30 * HOUR - 20 * 60,
        in_flight=0,
        feed_on=False,
        restart_after=24 * HOUR,
        quiet_for=15 * 60,
    )
    args.update(overrides)
    return maintenance.should_restart(**args)


def test_restarts_when_old_and_idle():
    assert _restart()


def test_no_restart_before_uptime_threshold():
    assert not _restart(now=23 * HOUR, last_activity=22 * HOUR)


def test_no_restart_with_call_in_flight():
    assert not _restart(in_flight=1)


def test_no_restart_shortly_after_last_call():
    assert not _restart(last_activity=30 * HOUR - 60)


def test_no_restart_while_incoming_feed_is_on():
    assert not _restart(feed_on=True)


def test_zero_threshold_disables_restart():
    assert not _restart(restart_after=0)


@pytest.mark.asyncio
async def test_tracker_counts_posts_and_ignores_notification_streams():
    clock = iter([0.0, 10.0, 20.0, 30.0]).__next__
    seen = []

    async def app(scope, receive, send):
        seen.append(tracker.in_flight)

    tracker = maintenance.ActivityTracker(app, clock=clock)
    await tracker({"type": "http", "method": "GET"}, None, None)
    assert tracker.last_activity == 0.0
    await tracker({"type": "http", "method": "POST"}, None, None)
    assert seen == [0, 1]
    assert tracker.in_flight == 0
    assert tracker.last_activity == 20.0


def test_rotate_log_gzips_truncates_and_shifts(tmp_path):
    log = tmp_path / "stderr.log"
    (tmp_path / "stderr.log.1.gz").write_bytes(gzip.compress(b"old"))
    log.write_bytes(b"x" * 2000)

    assert maintenance.rotate_log(str(log), max_bytes=1000, keep=3)

    assert log.stat().st_size == 0
    assert gzip.decompress((tmp_path / "stderr.log.1.gz").read_bytes()) == b"x" * 2000
    assert gzip.decompress((tmp_path / "stderr.log.2.gz").read_bytes()) == b"old"


def test_rotate_log_keeps_small_file(tmp_path):
    log = tmp_path / "stderr.log"
    log.write_bytes(b"x" * 10)
    assert not maintenance.rotate_log(str(log), max_bytes=1000, keep=3)
    assert log.read_bytes() == b"x" * 10


def test_rotate_log_drops_oldest_beyond_keep(tmp_path):
    log = tmp_path / "out.log"
    (tmp_path / "out.log.1.gz").write_bytes(gzip.compress(b"one"))
    (tmp_path / "out.log.2.gz").write_bytes(gzip.compress(b"two"))
    log.write_bytes(b"y" * 2000)
    maintenance.rotate_log(str(log), max_bytes=1000, keep=2)
    assert gzip.decompress((tmp_path / "out.log.2.gz").read_bytes()) == b"one"
    assert not os.path.exists(tmp_path / "out.log.3.gz")


class _Server:
    should_exit = False


@pytest.mark.asyncio
async def test_loop_stops_server_when_idle(monkeypatch):
    monkeypatch.setattr(maintenance, "CHECK_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(maintenance, "std_log_files", lambda: [])
    now = [0.0]
    tracker = maintenance.ActivityTracker(None, clock=lambda: now[0])
    server = _Server()
    now[0] = 25 * HOUR
    await asyncio.wait_for(
        maintenance.maintenance_loop(server, tracker, lambda: False, started_at=0.0), 2
    )
    assert server.should_exit


@pytest.mark.asyncio
async def test_loop_keeps_server_while_call_in_flight(monkeypatch):
    monkeypatch.setattr(maintenance, "CHECK_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(maintenance, "std_log_files", lambda: [])
    tracker = maintenance.ActivityTracker(None, clock=lambda: 25 * HOUR)
    tracker.last_activity = 0.0
    tracker.in_flight = 1
    server = _Server()
    task = asyncio.create_task(
        maintenance.maintenance_loop(server, tracker, lambda: False, started_at=0.0)
    )
    await asyncio.sleep(0.05)
    assert not server.should_exit
    task.cancel()


@pytest.mark.asyncio
async def test_http_transport_runs_behind_tracker_with_housekeeping(monkeypatch):
    import uvicorn

    captured = {}

    class _FakeServer:
        def __init__(self, config):
            captured["config"] = config
            self.should_exit = False

        async def serve(self):
            await asyncio.sleep(0)
            captured["loop_running"] = any(
                "maintenance_loop" in repr(t.get_coro()) for t in asyncio.all_tasks()
            )

    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    await runner._serve_streamable_http()

    assert isinstance(captured["config"].app, maintenance.ActivityTracker)
    assert captured["config"].timeout_graceful_shutdown == 5
    assert captured["loop_running"]
