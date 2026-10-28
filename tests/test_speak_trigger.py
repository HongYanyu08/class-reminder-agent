"""Event-driven speak path (the 2026-10-28 root fix):

- src.session_dispatch.list_queued_tasks — the state-based speak list;
- run_scheduler.py --speak-pending — its read-only CLI rendering;
- src.speak_trigger.run_speak_trigger — the throttled one-shot wake
  (always with a fake runner here: zero real openclaw calls);
- run_scheduler.py's --from default ("today" in the term timezone).
"""
import contextlib, importlib.util, io, json, os, subprocess, sys, tempfile, unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.session_dispatch import list_queued_tasks
from src.speak_trigger import (DEFAULT_SPEAK_CMD_TEMPLATE, SPEAK_WAKE_TEXT,
                               STATE_FILENAME, run_speak_trigger)

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 28, 21, 0, 0, tzinfo=TZ)

QUEUED_OLD = {"id": "r_old", "occurrenceId": "o_1", "type": "night_before",
              "scheduledAt": "2026-10-27T21:00:00+08:00", "status": "queued",
              "queuedAt": "2026-10-27T21:00:05+08:00", "channel": "session"}
QUEUED_NEW = {"id": "r_new", "occurrenceId": "o_2", "type": "departure",
              "scheduledAt": "2026-10-28T08:05:00+08:00", "status": "queued",
              "queuedAt": "2026-10-28T08:05:03+08:00", "channel": "session"}
SENT = {"id": "r_sent", "occurrenceId": "o_3", "type": "pre_class",
        "scheduledAt": "2026-10-28T08:20:00+08:00", "status": "sent",
        "sentAt": "2026-10-28T08:20:01+08:00", "channel": "session"}
PENDING = {"id": "r_pending", "occurrenceId": "o_4", "type": "pre_class",
           "scheduledAt": "2026-10-28T09:50:00+08:00", "status": "pending",
           "retryCount": 0}


def _write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------- list_queued_tasks ----
class ListQueuedTasksTest(unittest.TestCase):
    def test_only_queued_sorted_by_scheduled_at_with_messages(self):
        tasks = [dict(QUEUED_NEW), dict(SENT), dict(QUEUED_OLD), dict(PENDING)]
        entries = list_queued_tasks(tasks, {"r_old": "旧文案", "r_new": "新文案"})
        self.assertEqual([e["taskId"] for e in entries], ["r_old", "r_new"])
        self.assertEqual(entries[0]["message"], "旧文案")
        self.assertEqual(entries[1]["type"], "departure")

    def test_no_queued_means_empty_list(self):
        self.assertEqual(list_queued_tasks([dict(SENT), dict(PENDING)]), [])

    def test_input_tasks_are_not_mutated(self):
        tasks = [dict(QUEUED_OLD)]
        before = dict(tasks[0])
        list_queued_tasks(tasks, {})
        self.assertEqual(tasks[0], before)


# ------------------------------------------------------------- speak-pending ----
class SpeakPendingCliTest(unittest.TestCase):
    def _run(self, data, *extra):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_scheduler.py"),
             "--data-dir", str(data), *extra],
            capture_output=True, text=True)

    def _data(self, tmp, tasks, outbox_records=()):
        data = Path(tmp)
        _write_json(data / "reminders.json", tasks)
        if outbox_records:
            (data / "session_outbox.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n"
                        for r in outbox_records), encoding="utf-8")
        return data

    def test_lists_cross_tick_queued_latest_wording_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(
                tmp, [QUEUED_NEW, SENT, QUEUED_OLD, PENDING],
                [{"taskId": "r_old", "message": "第一版文案"},
                 {"taskId": "r_new", "message": "出门文案"},
                 {"taskId": "r_old", "message": "最新文案"},
                 {"taskId": "r_sent", "message": "已发文案"}])
            r = self._run(data, "--speak-pending")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            lines = r.stdout.strip().splitlines()
            self.assertEqual(lines, [
                "[SESSION-SPEAK] r_old 最新文案",
                "[SESSION-SPEAK] r_new 出门文案",
                "[SESSION-CONFIRM] py scripts/run_scheduler.py "
                "--confirm-sent r_old,r_new",
            ])

    def test_empty_queue_prints_exactly_the_placeholder_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp, [SENT, PENDING])
            r = self._run(data, "--speak-pending")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(r.stdout, "[SESSION-SPEAK] （无）\n")

    def test_missing_reminders_file_is_an_empty_list_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(Path(tmp), "--speak-pending")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(r.stdout, "[SESSION-SPEAK] （无）\n")

    def test_speak_pending_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp, [QUEUED_OLD],
                              [{"taskId": "r_old", "message": "文案"}])

            def snapshot():
                return {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                        for p in data.iterdir()}

            before = snapshot()
            r = self._run(data, "--speak-pending")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(snapshot(), before)

    def test_speak_pending_refuses_to_mix_with_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(Path(tmp), "--tick", "--speak-pending")
            self.assertEqual(r.returncode, 2)


# -------------------------------------------------------------- speak trigger ----
class FakeRunner:
    def __init__(self, returncode=0, stderr=""):
        self.calls = []
        self.returncode, self.stderr = returncode, stderr

    def __call__(self, command):
        self.calls.append(command)
        return SimpleNamespace(returncode=self.returncode,
                               stderr=self.stderr, stdout="")


