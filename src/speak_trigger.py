"""Speak trigger: wake the OpenClaw main session ONLY when there is
something queued for it to say.

Part of the root fix for the 2026-10-28 incident (see
src/session_dispatch.py): the deterministic tick moved out of the
main session into the Windows Task Scheduler, so the session no
longer burns an LLM turn every 5 minutes. But speaking still has to
happen inside the main session (iLink context token), so after each
OS-level tick this trigger checks reminders.json: if any task is in
status "queued", it fires ONE one-shot `--session main
--system-event` cron wake whose text tells the agent to read the
state-based speak list (`run_scheduler.py --speak-pending`), speak
every line verbatim, and run the printed --confirm-sent command.
Nothing queued -> no wake, no output, exit 0: silence costs nothing.

The wake command is a TEMPLATE with {stamp} and {text} placeholders,
because the exact one-shot syntax of `openclaw cron add` has not been
verified against the target machine's OpenClaw build: the default
below is the documented shape, DEPLOY.md tells the operator to check
`openclaw.cmd cron add --help` on the laptop first, and the env var
REMINDER_SPEAK_CMD overrides the template without touching code.
The wake text itself contains no quote characters on purpose, so the
template may safely wrap {text} in quotes if that build requires it.

Throttle: wakes are rate-limited to one per
SPEAK_TRIGGER_MIN_INTERVAL_MIN minutes per data dir, recorded in
<data-dir>/speak_trigger_state.json (runtime file, gitignored). The
tick requeues unconfirmed tasks every ~15 minutes anyway, so a
throttled wake loses nothing: the batch is re-listed from state by
whichever wake comes next.
"""
import json, os, subprocess, sys
from datetime import datetime, timedelta
from pathlib import Path

from .session_dispatch import list_queued_tasks

SPEAK_TRIGGER_MIN_INTERVAL_MIN = 10
STATE_FILENAME = "speak_trigger_state.json"
SPEAK_CMD_ENV = "REMINDER_SPEAK_CMD"

# One-shot wake: fire ~20 s later, in the main session, delete after
# the run. VERIFY on the target machine (`openclaw.cmd cron add
# --help`) and override via REMINDER_SPEAK_CMD if the syntax differs.
DEFAULT_SPEAK_CMD_TEMPLATE = (
    "openclaw.cmd cron add --name cr-speak-{stamp} --at +20s "
    "--session main --system-event {text} --delete-after-run")

# Fixed wake wording (no quote characters, by design — see above).
SPEAK_WAKE_TEXT = (
    "【上课提醒·待念】请在项目目录运行 py scripts\\run_scheduler.py "
    "--speak-pending，把输出中 [SESSION-SPEAK] 区块的每一行原样、逐条单独"
    "发到本微信会话，然后运行输出末尾给出的 --confirm-sent 命令。"
    "若区块为空，只回复 NO_REPLY，不要输出任何其他文字。")


def _shell_runner(command):
    """Production runner: execute the rendered template via the shell
    (on Windows this resolves openclaw.cmd through cmd.exe). Returns
    the CompletedProcess; may raise, the caller converts to exit 1."""
    return subprocess.run(command, shell=True, capture_output=True, text=True)


def build_speak_command(now, template=None):
    """Render the wake command for this moment. {stamp} keeps every
    one-shot cron name unique; {text} is the fixed wake wording."""
    template = template or DEFAULT_SPEAK_CMD_TEMPLATE
    stamp = now.strftime("%Y%m%d-%H%M%S")
    return template.replace("{stamp}", stamp).replace("{text}", SPEAK_WAKE_TEXT)


def _load_tasks(data):
    path = data / "reminders.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _last_triggered_at(data):
    path = data / STATE_FILENAME
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return datetime.fromisoformat(state["lastTriggeredAt"])
    except (ValueError, KeyError, TypeError):
        return None  # unreadable state must not throttle forever


def _save_trigger_state(data, now, task_ids):
    state = {"lastTriggeredAt": now.isoformat(), "taskIds": list(task_ids)}
    (data / STATE_FILENAME).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def run_speak_trigger(data_dir, now=None, runner=None, env=None,
                      dry_run=False,
                      min_interval_min=SPEAK_TRIGGER_MIN_INTERVAL_MIN):
    """Fire the one-shot wake if reminders are queued. Returns an exit
    code: 0 = nothing to do / wake fired / dry-run printed; 1 = the
    runner failed (reported on stderr, never an uncaught exception);
    2 = reminders.json exists but cannot be parsed.

    now / runner / env are injectable for tests; production defaults
    are the local time, the shell runner and os.environ.
    """
    data = Path(data_dir)
    now = now or datetime.now().astimezone()
    env = os.environ if env is None else env
    runner = runner or _shell_runner
    try:
        tasks = _load_tasks(data)
    except ValueError as e:
        print(f"[说话触发中止] reminders.json 无法解析：{e}", file=sys.stderr)
        return 2
    queued = list_queued_tasks(tasks)
    if not queued:
        return 0
    last = _last_triggered_at(data)
    try:
        throttled = last is not None and \
            now - last < timedelta(minutes=int(min_interval_min))
    except TypeError:
        throttled = False  # naive/aware mix in a hand-edited state file
    if throttled:
        return 0  # throttled: a wake went out recently for this queue
    command = build_speak_command(
        now, template=env.get(SPEAK_CMD_ENV) or None)
    if dry_run:
        print(command)
        return 0
    try:
        proc = runner(command)
    except Exception as e:  # missing CLI, shell error, ...
        print(f"[说话触发失败] 唤醒命令执行异常：{type(e).__name__}: {e}",
              file=sys.stderr)
        return 1
    if getattr(proc, "returncode", 0) != 0:
        detail = (getattr(proc, "stderr", "") or "").strip()[:200]
        print(f"[说话触发失败] 唤醒命令退出码 {proc.returncode}"
              + (f"：{detail}" if detail else ""), file=sys.stderr)
        return 1
    _save_trigger_state(data, now, [e["taskId"] for e in queued])
    print(f"[说话触发] 已发出一次性主会话唤醒，待念 {len(queued)} 条。")
    return 0
