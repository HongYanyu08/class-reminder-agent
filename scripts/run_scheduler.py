#!/usr/bin/env python3
"""Scheduler tick (called by HEARTBEAT / cron every 5 minutes).

Usage:
  python3 scripts/run_scheduler.py --expand --from 2026-09-07 --days 14
  python3 scripts/run_scheduler.py --tick --now 2026-09-13T21:00:00+08:00
      # preview (default): regenerate + print due reminders, send nothing
  python3 scripts/run_scheduler.py --tick --send [--channel outbox]
      # really send due reminders through a channel and write the outcome
      # (sent/failed) back into reminders.json
  python3 scripts/run_scheduler.py --tick --send --dry-run
      # preview only, guaranteed: nothing sent, no file written at all
  python3 scripts/run_scheduler.py --confirm-sent <id1,id2,...>
      # session channel phase 2: after the agent in the OpenClaw main
      # session has spoken the queued reminders aloud, flip exactly
      # those queued tasks to sent (standalone mode, never with --tick)
  python3 scripts/run_scheduler.py --speak-pending
      # read-only: print the [SESSION-SPEAK] block for every task that
      # is queued RIGHT NOW (state-based, so tasks queued by earlier
      # ticks are listed too) plus the [SESSION-CONFIRM] command line.
      # This is what the event-driven wake (scripts/speak_trigger.py)
      # tells the agent to run. Writes nothing; requeue / expire /
      # escalate remain the tick's job. With nothing queued it prints
      # exactly one line: [SESSION-SPEAK] （无）

--from defaults to TODAY in the term's timezone (the Windows task
scheduler entry runs --tick --send with no --from at all); pass it
explicitly to replay a past window.

Channels (src/sender.py): console (default) / outbox / webhook / gateway /
session. The session channel is two-phase (src/session_dispatch.py):
--tick --send only queues due reminders into session_outbox.jsonl
(status queued) and prints [SESSION-SPEAK] lines for the heartbeat agent
to read aloud verbatim; --confirm-sent then marks them sent. Queued
tasks left unconfirmed for over 15 minutes fall back to pending and are
re-queued on the next tick. If a fallback provider is configured
(fallbackChannel/fallbackKey in the sender settings, src/fallback.py),
a task first queued at least fallbackAfterMin minutes ago (default 15)
without being confirmed is additionally re-sent through that
token-free provider (Server酱 / PushPlus) and marked
sent with channel "fallback:<provider>" — the fix for a WeChat
context token that went stale overnight.
Configuration resolves in src/sender_settings.py: --channel flag >
REMINDER_* env vars > data/sender_settings.json (the file maintained by
the Web UI's 发送渠道 page; gitignored because the webhook URL is a
secret) > defaults. A corrupt or invalid settings file aborts the send
with exit code 2 — never a silent fallback to another channel. Departure
reminders gain weather lead time when
WEATHER_LAT / WEATHER_LON are set (src/weather.py, Open-Meteo, cached 3 h
in data/weather_cache.json); unconfigured or failing weather simply means
no extra minutes. Already-sent tasks are never re-sent: regeneration
merges the stored runtime state back in (scheduler.merge_task_states).
Failed tasks are not retried automatically — flip status back to pending
in reminders.json to retry by hand. A --now without a UTC offset is read
in the term's timezone (never a naive/aware TypeError). Pending tasks
whose moment passed more than staleExpireMin minutes ago (preference
in user.json, default 120) are marked "expired" instead of being sent,
so a first deployment or a long shutdown does not produce a flood of
stale reminders.

Exit codes: 0 ok · 1 at least one send failed · 2 channel misconfigured.
"""
import argparse, json, sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.expand import expand
from src.scheduler import generate_reminders, find_due, compose_message, \
    merge_task_states, dispatch_due, generate_follow_ups, reactivate_snoozed, \
    expire_stale_tasks, normalize_now_iso, data_tz_name, \
    DEFAULT_FOLLOWUP_GRACE_MIN, DEFAULT_STALE_EXPIRE_MIN
from src.sender import build_sender, SenderConfigError
from src.fallback import build_fallback_sender
from src.session_dispatch import queue_due_for_session, confirm_session_sent, \
    requeue_stale_session_tasks, escalate_stale_queued_tasks, \
    list_queued_tasks, SESSION_REQUEUE_MIN
from src.weather import build_weather_provider

ROOT = Path(__file__).resolve().parent.parent

def load(data, name):
    return json.loads((data / name).read_text(encoding="utf-8"))

