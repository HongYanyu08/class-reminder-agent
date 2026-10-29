"""deploy_setup.py tests.

- The cron system-event text must stay byte-faithful to the verified
  wording, with only the project path and the Python launcher
  substituted per platform (py on Windows — where the agent is also
  told to rewrite the scheduler's printed python3 command — and
  python3 elsewhere, where that rewrite note is dropped).
- The cost-saving step (2026-10-29): under --dry-run it must never
  invoke a mutation command; for real it runs the four gateway
  commands in order; a failure warns and continues, never raises.
- The config/openclaw.json template must pin the flash/pro models,
  the daily session reset, and the ${DEEPSEEK_API_KEY} placeholder.
"""
import contextlib, importlib.util, io, json, re, subprocess, sys, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

spec = importlib.util.spec_from_file_location(
    "deploy_setup", Path(__file__).resolve().parent.parent / "scripts" / "deploy_setup.py")
deploy_setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy_setup)


class SystemEventTextTest(unittest.TestCase):
    def test_windows_text_uses_py_and_keeps_rewrite_note(self):
        with mock.patch.object(deploy_setup, "is_windows", return_value=True):
            text = deploy_setup.system_event_text()
        self.assertIn(f"请在目录 {deploy_setup.ROOT} 下运行命令 "
                      "py scripts/run_scheduler.py --tick --send", text)
        self.assertIn("（命令里的 python3 换成 py）", text)

    def test_posix_text_uses_python3_and_drops_rewrite_note(self):
        with mock.patch.object(deploy_setup, "is_windows", return_value=False):
            text = deploy_setup.system_event_text()
        self.assertIn("python3 scripts/run_scheduler.py --tick --send", text)
        self.assertNotIn("换成 py", text)

    def test_wording_anchors(self):
        with mock.patch.object(deploy_setup, "is_windows", return_value=True):
            text = deploy_setup.system_event_text()
        self.assertTrue(text.startswith("定时上课提醒检查：请在目录 "))
        self.assertIn("--from 今天日期（格式 YYYY-MM-DD，每天运行时换成当天日期）", text)
        self.assertIn("逐条把每行里 taskId 后面的提醒正文原样作为独立消息发到当前对话", text)
        self.assertTrue(text.endswith("保持沉默。"))


def _ok_proc():
    return subprocess.CompletedProcess(args=[], returncode=0,
                                       stdout="", stderr="")


class CostSavingStepTest(unittest.TestCase):
    def test_dry_run_never_calls_run_openclaw(self):
        out = io.StringIO()
        with mock.patch.object(deploy_setup, "run_openclaw") as m, \
                contextlib.redirect_stdout(out):
            result = deploy_setup.setup_cost_saving(dry_run=True)
        self.assertTrue(result)
        m.assert_not_called()
        printed = out.getvalue()
        self.assertEqual(printed.count("[dry-run] 将执行省钱设置"), 4)
        self.assertIn("models set deepseek/deepseek-v4-flash", printed)
        self.assertIn("session.reset.atHour", printed)

    def test_real_run_executes_four_commands_in_order(self):
        with mock.patch.object(deploy_setup, "run_openclaw",
                               return_value=_ok_proc()) as m, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = deploy_setup.setup_cost_saving(dry_run=False)
        self.assertTrue(result)
        called = [c.args[0] for c in m.call_args_list]
        self.assertEqual(called, [
            ["models", "set", "deepseek/deepseek-v4-flash"],
            ["gateway", "restart"],
            ["config", "set", "session.reset.mode", "daily"],
            ["config", "set", "session.reset.atHour", "4"],
        ])
        self.assertIn("✓ 省钱设置已应用", out.getvalue())

    def test_failure_warns_and_continues_without_raising(self):
        bad = subprocess.CompletedProcess(args=[], returncode=1,
                                          stdout="", stderr="boom")
        with mock.patch.object(
                deploy_setup, "run_openclaw",
                side_effect=[bad, _ok_proc(), _ok_proc(), _ok_proc()]) as m, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = deploy_setup.setup_cost_saving(dry_run=False)
        self.assertTrue(result)  # failure must not fail the setup
        self.assertEqual(m.call_count, 4)  # later commands still ran
        printed = out.getvalue()
        self.assertIn("⚠ 省钱设置未完成", printed)
        self.assertIn("models set deepseek/deepseek-v4-flash", printed)

    def test_cli_unavailable_still_returns_true(self):
        with mock.patch.object(deploy_setup, "run_openclaw",
                               return_value=None), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = deploy_setup.setup_cost_saving(dry_run=False)
        self.assertTrue(result)
        self.assertIn("⚠", out.getvalue())


class OpenclawTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = (Path(__file__).resolve().parent.parent
                    / "config" / "openclaw.json")
        cls.raw = cls.path.read_text(encoding="utf-8")
        cls.cfg = json.loads(cls.raw)

    def test_models_are_flash_primary_pro_fallback(self):
        defaults = self.cfg["agents"]["defaults"]
        self.assertEqual(defaults["model"]["primary"],
                         "deepseek/deepseek-v4-flash")
        self.assertEqual(defaults["model"]["fallbacks"],
                         ["deepseek/deepseek-v4-pro"])
        self.assertEqual(set(defaults["models"]),
                         {"deepseek/deepseek-v4-flash",
                          "deepseek/deepseek-v4-pro"})
        provider_models = {m["id"] for m in
                           self.cfg["models"]["providers"]["deepseek"]["models"]}
        self.assertEqual(provider_models,
                         {"deepseek-v4-flash", "deepseek-v4-pro"})
        agent = self.cfg["agents"]["list"][0]
        self.assertEqual(agent["model"], "deepseek/deepseek-v4-flash")

    def test_session_reset_daily_at_4(self):
        self.assertEqual(self.cfg["session"]["reset"],
                         {"mode": "daily", "atHour": 4})
        self.assertEqual(self.cfg["session"]["dmScope"], "per-channel-peer")

    def test_api_key_is_env_placeholder_only(self):
        api_key = self.cfg["models"]["providers"]["deepseek"]["apiKey"]
        self.assertEqual(api_key, "${DEEPSEEK_API_KEY}")
        self.assertIsNone(re.search(r"sk-[A-Za-z0-9]{16,}", self.raw))


if __name__ == "__main__":
    unittest.main()
