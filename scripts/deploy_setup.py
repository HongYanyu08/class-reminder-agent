#!/usr/bin/env python3
"""One-command deployment setup for the class-reminder agent.

Run this ONCE on the machine that hosts OpenClaw (after OpenClaw itself
is installed + onboarded and the WeChat plugin is logged in — see
DEPLOY.md). It performs, in order:

  1. environment checks (printed as ✓ / ✗ / ⚠ with a fix hint each):
     Python >= 3.10, the openclaw CLI is reachable, and the WeChat
     plugin (openclaw-weixin) appears installed/enabled;
  2. data/sender_settings.json: created as {"channel": "session"} when
     missing; an existing file is NEVER overwritten unless --force
     (it may hold a webhook URL, which is a secret);
  3. cost-saving gateway settings (2026-10-29 field-tested, see
     DEPLOY.md "省钱与防烧干设置"): the default model is set to
     deepseek/deepseek-v4-flash (the pro default burned ~61M tokens /
     ¥16.52 in one day, and deepseek-chat no longer exists on the
     DeepSeek API), the gateway is restarted, and the main session is
     set to reset daily at 04:00 so context cannot grow unbounded.
     A failed command only prints ⚠ plus the manual command to run —
     it never fails the whole setup;
  4. the recurring cron job cr-tick-5min (--every 5m, --session main,
     --wake now) whose system-event tells the main-session agent to run
     the scheduler tick, speak any [SESSION-SPEAK] reminders verbatim
     into the WeChat conversation, then --confirm-sent them. If a job
     with that name already exists the step is skipped, so the script
     is safe to re-run.

--dry-run prints every step and every command without writing any file
or creating any job (read-only probes — version/list — still run, so
the output reflects the real machine).

Windows notes: the CLI is openclaw.cmd there and commands are executed
through `cmd /c` (a bare .cmd cannot be CreateProcess'd directly);
elsewhere the binary is plain `openclaw` and the agent is told to run
`python3` instead of `py`. Stdlib only.

Exit codes: 0 all steps ok or already done · 1 at least one ✗ check.
"""
import argparse, json, os, shlex, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRON_NAME = "cr-tick-5min"

# The wake-up text the cron job delivers into the main session every
# 5 minutes. {root} is this project's absolute path, {py} the platform
# Python launcher ("py" on Windows, "python3" elsewhere). Wording is
# fixed on purpose: the agent must not improvise around it.
SYSTEM_EVENT_TEMPLATE = (
    "定时上课提醒检查：请在目录 {root} 下运行命令 {py} scripts/run_scheduler.py "
    "--tick --send --from 今天日期（格式 YYYY-MM-DD，每天运行时换成当天日期）。"
    "如果输出里有 [SESSION-SPEAK] 行，就逐条把每行里 taskId 后面的提醒正文原样作为"
    "独立消息发到当前对话，不要改动文字，不要加任何解释；全部发完后，再运行输出里"
    "给出的 confirm-sent 命令回写确认（命令里的 python3 换成 py）。"
    "如果没有 [SESSION-SPEAK] 行，或者命令出错，就不要发任何消息，保持沉默。")


def is_windows():
    return os.name == "nt"


def python_launcher():
    return "py" if is_windows() else "python3"


def system_event_text():
    text = SYSTEM_EVENT_TEMPLATE.format(root=str(ROOT), py=python_launcher())
    if not is_windows():
        # The "(把命令里的 python3 换成 py)" instruction only applies
        # to Windows, where the scheduler prints a python3 command line.
        text = text.replace("（命令里的 python3 换成 py）", "")
    return text


def openclaw_exe():
    return shutil.which("openclaw.cmd" if is_windows() else "openclaw")


def _display(cmd):
    return subprocess.list2cmdline(cmd) if is_windows() else shlex.join(cmd)


