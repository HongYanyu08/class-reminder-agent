import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.timetable_import import (parse_text_line, parse_csv_text, parse_json_text,
                                  parse_weeks_string, merge_courses)

ROOT = Path(__file__).resolve().parent.parent
term = json.loads((ROOT / "data" / "term.json").read_text(encoding="utf-8"))

class TextParseTest(unittest.TestCase):
    def test_odd_weeks_and_section_time(self):
        c = parse_text_line("高等数学 周一 第1-2节 1-17周(单) 教三301 王老师 带:课本、习题册", term)
        self.assertEqual(c["weekday"], 1)
        self.assertEqual(c["sections"], {"start": 1, "end": 2})
        self.assertEqual((c["startTime"], c["endTime"]), ("08:00", "09:40"))  # derived from sectionTimes
        self.assertEqual(c["weeks"], {"mode": "odd", "startWeek": 1, "endWeek": 17})
        self.assertEqual(c["location"], {"campus": "", "building": "教三", "room": "301"})
        self.assertEqual(c["teacher"], "王老师")
        self.assertEqual(c["requiredItems"], ["课本", "习题册"])
        self.assertEqual(c["_issues"], [])

    def test_even_weeks(self):
        c = parse_text_line("线性代数 星期二 第3-4节 2-18周(双) 教一102 赵老师", term)
        self.assertEqual(c["weeks"]["mode"], "even")
        self.assertEqual((c["startTime"], c["endTime"]), ("10:00", "11:40"))

    def test_list_weeks_and_missing_teacher_flagged(self):
        c = parse_text_line("体育 周五 第7节 第1,3,5,7,9周 体育馆", term)
        self.assertEqual(c["weeks"], {"mode": "list", "list": [1, 3, 5, 7, 9]})
        self.assertIn("教师待确认", c["_issues"])          # never guessed
        self.assertIn("教室号待确认", c["_issues"])        # 体育馆 has no room number

    def test_explicit_time_and_online(self):
        c = parse_text_line("人工智能导论 周三 19:00-20:40 1-12周 线上 刘老师", term)
        self.assertEqual((c["startTime"], c["endTime"]), ("19:00", "20:40"))
        self.assertEqual(c["location"]["room"], "线上")
        self.assertIn("节次待确认", c["_issues"])

class WeeksStringTest(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(parse_weeks_string("全学期", term)["mode"], "all")
        self.assertEqual(parse_weeks_string("第3周", term), {"mode": "list", "list": [3]})
        self.assertIsNone(parse_weeks_string("不知道", term))

class CsvJsonTest(unittest.TestCase):
    def test_csv(self):
        text = (ROOT / "examples" / "sample_timetable.csv").read_text(encoding="utf-8")
        courses = parse_csv_text(text, term)
        self.assertEqual(len(courses), 3)
        os_course = [c for c in courses if c["name"] == "操作系统"][0]
        self.assertEqual(os_course["weekday"], 2)
        self.assertEqual((os_course["startTime"], os_course["endTime"]), ("10:00", "11:40"))

    def test_json_passthrough(self):
        raw = json.dumps([{"name": "编译原理", "weekday": "周四", "sections": "5-6",
                           "weeks": "1-16周", "location": "教四202", "teacher": "吴老师"}])
        c = parse_json_text(raw, term)[0]
        self.assertEqual(c["weekday"], 4)
        self.assertEqual(c["location"]["room"], "202")

class MergeTest(unittest.TestCase):
    def test_dedupe_against_existing(self):
        existing = json.loads((ROOT / "data" / "courses.json").read_text(encoding="utf-8"))
        new = [parse_text_line("高等数学 周一 第1-2节 1-17周(单) 教三301 王老师", term),
               parse_text_line("线性代数 星期二 第3-4节 2-18周(双) 教一102 赵老师", term)]
        merged, added, dup = merge_courses(existing, new)
        self.assertEqual(len(dup), 1)         # 高数 already exists
        self.assertEqual(len(added), 1)       # only 线性代数 is new
        self.assertNotIn("_issues", merged[-1])  # stored entries are clean

if __name__ == "__main__":
    unittest.main()
