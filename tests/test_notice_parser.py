import json, shutil, subprocess, sys, tempfile, unittest
from copy import deepcopy
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.notice_parser import parse_notice, merge_exceptions
from src.expand import expand

ROOT = Path(__file__).resolve().parent.parent
term = json.loads((ROOT / "data" / "term.json").read_text(encoding="utf-8"))
courses = json.loads((ROOT / "data" / "courses.json").read_text(encoding="utf-8"))
exceptions = json.loads((ROOT / "data" / "exceptions.json").read_text(encoding="utf-8"))

class CancelTest(unittest.TestCase):
    def test_cancel_with_explicit_date(self):
        p = parse_notice("通知：高等数学 2026年9月21日停课一次，请同学们知悉。", courses, term)
        self.assertEqual(p["_issues"], [])
        self.assertEqual(p["courseId"], "c_math")
        self.assertEqual(p["originalDate"], "2026-09-21")  # Monday, week 3 (odd) — a real 高数 slot
        self.assertEqual(p["type"], "cancel")
        self.assertIsNone(p["newDate"])
        self.assertIsNone(p["newLocation"])
        self.assertFalse(p["confirmed"])  # preview only until --apply

    def test_cancel_with_core_name_alias_and_bare_month_day(self):
        p = parse_notice("英语 9月30日停课一次", courses, term)
        self.assertEqual(p["_issues"], [])
        self.assertEqual(p["courseId"], "c_eng")            # 英语 -> 大学英语
        self.assertEqual(p["originalDate"], "2026-09-30")   # year inferred from the term
        self.assertEqual(p["type"], "cancel")

class RescheduleTest(unittest.TestCase):
    def test_reschedule_to_friday_online(self):
        p = parse_notice("大学英语 2026年9月16日（周三）的课改到周五线上上课", courses, term)
        self.assertEqual(p["_issues"], [])
        self.assertEqual(p["courseId"], "c_eng")
        self.assertEqual(p["originalDate"], "2026-09-16")
        self.assertEqual(p["type"], "reschedule")
        self.assertEqual(p["newDate"], "2026-09-18")  # Friday of the same week
        self.assertEqual(p["newLocation"]["room"], "线上")

    def test_reschedule_with_explicit_new_date(self):
        p = parse_notice("程序设计基础原定2026年10月9日（周五）的课改到2026年10月12日（周一）线上上课",
                         courses, term)
        self.assertEqual(p["_issues"], [])
        self.assertEqual(p["type"], "reschedule")
        self.assertEqual(p["newDate"], "2026-10-12")
        self.assertEqual(p["newLocation"]["room"], "线上")

class LocationOnlineTest(unittest.TestCase):
    def test_location_change_with_building_and_room_code(self):
        p = parse_notice("高等数学 2026年9月21日换教室到教三A301", courses, term)
        self.assertEqual(p["_issues"], [])
        self.assertEqual(p["type"], "location_change")
        self.assertEqual(p["newLocation"], {"campus": "", "building": "教三", "room": "A301"})

    def test_location_change_room_code_only_never_invents_building(self):
        p = parse_notice("大学英语 2026年9月16日换教室到A301", courses, term)
        self.assertEqual(p["_issues"], [])
        self.assertEqual(p["type"], "location_change")
        self.assertEqual(p["newLocation"]["room"], "A301")
        self.assertEqual(p["newLocation"]["building"], "")  # notice gave no building — left blank

    def test_online_same_day(self):
        p = parse_notice("大学英语 2026年9月16日改为线上上课", courses, term)
        self.assertEqual(p["_issues"], [])
        self.assertEqual(p["type"], "online")
        self.assertIsNone(p["newDate"])  # same day, only the mode changes
        self.assertEqual(p["newLocation"]["room"], "线上")