class SpeakTriggerTest(unittest.TestCase):
    def _data(self, tmp, tasks):
        data = Path(tmp)
        _write_json(data / "reminders.json", tasks)
        return data

    def test_no_queued_never_calls_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp, [SENT, PENDING])
            runner = FakeRunner()
            code = run_speak_trigger(data, now=NOW, runner=runner, env={})
            self.assertEqual(code, 0)
            self.assertEqual(runner.calls, [])
            self.assertFalse((data / STATE_FILENAME).exists())

    def test_missing_reminders_file_never_calls_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner()
            code = run_speak_trigger(Path(tmp), now=NOW, runner=runner, env={})
            self.assertEqual(code, 0)
            self.assertEqual(runner.calls, [])

    def test_queued_fires_one_wake_with_wake_text_and_writes_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp, [QUEUED_OLD])
            runner = FakeRunner()
            with contextlib.redirect_stdout(io.StringIO()):
                code = run_speak_trigger(data, now=NOW, runner=runner, env={})
            self.assertEqual(code, 0)
            self.assertEqual(len(runner.calls), 1)
            command = runner.calls[0]
            self.assertIn("openclaw.cmd cron add", command)
            self.assertIn("--name cr-speak-20261028-210000", command)
            self.assertIn("--session main", command)
            self.assertIn("【上课提醒·待念】", command)
            self.assertIn("--speak-pending", command)
            state = json.loads((data / STATE_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(state["lastTriggeredAt"], NOW.isoformat())
            self.assertEqual(state["taskIds"], ["r_old"])

    def test_throttle_window_blocks_second_wake_then_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp, [QUEUED_OLD])
            runner = FakeRunner()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    run_speak_trigger(data, now=NOW, runner=runner, env={}), 0)
                self.assertEqual(
                    run_speak_trigger(data, now=NOW + timedelta(minutes=5),
                                      runner=runner, env={}), 0)
            self.assertEqual(len(runner.calls), 1)  # 5 min later: throttled
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    run_speak_trigger(data, now=NOW + timedelta(minutes=11),
                                      runner=runner, env={}), 0)
            self.assertEqual(len(runner.calls), 2)  # 11 min later: fires again

    def test_dry_run_prints_command_calls_nothing_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp, [QUEUED_OLD])
            runner = FakeRunner()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = run_speak_trigger(data, now=NOW, runner=runner,
                                         env={}, dry_run=True)
            self.assertEqual(code, 0)
            self.assertEqual(runner.calls, [])
            self.assertFalse((data / STATE_FILENAME).exists())
            self.assertIn("openclaw.cmd cron add", out.getvalue())
            self.assertIn("cr-speak-20261028-210000", out.getvalue())

    def test_runner_nonzero_exit_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp, [QUEUED_OLD])
            runner = FakeRunner(returncode=3, stderr="boom")
            err = io.StringIO()
            with contextlib.redirect_stderr(err), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = run_speak_trigger(data, now=NOW, runner=runner, env={})
            self.assertEqual(code, 1)
            self.assertIn("boom", err.getvalue())
            self.assertFalse((data / STATE_FILENAME).exists())

    def test_runner_exception_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp, [QUEUED_OLD])

            def exploding(command):
                raise FileNotFoundError("openclaw.cmd")

            with contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = run_speak_trigger(data, now=NOW, runner=exploding, env={})
            self.assertEqual(code, 1)

    def test_env_template_overrides_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp, [QUEUED_OLD])
            runner = FakeRunner()
            env = {"REMINDER_SPEAK_CMD": "echo wake {stamp} :: {text}"}
            with contextlib.redirect_stdout(io.StringIO()):
                code = run_speak_trigger(data, now=NOW, runner=runner, env=env)
            self.assertEqual(code, 0)
            self.assertTrue(runner.calls[0].startswith("echo wake 20261028-210000 :: "))
            self.assertIn("【上课提醒·待念】", runner.calls[0])

    def test_wake_text_and_default_template_contract(self):
        # The wake text must stay quotable: no quote characters at all,
        # and the default template must carry both placeholders.
        self.assertNotIn('"', SPEAK_WAKE_TEXT)
        self.assertNotIn("'", SPEAK_WAKE_TEXT)
        self.assertIn("{stamp}", DEFAULT_SPEAK_CMD_TEMPLATE)
        self.assertIn("{text}", DEFAULT_SPEAK_CMD_TEMPLATE)

    def test_script_dry_run_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self._data(tmp, [QUEUED_OLD])
            r = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "speak_trigger.py"),
                 "--data-dir", str(data), "--dry-run"],
                capture_output=True, text=True,
                env={k: v for k, v in os.environ.items()
                     if k != "REMINDER_SPEAK_CMD"})
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("openclaw.cmd cron add", r.stdout)
            self.assertFalse((data / STATE_FILENAME).exists())


# ------------------------------------------------------------ --from default ----
class FromDefaultTest(unittest.TestCase):
    def test_default_from_date_is_today_in_the_term_timezone(self):
        spec = importlib.util.spec_from_file_location(
            "run_scheduler_from_test", ROOT / "scripts" / "run_scheduler.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        before = datetime.now(TZ).date().isoformat()
        value = module.default_from_date("Asia/Shanghai")
        after = datetime.now(TZ).date().isoformat()
        self.assertIn(value, {before, after})  # midnight-rollover safe


if __name__ == "__main__":
    unittest.main()
