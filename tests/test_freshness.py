"""Naive --now normalization (bug fix) and stale-task expiry (the
"stale storm" guard), unit level plus CLI end-to-end.

Bug background: `run_scheduler.py --tick --now 2026-10-27T21:05` (no
UTC offset) crashed in scheduler.find_due with "can't compare
offset-naive and offset-aware datetimes" — internal timestamps are
aware, the user's --now was not. Naive user input is now interpreted
in the term's timezone (scheduler.normalize_now_iso) at every CLI
entry point that accepts a moment.

Stale expiry: a pending task whose scheduledAt is more than
staleExpireMin minutes (default 120) before the tick's now is flipped
to the terminal status "expired" — never sent, never queued, never
followed up — so a first deployment or a long shutdown does not flood
the user with reminders for classes already over.
"""
import json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.expand import expand
from src.scheduler import (DEFAULT_STALE_EXPIRE_MIN, dispatch_due,
                           expire_stale_tasks, find_due, generate_follow_ups,
                           generate_reminders, merge_task_states,
                           normalize_now_iso, data_tz_name)
from src.sender import SessionSender
from src.session_dispatch import queue_due_for_session, requeue_stale_session_tasks

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
term = json.loads((DATA / "term.json").read_text(encoding="utf-8"))
courses = json.loads((DATA / "courses.json").read_text(encoding="utf-8"))
exceptions = json.loads((DATA / "exceptions.json").read_text(encoding="utf-8"))

NOW = "2026-09-14T12:00:00+08:00"
OCC = {"id": "c_math@2026-09-14", "courseId": "c_math", "courseName": "高等数学",
       "startAt": "2026-09-14T08:00:00+08:00", "endAt": "2026-09-14T09:40:00+08:00",
       "startTime": "08:00", "endTime": "09:40",
       "location": {"campus": "本部", "building": "教三", "room": "301"},
       "requiredItems": ["课本"]}


def make_task(scheduled_at, status="pending", typ="departure", **extra):
    task = {"id": f"r_{OCC['id']}_{typ}", "occurrenceId": OCC["id"], "type": typ,
            "scheduledAt": scheduled_at, "status": status, "retryCount": 0}
    task.update(extra)
    return task


class RecordingSender:
    name = "console"

    def __init__(self):
        self.calls = []

    def send(self, task, message, context=None):
        self.calls.append((task["id"], message))


# ------------------------------------------------------- naive --now fix ----
class NormalizeNowTest(unittest.TestCase):
    def test_naive_is_read_in_term_timezone(self):
        self.assertEqual(normalize_now_iso("2026-10-27T21:05", "Asia/Shanghai"),
                         "2026-10-27T21:05:00+08:00")

    def test_aware_passes_through_untouched(self):
        self.assertEqual(normalize_now_iso("2026-10-27T21:05:00+08:00"),
                         "2026-10-27T21:05:00+08:00")
        # A different explicit offset is kept as-is (same instant rules).
        self.assertEqual(normalize_now_iso("2026-10-27T13:05:00+00:00"),
                         "2026-10-27T13:05:00+00:00")

    def test_find_due_naive_matches_aware(self):
        occs = expand(term, courses, exceptions, "2026-10-26", 7)
        prefs = {"nightBeforeTime": "21:00", "preClassLeadMin": 10,
                 "defaultTravelMin": 15, "defaultBufferMin": 10}
        tasks = generate_reminders(occs, courses, prefs)
        naive = find_due(tasks, normalize_now_iso("2026-10-27T21:05", term["timezone"]))
        aware = find_due(tasks, "2026-10-27T21:05:00+08:00")
        self.assertEqual([t["id"] for t in naive], [t["id"] for t in aware])
        self.assertEqual(len(aware), 1)  # Wed English night_before, due 21:00
        self.assertEqual(aware[0]["type"], "night_before")

    def test_data_tz_name_prefers_term_then_user_then_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            self.assertEqual(data_tz_name(data), "Asia/Shanghai")
            (data / "user.json").write_text(
                json.dumps({"timezone": "Asia/Tokyo"}), encoding="utf-8")
            self.assertEqual(data_tz_name(data), "Asia/Tokyo")
            (data / "term.json").write_text(
                json.dumps({"timezone": "Asia/Shanghai"}), encoding="utf-8")
            self.assertEqual(data_tz_name(data), "Asia/Shanghai")


