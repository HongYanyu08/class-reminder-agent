import hashlib, json, shutil, subprocess, sys, tempfile, unittest
from copy import deepcopy
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config_edit import plan_config_change, apply_plan, describe_settings
from src.expand import expand
from src.scheduler import generate_reminders, generate_follow_ups, merge_task_states, find_due

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
USER = json.loads((DATA / "user.json").read_text(encoding="utf-8"))
COURSES = json.loads((DATA / "courses.json").read_text(encoding="utf-8"))
TERM = json.loads((DATA / "term.json").read_text(encoding="utf-8"))
EXCEPTIONS = json.loads((DATA / "exceptions.json").read_text(encoding="utf-8"))
PREFS = USER["preferences"]


def plan(text, user=None, courses=None):
    return plan_config_change(text, deepcopy(user or USER), deepcopy(courses or COURSES))


def one_change(text, **kw):
    p = plan(text, **kw)
    assert not p["_issues"], p["_issues"]
    assert len(p["changes"]) == 1, p
    return p["changes"][0]


# ------------------------------------------------------------------ parsing ----
class ParseGlobalTest(unittest.TestCase):
    def test_night_before_time(self):
        ch = one_change("前晚提醒改到20:30")
        self.assertEqual((ch["target"], ch["path"], ch["oldValue"], ch["newValue"]),
                         ("user", ["preferences", "nightBeforeTime"], "21:00", "20:30"))

    def test_night_before_time_dian_form(self):
        ch = one_change("前晚提醒改到8点半")
        self.assertEqual(ch["newValue"], "08:30")

    def test_pre_class_lead(self):
        ch = one_change("课前提前20分钟提醒")
        self.assertEqual((ch["field"], ch["oldValue"], ch["newValue"]),
                         ("preClassLeadMin", 10, 20))

    def test_chinese_numerals(self):
        self.assertEqual(one_change("课前提前二十分钟提醒")["newValue"], 20)

    def test_travel_and_buffer(self):
        self.assertEqual(one_change("路程改成20分钟")["field"], "defaultTravelMin")
        self.assertEqual(one_change("路程改成20分钟")["newValue"], 20)
        self.assertEqual(one_change("缓冲改成5分钟")["field"], "defaultBufferMin")

    def test_rain_and_severe(self):
        self.assertEqual(one_change("雨天提前量改成15分钟")["newValue"], 15)
        ch = one_change("恶劣天气提前量改成30分钟")
        self.assertEqual((ch["field"], ch["newValue"]), ("severeExtraMin", 30))

    def test_followup_grace(self):
        self.assertEqual(one_change("追问宽限改成15分钟")["newValue"], 15)

    def test_quiet_hours(self):
        ch = one_change("静默时间改成23:30到7:30")
        self.assertEqual(ch["newValue"], {"start": "23:30", "end": "07:30"})
        self.assertEqual(ch["oldValue"], {"start": "23:00", "end": "07:00"})

    def test_additive_phrasing(self):
        ch = one_change("雨天多提前5分钟")
        self.assertEqual((ch["oldValue"], ch["newValue"]), (10, 15))


class ValidationTest(unittest.TestCase):
    def test_out_of_range_minutes(self):
        p = plan("课前提前1000分钟")
        self.assertTrue(p["_issues"] and not p["changes"])

    def test_invalid_time(self):
        p = plan("前晚提醒改到25:70")
        self.assertTrue(p["_issues"] and not p["changes"])

    def test_quiet_needs_two_times(self):
        self.assertTrue(plan("静默时间改成23:30")["_issues"])

    def test_vague_value_is_clarify(self):
        p = plan("课前提前一点提醒")
        self.assertTrue(p["_issues"])
        self.assertIn("具体数值", p["_issues"][0])

    def test_global_departure_needs_field_or_course(self):
        p = plan("出门提前30分钟")
        self.assertTrue(p["_issues"])
        self.assertIn("路程", p["_issues"][0])

    def test_apply_refuses_issues(self):
        user, courses = deepcopy(USER), deepcopy(COURSES)
        with self.assertRaises(ValueError):
            apply_plan(user, courses, plan("课前提前1000分钟"))
        self.assertEqual(user, USER)

    def test_unknown_sentence(self):
        p = plan("今天天气怎么样")
        self.assertIsNone(p["kind"])
        self.assertIn("没听懂", p["_issues"][0])
        self.assertIn("课前提前20分钟提醒", p["_issues"][0])


