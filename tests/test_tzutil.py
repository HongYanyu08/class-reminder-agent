"""src.tzutil — the never-raising timezone fallback (Bug 1 of the
2026-10-28 Windows smoke batch).

On the user's Windows laptop, Python has no system tz database and
no tzdata package, so ZoneInfo("Asia/Shanghai") raised
ZoneInfoNotFoundError and every OS-scheduled tick (which runs
without --from) died on the spot. These tests simulate that host by
patching tzutil.ZoneInfo to raise, and pin the layered fallback:
ZoneInfo -> fixed UTC+8 for Asia/Shanghai -> local timezone.
"""
import sys, unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfoNotFoundError
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import tzutil
from src.scheduler import as_aware


def _no_tzdb(key):
    raise ZoneInfoNotFoundError(f"No time zone found with key {key}")


class TodayInTzFallbackTest(unittest.TestCase):
    def test_shanghai_falls_back_to_fixed_plus8_across_midnight(self):
        # UTC 2026-10-28 17:30 is already 2026-10-29 01:30 in Shanghai.
        now = datetime(2026, 10, 28, 17, 30, tzinfo=timezone.utc)
        with mock.patch.object(tzutil, "ZoneInfo", _no_tzdb):
            self.assertEqual(tzutil.today_in_tz("Asia/Shanghai", now),
                             date(2026, 10, 29))

    def test_shanghai_fallback_same_day_before_midnight(self):
        # UTC 2026-10-28 15:59 -> Shanghai 23:59, still the 28th.
        now = datetime(2026, 10, 28, 15, 59, tzinfo=timezone.utc)
        with mock.patch.object(tzutil, "ZoneInfo", _no_tzdb):
            self.assertEqual(tzutil.today_in_tz("Asia/Shanghai", now),
                             date(2026, 10, 28))

    def test_tzinfo_for_shanghai_is_fixed_plus8_without_tzdb(self):
        with mock.patch.object(tzutil, "ZoneInfo", _no_tzdb):
            tz = tzutil.tzinfo_for("Asia/Shanghai")
        self.assertEqual(
            datetime(2026, 1, 1, tzinfo=tz).utcoffset(), timedelta(hours=8))

    def test_unknown_tz_without_tzdb_falls_back_to_local_no_raise(self):
        now = datetime(2026, 10, 28, 17, 30, tzinfo=timezone.utc)
        with mock.patch.object(tzutil, "ZoneInfo", _no_tzdb):
            value = tzutil.today_in_tz("Mars/Olympus_Mons", now)
        self.assertEqual(value, now.astimezone().date())

    def test_real_zoneinfo_path_agrees_with_the_fallback(self):
        # With a working tz database the answer must be identical.
        now = datetime(2026, 10, 28, 17, 30, tzinfo=timezone.utc)
        self.assertEqual(tzutil.today_in_tz("Asia/Shanghai", now),
                         date(2026, 10, 29))

    def test_no_injected_now_still_returns_a_date(self):
        self.assertIsInstance(tzutil.today_in_tz("Asia/Shanghai"), date)


class AsAwareFallbackTest(unittest.TestCase):
    def test_naive_input_gets_plus8_not_a_crash_without_tzdb(self):
        # scheduler.as_aware had the same bare-ZoneInfo bug class:
        # a naive --now on the Windows laptop would have raised too.
        with mock.patch.object(tzutil, "ZoneInfo", _no_tzdb):
            dt = as_aware("2026-10-28T21:05:00")
        self.assertEqual(dt.utcoffset(), timedelta(hours=8))
        self.assertEqual(dt.isoformat(), "2026-10-28T21:05:00+08:00")

    def test_aware_input_passes_through_untouched(self):
        aware = datetime(2026, 10, 28, 21, 5, tzinfo=timezone.utc)
        self.assertIs(as_aware(aware), aware)


if __name__ == "__main__":
    unittest.main()