# ------------------------------------------------------------ stale expiry ----
class ExpireStaleTest(unittest.TestCase):
    def test_three_hour_old_pending_expires_and_never_sends(self):
        task = make_task("2026-09-14T09:00:00+08:00")  # 3 h before NOW
        sender = RecordingSender()
        self.assertEqual(expire_stale_tasks([task], NOW), 1)
        self.assertEqual(task["status"], "expired")
        self.assertEqual(task["expiredAt"], NOW)
        results = dispatch_due([task], {OCC["id"]: OCC}, sender, NOW)
        self.assertEqual(results, [])
        self.assertEqual(sender.calls, [])

    def test_ten_minute_old_pending_still_sends(self):
        task = make_task("2026-09-14T11:50:00+08:00")  # 10 min before NOW
        sender = RecordingSender()
        self.assertEqual(expire_stale_tasks([task], NOW), 0)
        self.assertEqual(task["status"], "pending")
        results = dispatch_due([task], {OCC["id"]: OCC}, sender, NOW)
        self.assertEqual([r["status"] for r in results], ["sent"])
        self.assertEqual(task["status"], "sent")
        self.assertEqual(len(sender.calls), 1)

    def test_exactly_at_threshold_is_not_expired(self):
        task = make_task("2026-09-14T10:00:00+08:00")  # exactly 120 min
        self.assertEqual(expire_stale_tasks([task], NOW, DEFAULT_STALE_EXPIRE_MIN), 0)
        self.assertEqual(task["status"], "pending")

    def test_custom_threshold_and_disable(self):
        old = make_task("2026-09-14T11:15:00+08:00", typ="pre_class")   # 45 min
        self.assertEqual(expire_stale_tasks([old], NOW, 30), 1)
        fresh = make_task("2026-09-14T11:40:00+08:00", typ="pre_class")  # 20 min
        self.assertEqual(expire_stale_tasks([fresh], NOW, 30), 0)
        ancient = make_task("2026-09-14T01:00:00+08:00")
        self.assertEqual(expire_stale_tasks([ancient], NOW, 0), 0)  # disabled
        self.assertEqual(ancient["status"], "pending")

    def test_expired_survives_regeneration_merge(self):
        stored = make_task("2026-09-14T07:35:00+08:00", status="expired",
                           expiredAt="2026-09-14T12:00:00+08:00")
        regenerated = make_task("2026-09-14T07:35:00+08:00")  # fresh: pending
        merged = merge_task_states([regenerated], [stored])
        self.assertEqual(merged[0]["status"], "expired")
        self.assertEqual(merged[0]["expiredAt"], "2026-09-14T12:00:00+08:00")
        self.assertEqual(find_due(merged, "2026-09-15T12:00:00+08:00"), [])

    def test_expired_source_generates_no_follow_up(self):
        # now is inside the [start+grace, end] window, so only the
        # source status can block the follow_up: expired blocks, sent allows.
        moment = "2026-09-14T08:30:00+08:00"
        expired = make_task("2026-09-14T07:35:00+08:00", status="expired",
                            expiredAt=NOW)
        self.assertEqual(
            generate_follow_ups([expired], {OCC["id"]: OCC}, moment), [])
        sent = make_task("2026-09-14T07:35:00+08:00", status="sent",
                         sentAt="2026-09-14T07:35:00+08:00")
        ups = generate_follow_ups([sent], {OCC["id"]: OCC}, moment)
        self.assertEqual(len(ups), 1)
        self.assertEqual(ups[0]["type"], "follow_up")

    def test_queued_task_untouched_until_requeue_then_expires(self):
        # A queued session task is NOT expired in place (its own 15-min
        # requeue recovery owns it); once requeued to pending, the same
        # tick's expire pass voids it instead of re-speaking it.
        task = make_task("2026-09-14T08:30:00+08:00", status="queued",
                         queuedAt="2026-09-14T11:40:00+08:00", channel="session")
        self.assertEqual(expire_stale_tasks([task], NOW), 0)
        self.assertEqual(task["status"], "queued")
        self.assertEqual(requeue_stale_session_tasks([task], NOW), 1)
        self.assertEqual(task["status"], "pending")
        self.assertEqual(expire_stale_tasks([task], NOW), 1)
        with tempfile.TemporaryDirectory() as tmp:
            results = queue_due_for_session([task], {OCC["id"]: OCC},
                                            SessionSender(tmp), NOW)
            self.assertEqual(results, [])
            self.assertFalse((Path(tmp) / "session_outbox.jsonl").exists())


