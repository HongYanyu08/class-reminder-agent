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

Channels (src/sender.py): console (default) / outbox / webhook / gateway,
chosen by --channel or the REMINDER_CHANNEL env var; webhook and gateway
read their secrets/targets from env vars only (REMINDER_WEBHOOK_URL,
REMINDER_GATEWAY_CMD). Departure reminders gain weather lead time when
WEATHER_LAT / WEATHER_LON are set (src/weather.py, Open-Meteo, cached 3 h
in data/weather_cache.json); unconfigured or failing weather simply means
no extra minutes. Already-sent tasks are never re-sent: regeneration
merges the stored runtime state back in (scheduler.merge_task_states).
Failed tasks are not retried automatically — flip status back to pending
in reminders.json to retry by hand.

Exit codes: 0 ok · 1 at least one send failed · 2 channel misconfigured.
"""
import argparse, json, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.expand import expand
from src.scheduler import generate_reminders, find_due, compose_message, \
    merge_task_states, dispatch_due, generate_follow_ups, reactivate_snoozed, \
    DEFAULT_FOLLOWUP_GRACE_MIN
from src.sender import build_sender, SenderConfigError
from src.weather import build_weather_provider

ROOT = Path(__file__).resolve().parent.parent

def load(data, name):
    return json.loads((data / name).read_text(encoding="utf-8"))

def save(data, name, obj):
    (data / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tick", action="store_true")
    ap.add_argument("--expand", action="store_true")
    ap.add_argument("--from", dest="from_date", default="2026-09-07")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--now", default=None)
    ap.add_argument("--send", action="store_true",
                    help="really send due reminders (default with --tick: preview only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="preview only: send nothing and write no files (wins over --send)")
    ap.add_argument("--channel", default=None,
                    help="console|outbox|webhook|gateway (default: REMINDER_CHANNEL env or console)")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    args = ap.parse_args()

    data = Path(args.data_dir)
    dry_run = args.dry_run or not args.send

    term, courses = load(data, "term.json"), load(data, "courses.json")
    exceptions, user = load(data, "exceptions.json"), load(data, "user.json")
    existing = []
    if (data / "reminders.json").exists():
        existing = load(data, "reminders.json")

    occs = expand(term, courses, exceptions, args.from_date, args.days)
    prefs = user.get("preferences", {})
    now = args.now or datetime.now().astimezone().isoformat()
    now_dt = datetime.fromisoformat(now)

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
