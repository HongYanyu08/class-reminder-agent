import json, shutil, subprocess, sys, tempfile, unittest
from copy import deepcopy
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.ack import parse_reply, plan_reply, apply_plan
from src.scheduler import (generate_reminders, merge_task_states, find_due,
                           generate_follow_ups, reactivate_snoozed)

ROOT = Path(__file__).resolve().parent.parent
COURSES = [{"id": "c_math", "name": "高等数学"}, {"id": "c_eng", "name": "大学英语"}]
COURSE_BY_ID = {c["id"]: c for c in COURSES}
OCC_MATH = {"id": "c_math@2026-09-14", "courseId": "c_math", "courseName": "高等数学",
            "date": "2026-09-14", "startAt": "2026-09-14T08:00:00+08:00",
            "endAt": "2026-09-14T09:40:00+08:00", "startTime": "08:00",
            "status": "scheduled"}
OCC_ENG = {"id": "c_eng@2026-09-16", "courseId": "c_eng", "courseName": "大学英语",
           "date": "2026-09-16", "startAt": "2026-09-16T10:00:00+08:00",
           "endAt": "2026-09-16T11:40:00+08:00", "startTime": "10:00",
           "status": "scheduled"}
OCC_BY_ID = {o["id"]: o for o in (OCC_MATH, OCC_ENG)}
NOW = "2026-09-16T09:55:00+08:00"

def task(occ, typ, status, sent_at=None, scheduled=None):
    t = {"id": f"r_{occ['id']}_{typ}", "occurrenceId": occ["id"], "type": typ,
         "scheduledAt": scheduled or sent_at or NOW, "status": status, "retryCount": 0}
    if sent_at:
        t["sentAt"] = sent_at
        t["channel"] = "outbox"
    return t

def plan(text, tasks, now=NOW, **kw):
    return plan_reply(parse_reply(text, COURSES), tasks, OCC_BY_ID, COURSE_BY_ID, now, **kw)

# ------------------------------------------------------------------ parsing ----
class ParseReplyTest(unittest.TestCase):
    def test_ack_variants(self):
        for text in ("收到", "知道了", "确认", "OK", "好的"):
            self.assertEqual(parse_reply(text, COURSES)["kind"], "ack", text)

    def test_leave_variants(self):
        for text in ("请假了", "请假了，今天不去上课", "今天不去上课了"):
            self.assertEqual(parse_reply(text, COURSES)["kind"], "leave", text)

    def test_leave_beats_snooze_words(self):
        # contains 再提醒 but the decisive content is 不用提醒 -> leave
        self.assertEqual(parse_reply("请假了，不用再提醒了", COURSES)["kind"], "leave")

    def test_snooze_with_explicit_duration(self):
        p = parse_reply("snooze 10分钟", COURSES)
        self.assertEqual((p["kind"], p["snoozeMinutes"]), ("snooze", 10))
        p = parse_reply("过20分钟再提醒我", COURSES)
        self.assertEqual((p["kind"], p["snoozeMinutes"]), ("snooze", 20))
        p = parse_reply("半小时后再提醒我", COURSES)
        self.assertEqual((p["kind"], p["snoozeMinutes"]), ("snooze", 30))
        p = parse_reply("1小时后再提醒", COURSES)
        self.assertEqual((p["kind"], p["snoozeMinutes"]), ("snooze", 60))

    def test_snooze_without_duration_uses_caller_default(self):
        self.assertIsNone(parse_reply("晚点提醒我", COURSES)["snoozeMinutes"])

    def test_unrecognised_reply_is_flagged_not_forced(self):
        p = parse_reply("今天天气不错", COURSES)
        self.assertIsNone(p["kind"])
        self.assertTrue(p["_issues"])

    def test_course_hint_from_full_or_short_name(self):
        self.assertEqual(parse_reply("高等数学收到", COURSES)["courseIdHint"], "c_math")
        self.assertEqual(parse_reply("英语收到", COURSES)["courseIdHint"], "c_eng")

