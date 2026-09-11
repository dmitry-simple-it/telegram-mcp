"""Local-time formatting for datetimes returned in MCP tool results.  [fork]

Telegram (Telethon) returns every datetime in UTC. Emitting those timestamps
as-is makes the consuming model read them as local wall-clock time, which
silently shifts every message by the machine's UTC offset.

All datetimes leaving the server are therefore converted to the machine's
local timezone and rendered as ISO-8601 *with* the offset (e.g.
``2026-09-04T15:07:35+03:00``), so the value stays unambiguous and remains
directly comparable to the UTC form.

Set ``TELEGRAM_MCP_TZ`` (an IANA name such as ``Europe/Moscow``) to pin a
timezone explicitly; without it the machine's current local zone is used.
"""

import logging
import os
from datetime import datetime, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_TZ_ENV_VAR = "TELEGRAM_MCP_TZ"

# Cache keyed by the env var value so a changed setting is picked up without a restart.
_cached_tz_name: Optional[str] = None
_cached_tz: Optional[tzinfo] = None
_warned_tz_names: set = set()


def output_timezone() -> tzinfo:
    """Return the timezone used for datetimes in tool results."""
    global _cached_tz_name, _cached_tz

    name = os.environ.get(_TZ_ENV_VAR) or ""
    if _cached_tz is not None and _cached_tz_name == name:
        return _cached_tz

    tz: Optional[tzinfo] = None
    if name:
        try:
            tz = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            if name not in _warned_tz_names:
                _warned_tz_names.add(name)
                logger.warning(
                    "Invalid %s=%r, falling back to the machine's local timezone",
                    _TZ_ENV_VAR,
                    name,
                )

    if tz is None:
        # astimezone() on a naive "now" attaches the machine's current local zone.
        tz = datetime.now().astimezone().tzinfo or timezone.utc

    _cached_tz_name = name
    _cached_tz = tz
    return tz


def to_local(dt: datetime) -> datetime:
    """Convert a datetime to the output timezone, assuming UTC when naive."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(output_timezone())


def to_local_iso(dt: datetime) -> str:
    """Render a datetime as ISO-8601 in the output timezone, offset included."""
    return to_local(dt).isoformat()
