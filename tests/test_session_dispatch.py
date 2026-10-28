import json, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.sender import SessionSender, SenderError, build_sender
from src.sender_settings import build_sender_from_config, validate_settings
from src.session_dispatch import (SESSION_REQUEUE_MIN, queue_due_for_session,
                                  confirm_session_sent, requeue_stale_session_tasks)
from src.scheduler import (generate_reminders, generate_follow_ups,
                           merge_task_states, find_due)

ROOT = Path(__file__).resolve().parent.parent
NOW = "2026-09-13T21:00:00+08:00"
OCC = {"id": "c_math@2026-09-14", "courseId": "c_math", "courseName": "高等数学",
       "startAt": "2026-09-14T08:00:00+08:00", "endAt": "2026-09-14T09:40:00+08:00",
       "startTime": "08:00", "endTime": "09:40",
       "location": {"campus": "本部", "building": "教三", "room": "301"},
       "requiredItems": ["课本"]}
TASK = {"id": "r_c_math@2026-09-14_night_before", "occurrenceId": OCC["id"],
        "type": "night_before", "scheduledAt": "2026-09-13T21:00:00+08:00",
        "status": "pending", "retryCount": 0}
PREFS = {"nightBeforeTime": "21:00", "preClassLeadMin": 10,
         "defaultTravelMin": 15, "defaultBufferMin": 10}


class BoomSender:
    name = "session"
    def send(self, task, message, context=None):
        raise SenderError("disk full")


# ------------------------------------------------------------------ sender ----
class SessionSenderTest(unittest.TestCase):
    def test_send_appends_session_outbox_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            sender = SessionSender(tmp)
            result = sender.send(dict(TASK), "msg-1", {"occurrence": OCC})
            path = Path(tmp) / "session_outbox.jsonl"
            self.assertEqual(result["detail"], str(path))
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec["taskId"], TASK["id"])
            self.assertEqual(rec["occurrenceId"], OCC["id"])
            self.assertEqual(rec["type"], "night_before")
            self.assertEqual(rec["scheduledAt"], TASK["scheduledAt"])
            self.assertEqual(rec["channel"], "session")
            self.assertEqual(rec["message"], "msg-1")
            self.assertEqual(rec["courseName"], "高等数学")
            self.assertIn("queuedAt", rec)          # two-phase: queued, not sent
            self.assertNotIn("sentAt", rec)


