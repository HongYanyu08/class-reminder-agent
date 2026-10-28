"""Timezone helpers that never crash on a host without an IANA tz
database.

Windows Python ships no system tz database, and unless the `tzdata`
pip package happens to be installed, `zoneinfo.ZoneInfo(
"Asia/Shanghai")` raises ZoneInfoNotFoundError. The 2026-10-28
laptop smoke test hit exactly that: run_scheduler.py's --from
default (and scheduler.as_aware for a naive --now) called ZoneInfo
directly, so the OS-scheduled tick died before doing anything.

Every "what is today in the term's timezone / what does this tz
name mean" question in the deterministic core goes through here,
with a layered fallback:

1. zoneinfo.ZoneInfo(name) — full IANA rules when available;
2. for Asia/Shanghai only: a fixed UTC+8 offset (mainland China
   has had no daylight saving since 1991, so +8 is exact, not an
   approximation);
3. the host's local timezone — an approximation, but never a crash.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SHANGHAI_TZ_NAME = "Asia/Shanghai"
_FIXED_SHANGHAI = timezone(timedelta(hours=8), SHANGHAI_TZ_NAME)


def tzinfo_for(tz_name):
    """Resolve a tz name to a tzinfo object, never raising."""
    name = tz_name or SHANGHAI_TZ_NAME
    try:
        return ZoneInfo(name)
    except Exception:
        pass  # no tz database / unknown name: fall through, never crash
    if name == SHANGHAI_TZ_NAME:
        return _FIXED_SHANGHAI
    return datetime.now().astimezone().tzinfo


def today_in_tz(tz_name, now=None):
    """Today's date in tz_name, as a datetime.date.

    `now` is injectable for tests: an aware datetime is converted
    into the target zone first; a naive one is read as wall-clock
    time already in that zone.
    """
    tz = tzinfo_for(tz_name)
    if now is None:
        return datetime.now(tz).date()
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    return now.astimezone(tz).date()