# ---------------------------------------------------------------- CLI e2e ----
class CliTest(unittest.TestCase):
    def _tmp_data(self, tmp):
        data = Path(tmp)
        for p in DATA.glob("*.json"):
            shutil.copyfile(p, data / p.name)
        return data

    def _run(self, data, *extra):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_scheduler.py"),
             "--data-dir", str(data), *extra],
            capture_output=True, text=True)

    def test_tick_with_naive_now_matches_aware(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._tmp_data(tmp)
            naive = self._run(data, "--tick", "--from", "2026-10-26",
                              "--now", "2026-10-27T21:05")
            self.assertEqual(naive.returncode, 0, naive.stdout + naive.stderr)
            self.assertNotIn("Traceback", naive.stderr)
            aware = self._run(data, "--tick", "--from", "2026-10-26",
                              "--now", "2026-10-27T21:05:00+08:00")
            self.assertEqual(aware.returncode, 0, aware.stdout + aware.stderr)
            due = lambda out: [l for l in out.splitlines() if l.startswith("[DUE]")]
            self.assertEqual(due(naive.stdout), due(aware.stdout))
            self.assertEqual(len(due(naive.stdout)), 1)

    def test_confirm_sent_with_naive_now_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._tmp_data(tmp)
            (data / "reminders.json").unlink(missing_ok=True)  # the case under test
            res = self._run(data, "--confirm-sent", "r_whatever",
                            "--now", "2026-10-27T21:05")
            self.assertEqual(res.returncode, 1)
            self.assertIn("reminders.json 不存在", res.stderr)
            self.assertNotIn("Traceback", res.stderr)

    def test_stale_storm_is_expired_not_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._tmp_data(tmp)
            # Day 0 (Sunday evening): a normal preview tick persists
            # pending tasks; nothing is due or stale yet.
            r1 = self._run(data, "--tick", "--from", "2026-09-07",
                           "--now", "2026-09-06T20:00:00+08:00")
            self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
            # The laptop then stays off for three days; the next tick
            # must expire the six long-overdue tasks, not send them.
            r2 = self._run(data, "--tick", "--send", "--channel", "console",
                           "--from", "2026-09-07",
                           "--now", "2026-09-10T12:00:00+08:00")
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
            self.assertIn("[过期作废] 6 条", r2.stdout)
            self.assertIn("成功 0 条", r2.stdout)
            tasks = {t["id"]: t for t in json.loads(
                (data / "reminders.json").read_text(encoding="utf-8"))}
            for task_id in ("r_c_math@2026-09-07_night_before",
                            "r_c_math@2026-09-07_departure",
                            "r_c_math@2026-09-07_pre_class",
                            "r_c_eng@2026-09-09_night_before",
                            "r_c_eng@2026-09-09_departure",
                            "r_c_eng@2026-09-09_pre_class"):
                self.assertEqual(tasks[task_id]["status"], "expired", task_id)
                self.assertIn("expiredAt", tasks[task_id])
            # A future task is untouched.
            self.assertEqual(
                tasks["r_c_prog@2026-09-11_night_before"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
