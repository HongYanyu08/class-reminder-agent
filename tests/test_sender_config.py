"""Sender channel settings: data/sender_settings.json, resolution
priority (argument > env > settings file > default), masked round-trip
through the Web UI API, the /api/sender/test endpoint against a fake
localhost webhook, and the scheduler CLI using the settings file.

Everything runs on temp data copies and localhost — nothing is ever sent
to a real external address, and the real data/ is only read as fixtures.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.sender import SenderConfigError, SenderError, build_sender
from src.sender_settings import (
    SenderSettingsError,
    load_settings_file,
    mask_secret,
    mask_webhook_url,
    resolve_sender_config,
    validate_settings,
)
from src.webui import (
    WebUIError,
    get_sender_view,
    handle_request,
    save_sender_settings,
    send_sender_test,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SECRET_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRETKEY1234567890"
NOW = "2026-09-13T21:00:00+08:00"


class TempDataCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        shutil.copytree(DATA, self.data)
        # the tracked example file is a template, not live settings
        (self.data / "sender_settings.example.json").unlink(missing_ok=True)
        # The CLI tests here drive sends at a fixed "now" where the
        # sample tasks are long overdue — disable stale expiry (its
        # own coverage lives in test_freshness.py).
        user = json.loads((self.data / "user.json").read_text(encoding="utf-8"))
        user.setdefault("preferences", {})["staleExpireMin"] = 0
        (self.data / "user.json").write_text(json.dumps(user), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write_settings(self, obj_or_text):
        path = self.data / "sender_settings.json"
        if isinstance(obj_or_text, str):
            path.write_text(obj_or_text, encoding="utf-8")
        else:
            path.write_text(json.dumps(obj_or_text, ensure_ascii=False),
                            encoding="utf-8")
        return path

    def read_settings(self):
        return json.loads(
            (self.data / "sender_settings.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------- settings file ----
class SettingsFileTest(TempDataCase):
    def test_missing_file_is_empty_settings(self):
        self.assertEqual(load_settings_file(self.data), {})

    def test_example_file_is_valid_and_inert(self):
        example = json.loads(
            (DATA / "sender_settings.example.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_settings(example),
                         {"channel": "console", "webhookTimeoutSec": 10,
                          "fallbackChannel": "none", "fallbackAfterMin": 15})

    def test_corrupt_json_is_a_loud_error(self):
        self.write_settings("{not json")
        with self.assertRaises(SenderSettingsError) as caught:
            load_settings_file(self.data)
        self.assertIn("损坏", str(caught.exception))

    def test_invalid_values_are_rejected(self):
        self.write_settings({"channel": "pigeon"})
        with self.assertRaises(SenderSettingsError):
            load_settings_file(self.data)
        self.write_settings({"channel": "webhook", "webhookUrl": "http://x"})
        with self.assertRaises(SenderSettingsError):
            load_settings_file(self.data)
        self.write_settings({"channel": "webhook", "webhookUrl": SECRET_URL,
                             "mystery": 1})
        with self.assertRaises(SenderSettingsError):
            load_settings_file(self.data)

    def test_gateway_template_contract(self):
        good = {"channel": "gateway", "gatewayCmd": "send --text {text}"}
        self.assertEqual(validate_settings(good)["gatewayCmd"],
                         "send --text {text}")
        for bad in ("send --body {message}", "send --text", "send {text"):
            with self.assertRaises(SenderSettingsError, msg=bad):
                validate_settings({"channel": "gateway", "gatewayCmd": bad})


class ResolutionPriorityTest(TempDataCase):
    def test_defaults_without_anything(self):
        config, sources = resolve_sender_config(env={}, data_dir=self.data)
        self.assertEqual(config["channel"], "console")
        self.assertTrue(all(s == "default" for s in sources.values()))

    def test_settings_file_is_used(self):
        self.write_settings({"channel": "webhook", "webhookUrl": SECRET_URL})
        config, sources = resolve_sender_config(env={}, data_dir=self.data)
        self.assertEqual(config["channel"], "webhook")
        self.assertEqual(config["webhookUrl"], SECRET_URL)
        self.assertEqual(sources["channel"], "settings")

    def test_env_beats_settings_and_argument_beats_env(self):
        self.write_settings({"channel": "webhook", "webhookUrl": SECRET_URL})
        env = {"REMINDER_CHANNEL": "outbox", "REMINDER_OUTBOX_DIR": "/tmp/ob"}
        config, sources = resolve_sender_config(env=env, data_dir=self.data)
        self.assertEqual(config["channel"], "outbox")
        self.assertEqual(sources["channel"], "env")
        # a field the env does not set still comes from the settings file
        self.assertEqual(config["webhookUrl"], SECRET_URL)
        config, sources = resolve_sender_config(
            channel="gateway", env=env, data_dir=self.data)
        self.assertEqual(config["channel"], "gateway")
        self.assertEqual(sources["channel"], "argument")

    def test_corrupt_settings_abort_resolution_and_build(self):
        self.write_settings("{broken")
        with self.assertRaises(SenderConfigError):
            resolve_sender_config(env={}, data_dir=self.data)
        with self.assertRaises(SenderConfigError):
            build_sender(env={}, data_dir=self.data)

    def test_build_sender_uses_settings_file(self):
        self.write_settings({"channel": "outbox"})
        sender = build_sender(env={}, data_dir=self.data)
        self.assertEqual(sender.name, "outbox")
        self.assertEqual(sender.path, self.data / "outbox" / "outbox.jsonl")


# ----------------------------------------------------------------- masking ----
class MaskingTest(unittest.TestCase):
    def test_webhook_url_mask_shows_scheme_host_and_last4_only(self):
        masked = mask_webhook_url(SECRET_URL)
        self.assertEqual(masked, "https://qyapi.weixin.qq.com/…7890")
        self.assertNotIn("SECRETKEY", masked)
        self.assertNotIn("webhook/send", masked)

    def test_short_secret_is_fully_masked(self):
        self.assertNotIn("abc", mask_secret("abc123"))
        long_masked = mask_secret("0123456789abcdefghijklmnopqrstuv")
        self.assertTrue(long_masked.startswith("0123456789ab"))
        self.assertTrue(long_masked.endswith("stuv"))


# ------------------------------------------------------------------- UI API ----
class SenderApiTest(TempDataCase):
    def test_get_view_masks_secrets_and_reports_sources(self):
        self.write_settings(
            {"channel": "webhook", "webhookUrl": SECRET_URL,
             "gatewayCmd": "send --token abcdefghijklmnopqrstuvwxyz {text}"})
        view = get_sender_view(self.data, env={})
        blob = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("SECRETKEY1234567890", blob)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", blob)
        self.assertEqual(view["effective"]["channel"], "webhook")
        self.assertEqual(view["effective"]["webhookUrlMasked"],
                         "https://qyapi.weixin.qq.com/…7890")
        self.assertEqual(view["sources"]["channel"], "settings")
        self.assertTrue(view["settingsFile"]["hasWebhookUrl"])

    def test_get_view_reports_env_override(self):
        self.write_settings({"channel": "outbox"})
        env = {"REMINDER_CHANNEL": "webhook",
               "REMINDER_WEBHOOK_URL": SECRET_URL}
        view = get_sender_view(self.data, env=env)
        self.assertEqual(view["effective"]["channel"], "webhook")
        self.assertEqual(view["sources"]["channel"], "env")
        self.assertNotIn("SECRETKEY1234567890",
                         json.dumps(view, ensure_ascii=False))

    def test_save_then_masked_roundtrip_keeps_real_url(self):
        save_sender_settings(
            self.data, {"channel": "webhook", "webhookUrl": SECRET_URL},
            env={})
        self.assertEqual(self.read_settings()["webhookUrl"], SECRET_URL)
        # nothing existed before the first save, so no backup yet
        self.assertEqual(list(self.data.glob("sender_settings.json.bak-*")),
                         [])

        view = get_sender_view(self.data, env={})
        masked = view["effective"]["webhookUrlMasked"]
        # the browser sends the masked display value straight back
        save_sender_settings(
            self.data, {"channel": "webhook", "webhookUrl": masked}, env={})
        self.assertEqual(self.read_settings()["webhookUrl"], SECRET_URL)
        self.assertEqual(
            len(list(self.data.glob("sender_settings.json.bak-*"))), 1)
        # an empty string also keeps the stored secret
        save_sender_settings(
            self.data, {"channel": "webhook", "webhookUrl": ""}, env={})
        self.assertEqual(self.read_settings()["webhookUrl"], SECRET_URL)
        # a genuinely new URL replaces it
        new_url = "https://push.example.com/hook/newkey9999"
        save_sender_settings(
            self.data, {"channel": "webhook", "webhookUrl": new_url}, env={})
        self.assertEqual(self.read_settings()["webhookUrl"], new_url)
        # explicit null clears it
        save_sender_settings(
            self.data, {"channel": "webhook", "webhookUrl": None}, env={})
        self.assertNotIn("webhookUrl", self.read_settings())

    def test_save_validation_rejects_and_writes_nothing(self):
        for payload in (
            {"channel": "pigeon"},
            {"channel": "webhook",
             "webhookUrl": "http://insecure.example/x"},
            {"channel": "gateway", "gatewayCmd": "send {bogus} {text}"},
            {"channel": "webhook", "webhookTimeoutSec": 999},
            {"channel": "webhook", "surprise": True},
        ):
            with self.assertRaises(WebUIError, msg=str(payload)):
                save_sender_settings(self.data, payload, env={})
            self.assertFalse((self.data / "sender_settings.json").exists())

    def test_handle_request_routes(self):
        body = json.dumps({"channel": "outbox"}).encode("utf-8")
        status, _, payload = handle_request(
            self.data, "POST", "/api/sender", body, env={})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)["ok"])
        status, _, payload = handle_request(
            self.data, "GET", "/api/sender", env={})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["effective"]["channel"],
                         "outbox")


# --------------------------------------------------------------- test sending ----
class FakeWebhookServer:
    """A real localhost HTTP endpoint recording POSTs; status switchable."""

    def __init__(self, status=200):
        self.status = status
        self.posts = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.posts.append(self.rfile.read(length))
                self.send_response(outer.status)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    @property
    def url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}/hook"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class SenderTestEndpointTest(TempDataCase):
    def test_injected_factory_success_and_failure(self):
        class OkSender:
            name = "webhook"

            def __init__(self):
                self.message = None

            def send(self, task, message, context=None):
                self.message = message
                return {"detail": "HTTP 200"}

        ok = OkSender()
        result = send_sender_test(
            self.data, {"channel": "webhook", "webhookUrl": SECRET_URL},
            env={}, sender_factory=lambda config: ok)
        self.assertTrue(result["ok"])
        self.assertEqual(ok.message, "【测试】上课提醒渠道连通性测试")

        class BadSender:
            name = "webhook"

            def send(self, task, message, context=None):
                raise SenderError(f"dial failed for {SECRET_URL}")

        result = send_sender_test(
            self.data, {"channel": "webhook", "webhookUrl": SECRET_URL},
            env={}, sender_factory=lambda config: BadSender())
        self.assertFalse(result["ok"])
        self.assertNotIn("SECRETKEY1234567890", result["error"])
        self.assertIn("7890", result["error"])  # the masked form is fine

    def _localhost_factory(self, fake):
        from src.sender import WebhookSender, _urllib_transport

        def factory(config):
            def transport(url, payload, timeout):
                return _urllib_transport(fake.url, payload, timeout)
            return WebhookSender(config["webhookUrl"], transport=transport)

        return factory

    def test_real_webhook_roundtrip_via_localhost(self):
        # The UI validator demands https, so the stored https URL stays in
        # the config while the injected transport delivers to the local
        # fake; WebhookSender + the real _urllib_transport do the POST.
        with FakeWebhookServer(200) as fake:
            result = send_sender_test(
                self.data, {"channel": "webhook", "webhookUrl": SECRET_URL},
                env={}, sender_factory=self._localhost_factory(fake))
            self.assertTrue(result["ok"], result)
            self.assertEqual(len(fake.posts), 1)
            posted = json.loads(fake.posts[0])
            self.assertEqual(posted["message"], "【测试】上课提醒渠道连通性测试")
            self.assertNotIn(SECRET_URL,
                             json.dumps(result, ensure_ascii=False))

    def test_localhost_webhook_500_is_reported_as_failure(self):
        with FakeWebhookServer(500) as fake:
            result = send_sender_test(
                self.data, {"channel": "webhook", "webhookUrl": SECRET_URL},
                env={}, sender_factory=self._localhost_factory(fake))
            self.assertFalse(result["ok"])
            self.assertIn("500", result["error"])

    def test_unconfigured_draft_is_a_clean_failure_not_a_crash(self):
        result = send_sender_test(self.data, {"channel": "webhook"}, env={})
        self.assertFalse(result["ok"])
        self.assertIn("未配置", result["error"])

    def test_gateway_draft_uses_stored_command_via_factory(self):
        seen = {}

        class RecSender:
            name = "gateway"

            def send(self, task, message, context=None):
                return {"detail": "ok"}

        def factory(config):
            seen.update(config)
            return RecSender()

        result = send_sender_test(
            self.data, {"channel": "gateway",
                        "gatewayCmd": "oc send --text {text}"},
            env={}, sender_factory=factory)
        self.assertTrue(result["ok"])
        self.assertEqual(seen["gatewayCmd"], "oc send --text {text}")
        # draft only: nothing was persisted
        self.assertFalse((self.data / "sender_settings.json").exists())


# ------------------------------------------------------------------- CLI e2e ----
class SchedulerCliSettingsTest(TempDataCase):
    def _run(self, *extra, extra_env=None):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("REMINDER_")}
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_scheduler.py"),
             "--tick", "--send", "--data-dir", str(self.data),
             "--now", NOW, *extra],
            capture_output=True, text=True, env=env)

    def test_settings_file_drives_send_without_flags_or_env(self):
        self.write_settings({"channel": "outbox"})
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[SENT via outbox]", result.stdout)
        self.assertTrue((self.data / "outbox" / "outbox.jsonl").exists())

    def test_env_overrides_settings_file(self):
        self.write_settings({"channel": "outbox"})
        result = self._run(extra_env={"REMINDER_CHANNEL": "console"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[SENT via console]", result.stdout)
        self.assertFalse((self.data / "outbox").exists())

    def test_corrupt_settings_abort_with_exit_2_and_no_dispatch(self):
        self.write_settings("{broken json")
        result = self._run()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("设置文件", result.stderr)
        tasks = json.loads((self.data / "reminders.json").read_text(
            encoding="utf-8"))
        self.assertTrue(all(t["status"] == "pending" for t in tasks))
        self.assertFalse((self.data / "outbox").exists())


if __name__ == "__main__":
    unittest.main()
