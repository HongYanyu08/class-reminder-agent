import io, json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.sender import (ConsoleSender, OutboxSender, WebhookSender, GatewaySender,
                        SenderError, SenderConfigError, build_sender)
from src.scheduler import generate_reminders, merge_task_states, dispatch_due, find_due

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

class RecordingSender:
    """Injectable fake channel: records calls, optionally fails."""
    name = "fake"
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail
    def send(self, task, message, context=None):
        self.calls.append((task["id"], message))
        if self.fail:
            raise SenderError("fake transport down")
        return {"detail": "ok"}

# ---------------------------------------------------------------- channels ----
class ConsoleChannelTest(unittest.TestCase):
    def test_console_prints_message(self):
        stream = io.StringIO()
        ConsoleSender(stream=stream).send(dict(TASK), "🌙 明天预告｜高等数学")
        self.assertIn("高等数学", stream.getvalue())

class OutboxChannelTest(unittest.TestCase):
    def test_outbox_appends_jsonl_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            sender = OutboxSender(tmp)
            sender.send(dict(TASK), "msg-1", {"occurrence": OCC})
            sender.send(dict(TASK), "msg-2", {"occurrence": OCC})
            lines = (Path(tmp) / "outbox.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            rec = json.loads(lines[0])
            self.assertEqual(rec["taskId"], TASK["id"])
            self.assertEqual(rec["channel"], "outbox")
            self.assertEqual(rec["message"], "msg-1")
            self.assertEqual(rec["courseName"], "高等数学")
            self.assertIn("sentAt", rec)

class WebhookChannelTest(unittest.TestCase):
    def test_webhook_posts_payload_via_injected_transport(self):
        seen = {}
        def fake_transport(url, payload, timeout):
            seen.update(url=url, payload=payload, timeout=timeout)
            return 200, "ok"
        sender = WebhookSender("https://example.invalid/hook", timeout=3,
                               transport=fake_transport)
        sender.send(dict(TASK), "hello", {})
        self.assertEqual(seen["url"], "https://example.invalid/hook")
        self.assertEqual(seen["timeout"], 3)
        self.assertEqual(seen["payload"]["message"], "hello")
        self.assertEqual(seen["payload"]["taskId"], TASK["id"])

    def test_webhook_non_2xx_is_a_send_failure(self):
        sender = WebhookSender("https://example.invalid/hook",
                               transport=lambda u, p, t: (500, "boom"))
        with self.assertRaises(SenderError) as cm:
            sender.send(dict(TASK), "hello", {})
        self.assertIn("500", str(cm.exception))

    def test_webhook_transport_error_is_a_send_failure(self):
        def boom(url, payload, timeout):
            raise TimeoutError("timed out")
        sender = WebhookSender("https://example.invalid/hook", transport=boom)
        with self.assertRaises(SenderError):
            sender.send(dict(TASK), "hello", {})

    def test_webhook_without_url_is_a_config_error(self):
        with self.assertRaises(SenderConfigError) as cm:
            WebhookSender("")
        self.assertIn("REMINDER_WEBHOOK_URL", str(cm.exception))

class GatewayChannelTest(unittest.TestCase):
    class Proc:
        def __init__(self, rc, stderr=""):
            self.returncode, self.stderr = rc, stderr
    def test_gateway_runs_configured_command_with_placeholders(self):
        seen = {}
        def fake_runner(argv, timeout):
            seen["argv"] = argv
            return self.Proc(0)
        sender = GatewaySender("openclaw-send --to {user_id} --text {text}", runner=fake_runner)
        sender.send(dict(TASK), "提醒内容", {"user": {"id": "u_001"}})
        self.assertEqual(seen["argv"], ["openclaw-send", "--to", "u_001", "--text", "提醒内容"])
    def test_gateway_nonzero_exit_is_a_send_failure(self):
        sender = GatewaySender("whatever {text}", runner=lambda a, t: self.Proc(3, "no channel"))
        with self.assertRaises(SenderError) as cm:
            sender.send(dict(TASK), "x", {})
        self.assertIn("退出码 3", str(cm.exception))
    def test_gateway_without_command_is_a_config_error(self):
        with self.assertRaises(SenderConfigError) as cm:
            GatewaySender("")
        self.assertIn("REMINDER_GATEWAY_CMD", str(cm.exception))

class BuildSenderTest(unittest.TestCase):
    def test_default_is_console(self):
        self.assertEqual(build_sender(env={}).name, "console")
    def test_env_selects_outbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            sender = build_sender(env={"REMINDER_CHANNEL": "outbox",
                                       "REMINDER_OUTBOX_DIR": tmp})
            self.assertEqual(sender.name, "outbox")
    def test_webhook_missing_url_fails_loudly_not_silently(self):
        with self.assertRaises(SenderConfigError) as cm:
            build_sender(env={"REMINDER_CHANNEL": "webhook"})
        self.assertIn("REMINDER_WEBHOOK_URL", str(cm.exception))
    def test_unknown_channel_fails_loudly(self):
        with self.assertRaises(SenderConfigError) as cm:
            build_sender(env={"REMINDER_CHANNEL": "pigeon"})
        self.assertIn("pigeon", str(cm.exception))

# ---------------------------------------------------------------- dispatch ----
class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tasks = [dict(TASK)]
        self.by_id = {OCC["id"]: OCC}
    def test_success_writes_back_sent_state(self):
        sender = RecordingSender()
        results = dispatch_due(self.tasks, self.by_id, sender, NOW)
        self.assertEqual(results[0]["status"], "sent")
        task = self.tasks[0]
        self.assertEqual(task["status"], "sent")
        self.assertEqual(task["sentAt"], NOW)
        self.assertEqual(task["channel"], "fake")
        self.assertEqual(len(sender.calls), 1)
    def test_sent_task_is_never_resent(self):
        sender = RecordingSender()
        dispatch_due(self.tasks, self.by_id, sender, NOW)
        results2 = dispatch_due(self.tasks, self.by_id, sender, NOW)
        self.assertEqual(results2, [])               # no longer due
        self.assertEqual(len(sender.calls), 1)       # and not sent twice
    def test_failure_writes_back_failed_and_is_not_auto_retried(self):
        sender = RecordingSender(fail=True)
        results = dispatch_due(self.tasks, self.by_id, sender, NOW)
        self.assertEqual(results[0]["status"], "failed")
        task = self.tasks[0]
        self.assertEqual(task["status"], "failed")
        self.assertIn("fake transport down", task["error"])
        self.assertEqual(task["retryCount"], 1)
        self.assertEqual(task["lastAttemptAt"], NOW)
        dispatch_due(self.tasks, self.by_id, sender, NOW)   # later tick
        self.assertEqual(len(sender.calls), 1)              # no automatic retry
    def test_dry_run_sends_nothing_and_touches_nothing(self):
        sender = RecordingSender()
        before = dict(self.tasks[0])
        results = dispatch_due(self.tasks, self.by_id, sender, NOW, dry_run=True)
        self.assertEqual(results[0]["status"], "dry_run")
        self.assertIn("message", results[0])         # preview still shows wording
        self.assertEqual(sender.calls, [])
        self.assertEqual(self.tasks[0], before)      # no status write-back
    def test_missing_occurrence_fails_loudly(self):
        results = dispatch_due(self.tasks, {}, RecordingSender(), NOW)
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(self.tasks[0]["status"], "failed")
        self.assertIn("occurrence 不存在", self.tasks[0]["error"])

class MergeStateTest(unittest.TestCase):
    def test_regeneration_preserves_sent_state(self):
        prefs = {"nightBeforeTime": "21:00", "preClassLeadMin": 10,
                 "defaultTravelMin": 15, "defaultBufferMin": 10}
        generated = generate_reminders([OCC], [{"id": "c_math"}], prefs)
        existing = [dict(t, status="sent", sentAt=NOW, channel="outbox")
                    if t["type"] == "night_before" else t for t in generated]
        merged = merge_task_states(generate_reminders([OCC], [{"id": "c_math"}], prefs),
                                   existing)
        nb = [t for t in merged if t["type"] == "night_before"][0]
        self.assertEqual(nb["status"], "sent")       # not reset to pending
        self.assertEqual(nb["channel"], "outbox")
        self.assertEqual(find_due(merged, "2027-01-01T00:00:00+08:00"),
                         [t for t in merged if t["status"] == "pending"])

# ----------------------------------------------------------------- CLI e2e ----
class SchedulerCliTest(unittest.TestCase):
    def _tmp_data(self, tmp):
        data = Path(tmp)
        for p in (ROOT / "data").glob("*.json"):
            shutil.copyfile(p, data / p.name)
        # These tests exercise dispatch mechanics at a fixed "now"
        # where the sample tasks are long overdue — disable stale
        # expiry (its own coverage lives in test_freshness.py).
        user = json.loads((data / "user.json").read_text(encoding="utf-8"))
        user.setdefault("preferences", {})["staleExpireMin"] = 0
        (data / "user.json").write_text(json.dumps(user), encoding="utf-8")
        return data
    def _run(self, data, *extra):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_scheduler.py"),
             "--tick", "--data-dir", str(data), "--now", NOW, *extra],
            capture_output=True, text=True)
    def test_send_via_outbox_writes_back_and_never_resends(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._tmp_data(tmp)
            r1 = self._run(data, "--send", "--channel", "outbox")
            self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
            self.assertIn("[SENT via outbox]", r1.stdout)
            outbox = data / "outbox" / "outbox.jsonl"
            lines1 = outbox.read_text(encoding="utf-8").splitlines()
            self.assertTrue(lines1)
            tasks = json.loads((data / "reminders.json").read_text(encoding="utf-8"))
            sent = [t for t in tasks if t["status"] == "sent"]
            self.assertEqual(len(sent), len(lines1))
            self.assertTrue(all(t["channel"] == "outbox" and t["sentAt"] for t in sent))
            r2 = self._run(data, "--send", "--channel", "outbox")
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
            self.assertEqual(outbox.read_text(encoding="utf-8").splitlines(), lines1)
    def test_dry_run_writes_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._tmp_data(tmp)
            before = {p.name: p.read_bytes() for p in data.glob("*.json")}
            r = self._run(data, "--send", "--dry-run")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("预览模式", r.stdout)
            self.assertEqual({p.name: p.read_bytes() for p in data.glob("*.json")}, before)
            self.assertFalse((data / "outbox").exists())
    def test_unconfigured_webhook_aborts_with_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._tmp_data(tmp)
            env = dict(**{k: v for k, v in __import__("os").environ.items()
                          if not k.startswith("REMINDER_")})
            r = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "run_scheduler.py"),
                 "--tick", "--send", "--channel", "webhook",
                 "--data-dir", str(data), "--now", NOW],
                capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 2)
            self.assertIn("REMINDER_WEBHOOK_URL", r.stderr)
            tasks = json.loads((data / "reminders.json").read_text(encoding="utf-8"))
            self.assertTrue(all(t["status"] == "pending" for t in tasks))

if __name__ == "__main__":
    unittest.main()