# ------------------------------------------------------------------ toggles ----
class ToggleTest(unittest.TestCase):
    def test_global_off_and_on(self):
        ch = one_change("关掉前晚提醒")
        self.assertEqual(ch["newValue"], ["night_before"])
        user = deepcopy(USER)
        user["preferences"]["disabledTypes"] = ["pre_class"]
        ch = one_change("打开课前提醒", user=user)
        self.assertEqual(ch["newValue"], [])

    def test_toggle_already_in_state_is_note_only(self):
        p = plan("打开课前提醒")  # everything on by default
        self.assertFalse(p["_issues"])
        self.assertEqual(p["changes"], [])
        self.assertTrue(p["notes"])

    def test_followup_toggle(self):
        self.assertEqual(one_change("关掉追问提醒")["newValue"], ["follow_up"])


# ------------------------------------------------------------------- courses ----
class CourseOverrideTest(unittest.TestCase):
    def test_course_departure_lead_by_alias(self):
        ch = one_change("高数出门提前50分钟")
        self.assertEqual((ch["target"], ch["courseId"], ch["oldValue"], ch["newValue"]),
                         ("course", "c_math", 40, 50))

    def test_course_toggle(self):
        ch = one_change("关掉高数的前晚提醒")
        self.assertEqual((ch["path"], ch["newValue"]),
                         (["reminderOverride", "disabledTypes"], ["night_before"]))

    def test_ambiguous_course_alias_clarifies(self):
        courses = deepcopy(COURSES) + [{
            "id": "c_math2", "name": "大学数学", "teacher": "", "weekday": 2,
            "startTime": "08:00", "endTime": "09:40",
            "location": {}, "weeks": {"mode": "all"}, "reminderOverride": {}}]
        p = plan("数学出门提前50分钟", courses=courses)
        self.assertTrue(p["_issues"])
        self.assertEqual(set(p["candidates"]), {"c_math", "c_math2"})

    def test_course_named_global_only_field_clarifies(self):
        p = plan("高数课前提前20分钟")
        self.assertTrue(p["_issues"])
        self.assertIn("全局", p["_issues"][0])


# ---------------------------------------------------------------------- view ----
class ViewTest(unittest.TestCase):
    def test_view_lists_effective_settings(self):
        p = plan("我现在的提醒设置是怎样的")
        self.assertEqual(p["kind"], "view")
        self.assertIn("21:00", p["answer"])
        self.assertIn("课前提前量：10 分钟", p["answer"])
        self.assertIn("《高等数学》：出门提前 40 分钟", p["answer"])

    def test_describe_settings_reflects_disabled(self):
        user = deepcopy(USER)
        user["preferences"]["disabledTypes"] = ["night_before"]
        text = describe_settings(user, COURSES)
        self.assertIn("前晚提醒时间：21:00（已关闭）", text)


