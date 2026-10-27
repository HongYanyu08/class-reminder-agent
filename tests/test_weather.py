import json, os, shutil, subprocess, sys, tempfile, unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.weather import (WeatherProvider, WeatherError, build_weather_provider,
                         classify_hour, extra_minutes, parse_forecast, level_in_hours)
from src.scheduler import generate_reminders, merge_task_states, compose_message

ROOT = Path(__file__).resolve().parent.parent
TZ8 = "+08:00"
OCC = {"id": "c_math@2026-09-14", "courseId": "c_math", "courseName": "高等数学",
       "startAt": "2026-09-14T08:00:00+08:00", "endAt": "2026-09-14T09:40:00+08:00",
       "startTime": "08:00", "endTime": "09:40",
       "location": {"campus": "本部", "building": "教三", "room": "301"},
       "requiredItems": ["课本"], "status": "scheduled"}
COURSES = [{"id": "c_math", "name": "高等数学", "reminderOverride": {}}]
PREFS = {"nightBeforeTime": "21:00", "preClassLeadMin": 10,
         "defaultTravelMin": 15, "defaultBufferMin": 10,
         "rainExtraMin": 10, "severeExtraMin": 20}
NOW = datetime.fromisoformat("2026-09-13T12:00:00+08:00")
# base departure for OCC: 08:00 - (15 travel + 10 buffer) = 07:35 on 2026-09-14

def payload(code_at_07=61, prob_at_07=20):
    """Minimal Open-Meteo-shaped response covering 2026-09-14 06:00-08:00."""
    return {"hourly": {
        "time": ["2026-09-14T06:00", "2026-09-14T07:00", "2026-09-14T08:00"],
        "weather_code": [0, code_at_07, 3],
        "precipitation_probability": [0, prob_at_07, 10]}}

def counting_transport(body, calls, fail=False):
    def transport(url, timeout):
        calls.append(url)
        if fail:
            raise RuntimeError("network down")
        return 200, body
    return transport

def by_type(tasks):
    return {t["type"]: t for t in tasks}

# ------------------------------------------------------- pure judgement ----
class ClassifyTest(unittest.TestCase):
    def test_precip_codes(self):
        self.assertEqual(classify_hour(61, 20), "rain")    # slight rain, low prob
        self.assertEqual(classify_hour(63, None), "rain")
        self.assertEqual(classify_hour(71, None), "rain")  # snowfall counts as precip
        self.assertEqual(classify_hour(0, 0), "clear")
        self.assertEqual(classify_hour(3, None), "clear")  # overcast is not rain

    def test_severe_codes(self):
        for code in (65, 67, 75, 82, 86, 95, 96, 99):
            self.assertEqual(classify_hour(code, 0), "severe", code)

    def test_probability_threshold_boundary(self):
        self.assertEqual(classify_hour(0, 49), "clear")
        self.assertEqual(classify_hour(0, 50), "rain")   # >= threshold
        self.assertEqual(classify_hour(0, 80, rain_probability_threshold=90), "clear")

    def test_no_signal_is_unknown_not_clear(self):
        self.assertIsNone(classify_hour(None, None))
        self.assertEqual(classify_hour(None, 80), "rain")
        self.assertEqual(classify_hour(None, 10), "clear")

