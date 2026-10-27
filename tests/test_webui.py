import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.webui import (
    WebUIError,
    get_state,
    handle_request,
    save_config,
    save_course_override,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")


def _md5(path):
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


class WebUITestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        shutil.copytree(DATA, self.data)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def read_json(self, name):
        return json.loads((self.data / name).read_text(encoding="utf-8"))


class StateTest(WebUITestCase):
    def test_state_structure_is_complete(self):
        state = get_state(self.data, today="2026-09-07")
        for key in (
            "term",
            "user",
            "preferences",
            "courses",
            "occurrences",
            "upcomingDays",
            "reminders",
            "summary",
        ):
            self.assertIn(key, state)
        self.assertEqual(state["term"]["name"], "2026-2027 学年第一学期")
        self.assertEqual(state["summary"]["currentWeek"], 1)
        self.assertEqual(state["summary"]["courseCount"], 3)
        self.assertEqual(state["summary"]["upcoming7DaysCount"], 3)
        self.assertEqual(state["summary"]["upcoming14DaysCount"], 5)
        self.assertEqual(len(state["preferences"]["typeStates"]), 4)
        self.assertTrue(
            all(len(course["typeStates"]) == 4 for course in state["courses"])
        )
        math = next(c for c in state["courses"] if c["id"] == "c_math")
        self.assertEqual(math["reminderOverride"]["departureLeadMin"], 40)
        self.assertEqual(state["reminders"]["statusCounts"], {})

    def test_state_marks_cancelled_class_honestly(self):
        state = get_state(self.data, today="2026-09-28")
        day = next(d for d in state["upcomingDays"] if d["date"] == "2026-09-30")
        self.assertNotIn(
            "大学英语", [occ["courseName"] for occ in day["occurrences"]]
        )
        self.assertTrue(any("已停课" in note for note in day["notes"]))

    def test_handle_request_serves_state_and_html(self):
        status, content_type, payload = handle_request(
            self.data, "GET", "/api/state"
        )
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        self.assertIn("preferences", json.loads(payload))
        status, content_type, payload = handle_request(self.data, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("上课提醒配置中心", payload.decode("utf-8"))


class SaveConfigTest(WebUITestCase):
    def payload(self, **overrides):
        payload = {
            "nightBeforeTime": "20:30",
            "preClassLeadMin": 20,
            "defaultTravelMin": 18,
            "defaultBufferMin": 8,
            "rainExtraMin": 12,
            "severeExtraMin": 25,
            "followUpGraceMin": 15,
            "quietHours": {"start": "23:30", "end": "07:30"},
            "disabledTypes": ["follow_up"],
        }
        payload.update(overrides)
        return payload

    def test_valid_config_writes_and_backs_up(self):
        result = save_config(self.data, self.payload())
        user = self.read_json("user.json")
        self.assertEqual(user["preferences"]["nightBeforeTime"], "20:30")
        self.assertEqual(user["preferences"]["preClassLeadMin"], 20)
        self.assertEqual(user["preferences"]["disabledTypes"], ["follow_up"])
        self.assertTrue(result["backup"])
        backups = list(self.data.glob("user.json.bak-*"))
        self.assertEqual(len(backups), 1)
        old = json.loads(backups[0].read_text(encoding="utf-8"))
        self.assertEqual(old["preferences"]["preClassLeadMin"], 10)

    def test_invalid_value_rejects_whole_order_and_file_is_unchanged(self):
        before = _md5(self.data / "user.json")
        with self.assertRaises(WebUIError) as caught:
            save_config(self.data, self.payload(preClassLeadMin=181))
        self.assertIn("preClassLeadMin", " ".join(caught.exception.errors))
        self.assertEqual(_md5(self.data / "user.json"), before)
        self.assertEqual(list(self.data.glob("user.json.bak-*")), [])

    def test_unknown_field_and_unknown_type_are_rejected(self):
        before = _md5(self.data / "user.json")
        with self.assertRaises(WebUIError) as caught:
            save_config(self.data, self.payload(secretField=True))
        self.assertIn("secretField", " ".join(caught.exception.errors))
        with self.assertRaises(WebUIError) as caught:
            save_config(self.data, self.payload(disabledTypes=["sms"]))
        self.assertIn("sms", " ".join(caught.exception.errors))
        self.assertEqual(_md5(self.data / "user.json"), before)

    def test_bool_is_not_a_minute_value_and_bad_time_is_rejected(self):
        with self.assertRaises(WebUIError):
            save_config(self.data, self.payload(preClassLeadMin=True))
        with self.assertRaises(WebUIError) as caught:
            save_config(self.data, self.payload(nightBeforeTime="24:00"))
        self.assertIn("nightBeforeTime", " ".join(caught.exception.errors))

    def test_handle_request_reports_specific_error_without_writing(self):
        before = _md5(self.data / "user.json")
        body = json.dumps(self.payload(rainExtraMin=-1)).encode("utf-8")
        status, _, payload = handle_request(
            self.data, "POST", "/api/config", body
        )
        self.assertEqual(status, 400)
        response = json.loads(payload)
        self.assertFalse(response["ok"])
        self.assertIn("rainExtraMin", " ".join(response["errors"]))
        self.assertEqual(_md5(self.data / "user.json"), before)


class CourseOverrideTest(WebUITestCase):
    def test_override_writes_and_backs_up(self):
        result = save_course_override(
            self.data,
            {
                "courseId": "c_eng",
                "departureLeadMin": 35,
                "disabledTypes": ["night_before"],
            },
        )
        courses = self.read_json("courses.json")
        english = next(c for c in courses if c["id"] == "c_eng")
        self.assertEqual(english["reminderOverride"]["departureLeadMin"], 35)
        self.assertEqual(
            english["reminderOverride"]["disabledTypes"], ["night_before"]
        )
        self.assertTrue(result["backup"])
        self.assertEqual(len(list(self.data.glob("courses.json.bak-*"))), 1)

    def test_override_can_clear_lead_and_types(self):
        save_course_override(
            self.data,
            {
                "courseId": "c_math",
                "departureLeadMin": None,
                "disabledTypes": [],
            },
        )
        courses = self.read_json("courses.json")
        math = next(c for c in courses if c["id"] == "c_math")
        self.assertNotIn("departureLeadMin", math["reminderOverride"])
        self.assertNotIn("disabledTypes", math["reminderOverride"])

    def test_unknown_course_is_a_clear_404(self):
        before = _md5(self.data / "courses.json")
        with self.assertRaises(WebUIError) as caught:
            save_course_override(
                self.data,
                {"courseId": "c_missing", "departureLeadMin": 30},
            )
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("c_missing", str(caught.exception))
        self.assertEqual(_md5(self.data / "courses.json"), before)

    def test_invalid_override_does_not_write(self):
        before = _md5(self.data / "courses.json")
        with self.assertRaises(WebUIError) as caught:
            save_course_override(
                self.data,
                {
                    "courseId": "c_math",
                    "departureLeadMin": 30,
                    "disabledTypes": ["email"],
                },
            )
        self.assertIn("email", " ".join(caught.exception.errors))
        self.assertEqual(_md5(self.data / "courses.json"), before)


class DemoModeTest(unittest.TestCase):
    def test_demo_data_contains_the_three_sample_courses(self):
        for name in ("高等数学", "大学英语", "程序设计基础"):
            self.assertIn(name, HTML)

    def test_demo_banner_and_local_save_copy_are_present(self):
        self.assertIn("演示模式（示例数据，未连接服务）", HTML)
        self.assertIn("演示模式不会保存到文件", HTML)
        self.assertIn("fetch('/api/state'", HTML)

    def test_sender_tab_renders_in_demo_with_masked_sample(self):
        self.assertIn('data-tab="sender"', HTML)
        self.assertIn("buildDemoSender", HTML)
        self.assertIn("https://qyapi.weixin.qq.com/…a1b2", HTML)
        self.assertIn("个人微信没有官方机器人接口", HTML)
        self.assertIn("fetch('/api/sender'", HTML)
        self.assertIn("演示模式：这是模拟测试", HTML)

    def test_dynamic_text_does_not_use_inner_html(self):
        self.assertNotIn(".innerHTML", HTML)
        self.assertIn("textContent", HTML)


if __name__ == "__main__":
    unittest.main()
