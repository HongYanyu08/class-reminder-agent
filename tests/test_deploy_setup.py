"""deploy_setup.py: the cron system-event text must stay byte-faithful
to the verified wording, with only the project path and the Python
launcher substituted per platform (py on Windows — where the agent is
also told to rewrite the scheduler's printed python3 command — and
python3 elsewhere, where that rewrite note is dropped)."""
import importlib.util, sys, unittest
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


if __name__ == "__main__":
    unittest.main()
