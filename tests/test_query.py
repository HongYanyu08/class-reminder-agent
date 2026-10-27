"""Tests for src/query.py + scripts/ask.py （对话查课表）.

All read-only: engine tests run against the shipped example data loaded
in-memory; CLI tests hash data/*.json before/after to prove nothing moved.
"""
import hashlib, json, subprocess, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.query import answer_query

DATA = ROOT / "data"
TERM = json.loads((DATA / "term.json").read_text(encoding="utf-8"))
COURSES = json.loads((DATA / "courses.json").read_text(encoding="utf-8"))
EXCEPTIONS = json.loads((DATA / "exceptions.json").read_text(encoding="utf-8"))

def ask(text, ref="2026-09-07", exceptions=None, courses=None):
    return answer_query(text, TERM, courses if courses is not None else COURSES,
                        exceptions if exceptions is not None else EXCEPTIONS,
                        reference=ref)

RESCHED = [{"id": "e1", "courseId": "c_eng", "originalDate": "2026-09-16",
            "type": "reschedule", "newDate": "2026-09-18",
            "newLocation": {"campus": "", "building": "", "room": "线上"},
            "confirmed": True}]
LOC_CHANGE = [{"id": "e2", "courseId": "c_math", "originalDate": "2026-09-21",
               "type": "location_change",
               "newLocation": {"campus": "本部", "building": "教一", "room": "101"},
               "confirmed": True}]

class TestDayQueries(unittest.TestCase):
    def test_today(self):
        r = ask("今天有什么课", "2026-09-07")  # Mon, week 1 (odd) -> math
        self.assertEqual(r["kind"], "day")
        self.assertIn("高等数学", r["answer"])
        self.assertIn("教三301", r["answer"])

    def test_tomorrow_no_class(self):
        r = ask("明天有什么课", "2026-09-07")  # Tue Sep 8
        self.assertIn("没有课", r["answer"])
        self.assertEqual(r["occurrences"], [])

    def test_day_after_tomorrow(self):
        r = ask("后天有什么课", "2026-09-07")  # Wed Sep 9 -> English
        self.assertIn("大学英语", r["answer"])

    def test_this_week_weekday(self):
        r = ask("本周三有什么课", "2026-09-14")
        self.assertEqual(r["date"], "2026-09-16")
        self.assertIn("大学英语", r["answer"])

    def test_next_week_weekday(self):
        r = ask("下周一有什么课", "2026-09-14")  # Mon Sep 21, week 3 odd -> math
        self.assertEqual(r["date"], "2026-09-21")
        self.assertIn("高等数学", r["answer"])

    def test_explicit_month_day(self):
        r = ask("9月16日有什么课", "2026-09-07")
        self.assertEqual(r["date"], "2026-09-16")
        self.assertIn("大学英语", r["answer"])

    def test_week_n_weekday(self):
        r = ask("第3周周五有什么课", "2026-09-07")  # Fri Sep 25 -> programming
        self.assertEqual(r["date"], "2026-09-25")
        self.assertIn("程序设计基础", r["answer"])

    def test_holiday_says_holiday(self):
        r = ask("10月1日有什么课", "2026-09-07")
        self.assertIn("假期", r["answer"])
        self.assertEqual(r["occurrences"], [])

    def test_even_week_monday_no_math(self):
        # Math is odd-weeks only: Mon of week 2 (Sep 14) must NOT list it.
        r = ask("9月14日有什么课", "2026-09-07")
        self.assertIn("没有课", r["answer"])

    def test_day_plus_named_course(self):
        self.assertIn("有《高等数学》", ask("明天高等数学有课吗", "2026-09-20")["answer"])
        self.assertIn("没有《高等数学》", ask("明天高等数学有课吗", "2026-09-07")["answer"])

class TestCourseQueries(unittest.TestCase):
    def test_when_full_name(self):
        r = ask("高等数学什么时候上")
        self.assertEqual(r["kind"], "course")
        self.assertIn("周一", r["answer"])
        self.assertIn("08:00", r["answer"])
        self.assertIn("单周", r["answer"])

    def test_when_core_name(self):
        self.assertIn("高等数学", ask("数学什么时候上")["answer"])

    def test_where(self):
        r = ask("英语课在哪上")
        self.assertIn("外语楼205", r["answer"])

    def test_items(self):
        self.assertIn("笔记本电脑", ask("程序设计要带什么")["answer"])
        self.assertIn("课本", ask("数学要带什么")["answer"])

    def test_teacher(self):
        self.assertIn("王老师", ask("数学老师是谁")["answer"])
        self.assertIn("李老师", ask("英语老师是谁")["answer"])

    def test_ambiguous_core_name_clarifies(self):
        extra = COURSES + [{"id": "c_math2", "name": "大学数学", "teacher": "赵老师",
                            "location": {"campus": "本部", "building": "教二", "room": "102"},
                            "weekday": 2, "sections": {"start": 1, "end": 2},
                            "startTime": "08:00", "endTime": "09:40",
                            "weeks": {"mode": "all", "startWeek": 1, "endWeek": 16},
                            "requiredItems": [], "reminderOverride": {}}]
        r = ask("数学什么时候上", courses=extra)
        self.assertEqual(r["kind"], "clarify")
        self.assertIn("高等数学", r["answer"])
        self.assertIn("大学数学", r["answer"])
        self.assertEqual(len(r["candidates"]), 2)

