"""[fork] Housekeeping for the long-lived HTTP daemon: idle self-restart and log rotation.

Idle restart: the launchd singleton degrades over days of uptime (Whisper output
turned to filler after ~31h, 14.10.2026), and a restart is what always cleared it.
Once uptime exceeds MCP_IDLE_RESTART_HOURS and no tool call has been in flight for
MCP_IDLE_RESTART_QUIET_MINUTES, the server stops itself cleanly and launchd
(KeepAlive) starts a fresh one. A call in progress is never cut off.

Log rotation: launchd points stdout/stderr at plain files that nothing rotated
(stderr.log reached 787MB). Every minute, a file over MCP_LOG_MAX_MB is gzipped to
<name>.1.gz (older copies shift up to MCP_LOG_KEEP) and truncated in place.
"""

import asyncio
import fcntl
import gzip
import os
import shutil
import stat
import sys
import time
from typing import Callable, List, Optional

CHECK_INTERVAL_SECONDS = 60


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def idle_restart_after_seconds() -> float:
    """0 disables idle restart."""
    return _env_float("MCP_IDLE_RESTART_HOURS", 24) * 3600


def idle_quiet_seconds() -> float:
    return _env_float("MCP_IDLE_RESTART_QUIET_MINUTES", 15) * 60


def log_max_bytes() -> int:
    """0 disables rotation."""
    return int(_env_float("MCP_LOG_MAX_MB", 50) * 1024 * 1024)


def log_keep() -> int:
    return max(1, int(_env_float("MCP_LOG_KEEP", 3)))


class ActivityTracker:
    """ASGI middleware counting in-flight tool traffic.

    Streamable HTTP clients keep a GET stream open for server notifications for
    the whole session, so only POSTs (initialize, list, tool calls) count as work.
    """

    def __init__(self, app, clock: Callable[[], float] = time.monotonic):
        self.app = app
        self.clock = clock
        self.in_flight = 0
        self.last_activity = clock()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        self.in_flight += 1
        self.last_activity = self.clock()
        try:
            return await self.app(scope, receive, send)
        finally:
            self.in_flight -= 1
            self.last_activity = self.clock()


def should_restart(
    *,
    now: float,
    started_at: float,
    last_activity: float,
    in_flight: int,
    feed_on: bool,
    restart_after: float,
    quiet_for: float,
) -> bool:
    if restart_after <= 0 or in_flight > 0 or feed_on:
        # The incoming feed lives in memory only; a restart would silently stop it.
        return False
    return now - started_at >= restart_after and now - last_activity >= quiet_for


def std_log_files() -> List[str]:
    """Paths of regular files behind stdout/stderr (launchd's log files)."""
    paths = []
    for fd in (1, 2):
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                continue
            path = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024).split(b"\0", 1)[0].decode()
        except (OSError, AttributeError):
            continue
        if path and path not in paths:
            paths.append(path)
    return paths


def rotate_log(path: str, max_bytes: int, keep: int) -> bool:
    """Gzip ``path`` to ``path.1.gz`` and truncate it in place if it exceeds max_bytes.

    Truncating (not renaming) keeps launchd's open descriptors valid; any of our
    own descriptors on the file are rewound so writes don't land at the old offset.
    """
    if max_bytes <= 0:
        return False
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_size <= max_bytes:
        return False
    for i in range(keep - 1, 0, -1):
        older = f"{path}.{i}.gz"
        if os.path.exists(older):
            os.replace(older, f"{path}.{i + 1}.gz")
    tmp = f"{path}.1.gz.tmp"
    with open(path, "rb") as src, gzip.open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
    os.replace(tmp, f"{path}.1.gz")
    os.truncate(path, 0)
    for fd in (1, 2):
        try:
            fst = os.fstat(fd)
            if (fst.st_dev, fst.st_ino) == (st.st_dev, st.st_ino):
                os.lseek(fd, 0, os.SEEK_SET)
        except OSError:
            pass
    return True


async def maintenance_loop(
    server,
    tracker: ActivityTracker,
    feed_on: Callable[[], bool],
    started_at: Optional[float] = None,
) -> None:
    """Rotate logs and, once idle past the uptime threshold, stop ``server``."""
    started_at = tracker.clock() if started_at is None else started_at
    while not server.should_exit:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        for path in std_log_files():
            try:
                if await asyncio.to_thread(rotate_log, path, log_max_bytes(), log_keep()):
                    print(f"Rotated log {path}", file=sys.stderr)
            except OSError as err:
                print(f"Log rotation failed for {path}: {type(err).__name__}", file=sys.stderr)
        if should_restart(
            now=tracker.clock(),
            started_at=started_at,
            last_activity=tracker.last_activity,
            in_flight=tracker.in_flight,
            feed_on=feed_on(),
            restart_after=idle_restart_after_seconds(),
            quiet_for=idle_quiet_seconds(),
        ):
            hours = (tracker.clock() - started_at) / 3600
            print(
                f"Idle restart: up {hours:.1f}h, no tool calls in flight; "
                "exiting for launchd to start a fresh process.",
                file=sys.stderr,
            )
            server.should_exit = True
