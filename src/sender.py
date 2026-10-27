"""Reminder sending layer: turn a due ReminderTask into an actual message.

Pluggable channels, selected by the REMINDER_CHANNEL environment variable
(or the run_scheduler --channel flag):

  console  print the message (the original MVP behaviour; default + fallback)
  outbox   append one JSON line per message to a JSONL file that a human or
           the OpenClaw side can consume/verify locally
           (dir: REMINDER_OUTBOX_DIR, default <data-dir>/outbox)
  webhook  POST the message as JSON to REMINDER_WEBHOOK_URL
           (optional REMINDER_WEBHOOK_TIMEOUT, seconds, default 10)
  gateway  run an operator-configured command, REMINDER_GATEWAY_CMD, with
           placeholders {text} {task_id} {type} {user_id}. config/openclaw.json
           ships an empty "channels" object and a loopback gateway skeleton
           but no message-endpoint contract this project can rely on, so the
           actual command must come from the deployment's OpenClaw version
           and is only integration-tested against a real OpenClaw install.
           Nothing about that external CLI is invented here.

Secrets (webhook URL, gateway command, any tokens they embed) are read from
the environment ONLY — never from data files, never written to disk by us.

Failure model (see scheduler.dispatch_due for the state writes):
- a send either succeeds (-> task sent) or raises SenderError (-> failed).
- SenderConfigError is a SenderError raised BEFORE anything is dispatched
  (channel missing/misconfigured): the CLI reports it and exits without
  touching task states, so a typo'd channel never marks tasks failed.
"""
import json, os, shlex, subprocess, sys, urllib.error, urllib.request
from datetime import datetime
from pathlib import Path

CHANNELS = ("console", "outbox", "webhook", "gateway")


class SenderError(Exception):
    """One send attempt failed (transport error, bad status code, ...)."""


class SenderConfigError(SenderError):
    """The channel is not configured / misconfigured. Raised before dispatch."""


def _now_iso():
    return datetime.now().astimezone().isoformat()


# ---------------------------------------------------------------- channels ----
class ConsoleSender:
    name = "console"

    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout

    def send(self, task, message, context=None):
        print(message, file=self.stream)
        return {"detail": "printed"}


class OutboxSender:
    name = "outbox"
    FILENAME = "outbox.jsonl"

    def __init__(self, outbox_dir):
        self.dir = Path(outbox_dir)

    @property
    def path(self):
        return self.dir / self.FILENAME

    def send(self, task, message, context=None):
        context = context or {}
        self.dir.mkdir(parents=True, exist_ok=True)
        record = {
            "taskId": task["id"], "occurrenceId": task.get("occurrenceId"),
            "type": task.get("type"), "scheduledAt": task.get("scheduledAt"),
            "sentAt": _now_iso(), "channel": self.name, "message": message,
            "courseName": (context.get("occurrence") or {}).get("courseName"),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {"detail": str(self.path)}


def _urllib_transport(url, payload, timeout):
    """Default webhook transport: POST JSON via urllib (stdlib only).
    Returns (status_code, body_text). Raises on transport-level errors."""
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:  # a response arrived, just not 2xx
        return e.code, e.read().decode("utf-8", "replace")


class WebhookSender:
    name = "webhook"

    def __init__(self, url, timeout=10, transport=None):
        if not url:
            raise SenderConfigError(
                "webhook 通道未配置：请设置环境变量 REMINDER_WEBHOOK_URL")
        self.url, self.timeout = url, timeout
        self.transport = transport or _urllib_transport

    def send(self, task, message, context=None):
        payload = {
            "taskId": task["id"], "occurrenceId": task.get("occurrenceId"),
            "type": task.get("type"), "scheduledAt": task.get("scheduledAt"),
            "sentAt": _now_iso(), "channel": self.name, "message": message,
            "text": message,
        }
        try:
            status, body = self.transport(self.url, payload, self.timeout)
        except SenderError:
            raise
        except Exception as e:  # timeout / DNS / connection refused / ...
            raise SenderError(f"webhook 请求失败：{type(e).__name__}: {e}") from e
        if not (200 <= int(status) < 300):
            raise SenderError(f"webhook 返回 HTTP {status}：{str(body)[:200]}")
        return {"detail": f"HTTP {status}"}


class GatewaySender:
    """Adapter for sending through the deployment's OpenClaw gateway.

    The send is delegated to an operator-configured command template
    (REMINDER_GATEWAY_CMD). Placeholders are substituted per argument, the
    command is split with shlex and run WITHOUT a shell. A non-zero exit
    code is a send failure. Which exact command a deployment should use
    depends on its OpenClaw version's channel CLI — see README 发送通道一节.
    """
    name = "gateway"

    def __init__(self, command_template, runner=None, timeout=30):
        if not command_template:
            raise SenderConfigError(
                "gateway 通道未配置：请设置环境变量 REMINDER_GATEWAY_CMD"
                "（由部署环境的 OpenClaw 命令构成的命令模板）")
        self.template, self.timeout = command_template, timeout
        self.runner = runner or self._run

    @staticmethod
    def _run(argv, timeout):
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    def build_argv(self, task, message, context=None):
        context = context or {}
        values = {"text": message, "task_id": task["id"],
                  "type": task.get("type", ""),
                  "user_id": (context.get("user") or {}).get("id", "")}
        return [part.format(**values) for part in shlex.split(self.template)]

    def send(self, task, message, context=None):
        argv = self.build_argv(task, message, context)
        try:
            proc = self.runner(argv, self.timeout)
        except Exception as e:
            raise SenderError(f"gateway 命令执行失败：{type(e).__name__}: {e}") from e
        if proc.returncode != 0:
            raise SenderError(
                f"gateway 命令退出码 {proc.returncode}：{(proc.stderr or '').strip()[:200]}")
        return {"detail": " ".join(argv[:1])}


# ----------------------------------------------------------------- factory ----
def build_sender(channel=None, env=None, data_dir=None, stream=None, transport=None):
    """Build a sender. Channel resolution: argument > REMINDER_CHANNEL env >
    'console'. Raises SenderConfigError with an actionable message when the
    chosen channel is unknown or its required env config is missing."""
    env = os.environ if env is None else env
    channel = (channel or env.get("REMINDER_CHANNEL") or "console").strip().lower()
    if channel == "console":
        return ConsoleSender(stream=stream)
    if channel == "outbox":
        outbox_dir = env.get("REMINDER_OUTBOX_DIR")
        if not outbox_dir:
            base = Path(data_dir) if data_dir else Path(__file__).resolve().parent.parent / "data"
            outbox_dir = base / "outbox"
        return OutboxSender(outbox_dir)
    if channel == "webhook":
        timeout_raw = env.get("REMINDER_WEBHOOK_TIMEOUT", "10")
        try:
            timeout = float(timeout_raw)
        except (TypeError, ValueError):
            raise SenderConfigError(
                f"REMINDER_WEBHOOK_TIMEOUT 不是数字：{timeout_raw!r}")
        return WebhookSender(env.get("REMINDER_WEBHOOK_URL"),
                             timeout=timeout, transport=transport)
    if channel == "gateway":
        return GatewaySender(env.get("REMINDER_GATEWAY_CMD"))
    raise SenderConfigError(
        f"未知发送通道 REMINDER_CHANNEL={channel!r}，可用通道：{', '.join(CHANNELS)}")
