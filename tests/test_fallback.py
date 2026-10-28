"""Session-channel fallback escalation (src/fallback.py).

The failure this fixes was measured live on 2026-10-28: the WeChat
context token went stale overnight, so the 08:05 departure reminder
sat queued/re-queued until the user's 08:35 inbound refreshed the
token and it flushed at 08:55. These tests pin the fix:

- settings: fallbackChannel / fallbackKey / fallbackAfterMin validate
  and resolve with the usual priority (env > settings file > default);
- providers: Server酱 / PushPlus request shapes and success codes,
  always through an injected fake transport — nothing in this file can
  reach a real domain;
- escalation: queued tasks older than fallbackAfterMin (from
  firstQueuedAt) are re-sent and become sent with an honest
  channel="fallback:<provider>"; failures stay queued, are retried at
  most once per requeue cycle, and never block the session path;
- Web UI: masked view, masked save round-trip, target="fallback" test
  endpoint;
- CLI end-to-end (in-process, default fallback transport patched out):
  tick queues, nobody confirms, next tick escalates.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import src.fallback as fallback_mod
from src.fallback import FallbackSender, build_fallback_sender
from src.scheduler import generate_follow_ups, generate_reminders, \
    merge_task_states
from src.sender import SenderConfigError, SenderError, SessionSender
from src.sender_settings import (SenderSettingsError, resolve_sender_config,
                                 validate_settings)
from src.session_dispatch import (escalate_stale_queued_tasks,
                                  queue_due_for_session,
                                  requeue_stale_session_tasks)
from src.webui import (WebUIError, get_sender_view, save_sender_settings,
                       send_sender_test)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OCC = {"id": "c_math@2026-09-14", "courseId": "c_math", "courseName": "高等数学",
       "startAt": "2026-09-14T08:00:00+08:00", "endAt": "2026-09-14T09:40:00+08:00",
       "startTime": "08:00", "endTime": "09:40",
       "location": {"campus": "本部", "building": "教三", "room": "301"},
       "requiredItems": ["课本"]}
TASK = {"id": "r_c_math@2026-09-14_night_before", "occurrenceId": OCC["id"],
        "type": "night_before", "scheduledAt": "2026-09-13T21:00:00+08:00",
        "status": "pending", "retryCount": 0}
T0 = "2026-09-13T21:00:00+08:00"
KEY = "SCT-FAKE-KEY-1234567890"


class RecordingTransport:
    """Fake fallback transport: records (url, data, headers, timeout),
    returns a canned (status, body) or raises a canned exception."""

    def __init__(self, response=(200, '{"code": 0}'), exc=None):
        self.calls = []
        self.response = response
        self.exc = exc

    def __call__(self, url, data, headers, timeout):
        self.calls.append({"url": url, "data": data,
                           "headers": headers, "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        return self.response


def queued_task(tmp, when=T0, task=None):
    """Queue TASK through the real session phase-1 in a temp outbox."""
    task = dict(task or TASK)
    tasks = [task]
    queue_due_for_session(tasks, {OCC["id"]: OCC}, SessionSender(tmp), when)
    assert task["status"] == "queued", task
    return task


# ------------------------------------------------------------- settings ----
class FallbackSettingsTest(unittest.TestCase):
    def test_validate_accepts_full_fallback_config(self):
        cleaned = validate_settings(
            {"channel": "session", "fallbackChannel": "ServerChan",
             "fallbackKey": "  abc  ", "fallbackAfterMin": 20})
        self.assertEqual(cleaned["fallbackChannel"], "serverchan")
        self.assertEqual(cleaned["fallbackKey"], "abc")
        self.assertEqual(cleaned["fallbackAfterMin"], 20)

    def test_validate_rejects_bad_fallback_values(self):
        for bad in ({"fallbackChannel": "pigeon"},
                    {"fallbackChannel": 3},
                    {"fallbackKey": 123},
                    {"fallbackAfterMin": 4},
                    {"fallbackAfterMin": 121},
                    {"fallbackAfterMin": "15"},
                    {"fallbackAfterMin": True}):
            with self.assertRaises(SenderSettingsError, msg=str(bad)):
                validate_settings(bad)

    def test_resolution_priority_env_beats_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "sender_settings.json").write_text(json.dumps(
                {"channel": "session", "fallbackChannel": "pushplus",
                 "fallbackKey": "FILEKEY", "fallbackAfterMin": 30}),
                encoding="utf-8")
            config, sources = resolve_sender_config(env={}, data_dir=data)
            self.assertEqual(config["fallbackChannel"], "pushplus")
            self.assertEqual(config["fallbackKey"], "FILEKEY")
            self.assertEqual(config["fallbackAfterMin"], 30)
            self.assertEqual(sources["fallbackChannel"], "settings")

            env = {"REMINDER_FALLBACK_CHANNEL": "serverchan",
                   "REMINDER_FALLBACK_AFTER_MIN": "45"}
            config, sources = resolve_sender_config(env=env, data_dir=data)
            self.assertEqual(config["fallbackChannel"], "serverchan")
            self.assertEqual(config["fallbackAfterMin"], 45.0)
            self.assertEqual(sources["fallbackAfterMin"], "env")
            # a field the env does not set still comes from the file
            self.assertEqual(config["fallbackKey"], "FILEKEY")
            self.assertEqual(sources["fallbackKey"], "settings")

    def test_defaults_are_inert(self):
        config, sources = resolve_sender_config(env={}, data_dir=None)
        self.assertEqual(config["fallbackChannel"], "none")
        self.assertIsNone(config["fallbackKey"])
        self.assertEqual(config["fallbackAfterMin"], 15)
        self.assertEqual(sources["fallbackChannel"], "default")

    def test_bad_env_provider_is_a_config_error(self):
        with self.assertRaises(SenderConfigError):
            resolve_sender_config(
                env={"REMINDER_FALLBACK_CHANNEL": "pigeon"}, data_dir=None)


class BuildFallbackSenderTest(unittest.TestCase):
    def test_none_and_missing_key_disable_silently(self):
        sender, _ = build_fallback_sender(env={})
        self.assertIsNone(sender)
        sender, _ = build_fallback_sender(
            env={"REMINDER_FALLBACK_CHANNEL": "serverchan"})
        self.assertIsNone(sender)  # provider chosen, but no key anywhere

    def test_configured_provider_builds_with_after_min(self):
        sender, after = build_fallback_sender(env={
            "REMINDER_FALLBACK_CHANNEL": "pushplus",
            "REMINDER_FALLBACK_KEY": KEY,
            "REMINDER_FALLBACK_AFTER_MIN": "25"})
        self.assertIsInstance(sender, FallbackSender)
        self.assertEqual(sender.provider, "pushplus")
        self.assertEqual(after, 25)

    def test_bad_after_min_with_key_aborts_loudly(self):
        for bad in ("abc", "3", "999"):
            with self.assertRaises(SenderConfigError, msg=bad):
                build_fallback_sender(env={
                    "REMINDER_FALLBACK_CHANNEL": "serverchan",
                    "REMINDER_FALLBACK_KEY": KEY,
                    "REMINDER_FALLBACK_AFTER_MIN": bad})

    def test_bad_after_min_without_key_stays_silent(self):
        # Nothing configured to send with => nothing to abort over.
        sender, _ = build_fallback_sender(env={
            "REMINDER_FALLBACK_CHANNEL": "serverchan",
            "REMINDER_FALLBACK_AFTER_MIN": "abc"})
        self.assertIsNone(sender)


# ------------------------------------------------------------- escalation ----
class EscalateTest(unittest.TestCase):
    def _sender(self, transport):
        return FallbackSender("serverchan", KEY, transport=transport)

    def test_before_threshold_nothing_happens(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = queued_task(tmp)
            transport = RecordingTransport()
            results = escalate_stale_queued_tasks(
                [task], {OCC["id"]: OCC}, self._sender(transport),
                "2026-09-13T21:14:00+08:00", 15)
            self.assertEqual(results, [])
            self.assertEqual(transport.calls, [])
            self.assertEqual(task["status"], "queued")

    def test_at_threshold_escalates_and_marks_sent_honestly(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = queued_task(tmp)
            transport = RecordingTransport()
            results = escalate_stale_queued_tasks(
                [task], {OCC["id"]: OCC}, self._sender(transport),
                "2026-09-13T21:15:00+08:00", 15)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["status"], "fallback_sent")
            self.assertEqual(task["status"], "sent")
            self.assertEqual(task["channel"], "fallback:serverchan")
            self.assertEqual(task["sentAt"], "2026-09-13T21:15:00+08:00")
            self.assertEqual(task["fallbackSentAt"],
                             "2026-09-13T21:15:00+08:00")
            self.assertEqual(len(transport.calls), 1)
            form = urllib.parse.parse_qs(
                transport.calls[0]["data"].decode("utf-8"))
            self.assertIn("高等数学", form["desp"][0])

    def test_custom_after_min_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = queued_task(tmp)
            transport = RecordingTransport()
            results = escalate_stale_queued_tasks(
                [task], {OCC["id"]: OCC}, self._sender(transport),
                "2026-09-13T21:20:00+08:00", 30)
            self.assertEqual(results, [])  # 20 min old, threshold 30
            self.assertEqual(task["status"], "queued")

    def test_failure_stays_queued_and_retries_once_per_requeue_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = queued_task(tmp)
            transport = RecordingTransport(
                response=(200, '{"code": 40001}'))
            sender = self._sender(transport)
            by_id = {OCC["id"]: OCC}
            results = escalate_stale_queued_tasks(
                [task], by_id, sender, "2026-09-13T21:20:00+08:00", 15)
            self.assertEqual(results[0]["status"], "fallback_failed")
            self.assertEqual(task["status"], "queued")
            self.assertEqual(task["fallbackAttempts"], 1)
            self.assertIn("fallbackError", task)
            self.assertEqual(len(transport.calls), 1)

            # same requeue cycle (queuedAt unchanged): no second attempt
            results = escalate_stale_queued_tasks(
                [task], by_id, sender, "2026-09-13T21:25:00+08:00", 15)
            self.assertEqual(results, [])
            self.assertEqual(len(transport.calls), 1)

            # a requeue stamps a fresh queuedAt -> a new cycle -> retry
            self.assertEqual(requeue_stale_session_tasks(
                [task], "2026-09-13T21:25:00+08:00"), 1)
            queue_due_for_session([task], by_id, SessionSender(tmp),
                                  "2026-09-13T21:25:00+08:00")
            self.assertEqual(task["queuedAt"], "2026-09-13T21:25:00+08:00")
            results = escalate_stale_queued_tasks(
                [task], by_id, sender, "2026-09-13T21:26:00+08:00", 15)
            self.assertEqual(len(results), 1)
            self.assertEqual(len(transport.calls), 2)
            self.assertEqual(task["fallbackAttempts"], 2)

    def test_first_queued_at_survives_requeue(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = queued_task(tmp)
            self.assertEqual(task["firstQueuedAt"], T0)
            by_id = {OCC["id"]: OCC}
            later = "2026-09-13T21:20:00+08:00"
            requeue_stale_session_tasks([task], later)
            queue_due_for_session([task], by_id, SessionSender(tmp), later)
            self.assertEqual(task["queuedAt"], later)
            self.assertEqual(task["firstQueuedAt"], T0)  # clock not reset

    def test_legacy_task_without_first_queued_at_ages_from_queued_at(self):
        task = dict(TASK, status="queued", channel="session",
                    queuedAt="2026-09-13T20:30:00+08:00")
        transport = RecordingTransport()
        results = escalate_stale_queued_tasks(
            [task], {OCC["id"]: OCC}, self._sender(transport), T0, 15)
        self.assertEqual(len(results), 1)
        self.assertEqual(task["status"], "sent")

    def test_non_queued_and_missing_occurrence_are_skipped(self):
        sent = dict(TASK, status="sent", sentAt=T0)
        orphan = dict(TASK, id="r_orphan", status="queued", channel="session",
                      queuedAt=T0, firstQueuedAt="2026-09-13T20:00:00+08:00",
                      occurrenceId="c_gone@2026-09-14")
        transport = RecordingTransport()
        results = escalate_stale_queued_tasks(
            [sent, orphan], {OCC["id"]: OCC}, self._sender(transport),
            "2026-09-13T21:20:00+08:00", 15)
        self.assertEqual(results, [])
        self.assertEqual(transport.calls, [])
        self.assertEqual(orphan["status"], "queued")

    def test_none_sender_disables_escalation(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = queued_task(tmp)
            results = escalate_stale_queued_tasks(
                [task], {OCC["id"]: OCC}, None,
                "2026-09-13T23:00:00+08:00", 15)
            self.assertEqual(results, [])
            self.assertEqual(task["status"], "queued")

    def test_escalation_bookkeeping_survives_regeneration_merge(self):
        prefs = {"nightBeforeTime": "21:00", "preClassLeadMin": 10,
                 "defaultTravelMin": 15, "defaultBufferMin": 10}
        generated = generate_reminders([OCC], [{"id": "c_math"}], prefs)
        existing = [dict(t, status="queued", queuedAt=T0, firstQueuedAt=T0,
                         channel="session", fallbackAttempts=2,
                         fallbackError="SenderError: x",
                         fallbackAttemptQueuedAt=T0,
                         fallbackLastAttemptAt=T0)
                    if t["type"] == "night_before" else t
                    for t in generated]
        merged = merge_task_states(
            generate_reminders([OCC], [{"id": "c_math"}], prefs), existing)
        nb = [t for t in merged if t["type"] == "night_before"][0]
        self.assertEqual(nb["firstQueuedAt"], T0)
        self.assertEqual(nb["fallbackAttempts"], 2)
        self.assertEqual(nb["fallbackAttemptQueuedAt"], T0)
        self.assertIn("fallbackError", nb)


class FallbackFollowUpTest(unittest.TestCase):
    """A fallback-delivered departure counts as sent for follow_up,
    exactly like a session-confirmed one."""
    OCC2 = {"id": "c_math@2026-09-13", "courseId": "c_math",
            "courseName": "高等数学",
            "startAt": "2026-09-13T20:40:00+08:00",
            "endAt": "2026-09-13T21:40:00+08:00",
            "startTime": "20:40", "endTime": "21:40",
            "location": {"campus": "本部", "building": "教三", "room": "301"},
            "requiredItems": []}
    DEP = {"id": "r_c_math@2026-09-13_departure", "occurrenceId": OCC2["id"],
           "type": "departure", "scheduledAt": "2026-09-13T20:15:00+08:00",
           "status": "pending", "retryCount": 0}

    def test_follow_up_generated_after_fallback_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = [dict(self.DEP)]
            by_id = {self.OCC2["id"]: self.OCC2}
            queue_due_for_session(tasks, by_id, SessionSender(tmp), T0)
            sender = FallbackSender("serverchan", KEY,
                                    transport=RecordingTransport())
            escalate_stale_queued_tasks(
                tasks, by_id, sender, "2026-09-13T21:16:00+08:00", 15)
            self.assertEqual(tasks[0]["status"], "sent")
            self.assertEqual(tasks[0]["channel"], "fallback:serverchan")
            follow = generate_follow_ups(
                tasks, by_id, "2026-09-13T21:16:00+08:00", grace_min=10)
            self.assertEqual(len(follow), 1)
            self.assertEqual(follow[0]["type"], "follow_up")


# ---------------------------------------------------------------- Web UI ----
class FallbackWebUITest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        shutil.copytree(DATA, self.data)
        (self.data / "sender_settings.example.json").unlink(missing_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write_settings(self, obj):
        (self.data / "sender_settings.json").write_text(
            json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def read_settings(self):
        return json.loads(
            (self.data / "sender_settings.json").read_text(encoding="utf-8"))

    def test_view_masks_fallback_key(self):
        self.write_settings({"channel": "session",
                             "fallbackChannel": "serverchan",
                             "fallbackKey": KEY, "fallbackAfterMin": 20})
        view = get_sender_view(self.data, env={})
        blob = json.dumps(view, ensure_ascii=False)
        self.assertNotIn(KEY, blob)
        eff = view["effective"]
        self.assertEqual(eff["fallbackChannel"], "serverchan")
        self.assertTrue(eff["hasFallbackKey"])
        self.assertEqual(eff["fallbackAfterMin"], 20)
        self.assertEqual(view["settingsFile"]["fallbackChannel"],
                         "serverchan")

    def test_save_masked_roundtrip_keeps_key_and_null_clears(self):
        save_sender_settings(
            self.data, {"channel": "session",
                        "fallbackChannel": "serverchan",
                        "fallbackKey": KEY, "fallbackAfterMin": 25}, env={})
        self.assertEqual(self.read_settings()["fallbackKey"], KEY)
        masked = get_sender_view(self.data, env={})[
            "settingsFile"]["fallbackKeyMasked"]
        save_sender_settings(
            self.data, {"fallbackKey": masked}, env={})
        self.assertEqual(self.read_settings()["fallbackKey"], KEY)
        save_sender_settings(
            self.data, {"fallbackKey": None}, env={})
        self.assertNotIn("fallbackKey", self.read_settings())

    def test_save_rejects_bad_fallback_and_writes_nothing(self):
        for payload in ({"fallbackChannel": "pigeon"},
                        {"fallbackAfterMin": 2},
                        {"fallbackAfterMin": 500}):
            with self.assertRaises(WebUIError, msg=str(payload)):
                save_sender_settings(self.data, payload, env={})
            self.assertFalse(
                (self.data / "sender_settings.json").exists())

    def test_fallback_test_endpoint_via_injected_transport(self):
        transport = RecordingTransport()
        result = send_sender_test(
            self.data, {"channel": "session",
                        "fallbackChannel": "serverchan",
                        "fallbackKey": KEY, "target": "fallback"},
            env={}, transport=transport)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["channel"], "fallback:serverchan")
        self.assertEqual(len(transport.calls), 1)
        form = urllib.parse.parse_qs(
            transport.calls[0]["data"].decode("utf-8"))
        self.assertEqual(form["desp"], ["【测试】上课提醒渠道连通性测试"])
        # draft only: nothing persisted
        self.assertFalse((self.data / "sender_settings.json").exists())

    def test_fallback_test_unconfigured_is_clean_failure(self):
        result = send_sender_test(
            self.data, {"target": "fallback"}, env={},
            transport=RecordingTransport())
        self.assertFalse(result["ok"])
        self.assertIn("未启用", result["error"])
        result = send_sender_test(
            self.data, {"fallbackChannel": "pushplus",
                        "target": "fallback"},
            env={}, transport=RecordingTransport())
        self.assertFalse(result["ok"])
        self.assertIn("key", result["error"])

    def test_fallback_test_failure_never_leaks_key(self):
        url = f"https://sctapi.ftqq.com/{KEY}.send"
        transport = RecordingTransport(exc=RuntimeError(f"dial {url}"))
        result = send_sender_test(
            self.data, {"fallbackChannel": "serverchan",
                        "fallbackKey": KEY, "target": "fallback"},
            env={}, transport=transport)
        self.assertFalse(result["ok"])
        self.assertNotIn(KEY, json.dumps(result, ensure_ascii=False))

    def test_unknown_target_is_rejected(self):
        with self.assertRaises(WebUIError):
            send_sender_test(self.data, {"target": "sideways"}, env={})


# ---------------------------------------------------------------- CLI e2e ----
def _load_scheduler_module():
    spec = importlib.util.spec_from_file_location(
        "run_scheduler_under_test", ROOT / "scripts" / "run_scheduler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FallbackCliE2ETest(unittest.TestCase):
    """The 2026-10-28 scenario replayed end to end: tick queues, nobody
    confirms (token dead), the next tick escalates through the fallback.
    The default fallback transport is patched out — zero real traffic."""

    NOW1 = "2026-09-13T21:00:00+08:00"
    NOW2 = "2026-09-13T21:20:00+08:00"

    def _tmp_data(self, tmp):
        data = Path(tmp)
        for p in DATA.glob("*.json"):
            shutil.copyfile(p, data / p.name)
        (data / "sender_settings.example.json").unlink(missing_ok=True)
        user = json.loads((data / "user.json").read_text(encoding="utf-8"))
        user.setdefault("preferences", {})["staleExpireMin"] = 0
        (data / "user.json").write_text(json.dumps(user), encoding="utf-8")
        (data / "sender_settings.json").write_text(json.dumps(
            {"channel": "session", "fallbackChannel": "serverchan",
             "fallbackKey": KEY, "fallbackAfterMin": 15}),
            encoding="utf-8")
        return data

    def _tick(self, module, data, now):
        argv = ["run_scheduler.py", "--tick", "--send",
                "--data-dir", str(data), "--now", now]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out):
            code = module.main()
        return code, out.getvalue()

    @staticmethod
    def _clean_env():
        return {k: v for k, v in os.environ.items()
                if not k.startswith("REMINDER_")
                and k not in ("WEATHER_LAT", "WEATHER_LON")}

    def test_queued_tasks_escalate_next_tick(self):
        module = _load_scheduler_module()
        transport = RecordingTransport()
        with tempfile.TemporaryDirectory() as tmp:
            data = self._tmp_data(tmp)
            with mock.patch.dict(os.environ, self._clean_env(),
                                 clear=True), \
                    mock.patch.object(fallback_mod, "_default_transport",
                                      transport):
                code1, out1 = self._tick(module, data, self.NOW1)
                self.assertEqual(code1, 0, out1)
                self.assertIn("[SESSION-SPEAK]", out1)
                self.assertEqual(transport.calls, [])  # nothing escalated yet
                queued_ids = [t["id"] for t in json.loads(
                    (data / "reminders.json").read_text(encoding="utf-8"))
                    if t["status"] == "queued"]
                self.assertTrue(queued_ids)

                code2, out2 = self._tick(module, data, self.NOW2)
                self.assertEqual(code2, 0, out2)
                self.assertIn("[备用通道]", out2)
                self.assertIn(f"{len(queued_ids)} 条经 serverchan 补发成功",
                              out2)
                self.assertNotIn("[SESSION-SPEAK]", out2)
                tasks = {t["id"]: t for t in json.loads(
                    (data / "reminders.json").read_text(encoding="utf-8"))}
                for task_id in queued_ids:
                    self.assertEqual(tasks[task_id]["status"], "sent")
                    self.assertEqual(tasks[task_id]["channel"],
                                     "fallback:serverchan")
                    self.assertEqual(tasks[task_id]["fallbackSentAt"],
                                     self.NOW2)
                self.assertEqual(len(transport.calls), len(queued_ids))
                for call in transport.calls:
                    self.assertEqual(
                        call["url"], f"https://sctapi.ftqq.com/{KEY}.send")

                # third tick: everything already sent, nothing re-fires
                code3, out3 = self._tick(module, data, self.NOW2)
                self.assertEqual(code3, 0, out3)
                self.assertNotIn("[备用通道]", out3)
                self.assertEqual(len(transport.calls), len(queued_ids))

    def test_fallback_failure_keeps_session_path(self):
        module = _load_scheduler_module()
        transport = RecordingTransport(response=(200, '{"code": 40001}'))
        with tempfile.TemporaryDirectory() as tmp:
            data = self._tmp_data(tmp)
            with mock.patch.dict(os.environ, self._clean_env(),
                                 clear=True), \
                    mock.patch.object(fallback_mod, "_default_transport",
                                      transport):
                code1, _ = self._tick(module, data, self.NOW1)
                self.assertEqual(code1, 0)
                code2, out2 = self._tick(module, data, self.NOW2)
                self.assertEqual(code2, 0, out2)
                self.assertIn("[备用通道]", out2)
                self.assertIn("0 条经 serverchan 补发成功", out2)
                self.assertIn("[备用通道失败]", out2)
                # session path not deprived: tasks still spoken this tick
                self.assertIn("[SESSION-SPEAK]", out2)
                tasks = json.loads(
                    (data / "reminders.json").read_text(encoding="utf-8"))
                queued = [t for t in tasks if t["status"] == "queued"]
                self.assertTrue(queued)
                self.assertTrue(
                    all(t.get("fallbackAttempts") == 1 for t in queued))

    def test_no_fallback_configured_means_no_escalation(self):
        # A plain session deployment (no fallback keys anywhere) must
        # behave exactly like before: CLI smoke via subprocess.
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            for p in DATA.glob("*.json"):
                shutil.copyfile(p, data / p.name)
            user = json.loads((data / "user.json").read_text(
                encoding="utf-8"))
            user.setdefault("preferences", {})["staleExpireMin"] = 0
            (data / "user.json").write_text(
                json.dumps(user), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "run_scheduler.py"),
                 "--tick", "--send", "--channel", "session",
                 "--data-dir", str(data), "--now", self.NOW2],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0,
                             result.stdout + result.stderr)
            self.assertNotIn("[备用通道]", result.stdout)
            self.assertIn("[SESSION-SPEAK]", result.stdout)


if __name__ == "__main__":
    unittest.main()