class TestNextAndWeek(unittest.TestCase):
    def test_next_before_class(self):
        r = ask("下节课是什么", "2026-09-07T07:30:00")
        self.assertEqual(r["kind"], "next")
        self.assertIn("高等数学", r["answer"])
        self.assertIn("今天", r["answer"])

    def test_next_after_class_rolls_to_wednesday(self):
        r = ask("我下一节课是什么", "2026-09-07T09:50:00")  # math ended 09:40
        self.assertIn("大学英语", r["answer"])
        self.assertIn("9月9日", r["answer"])

    def test_week_overview_from_wednesday(self):
        r = ask("这周还有什么课", "2026-09-16")
        self.assertEqual(r["kind"], "week")
        self.assertIn("大学英语", r["answer"])       # today
        self.assertIn("程序设计基础", r["answer"])   # Friday
        self.assertNotIn("高等数学", r["answer"])    # week 2 is even + Monday passed

    def test_week_overview_mentions_cancellation(self):
        r = ask("这周还有什么课", "2026-09-28")  # week 4; English cancelled Sep 30
        self.assertIn("9月30日", r["answer"])
        self.assertIn("已停课", r["answer"])

class TestExceptionPresentation(unittest.TestCase):
    def test_cancelled_class_is_mentioned_not_hidden(self):
        r = ask("2026年9月30日有什么课", "2026-09-28")  # sample exception
        self.assertIn("已停课", r["answer"])
        self.assertIn("大学英语", r["answer"])
        self.assertEqual(r["occurrences"], [])

    def test_reschedule_original_date_says_moved(self):
        r = ask("9月16日有什么课", "2026-09-14", exceptions=RESCHED)
        self.assertIn("没有课", r["answer"])
        self.assertIn("已调到9月18日", r["answer"])

    def test_reschedule_new_date_lists_class_online(self):
        r = ask("9月18日有什么课", "2026-09-14", exceptions=RESCHED)
        names = [o["courseName"] for o in r["occurrences"]]
        self.assertIn("大学英语", names)
        self.assertIn("线上", r["answer"])
        self.assertIn("调来的", r["answer"])

    def test_location_change_shows_new_room(self):
        r = ask("9月21日有什么课", "2026-09-14", exceptions=LOC_CHANGE)
        self.assertIn("教一101", r["answer"])
        self.assertIn("已换教室", r["answer"])

    def test_online_exception_shows_online(self):
        ex = [{"id": "e3", "courseId": "c_eng", "originalDate": "2026-09-16",
               "type": "online",
               "newLocation": {"campus": "", "building": "", "room": "线上"},
               "confirmed": True}]
        r = ask("9月16日有什么课", "2026-09-14", exceptions=ex)
        self.assertIn("线上", r["answer"])

class TestClarifyAndUnknown(unittest.TestCase):
    def test_bare_weekday_clarifies(self):
        r = ask("周三有什么课", "2026-09-14")
        self.assertEqual(r["kind"], "clarify")
        self.assertIn("本周三", r["answer"])

    def test_unknown_question(self):
        r = ask("食堂今天吃什么", "2026-09-07")
        self.assertEqual(r["kind"], "unknown")
        self.assertIn("明天有什么课", r["answer"])  # lists example questions

    def test_gibberish_unknown(self):
        self.assertEqual(ask("讲个笑话吧")["kind"], "unknown")

def _data_hashes():
    return {p.name: hashlib.md5(p.read_bytes()).hexdigest()
            for p in sorted(DATA.glob("*.json"))}

class TestCli(unittest.TestCase):
    def _run(self, *argv):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "ask.py"), *argv],
                              capture_output=True, text=True, timeout=60)

    def test_cli_text_answer(self):
        cp = self._run("明天有什么课", "--date", "2026-09-07")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("没有课", cp.stdout)

    def test_cli_json(self):
        cp = self._run("下节课是什么", "--date", "2026-09-07T07:30:00", "--json")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        payload = json.loads(cp.stdout)
        self.assertEqual(payload["kind"], "next")
        self.assertEqual(payload["occurrences"][0]["courseName"], "高等数学")

    def test_cli_never_writes_data(self):
        before = _data_hashes()
        cp = self._run("这周还有什么课", "--date", "2026-09-16")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(before, _data_hashes())

if __name__ == "__main__":
    unittest.main()