class AnchorTest(unittest.TestCase):
    def test_relative_weekday_with_reference_date(self):
        p = parse_notice("本周三大学英语停课一次", courses, term, reference_date="2026-09-14")
        self.assertEqual(p["_issues"], [])
        self.assertEqual(p["originalDate"], "2026-09-16")  # Wednesday of the reference week

    def test_teaching_week_anchor(self):
        p = parse_notice("第3周周一高等数学停课一次", courses, term)
        self.assertEqual(p["_issues"], [])
        self.assertEqual(p["originalDate"], "2026-09-21")  # term week 3 Monday

    def test_unanchorable_relative_date_flagged(self):
        p = parse_notice("高等数学下周三停课一次", courses, term)  # no reference date given
        self.assertIsNone(p["originalDate"])               # never guessed
        self.assertTrue(any("原日期待确认" in i for i in p["_issues"]))

    def test_unknown_course_flagged(self):
        p = parse_notice("大学物理 2026年9月21日停课一次", courses, term)
        self.assertIsNone(p["courseId"])
        self.assertTrue(any("课程待确认" in i for i in p["_issues"]))

    def test_date_that_is_not_a_class_day_flagged(self):
        p = parse_notice("高等数学 2026年9月16日停课一次", courses, term)  # Wed; 高数 is Monday
        self.assertEqual(p["courseId"], "c_math")
        self.assertTrue(any("不是" in i and "上课日" in i for i in p["_issues"]))

    def test_missing_date_flagged(self):
        p = parse_notice("大学英语停课一次", courses, term)
        self.assertIsNone(p["originalDate"])
        self.assertTrue(any("原日期待确认" in i for i in p["_issues"]))

    def test_parsing_never_touches_course_templates(self):
        before = deepcopy(courses)
        for text in ("高等数学 2026年9月21日停课一次",
                     "大学英语 2026年9月16日改到周五线上上课",
                     "高等数学下周三停课一次"):
            parse_notice(text, courses, term)
        self.assertEqual(courses, before)

class MergeApplyTest(unittest.TestCase):
    def test_merge_dedupes_and_marks_confirmed(self):
        p = parse_notice("高等数学 2026年9月21日停课一次", courses, term)
        merged, added, dup, conflicts = merge_exceptions(exceptions, [p])
        self.assertEqual(len(added), 1)
        self.assertTrue(added[0]["confirmed"])
        self.assertNotIn("_issues", added[0])
        merged2, added2, dup2, _ = merge_exceptions(merged, [p])
        self.assertEqual((len(added2), len(dup2)), (0, 1))  # second apply is a duplicate

    def test_merge_conflict_same_day_different_type(self):
        p1 = parse_notice("高等数学 2026年9月21日停课一次", courses, term)
        p2 = parse_notice("高等数学 2026年9月21日改为线上上课", courses, term)
        merged, added, _, _ = merge_exceptions(exceptions, [p1])
        _, added2, _, conflicts = merge_exceptions(merged, [p2])
        self.assertEqual((len(added2), len(conflicts)), (0, 1))  # human must resolve

    def test_cli_preview_writes_nothing_and_apply_hits_one_occurrence_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            for name in ("term.json", "courses.json", "exceptions.json"):
                shutil.copyfile(ROOT / "data" / name, data / name)
            courses_before = (data / "courses.json").read_text(encoding="utf-8")
            exc_before = (data / "exceptions.json").read_text(encoding="utf-8")
            script = str(ROOT / "scripts" / "parse_notice.py")
            notice = "高等数学 2026年9月21日停课一次"

            preview = subprocess.run([sys.executable, script, notice, "--data-dir", tmp],
                                     capture_output=True, text=True)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertIn("预览模式", preview.stdout)
            self.assertEqual((data / "exceptions.json").read_text(encoding="utf-8"), exc_before)

            applied = subprocess.run([sys.executable, script, notice, "--data-dir", tmp, "--apply"],
                                     capture_output=True, text=True)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertIn("已写入", applied.stdout)
            self.assertTrue(list(data.glob("exceptions.json.bak-*")))  # backup made
            self.assertEqual((data / "courses.json").read_text(encoding="utf-8"),
                             courses_before)  # weekly template untouched

            new_exc = json.loads((data / "exceptions.json").read_text(encoding="utf-8"))
            self.assertEqual(len(new_exc), len(exceptions) + 1)
            occs = expand(term, courses, new_exc, "2026-09-07", 42)
            math_dates = [o["date"] for o in occs if o["courseId"] == "c_math"]
            self.assertNotIn("2026-09-21", math_dates)  # only that one occurrence cancelled
            self.assertIn("2026-09-07", math_dates)      # other weeks still meet

    def test_cli_refuses_to_apply_unanchored_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            for name in ("term.json", "courses.json", "exceptions.json"):
                shutil.copyfile(ROOT / "data" / name, data / name)
            exc_before = (data / "exceptions.json").read_text(encoding="utf-8")
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "parse_notice.py"),
                                "高等数学下周三停课一次", "--data-dir", tmp, "--apply"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn("未写入", r.stdout)
            self.assertEqual((data / "exceptions.json").read_text(encoding="utf-8"), exc_before)

if __name__ == "__main__":
    unittest.main()