# ------------------------------------------------------------------- queue ----
class QueueDueTest(unittest.TestCase):
    def setUp(self):
        self.by_id = {OCC["id"]: OCC}

    def test_due_pending_becomes_queued_and_writes_outbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = [dict(TASK)]
            results = queue_due_for_session(tasks, self.by_id, SessionSender(tmp), NOW)
            self.assertEqual(results[0]["status"], "queued")
            self.assertIn("message", results[0])
            task = tasks[0]
            self.assertEqual(task["status"], "queued")
            self.assertEqual(task["queuedAt"], NOW)
            self.assertEqual(task["channel"], "session")
            lines = (Path(tmp) / "session_outbox.jsonl").read_text(
                encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["taskId"], TASK["id"])

    def test_not_due_task_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            future = dict(TASK, scheduledAt="2026-09-14T21:00:00+08:00")
            results = queue_due_for_session([future], self.by_id, SessionSender(tmp), NOW)
            self.assertEqual(results, [])
            self.assertEqual(future["status"], "pending")
            self.assertFalse((Path(tmp) / "session_outbox.jsonl").exists())

    def test_missing_occurrence_fails_like_dispatch_due(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = [dict(TASK)]
            results = queue_due_for_session(tasks, {}, SessionSender(tmp), NOW)
            self.assertEqual(results[0]["status"], "failed")
            self.assertEqual(tasks[0]["status"], "failed")
            self.assertIn("occurrence 不存在", tasks[0]["error"])
            self.assertEqual(tasks[0]["retryCount"], 1)
            self.assertEqual(tasks[0]["lastAttemptAt"], NOW)
            self.assertFalse((Path(tmp) / "session_outbox.jsonl").exists())

    def test_queue_write_failure_marks_failed(self):
        tasks = [dict(TASK)]
        results = queue_due_for_session(tasks, self.by_id, BoomSender(), NOW)
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("disk full", tasks[0]["error"])


# ----------------------------------------------------------------- confirm ----
class ConfirmTest(unittest.TestCase):
    def _queued(self):
        return dict(TASK, status="queued", queuedAt=NOW, channel="session")

    def test_queued_becomes_sent_with_sent_at(self):
        tasks = [self._queued()]
        confirmed, problems = confirm_session_sent(tasks, [TASK["id"]], NOW)
        self.assertEqual(confirmed, [TASK["id"]])
        self.assertEqual(problems, [])
        self.assertEqual(tasks[0]["status"], "sent")
        self.assertEqual(tasks[0]["sentAt"], NOW)

    def test_pending_id_is_a_problem_and_untouched(self):
        tasks = [dict(TASK)]
        before = dict(tasks[0])
        confirmed, problems = confirm_session_sent(tasks, [TASK["id"]], NOW)
        self.assertEqual(confirmed, [])
        self.assertEqual(len(problems), 1)
        self.assertEqual(tasks[0], before)

    def test_unknown_id_is_a_problem(self):
        confirmed, problems = confirm_session_sent([], ["r_nope"], NOW)
        self.assertEqual(confirmed, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("r_nope", problems[0])

    def test_already_sent_id_is_a_problem_and_untouched(self):
        tasks = [dict(TASK, status="sent", sentAt="2026-09-13T20:00:00+08:00")]
        confirmed, problems = confirm_session_sent(tasks, [TASK["id"]], NOW)
        self.assertEqual(confirmed, [])
        self.assertEqual(len(problems), 1)
        self.assertEqual(tasks[0]["sentAt"], "2026-09-13T20:00:00+08:00")

    def test_mixed_ids_confirm_only_the_queued_ones(self):
        tasks = [self._queued(), dict(TASK, id="r_other", status="pending")]
        confirmed, problems = confirm_session_sent(
            tasks, [TASK["id"], "r_other", "r_ghost"], NOW)
        self.assertEqual(confirmed, [TASK["id"]])
        self.assertEqual(len(problems), 2)
        self.assertEqual(tasks[1]["status"], "pending")


# ------------------------------------------------------------------- stale ----
class RequeueStaleTest(unittest.TestCase):
    def test_default_stale_window_is_15_minutes(self):
        self.assertEqual(SESSION_REQUEUE_MIN, 15)

    def test_stale_queued_falls_back_to_pending(self):
        task = dict(TASK, status="queued", channel="session",
                    queuedAt="2026-09-13T20:44:00+08:00")  # 16 min before NOW
        count = requeue_stale_session_tasks([task], NOW)
        self.assertEqual(count, 1)
        self.assertEqual(task["status"], "pending")
        self.assertNotIn("queuedAt", task)
        self.assertEqual(task["note"], "session requeue")

    def test_fresh_queued_is_untouched(self):
        task = dict(TASK, status="queued", channel="session",
                    queuedAt="2026-09-13T20:55:00+08:00")  # 5 min before NOW
        count = requeue_stale_session_tasks([task], NOW)
        self.assertEqual(count, 0)
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["queuedAt"], "2026-09-13T20:55:00+08:00")

    def test_requeued_task_can_queue_again_in_the_same_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = dict(TASK, status="queued", channel="session",
                        queuedAt="2026-09-13T20:40:00+08:00")
            tasks = [task]
            self.assertEqual(requeue_stale_session_tasks(tasks, NOW), 1)
            results = queue_due_for_session(tasks, {OCC["id"]: OCC},
                                            SessionSender(tmp), NOW)
            self.assertEqual(results[0]["status"], "queued")
            self.assertEqual(task["status"], "queued")
            self.assertEqual(task["queuedAt"], NOW)  # fresh stamp, not the stale one

    def test_non_queued_tasks_are_untouched(self):
        task = dict(TASK, status="sent", sentAt=NOW)
        self.assertEqual(requeue_stale_session_tasks([task], NOW), 0)
        self.assertEqual(task["status"], "sent")


# ------------------------------------------------------------- merge state ----
class MergeQueuedStateTest(unittest.TestCase):
    def test_regeneration_preserves_queued_state_and_queued_at(self):
        generated = generate_reminders([OCC], [{"id": "c_math"}], PREFS)
        existing = [dict(t, status="queued", queuedAt=NOW, channel="session")
                    if t["type"] == "night_before" else t for t in generated]
        merged = merge_task_states(
            generate_reminders([OCC], [{"id": "c_math"}], PREFS), existing)
        nb = [t for t in merged if t["type"] == "night_before"][0]
        self.assertEqual(nb["status"], "queued")
        self.assertEqual(nb["queuedAt"], NOW)
        self.assertEqual(nb["channel"], "session")
        # a queued task is not due again until it goes stale -> pending
        self.assertNotIn(nb, find_due(merged, "2027-01-01T00:00:00+08:00"))


# ------------------------------------------------------- follow_up gating ----
class FollowUpGatingTest(unittest.TestCase):
    """queued must not trigger follow_up; only a confirmed sent does."""
    OCC2 = {"id": "c_math@2026-09-13", "courseId": "c_math", "courseName": "高等数学",
            "startAt": "2026-09-13T20:40:00+08:00", "endAt": "2026-09-13T21:40:00+08:00",
            "startTime": "20:40", "endTime": "21:40",
            "location": {"campus": "本部", "building": "教三", "room": "301"},
            "requiredItems": []}
    DEP = {"id": "r_c_math@2026-09-13_departure", "occurrenceId": OCC2["id"],
           "type": "departure", "scheduledAt": "2026-09-13T20:15:00+08:00",
           "status": "pending", "retryCount": 0}

    def test_follow_up_waits_for_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = [dict(self.DEP)]
            by_id = {self.OCC2["id"]: self.OCC2}
            queue_due_for_session(tasks, by_id, SessionSender(tmp), NOW)
            self.assertEqual(tasks[0]["status"], "queued")
            self.assertEqual(generate_follow_ups(tasks, by_id, NOW, grace_min=10), [])
            confirm_session_sent(tasks, [self.DEP["id"]], NOW)
            follow = generate_follow_ups(tasks, by_id, NOW, grace_min=10)
            self.assertEqual(len(follow), 1)
            self.assertEqual(follow[0]["type"], "follow_up")


# ------------------------------------------------------------ sender config ----
class SessionSenderConfigTest(unittest.TestCase):
    def test_validate_settings_accepts_session(self):
        self.assertEqual(validate_settings({"channel": "session"}),
                         {"channel": "session"})

    def test_build_sender_via_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            sender = build_sender(env={"REMINDER_CHANNEL": "session"}, data_dir=tmp)
            self.assertIsInstance(sender, SessionSender)
            self.assertEqual(sender.name, "session")
            self.assertEqual(sender.path, Path(tmp) / "session_outbox.jsonl")

    def test_build_sender_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            sender = build_sender_from_config({"channel": "session"}, data_dir=tmp)
            self.assertIsInstance(sender, SessionSender)


# ----------------------------------------------------------------- CLI e2e ----
class SessionCliTest(unittest.TestCase):
    def _tmp_data(self, tmp):
        data = Path(tmp)
        for p in (ROOT / "data").glob("*.json"):
            shutil.copyfile(p, data / p.name)
        # The queue/confirm mechanics under test run at a fixed "now"
        # where the sample tasks are long overdue — disable stale
        # expiry (its own coverage lives in test_freshness.py).
        user = json.loads((data / "user.json").read_text(encoding="utf-8"))
        user.setdefault("preferences", {})["staleExpireMin"] = 0
        (data / "user.json").write_text(json.dumps(user), encoding="utf-8")
        return data

    def _run(self, data, *extra, now=NOW):
        # --from is pinned: the scheduler default is now "today", while
        # these tests replay the sample window starting 2026-09-07.
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_scheduler.py"),
             "--data-dir", str(data), "--from", "2026-09-07",
             "--now", now, *extra],
            capture_output=True, text=True)

    def _tasks(self, data):
        return json.loads((data / "reminders.json").read_text(encoding="utf-8"))

    def test_tick_queues_then_confirm_sends_and_never_requeues(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._tmp_data(tmp)
            r1 = self._run(data, "--tick", "--send", "--channel", "session")
            self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
            self.assertIn("[SESSION-OUTBOX]", r1.stdout)
            self.assertIn("[SESSION-SPEAK]", r1.stdout)
            m = re.search(r"--confirm-sent (\S+)", r1.stdout)
            self.assertIsNotNone(m, r1.stdout)
            queued_ids = m.group(1).split(",")
            tasks = {t["id"]: t for t in self._tasks(data)}
            for task_id in queued_ids:
                self.assertEqual(tasks[task_id]["status"], "queued")
                self.assertEqual(tasks[task_id]["channel"], "session")
                self.assertIn("queuedAt", tasks[task_id])
                self.assertNotIn("sentAt", tasks[task_id])
            outbox_lines = (data / "session_outbox.jsonl").read_text(
                encoding="utf-8").splitlines()
            self.assertEqual(len(outbox_lines), len(queued_ids))
            self.assertEqual({json.loads(l)["taskId"] for l in outbox_lines},
                             set(queued_ids))

            # Phase 2: the agent speaks, then confirms the first id.
            first = queued_ids[0]
            r2 = self._run(data, "--confirm-sent", first)
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
            self.assertIn(f"[CONFIRMED] {first}", r2.stdout)
            task = {t["id"]: t for t in self._tasks(data)}[first]
            self.assertEqual(task["status"], "sent")
            self.assertEqual(task["sentAt"], NOW)

            # Next tick at the same "now": the confirmed task is never
            # re-queued, and the still-fresh queued ones are not either.
            r3 = self._run(data, "--tick", "--send", "--channel", "session")
            self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)
            self.assertNotIn("[SESSION-OUTBOX]", r3.stdout)
            self.assertNotIn(first, r3.stdout)
            self.assertEqual((data / "session_outbox.jsonl").read_text(
                encoding="utf-8").splitlines(), outbox_lines)
            self.assertIn("已排队 0 条", r3.stdout)

    def test_confirm_rejects_pending_and_unknown_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._tmp_data(tmp)
            r0 = self._run(data, "--tick", "--send", "--channel", "session")
            self.assertEqual(r0.returncode, 0, r0.stdout + r0.stderr)
            pending_id = next(t["id"] for t in self._tasks(data)
                              if t["status"] == "pending")
            r = self._run(data, "--confirm-sent", pending_id)
            self.assertEqual(r.returncode, 1)
            self.assertIn("不是 queued", r.stderr)
            task = {t["id"]: t for t in self._tasks(data)}[pending_id]
            self.assertEqual(task["status"], "pending")
            r2 = self._run(data, "--confirm-sent", "r_ghost")
            self.assertEqual(r2.returncode, 1)
            self.assertIn("未知任务 id", r2.stderr)

    def test_confirm_sent_refuses_to_mix_with_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._tmp_data(tmp)
            r = self._run(data, "--tick", "--confirm-sent", "r_x")
            self.assertEqual(r.returncode, 2)

    def test_confirm_sent_without_reminders_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(Path(tmp), "--confirm-sent", "r_x")
            self.assertEqual(r.returncode, 1)
            self.assertIn("reminders.json 不存在", r.stderr)

    def test_dry_run_never_queues(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._tmp_data(tmp)
            r = self._run(data, "--tick", "--send", "--dry-run",
                          "--channel", "session")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("预览模式", r.stdout)
            self.assertNotIn("[SESSION-SPEAK]", r.stdout)
            self.assertFalse((data / "session_outbox.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