def run_openclaw(args, dry_run=False, timeout=60):
    """Run one openclaw command. Returns a CompletedProcess, or None
    when the CLI is missing / the call timed out (callers turn that
    into a ⚠/✗ line instead of crashing). With dry_run, read-only
    probes still run; pass dry_run only for probes — mutations are
    gated by the callers, which print the command instead."""
    exe = openclaw_exe()
    if exe is None:
        return None
    cmd = [exe, *args]
    actual = ["cmd", "/c", *cmd] if is_windows() else cmd
    try:
        return subprocess.run(actual, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _first_line(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


# ---------------------------------------------------------------- checks ----
def check_python():
    ok = sys.version_info >= (3, 10)
    version = ".".join(str(x) for x in sys.version_info[:3])
    print(f"{'✓' if ok else '✗'} Python {version}"
          + ("" if ok else " — 需要 Python 3.10+，请先升级（见 DEPLOY.md）"))
    return ok


def check_openclaw_cli():
    exe = openclaw_exe()
    if exe is None:
        print("✗ 找不到 openclaw 命令 — 请先安装 OpenClaw 并完成 onboard（见 DEPLOY.md 第 1 步）")
        return False
    res = run_openclaw(["--version"])
    version = _first_line(res.stdout) if res is not None else ""
    print(f"✓ openclaw CLI：{exe}" + (f"（{version}）" if version else ""))
    return True


def check_weixin_plugin():
    """Best-effort: look for openclaw-weixin in `plugins list`, falling
    back to `channels status`. If neither output can be interpreted,
    warn (⚠) instead of failing — CLI output formats vary by version,
    and a human can confirm with `openclaw channels status`."""
    saw_output = False
    for args in (["plugins", "list"], ["channels", "status"]):
        res = run_openclaw(args)
        if res is None or (res.returncode != 0 and not res.stdout):
            continue
        saw_output = True
        if "openclaw-weixin" in (res.stdout or "") + (res.stderr or ""):
            print("✓ 微信插件 openclaw-weixin 已安装/启用")
            return True
    if saw_output:
        print("✗ 未发现微信插件 openclaw-weixin — 请先安装并扫码登录：")
        print('    openclaw plugins install "@tencent-weixin/openclaw-weixin"')
        print("    openclaw channels login --channel openclaw-weixin")
        return False
    print("⚠ 无法判断微信插件状态（openclaw 命令无输出）— 请手动运行 "
          "`openclaw channels status` 确认 openclaw-weixin 已登录")
    return True  # a warning, not a failure


# ----------------------------------------------------------------- steps ----
def setup_sender_settings(force, dry_run):
    path = ROOT / "data" / "sender_settings.json"
    content = json.dumps({"channel": "session"}, ensure_ascii=False)
    existed = path.exists()
    if existed and not force:
        print(f"✓ data/sender_settings.json 已存在，保留不覆盖"
              f"（{path}；要重置请加 --force）")
        return True
    if dry_run:
        print(f"[dry-run] 将写入 {path}：{content}")
        return True
    path.write_text(content + "\n", encoding="utf-8")
    print(f"✓ 已写入 data/sender_settings.json：{content}"
          + ("（--force 覆盖）" if existed else ""))
    return True


# Cost-saving gateway mutations, in order (field-tested 2026-10-29 —
# see DEPLOY.md "省钱与防烧干设置"): flash as the default model instead
# of pro (~61M tokens / ¥16.52 burned in one day on pro), a gateway
# restart so the new model + config take effect, then a daily 04:00
# session reset so the main session's context cannot grow unbounded.
# These are MUTATIONS: unlike the read-only probes they must never run
# under --dry-run (setup_cost_saving prints them instead).
COST_SAVING_COMMANDS = [
    ["models", "set", "deepseek/deepseek-v4-flash"],
    ["gateway", "restart"],
    ["config", "set", "session.reset.mode", "daily"],
    ["config", "set", "session.reset.atHour", "4"],
]


def setup_cost_saving(dry_run):
    """Apply the cost-saving gateway settings. Best-effort by design:
    a failed command prints ⚠ and the exact command to run by hand,
    then the step carries on — it never raises and never fails the
    whole setup, so it always returns True."""
    exe = openclaw_exe() or "openclaw"
    failed = 0
    for args in COST_SAVING_COMMANDS:
        shown = _display([exe, *args])
        if dry_run:
            print(f"[dry-run] 将执行省钱设置：{shown}")
            continue
        res = run_openclaw(args, timeout=120)
        if res is not None and res.returncode == 0:
            continue
        failed += 1
        detail = _first_line(res.stderr) if res is not None else "openclaw 命令不可用"
        print(f"⚠ 省钱设置未完成：{shown}（{detail}）— 可手动执行这条命令")
    if dry_run:
        return True
    if failed:
        print(f"⚠ 省钱设置部分完成（{len(COST_SAVING_COMMANDS) - failed}"
              f"/{len(COST_SAVING_COMMANDS)} 条成功），未成功的按上面命令手动补跑即可")
    else:
        print("✓ 省钱设置已应用：默认模型 deepseek-v4-flash + 会话每天 04:00 自动重置")
    return True


def setup_cron(dry_run):
    list_res = run_openclaw(["cron", "list"])
    if (list_res is not None and list_res.returncode == 0
            and CRON_NAME in (list_res.stdout or "")):
        print(f"✓ 定时任务 {CRON_NAME} 已存在，跳过创建（可重复运行本脚本）")
        return True
    if list_res is None or list_res.returncode != 0:
        print(f"⚠ 无法确认 {CRON_NAME} 是否已存在（cron list 无结果），仍尝试创建；"
              "若提示重名，说明它已经在了，可忽略")
    add_cmd = [openclaw_exe() or "openclaw", "cron", "add",
               "--name", CRON_NAME, "--every", "5m",
               "--session", "main", "--wake", "now",
               "--system-event", system_event_text()]
    if dry_run:
        print(f"[dry-run] 将创建定时任务：{_display(add_cmd)}")
        return True
    res = run_openclaw(["cron", "add", "--name", CRON_NAME, "--every", "5m",
                        "--session", "main", "--wake", "now",
                        "--system-event", system_event_text()],
                       timeout=120)
    if res is not None and res.returncode == 0:
        print(f"✓ 已创建定时任务 {CRON_NAME}（每 5 分钟唤醒主会话跑一次 tick）")
        return True
    detail = _first_line(res.stderr) if res is not None else "openclaw 命令不可用"
    print(f"✗ 创建定时任务失败：{detail}")
    print(f"  可手动执行：{_display(add_cmd)}")
    return False


NEXT_STEPS = """\
后续步骤（详见 DEPLOY.md）：
  0. 省钱设置已在上面一并应用（默认模型 deepseek-v4-flash + 会话每天
     04:00 自动重置）；另请去 DeepSeek 平台（platform.deepseek.com）
     打开「余额预警」并设一个阈值——余额耗尽没有任何先兆，表现就是
     智能体全天静默，预警是唯一的提前量。
  1. 导入你自己的课表：仓库里只有示例数据。截图先转文字，再跑
     scripts/import_timetable.py 预览 → 核对 → --apply 写入；
     或直接用你自己的 term.json / courses.json / exceptions.json 替换 data/ 三件套。
  2. 给微信里的「微信 ClawBot」随便发一条消息，刷新投递用的 context token
     （长时间没互动时 token 会失效，主动发消息前必须先刷新；VPN/代理先关掉）。
  3. 等第一次自动提醒验证：当晚 21:00 的前晚预告应自动出现在微信里；
     笔记本保持开机、不合盖，网关随登录自启，无需再手动打开什么。"""


def main():
    ap = argparse.ArgumentParser(
        description="一键部署配置：检查环境 + 写 sender_settings + 省钱设置 + 建 cr-tick-5min 定时任务")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将要执行的每一步和命令，不写文件、不建任务")
    ap.add_argument("--force", action="store_true",
                    help="data/sender_settings.json 已存在时也覆盖为 {\"channel\": \"session\"}")
    args = ap.parse_args()

    print(f"项目目录：{ROOT}")
    if args.dry_run:
        print("[dry-run] 只预览：不会写入任何文件，也不会创建定时任务。")
    ok = True
    ok = check_python() and ok
    cli_ok = check_openclaw_cli()
    ok = cli_ok and ok
    if cli_ok:
        ok = check_weixin_plugin() and ok
        ok = setup_sender_settings(args.force, args.dry_run) and ok
        ok = setup_cost_saving(args.dry_run) and ok
        ok = setup_cron(args.dry_run) and ok
    else:
        print("（跳过插件检查、渠道配置、省钱设置与定时任务创建：先装好 OpenClaw 再重跑本脚本）")
    print()
    print(NEXT_STEPS)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