# ----------------------------------------------------------------- locating ----
class LocateTest(unittest.TestCase):
    def setUp(self):
        self.math_sent = task(OCC_MATH, "departure", "sent", "2026-09-14T07:35:00+08:00")
        self.eng_sent = task(OCC_ENG, "departure", "sent", "2026-09-16T09:35:00+08:00")

    def test_named_course_wins_over_recency(self):
        p = plan("高等数学收到", [self.math_sent, self.eng_sent])
        self.assertFalse(p["_issues"])
        self.assertEqual(p["occurrenceId"], OCC_MATH["id"])

    def test_no_name_picks_most_recently_sent(self):
        p = plan("收到", [self.math_sent, self.eng_sent])
        self.assertFalse(p["_issues"])
        self.assertEqual(p["occurrenceId"], OCC_ENG["id"])

    def test_tie_is_ambiguous_and_lists_candidates(self):
        same = "2026-09-16T09:35:00+08:00"
        a = task(OCC_MATH, "departure", "sent", same)
        b = task(OCC_ENG, "departure", "sent", same)
        p = plan("收到", [a, b])
        self.assertTrue(p["_issues"])
        self.assertEqual(len(p["candidates"]), 2)
        self.assertEqual(p["changes"], [])

    def test_named_course_without_outstanding_is_flagged(self):
        p = plan("高等数学收到", [self.eng_sent])
        self.assertTrue(p["_issues"])
        self.assertEqual(p["changes"], [])

    def test_nothing_outstanding_is_flagged(self):
        p = plan("收到", [task(OCC_MATH, "departure", "pending")])
        self.assertTrue(p["_issues"])

    def test_apply_refuses_ambiguous_plan(self):
        same = "2026-09-16T09:35:00+08:00"
        tasks = [task(OCC_MATH, "departure", "sent", same),
                 task(OCC_ENG, "departure", "sent", same)]
        with self.assertRaises(ValueError):
            apply_plan(tasks, plan("收到", tasks), NOW)

# --------------------------------------------------------------- write-back ----
class WriteBackTest(unittest.TestCase):
    def test_ack_writes_status_time_and_raw_reply(self):
        tasks = [task(OCC_ENG, "departure", "sent", "2026-09-16T09:35:00+08:00"),
                 task(OCC_ENG, "pre_class", "pending", scheduled="2026-09-16T09:50:00+08:00")]
        changed = apply_plan(tasks, plan("收到", tasks), NOW)
        self.assertEqual(changed, 1)
        done, pending = tasks
        self.assertEqual(done["status"], "acknowledged")
        self.assertEqual(done["acknowledgedAt"], NOW)
        self.assertEqual(done["reply"], "收到")
        self.assertEqual(pending["status"], "pending")   # not sent yet: untouched

    def test_leave_acks_outstanding_and_cancels_pending_of_same_occurrence(self):
        tasks = [task(OCC_MATH, "night_before", "sent", "2026-09-13T21:00:00+08:00"),
                 task(OCC_MATH, "departure", "pending", scheduled="2026-09-14T07:35:00+08:00"),
                 task(OCC_MATH, "pre_class", "pending", scheduled="2026-09-14T07:50:00+08:00"),
                 task(OCC_ENG, "departure", "pending", scheduled="2026-09-16T09:35:00+08:00")]
        earlier = "2026-09-13T21:05:00+08:00"
        changed = apply_plan(tasks, plan("请假了，今天不去上课", tasks, now=earlier), earlier)
        self.assertEqual(changed, 3)
        nb, dep, pre, eng = tasks
        self.assertEqual(nb["status"], "acknowledged")
        self.assertEqual(nb["note"], "leave")
        self.assertEqual(nb["acknowledgedAt"], earlier)
        for t in (dep, pre):
            self.assertEqual(t["status"], "cancelled")
            self.assertEqual(t["note"], "leave")
            self.assertEqual(t["cancelledAt"], earlier)
            self.assertIn("请假", t["cancelReason"])
            self.assertEqual(t["reply"], "请假了，今天不去上课")
        self.assertEqual(eng["status"], "pending")       # other course untouched

    def test_snooze_writes_until_and_reactivates_on_time(self):
        tasks = [task(OCC_ENG, "departure", "sent", "2026-09-16T09:35:00+08:00",
                      scheduled="2026-09-16T09:35:00+08:00")]
        p = plan("过15分钟再提醒我", tasks)
        self.assertEqual(p["snoozeUntil"], "2026-09-16T10:10:00+08:00")
        apply_plan(tasks, p, NOW)
        t = tasks[0]
        self.assertEqual(t["status"], "snoozed")
        self.assertEqual(t["snoozedAt"], NOW)
        self.assertEqual(reactivate_snoozed(tasks, "2026-09-16T10:09:00+08:00"), 0)
        self.assertEqual(t["status"], "snoozed")
        self.assertEqual(reactivate_snoozed(tasks, "2026-09-16T10:10:00+08:00"), 1)
        self.assertEqual(t["status"], "pending")
        self.assertEqual(t["resumedAt"], "2026-09-16T10:10:00+08:00")
        self.assertNotIn("snoozeUntil", t)
        self.assertIn(t, find_due(tasks, "2026-09-16T10:10:00+08:00"))

    def test_snooze_default_duration(self):
        tasks = [task(OCC_ENG, "departure", "sent", "2026-09-16T09:35:00+08:00")]
        p = plan("晚点提醒我", tasks)                    # default 10 min
        self.assertEqual(p["snoozeMinutes"], 10)
        self.assertEqual(p["snoozeUntil"], "2026-09-16T10:05:00+08:00")
        p = plan("晚点提醒我", tasks, default_snooze_min=25)
        self.assertEqual(p["snoozeUntil"], "2026-09-16T10:20:00+08:00")