def save(data, name, obj):
    (data / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def default_from_date(tz_name):
    """The --from default: today's date in the term's timezone."""
    return datetime.now(ZoneInfo(tz_name or "Asia/Shanghai")).date().isoformat()

def speak_pending_mode(data):
    """--speak-pending: print what the main-session agent must say NOW.

    Strictly read-only: loads reminders.json (+ the queued wording from
    session_outbox.jsonl) and prints the state-based speak list from
    src.session_dispatch.list_queued_tasks. It never saves, requeues,
    expires or escalates — those stay with --tick. A missing
    reminders.json simply means nothing was ever queued.
    """
    if not (data / "reminders.json").exists():
        print("[SESSION-SPEAK] （无）")
        return 0
    tasks = load(data, "reminders.json")
    # The exact wording queued for each task lives in the outbox the
    # SessionSender appended to; on requeue cycles a task has several
    # records and the latest one is the wording last queued.
    messages = {}
    outbox = data / "session_outbox.jsonl"
    if outbox.exists():
        for line in outbox.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("taskId") and rec.get("message"):
                messages[rec["taskId"]] = rec["message"]
    entries = list_queued_tasks(tasks, messages)
    # Fallback for a queued task with no outbox record (hand-edited
    # state): compose from the occurrences the last tick persisted.
    missing = [e for e in entries if not e["message"]]
    if missing and (data / "occurrences.json").exists():
        by_id = {o["id"]: o for o in load(data, "occurrences.json")}
        by_task = {t["id"]: t for t in tasks}
        for e in missing:
            occ = by_id.get(by_task[e["taskId"]].get("occurrenceId"))
            if occ is not None:
                e["message"] = compose_message(occ, e["type"],
                                               by_task[e["taskId"]])
    if not entries:
        print("[SESSION-SPEAK] （无）")
        return 0
    for e in entries:
        print(f"[SESSION-SPEAK] {e['taskId']} {e['message'] or ''}".rstrip())
    print("[SESSION-CONFIRM] py scripts/run_scheduler.py --confirm-sent "
          + ",".join(e["taskId"] for e in entries))
    return 0

def confirm_sent_mode(data, ids_arg, now):
    """--confirm-sent: flip queued session tasks to sent (phase 2 of the
    session channel). Only currently-queued ids are confirmed; anything
    else is reported and left untouched. Exit 0 when every id confirmed,
    1 otherwise (including a missing reminders.json or an empty id list).
    """
    if not (data / "reminders.json").exists():
        print("[确认失败] reminders.json 不存在，没有可确认的任务。", file=sys.stderr)
        return 1
    ids = [s.strip() for s in ids_arg.split(",") if s.strip()]
    if not ids:
        print("[确认失败] 没有给出任何任务 id。", file=sys.stderr)
        return 1
    tasks = load(data, "reminders.json")
    confirmed, problems = confirm_session_sent(tasks, ids, now)
    if confirmed:
        save(data, "reminders.json", tasks)
    for task_id in confirmed:
        print(f"[CONFIRMED] {task_id}")
    for problem in problems:
        print(f"[确认失败] {problem}", file=sys.stderr)
    print(f"[确认完成] 已确认 {len(confirmed)} 条，问题 {len(problems)} 条。")
    return 1 if problems else 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tick", action="store_true")
    ap.add_argument("--expand", action="store_true")
    ap.add_argument("--from", dest="from_date", default=None,
                    help="展开窗口起始日 YYYY-MM-DD（缺省：term 时区的今天）")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--now", default=None)
    ap.add_argument("--send", action="store_true",
                    help="really send due reminders (default with --tick: preview only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="preview only: send nothing and write no files (wins over --send)")
    ap.add_argument("--channel", default=None,
                    help="console|outbox|webhook|gateway|session (default: REMINDER_CHANNEL env or console)")
    ap.add_argument("--confirm-sent", default=None, metavar="IDS",
                    help="session 通道第二阶段：把逗号分隔的 queued 任务 id 确认为已发送"
                         "（独立模式，不能与 --tick/--expand 同用）")
    ap.add_argument("--speak-pending", action="store_true",
                    help="只读：打印当前全部 queued 任务的 [SESSION-SPEAK] 待念清单"
                         "与 [SESSION-CONFIRM] 确认命令（独立模式，不写任何文件）")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    args = ap.parse_args()

    data = Path(args.data_dir)
    if args.speak_pending:
        if args.tick or args.expand or args.confirm_sent is not None:
            print("[参数错误] --speak-pending 是独立只读模式，"
                  "不能与 --tick/--expand/--confirm-sent 同时使用。",
                  file=sys.stderr)
            return 2
        return speak_pending_mode(data)
    if args.confirm_sent is not None:
        if args.tick or args.expand:
            print("[参数错误] --confirm-sent 是独立模式，不能与 --tick/--expand 同时使用。",
                  file=sys.stderr)
            return 2
        # A naive --now is read in the data dir's timezone (term/user),
        # exactly like the tick path — never a naive/aware TypeError.
        now = (normalize_now_iso(args.now, data_tz_name(data)) if args.now
               else datetime.now().astimezone().isoformat())
        return confirm_sent_mode(data, args.confirm_sent, now)
    dry_run = args.dry_run or not args.send

    term, courses = load(data, "term.json"), load(data, "courses.json")
    exceptions, user = load(data, "exceptions.json"), load(data, "user.json")
    existing = []
    if (data / "reminders.json").exists():
        existing = load(data, "reminders.json")

    # Timezone first: both the --from default (today) and a naive --now
    # are read in the term's timezone.
    tz_name = term.get("timezone") or user.get("timezone") or "Asia/Shanghai"
    from_date = args.from_date or default_from_date(tz_name)
    occs = expand(term, courses, exceptions, from_date, args.days)
    prefs = user.get("preferences", {})
    # A naive --now (e.g. "2026-10-27T21:05", no offset) is interpreted
    # in the term's timezone instead of crashing against the aware
    # scheduledAt values; an aware --now passes through untouched.
    now = (normalize_now_iso(args.now, tz_name) if args.now
           else datetime.now().astimezone().isoformat())
    now_dt = datetime.fromisoformat(now)
    try:
        stale_min = int(prefs.get("staleExpireMin", DEFAULT_STALE_EXPIRE_MIN))
    except (TypeError, ValueError):
        stale_min = DEFAULT_STALE_EXPIRE_MIN

    # Weather lead time (src/weather.py): coordinates come from the
    # WEATHER_LAT / WEATHER_LON env vars only — unconfigured means the
    # feature is off and departures keep their base times. Weather is an
    # enhancement, never a blocker: any failure degrades to "no extra
    # minutes" inside the provider. A hard --dry-run must not write any
    # file, so fetching (which writes the cache) is disabled there; a
    # pre-existing cache may still be read.
    provider = build_weather_provider(
        cache_path=data / "weather_cache.json",
        timezone=user.get("timezone", "Asia/Shanghai"),
        allow_fetch=not args.dry_run)
    weather_at = (lambda dt: provider.level_at(dt, now_dt)) if provider.enabled else None
    tasks = merge_task_states(
        generate_reminders(occs, courses, prefs, weather_at=weather_at), existing)

    by_id = {o["id"]: o for o in occs}
    # Reply loop (src/ack.py): snoozed tasks whose time is up become due
    # again, and unacknowledged classes past their grace period gain their
    # single follow_up task — both before anything is saved or sent.
    reactivate_snoozed(tasks, now)
    # Stale storm guard: pending tasks whose moment passed more than
    # staleExpireMin minutes ago (first deployment, long shutdown) are
    # expired in place, so this tick neither sends nor queues them and
    # no follow_up is derived from them.
    expired_n = expire_stale_tasks(tasks, now, stale_min)
    tasks += generate_follow_ups(
        tasks, by_id, now,
        grace_min=int(prefs.get("followUpGraceMin", DEFAULT_FOLLOWUP_GRACE_MIN)),
        disabled_types=prefs.get("disabledTypes") or [],
        course_disabled={c["id"]: (c.get("reminderOverride") or {}).get("disabledTypes") or []
                         for c in courses})
    tasks.sort(key=lambda t: t["scheduledAt"])

    if not args.dry_run:
        save(data, "occurrences.json", occs)
        save(data, "reminders.json", tasks)
    print(f"occurrences: {len(occs)}  reminder tasks: {len(tasks)}")
    if expired_n:
        print(f"[过期作废] {expired_n} 条 pending 提醒已超过 {stale_min} 分钟未发送"
              "（staleExpireMin），已标记 expired，不会发送。")
    if provider.enabled:
        print(f"weather: {provider.status}（出门提醒已按天气叠加提前量，见任务 weatherNote）")
    if not args.tick:
        return 0

    if dry_run:
        due = find_due(tasks, now)
        for t in due:
            occ = by_id.get(t["occurrenceId"])
            msg = compose_message(occ, t["type"], t) if occ else "(occurrence 缺失)"
            print("[DUE]", t["type"], msg)
        mode = "--dry-run" if args.dry_run else "未加 --send"
        print(f"[预览模式] 到期 {len(due)} 条，未发送（{mode}）。确认后用 --tick --send 发送。")
        return 0

    try:
        sender = build_sender(args.channel, data_dir=data)
    except SenderConfigError as e:
        print(f"[发送中止] 通道配置错误：{e}", file=sys.stderr)
        return 2

    if sender.name == "session":
        # Fallback escalation config is resolved up front (like the
        # main channel): a provider WITH a key but a broken
        # fallbackAfterMin aborts the whole send with exit 2 before
        # any task state is touched, while "none" (default) or a
        # provider without a key disables escalation silently.
        try:
            fb_sender, fb_after_min = build_fallback_sender(data_dir=data)
        except SenderConfigError as e:
            print(f"[发送中止] 备用通道配置错误：{e}", file=sys.stderr)
            return 2
        # Two-phase session delivery (src/session_dispatch.py): stale
        # queued tasks fall back to pending first, then due tasks are
        # queued into session_outbox.jsonl for the heartbeat agent to
        # speak. Nothing here counts as sent yet.
        requeued = requeue_stale_session_tasks(tasks, now)
        if requeued:
            print(f"[会话重排] {requeued} 条 queued 超过 {SESSION_REQUEUE_MIN} 分钟未确认，"
                  "已回退 pending 并在本 tick 重新排队。")
        # Tasks the requeue just returned to pending may themselves be
        # long past their moment (unconfirmed since a shutdown): expire
        # those instead of re-speaking them in this same tick.
        expired2 = expire_stale_tasks(tasks, now, stale_min)
        if expired2:
            print(f"[过期作废] 其中 {expired2} 条回退任务本身已过期，标记 expired，不再重排。")
        results = queue_due_for_session(tasks, by_id, sender, now, user=user)
        # Escalation (src/fallback.py): tasks first queued at least
        # fallbackAfterMin minutes ago and STILL unconfirmed — the
        # signature of a stale WeChat context token — are re-sent
        # through the token-free fallback provider before the agent is
        # asked to speak anything. Fallback-delivered tasks become sent
        # and drop out of this tick's [SESSION-SPEAK] list; failed
        # attempts stay queued and are still spoken as usual.
        fb_results = escalate_stale_queued_tasks(
            tasks, by_id, fb_sender, now, fb_after_min, user=user)
        save(data, "reminders.json", tasks)  # write back queued/failed outcomes
        queued = [r for r in results if r["status"] == "queued"]
        failed = [r for r in results if r["status"] == "failed"]
        for r in failed:
            print(f"[FAILED] {r['type']} {r.get('message', '')} — {r['error']}")
        if fb_results:
            fb_ok = [r for r in fb_results if r["status"] == "fallback_sent"]
            fb_bad = [r for r in fb_results if r["status"] == "fallback_failed"]
            for r in fb_bad:
                print(f"[备用通道失败] {r['taskId']} — {r['error']}（仍保留 queued，本 tick 继续走主会话朗读）")
            print(f"[备用通道] {len(fb_ok)} 条经 {fb_sender.provider} 补发成功 / "
                  f"{len(fb_bad)} 条失败仍排队"
                  f"（queued 满 {fb_after_min} 分钟未确认后自动补发）")
            fb_ok_ids = {r["taskId"] for r in fb_ok}
            queued = [r for r in queued if r["taskId"] not in fb_ok_ids]
        if queued:
            print(f"[SESSION-OUTBOX] 以下 {len(queued)} 条提醒需要你逐条作为独立消息发送到当前会话，不要改动文字：")
            for r in queued:
                print(f"[SESSION-SPEAK] {r['taskId']} {r['message']}")
            print("全部发送后运行：python3 scripts/run_scheduler.py --confirm-sent "
                  + ",".join(r["taskId"] for r in queued))
        print(f"[发送完成] 通道 session：已排队 {len(queued)} 条，失败 {len(failed)} 条。"
              + ("失败任务不会自动重试，处理后把 reminders.json 里对应 status 改回 pending 再跑。" if failed else ""))
        return 1 if failed else 0

    results = dispatch_due(tasks, by_id, sender, now, user=user)
    save(data, "reminders.json", tasks)  # write back sent/failed outcomes
    sent = [r for r in results if r["status"] == "sent"]
    failed = [r for r in results if r["status"] == "failed"]
    for r in results:
        if r["status"] == "sent":
            print(f"[SENT via {r['channel']}] {r['type']} {r['message']}")
        else:
            print(f"[FAILED] {r['type']} {r.get('message', '')} — {r['error']}")
    print(f"[发送完成] 通道 {sender.name}：成功 {len(sent)} 条，失败 {len(failed)} 条。"
          + ("失败任务不会自动重试，处理后把 reminders.json 里对应 status 改回 pending 再跑。" if failed else ""))
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