class ExtraMinutesTest(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(extra_minutes("rain", PREFS), (10, "降雨，已提前 10 分钟"))
        self.assertEqual(extra_minutes("severe", PREFS), (20, "恶劣天气，已提前 20 分钟"))
        self.assertEqual(extra_minutes("clear", PREFS), (0, None))
        self.assertEqual(extra_minutes(None, PREFS), (0, None))

    def test_amounts_come_from_prefs(self):
        self.assertEqual(extra_minutes("rain", {"rainExtraMin": 15})[0], 15)
        self.assertEqual(extra_minutes("severe", {})[0], 20)  # documented default

# ------------------------------------------------------------ parsing ----
class ParseForecastTest(unittest.TestCase):
    def test_normalizes_entries(self):
        hours = parse_forecast(payload())
        self.assertEqual(len(hours), 3)
        self.assertEqual(hours[1], {"time": "2026-09-14T07:00", "weather_code": 61,
                                    "precipitation_probability": 20})

    def test_missing_probability_array_becomes_none(self):
        p = payload()
        del p["hourly"]["precipitation_probability"]
        hours = parse_forecast(p)
        self.assertIsNone(hours[0]["precipitation_probability"])
        self.assertEqual(hours[1]["weather_code"], 61)

    def test_garbage_payload_raises_not_clear(self):
        with self.assertRaises(WeatherError):
            parse_forecast({"hourly": {"weather_code": [0]}})
        with self.assertRaises(WeatherError):
            parse_forecast({"nope": 1})

    def test_level_in_hours(self):
        hours = parse_forecast(payload())
        when = datetime.fromisoformat("2026-09-14T07:35:00+08:00")
        self.assertEqual(level_in_hours(hours, when), "rain")  # 07:00 hour slot
        far = datetime.fromisoformat("2026-10-01T07:00:00+08:00")
        self.assertIsNone(level_in_hours(hours, far))  # outside coverage

# ------------------------------------------------------------ provider ----
class ProviderTest(unittest.TestCase):
    def provider(self, tmp, transport, **kw):
        return WeatherProvider(lat=30.0, lon=120.0,
                               cache_path=Path(tmp) / "weather_cache.json",
                               transport=transport, **kw)

    def test_fetch_via_injected_transport_and_url(self):
        calls = []
        p = self.provider(tempfile.mkdtemp(), counting_transport(json.dumps(payload()), calls))
        level = p.level_at(datetime.fromisoformat("2026-09-14T07:35:00+08:00"), NOW)
        self.assertEqual(level, "rain")
        self.assertEqual(p.status, "ok")
        self.assertEqual(len(calls), 1)
        self.assertIn("latitude=30.0", calls[0])
        self.assertIn("weather_code", calls[0])

    def test_memoized_one_fetch_per_instance(self):
        calls = []
        p = self.provider(tempfile.mkdtemp(), counting_transport(json.dumps(payload()), calls))
        for hour in ("06:10", "07:35", "08:00"):
            p.level_at(datetime.fromisoformat(f"2026-09-14T{hour}:00+08:00"), NOW)
        self.assertEqual(len(calls), 1)

    def test_cache_ttl_fresh_then_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            body = json.dumps(payload())
            self.provider(tmp, counting_transport(body, calls)).level_at(
                datetime.fromisoformat("2026-09-14T07:35:00+08:00"), NOW)
            self.assertEqual(len(calls), 1)  # fetched + cached
            later = datetime.fromisoformat("2026-09-13T14:00:00+08:00")  # +2h < TTL 3h
            p2 = self.provider(tmp, counting_transport(body, calls))
            self.assertEqual(p2.level_at(datetime.fromisoformat(
                "2026-09-14T07:35:00+08:00"), later), "rain")
            self.assertEqual(len(calls), 1)  # served from fresh cache
            much_later = datetime.fromisoformat("2026-09-13T16:00:01+08:00")  # +4h > TTL
            p3 = self.provider(tmp, counting_transport(body, calls))
            p3.level_at(datetime.fromisoformat("2026-09-14T07:35:00+08:00"), much_later)
            self.assertEqual(len(calls), 2)  # expired -> refetch

    def test_fetch_failure_falls_back_to_stale_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "weather_cache.json"
            cache.write_text(json.dumps({
                "fetchedAt": "2026-09-12T00:00:00+08:00",  # way past TTL
                "hourly": parse_forecast(payload())}), encoding="utf-8")
            p = self.provider(tmp, counting_transport("", [], fail=True))
            level = p.level_at(datetime.fromisoformat("2026-09-14T07:35:00+08:00"), NOW)
            self.assertEqual(level, "rain")       # stale data still used
            self.assertEqual(p.status, "stale")   # ...but honestly labelled

    def test_fetch_failure_without_cache_is_unavailable_not_a_crash(self):
        p = self.provider(tempfile.mkdtemp(), counting_transport("", [], fail=True))
        self.assertIsNone(p.level_at(datetime.fromisoformat("2026-09-14T07:35:00+08:00"), NOW))
        self.assertEqual(p.status, "unavailable")

    def test_http_error_status_is_failure(self):
        p = self.provider(tempfile.mkdtemp(), lambda url, timeout: (500, "boom"))
        self.assertIsNone(p.level_at(datetime.fromisoformat("2026-09-14T07:35:00+08:00"), NOW))
        self.assertEqual(p.status, "unavailable")

    def test_disabled_without_coordinates(self):
        p = build_weather_provider(env={}, cache_path=None)
        self.assertFalse(p.enabled)
        self.assertIsNone(p.level_at(datetime.fromisoformat("2026-09-14T07:35:00+08:00"), NOW))
        self.assertEqual(p.status, "disabled")

    def test_invalid_coordinates_disable(self):
        p = build_weather_provider(env={"WEATHER_LAT": "north", "WEATHER_LON": "120"},
                                    cache_path=None)
        self.assertFalse(p.enabled)
        p2 = build_weather_provider(env={"WEATHER_LAT": "30", "WEATHER_LON": "999"},
                                     cache_path=None)
        self.assertFalse(p2.enabled)

    def test_dry_run_mode_reads_cache_but_never_fetches(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            p = WeatherProvider(lat=30.0, lon=120.0, allow_fetch=False,
                                cache_path=Path(tmp) / "weather_cache.json",
                                transport=counting_transport(json.dumps(payload()), calls))
            self.assertIsNone(p.level_at(datetime.fromisoformat(
                "2026-09-14T07:35:00+08:00"), NOW))
            self.assertEqual(calls, [])  # no request, and therefore no cache write
            self.assertFalse((Path(tmp) / "weather_cache.json").exists())

# ------------------------------------------------- scheduler integration ----
class SchedulerWeatherTest(unittest.TestCase):
    def gen(self, level):
        weather_at = (lambda dt: level) if level is not None else None
        return by_type(generate_reminders([OCC], COURSES, PREFS, weather_at=weather_at))

    def test_baseline_without_weather(self):
        tasks = self.gen(None)
        self.assertEqual(tasks["departure"]["scheduledAt"], f"2026-09-14T07:35:00{TZ8}")
        self.assertNotIn("weatherExtraMin", tasks["departure"])

    def test_clear_adds_nothing(self):
        tasks = self.gen("clear")
        self.assertEqual(tasks["departure"]["scheduledAt"], f"2026-09-14T07:35:00{TZ8}")
        self.assertNotIn("weatherNote", tasks["departure"])

    def test_rain_moves_only_departure(self):
        tasks = self.gen("rain")
        self.assertEqual(tasks["departure"]["scheduledAt"], f"2026-09-14T07:25:00{TZ8}")
        self.assertEqual(tasks["departure"]["weatherExtraMin"], 10)
        self.assertEqual(tasks["departure"]["weatherNote"], "降雨，已提前 10 分钟")
        self.assertEqual(tasks["night_before"]["scheduledAt"], f"2026-09-13T21:00:00{TZ8}")
        self.assertEqual(tasks["pre_class"]["scheduledAt"], f"2026-09-14T07:50:00{TZ8}")
        self.assertNotIn("weatherNote", tasks["pre_class"])

    def test_severe_moves_departure_twenty(self):
        tasks = self.gen("severe")
        self.assertEqual(tasks["departure"]["scheduledAt"], f"2026-09-14T07:15:00{TZ8}")
        self.assertEqual(tasks["departure"]["weatherExtraMin"], 20)

    def test_unavailable_level_adds_nothing(self):
        tasks = generate_reminders([OCC], COURSES, PREFS, weather_at=lambda dt: None)
        self.assertEqual(by_type(tasks)["departure"]["scheduledAt"],
                         f"2026-09-14T07:35:00{TZ8}")

    def test_idempotent_across_ticks_and_merge(self):
        rain = lambda dt: "rain"
        first = generate_reminders([OCC], COURSES, PREFS, weather_at=rain)
        second = generate_reminders([OCC], COURSES, PREFS, weather_at=rain)
        self.assertEqual([t["scheduledAt"] for t in first],
                         [t["scheduledAt"] for t in second])  # no stacking
        merged = merge_task_states(second, first)  # next tick merges stored state
        dep = by_type(merged)["departure"]
        self.assertEqual(dep["scheduledAt"], f"2026-09-14T07:25:00{TZ8}")
        self.assertEqual(dep["weatherExtraMin"], 10)  # still 10, not 20

    def test_weather_evaluated_at_base_departure_moment(self):
        seen = []
        def weather_at(dt):
            seen.append(dt)
            return "clear"
        generate_reminders([OCC], COURSES, PREFS, weather_at=weather_at)
        self.assertEqual(seen, [datetime.fromisoformat(f"2026-09-14T07:35:00{TZ8}")])

    def test_compose_message_includes_note_only_for_weathered_departure(self):
        tasks = self.gen("rain")
        msg = compose_message(OCC, "departure", tasks["departure"])
        self.assertIn("天气：降雨，已提前 10 分钟", msg)
        plain = compose_message(OCC, "departure", by_type(
            generate_reminders([OCC], COURSES, PREFS))["departure"])
        self.assertNotIn("天气", plain)
        fu = compose_message(OCC, "pre_class", tasks["departure"])
        self.assertNotIn("天气", fu)  # note never leaks into other types

# ------------------------------------------------------------------- CLI ----
class CliNoCoordinatesTest(unittest.TestCase):
    def test_tick_without_coordinates_behaves_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            shutil.copytree(ROOT / "data", data)
            env = {k: v for k, v in os.environ.items() if not k.startswith("WEATHER_")}
            proc = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "run_scheduler.py"),
                 "--tick", "--now", "2026-09-13T21:00:00+08:00",
                 "--data-dir", str(data)],
                capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            tasks = json.loads((data / "reminders.json").read_text(encoding="utf-8"))
            self.assertTrue(tasks)
            self.assertFalse(any("weatherExtraMin" in t for t in tasks))
            self.assertFalse((data / "weather_cache.json").exists())

if __name__ == "__main__":
    unittest.main()