# ---------------------------------------------------------------- follow-up ----
class FollowUpTest(unittest.TestCase):
    def setUp(self):
        self.dep_sent = task(OCC_MATH, "departure", "sent", "2026-09-14T07:20:00+08:00",
                             scheduled="2026-09-14T07:20:00+08:00")
        self.after_grace = "2026-09-14T08:10:00+08:00"   # start 08:00 + grace 10

    def test_generated_once_after_grace_with_deterministic_schedule(self):
        tasks = [dict(self.dep_sent)]
        out = generate_follow_ups(tasks, OCC_BY_ID, self.after_grace)
        self.assertEqual(len(out), 1)
        fu = out[0]
        self.assertEqual(fu["id"], f"r_{OCC_MATH['id']}_follow_up")
        self.assertEqual(fu["scheduledAt"], "2026-09-14T08:10:00+08:00")
        self.assertEqual(fu["status"], "pending")
        self.assertEqual(generate_follow_ups(tasks + out, OCC_BY_ID,
                                             "2026-09-14T09:00:00+08:00"), [])

    def test_not_generated_before_grace(self):
        self.assertEqual(generate_follow_ups([dict(self.dep_sent)], OCC_BY_ID,
                                             "2026-09-14T08:09:00+08:00"), [])

    def test_not_generated_after_class_ended(self):
        # a tick long after the class (endAt 09:40) must not nag retroactively
        self.assertEqual(generate_follow_ups([dict(self.dep_sent)], OCC_BY_ID,
                                             "2026-09-20T08:10:00+08:00"), [])

    def test_not_generated_for_night_before_only(self):
        nb = task(OCC_MATH, "night_before", "sent", "2026-09-13T21:00:00+08:00")
        self.assertEqual(generate_follow_ups([nb], OCC_BY_ID, self.after_grace), [])

    def test_not_generated_after_ack(self):
        tasks = [dict(self.dep_sent),
                 task(OCC_MATH, "pre_class", "pending", scheduled="2026-09-14T07:50:00+08:00")]
        apply_plan(tasks, plan("收到", tasks, now="2026-09-14T07:25:00+08:00"),
                   "2026-09-14T07:25:00+08:00")
        self.assertEqual(generate_follow_ups(tasks, OCC_BY_ID, self.after_grace), [])

    def test_not_generated_after_leave_or_snooze(self):
        for text in ("请假了", "晚点提醒我"):
            tasks = [dict(self.dep_sent)]
            apply_plan(tasks, plan(text, tasks, now="2026-09-14T07:25:00+08:00"),
                       "2026-09-14T07:25:00+08:00")
            self.assertEqual(generate_follow_ups(tasks, OCC_BY_ID, self.after_grace),
                             [], text)

    def test_follow_up_survives_merge_and_blocks_duplicates(self):
        prefs = {"nightBeforeTime": "21:00", "preClassLeadMin": 10,
                 "defaultTravelMin": 15, "defaultBufferMin": 10}
        occ_full = dict(OCC_MATH, location={}, requiredItems=[])
        generated = generate_reminders([occ_full], [{"id": "c_math"}], prefs)
        fu = generate_follow_ups([dict(self.dep_sent)], OCC_BY_ID, self.after_grace)
        existing = generated + [dict(fu[0], status="sent",
                                     sentAt="2026-09-14T08:10:00+08:00", channel="outbox")]
        merged = merge_task_states(deepcopy(generated), existing)
        kept = [t for t in merged if t["type"] == "follow_up"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["status"], "sent")      # state not reset
        self.assertEqual(generate_follow_ups(merged, OCC_BY_ID,
                                             "2026-09-14T09:00:00+08:00"), [])

    def test_ack_state_survives_merge(self):
        prefs = {"nightBeforeTime": "21:00", "preClassLeadMin": 10,
                 "defaultTravelMin": 15, "defaultBufferMin": 10}
        occ_full = dict(OCC_MATH, location={}, requiredItems=[])
        generated = generate_reminders([occ_full], [{"id": "c_math"}], prefs)
        existing = [dict(t, status="acknowledged", acknowledgedAt=NOW, reply="收到")
                    if t["type"] == "departure" else t for t in generated]
        merged = merge_task_states(deepcopy(generated), existing)
        dep = [t for t in merged if t["type"] == "departure"][0]
        self.assertEqual(dep["status"], "acknowledged")
        self.assertEqual(dep["reply"], "收到")