# ----------------------------------------------------------------------- CLI ----
def _md5(path):
    return hashlib.md5(path.read_bytes()).hexdigest()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        shutil.copytree(DATA, self.data)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, *argv):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "configure.py"), *argv,
             "--data-dir", str(self.data)],
            capture_output=True, text=True, cwd=ROOT)

    def test_preview_does_not_write(self):
        before = _md5(self.data / "user.json")
        r = self.run_cli("课前提前20分钟提醒")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("预览模式", r.stdout)
        self.assertEqual(_md5(self.data / "user.json"), before)

    def test_apply_writes_and_backs_up(self):
        r = self.run_cli("课前提前20分钟提醒", "--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        user = json.loads((self.data / "user.json").read_text(encoding="utf-8"))
        self.assertEqual(user["preferences"]["preClassLeadMin"], 20)
        backups = list(self.data.glob("user.json.bak-*"))
        self.assertEqual(len(backups), 1)
        old = json.loads(backups[0].read_text(encoding="utf-8"))
        self.assertEqual(old["preferences"]["preClassLeadMin"], 10)

    def test_apply_course_change_touches_only_courses(self):
        before = _md5(self.data / "user.json")
        r = self.run_cli("高数出门提前50分钟", "--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        courses = json.loads((self.data / "courses.json").read_text(encoding="utf-8"))
        math = next(c for c in courses if c["id"] == "c_math")
        self.assertEqual(math["reminderOverride"]["departureLeadMin"], 50)
        self.assertEqual(_md5(self.data / "user.json"), before)
        self.assertEqual(len(list(self.data.glob("courses.json.bak-*"))), 1)

    def test_clarify_refused_with_rc1_even_on_apply(self):
        before = _md5(self.data / "user.json")
        r = self.run_cli("课前提前1000分钟", "--apply")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(_md5(self.data / "user.json"), before)


# ------------------------------------------------------- scheduler integration ----
class SchedulerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.occs = expand(TERM, COURSES, EXCEPTIONS, "2026-09-07", 14)

    def test_global_disabled_type_not_generated(self):
        prefs = dict(PREFS, disabledTypes=["night_before"])
        tasks = generate_reminders(self.occs, COURSES, prefs)
        self.assertTrue(tasks)
        self.assertNotIn("night_before", {t["type"] for t in tasks})
        self.assertIn("pre_class", {t["type"] for t in tasks})

    def test_course_disabled_type_only_for_that_course(self):
        courses = deepcopy(COURSES)
        next(c for c in courses if c["id"] == "c_math") \
            .setdefault("reminderOverride", {})["disabledTypes"] = ["pre_class"]
        tasks = generate_reminders(self.occs, courses, dict(PREFS))
        math_types = {t["type"] for t in tasks if t["occurrenceId"].startswith("c_math@")}
        eng_types = {t["type"] for t in tasks if t["occurrenceId"].startswith("c_eng@")}
        self.assertNotIn("pre_class", math_types)
        self.assertIn("pre_class", eng_types)

    def test_changed_lead_moves_pending_task(self):
        occ = self.occs[0]
        t10 = generate_reminders([occ], COURSES, dict(PREFS, preClassLeadMin=10))
        t20 = generate_reminders([occ], COURSES, dict(PREFS, preClassLeadMin=20))
        pre10 = next(t for t in t10 if t["type"] == "pre_class")
        pre20 = next(t for t in t20 if t["type"] == "pre_class")
        self.assertLess(pre20["scheduledAt"], pre10["scheduledAt"])
        self.assertEqual(
            (  # exactly 10 minutes earlier
                __import__("datetime").datetime.fromisoformat(pre10["scheduledAt"])
                - __import__("datetime").datetime.fromisoformat(pre20["scheduledAt"])
            ).total_seconds(), 600)

    def test_merge_preserves_sent_state_after_config_change(self):
        occ = self.occs[0]
        gen1 = generate_reminders([occ], COURSES, dict(PREFS, preClassLeadMin=10))
        existing = deepcopy(gen1)
        dep = next(t for t in existing if t["type"] == "departure")
        dep["status"] = "sent"
        dep["sentAt"] = "2026-09-06T07:30:00+08:00"
        dep["channel"] = "outbox"
        gen2 = generate_reminders([occ], COURSES, dict(PREFS, preClassLeadMin=20))
        merged = merge_task_states(gen2, existing)
        dep2 = next(t for t in merged if t["type"] == "departure")
        self.assertEqual(dep2["status"], "sent")  # never reset to pending
        self.assertNotIn(dep2, find_due(merged, "2027-01-01T00:00:00+08:00"))
        pre2 = next(t for t in merged if t["type"] == "pre_class")
        self.assertTrue(pre2["scheduledAt"].endswith("07:40:00+08:00")
                        or pre2["scheduledAt"] != next(
                            t for t in gen1 if t["type"] == "pre_class")["scheduledAt"])

    def test_followup_suppressed_when_disabled(self):
        occ = self.occs[0]
        tasks = [{"id": f"r_{occ['id']}_pre_class", "occurrenceId": occ["id"],
                  "type": "pre_class", "scheduledAt": occ["startAt"],
                  "status": "sent", "retryCount": 0,
                  "sentAt": occ["startAt"], "channel": "outbox"}]
        by_id = {occ["id"]: occ}
        now = "2026-09-07T08:11:00+08:00"  # start 08:00 + grace 10 has passed
        self.assertEqual(len(generate_follow_ups(tasks, by_id, now)), 1)
        self.assertEqual(generate_follow_ups(
            tasks, by_id, now, disabled_types=["follow_up"]), [])
        self.assertEqual(generate_follow_ups(
            tasks, by_id, now,
            course_disabled={occ["courseId"]: ["follow_up"]}), [])


if __name__ == "__main__":
    unittest.main()
