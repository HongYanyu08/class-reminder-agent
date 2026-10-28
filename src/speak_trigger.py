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
The wake text itself contains no spaces and no quote characters on
purpose (see SPEAK_WAKE_TEXT): under cmd.exe it stays a single
argument no matter how the template is quoted, because anything
with spaces risks being split into several arguments when the
laptop's REMINDER_SPEAK_CMD override dropped the wrapping quotes
(2026-10-28 smoke test: the wake command exited 1).

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
from types import SimpleNamespace

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

# Fixed wake wording. Deliberately ZERO half-width spaces and zero
# quote characters: on the laptop the rendered command runs through
# cmd.exe (GBK locale), where a spaced value can be split into
# several arguments depending on how the operator's
# REMINDER_SPEAK_CMD template quotes {text} — the 2026-10-28 smoke
# test's wake command exited 1 for exactly that class of reason.
# A single space-free token is one argument under every quoting.
# test_speak_trigger pins both properties; do not add spaces.
SPEAK_WAKE_TEXT = (
    "【上课提醒·待念】请按HEARTBEAT.md的待念流程处理："
    "运行待念清单命令、逐条原样念出待念提醒、再运行确认命令；"
    "没有待念只回复NO_REPLY")


def _shell_runner(command):
    """Production runner: execute the rendered template via the shell
    (on Windows this resolves openclaw.cmd through cmd.exe).

    Output is captured as BYTES and decoded as UTF-8 with
    replacement. The laptop console locale is GBK; decoding the
    CLI's UTF-8 output with the locale codec made the subprocess
    reader thread die with UnicodeDecodeError in the 2026-10-28
    smoke test. Returns a namespace with text stdout/stderr so the
    caller (and its failure messages) never touches bytes."""
    proc = subprocess.run(command, shell=True, capture_output=True)
    return SimpleNamespace(
        returncode=proc.returncode,
        stdout=proc.stdout.decode("utf-8", errors="replace"),
        stderr=proc.stderr.decode("utf-8", errors="replace"))


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