# -------------------------------------------------------------------- CLI e2e ----
class HandleReplyCliTest(unittest.TestCase):
    def _tmp_data(self, tmp):
        data = Path(tmp)
        for p in (ROOT / "data").glob("*.json"):
            shutil.copyfile(p, data / p.name)
        tasks = [task(OCC_ENG, "departure", "sent", "2026-09-16T09:35:00+08:00",
                      scheduled="2026-09-16T09:35:00+08:00"),
                 task(OCC_ENG, "pre_class", "pending", scheduled="2026-09-16T09:50:00+08:00")]
        (data / "occurrences.json").write_text(
            json.dumps([OCC_ENG], ensure_ascii=False), encoding="utf-8")
        (data / "reminders.json").write_text(
            json.dumps(tasks, ensure_ascii=False), encoding="utf-8")
        return data

    def _run(self, data, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "handle_reply.py"),
             "--data-dir", str(data), "--now", NOW, *args],
            capture_output=True, text=True)

    def test_preview_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._tmp_data(tmp)
            before = (data / "reminders.json").read_bytes()
            r = self._run(data, "收到")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("预览模式", r.stdout)
            self.assertIn("acknowledged", r.stdout)
            self.assertEqual((data / "reminders.json").read_bytes(), before)
            self.assertFalse(list(data.glob("reminders.json.bak-*")))

    def test_apply_writes_back_with_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._tmp_data(tmp)
            r = self._run(data, "收到", "--apply")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("已写入", r.stdout)
            self.assertTrue(list(data.glob("reminders.json.bak-*")))
            tasks = json.loads((data / "reminders.json").read_text(encoding="utf-8"))
            dep = [t for t in tasks if t["type"] == "departure"][0]
            self.assertEqual(dep["status"], "acknowledged")
            self.assertEqual(dep["acknowledgedAt"], NOW)
            self.assertEqual(dep["reply"], "收到")

    def test_unrecognised_reply_refused_even_with_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._tmp_data(tmp)
            before = (data / "reminders.json").read_bytes()
            r = self._run(data, "今天天气不错", "--apply")
            self.assertEqual(r.returncode, 1)
            self.assertIn("待确认", r.stdout)
            self.assertEqual((data / "reminders.json").read_bytes(), before)

if __name__ == "__main__":
    unittest.main()
