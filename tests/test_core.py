import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.week_calc import teaching_week, effective_weekday, weeks_match
from src.expand import expand
from src.scheduler import generate_reminders, find_due

DATA = Path(__file__).resolve().parent.parent / "data"
term = json.loads((DATA / "term.json").read_text(encoding="utf-8"))
courses = json.loads((DATA / "courses.json").read_text(encoding="utf-8"))
exceptions = json.loads((DATA / "exceptions.json").read_text(encoding="utf-8"))

class WeekCalcTest(unittest.TestCase):
    def test_week_numbers(self):
        self.assertEqual(teaching_week("2026-09-07", term), 1)   # first Monday
        self.assertEqual(teaching_week("2026-09-14", term), 2)
        self.assertIsNone(teaching_week("2026-09-06", term))     # before term
    def test_makeup_day(self):
        self.assertEqual(effective_weekday("2026-10-10", term), 4)  # Saturday acts as Thursday
    def test_weeks_match(self):
        self.assertTrue(weeks_match(3, {"mode": "odd", "startWeek": 1, "endWeek": 17}))
        self.assertFalse(weeks_match(2, {"mode": "odd", "startWeek": 1, "endWeek": 17}))

class ExpandTest(unittest.TestCase):
    def setUp(self):
        self.occs = expand(term, courses, exceptions, "2026-09-07", 14)
    def test_odd_week_math_only(self):
        maths = [o for o in self.occs if o["courseId"] == "c_math"]
        self.assertTrue(all(o["week"] % 2 == 1 for o in maths))  # week 1 in window, week 2 skipped
        self.assertEqual(len(maths), 1)
    def test_cancel_exception(self):
        engs = [o for o in expand(term, courses, exceptions, "2026-09-28", 7) if o["courseId"] == "c_eng"]
        self.assertEqual(engs, [])  # 2026-09-30 English cancelled by exception

class SchedulerTest(unittest.TestCase):
    def test_reminders_and_due(self):
        occs = expand(term, courses, exceptions, "2026-09-07", 14)
        prefs = {"nightBeforeTime": "21:00", "preClassLeadMin": 10, "defaultTravelMin": 15, "defaultBufferMin": 10}
        tasks = generate_reminders(occs, courses, prefs)
        self.assertEqual(len(tasks), len(occs) * 3)
        due = find_due(tasks, "2026-09-13T21:00:00+08:00")
        self.assertTrue(any(t["type"] == "night_before" for t in due))

if __name__ == "__main__":
    unittest.main()
