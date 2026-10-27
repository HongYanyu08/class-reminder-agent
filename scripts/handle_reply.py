#!/usr/bin/env python3
"""Handle a user's reply to a reminder （收到 / 请假了 / snooze …).

This closes the acknowledgement loop from AGENTS.md dataflow 3: the reply
is classified and located deterministically by src/ack.py, the resulting
ReminderTask state changes are shown as a PREVIEW by default, and only
--apply writes them into reminders.json (after a timestamped backup).
Ambiguous replies （待确认） are refused even with --apply — never guessed.

Usage:
  python3 scripts/handle_reply.py "收到"
  python3 scripts/handle_reply.py "高等数学请假了，今天不去上课" --apply
  python3 scripts/handle_reply.py "过10分钟再提醒我" --apply
  python3 scripts/handle_reply.py "晚点提醒" --snooze-min 15   # default snooze when no duration is given

Exit codes: 0 ok / preview · 1 refused （待确认 or nothing to write).
"""
import argparse, json, shutil, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.ack import parse_reply, plan_reply, apply_plan, KIND_LABELS, DEFAULT_SNOOZE_MIN
from src.scheduler import normalize_now_iso, data_tz_name

ROOT = Path(__file__).resolve().parent.parent

def load(data, name, default):
    p = data / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", help="reply text （用户回复原文）")
    ap.add_argument("--apply", action="store_true",
                    help="actually write the state changes (default: preview only)")
    ap.add_argument("--now", default=None, help="override 'now' (ISO, for tests/replay)")
    ap.add_argument("--snooze-min", type=int, default=DEFAULT_SNOOZE_MIN,
                    help=f"default snooze minutes when the reply gives no duration (default {DEFAULT_SNOOZE_MIN})")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    args = ap.parse_args()

    data = Path(args.data_dir)
    # A naive --now is read in the data dir's timezone, never compared
    # raw against the aware timestamps stored in reminders.json.
    now = (normalize_now_iso(args.now, data_tz_name(data)) if args.now
           else datetime.now().astimezone().isoformat())
    tasks = load(data, "reminders.json", [])
    occurrences = load(data, "occurrences.json", [])
    courses = load(data, "courses.json", [])
    occ_by_id = {o["id"]: o for o in occurrences}
    course_by_id = {c["id"]: c for c in courses}

    parsed = parse_reply(args.text, courses)
    plan = plan_reply(parsed, tasks, occ_by_id, course_by_id, now,
                      default_snooze_min=args.snooze_min)

    label = KIND_LABELS.get(plan["kind"], "类型待确认")
    print(f"回执解析：{label}｜原文：{plan['raw']!r}")
    if plan["occurrenceId"]:
        print(f"目标：{plan['courseName']}｜{plan['occurrenceDate']}"
              + (f"｜snooze {plan['snoozeMinutes']} 分钟至 {plan['snoozeUntil']}"
                 if plan["kind"] == "snooze" else ""))
        for ch in plan["changes"]:
            print(f"- [{ch['type']}] {ch['taskId']} → {ch['newStatus']}")
    for c in plan["candidates"]:
        print(f"    候选：{c['courseName']}｜{c['date']}｜类型 {'/'.join(c['types'])}"
              f"｜最近发送 {c['latestSentAt']}")
    for issue in plan["_issues"]:
        print(f"    ⚠ 待确认：{issue}")

    if plan["_issues"]:
        print("\n[未写入] 存在待确认项，绝不猜测回写。请补全信息（如点名课程）后重试。")
        return 1
    if not args.apply:
        print("\n[预览模式] 未写入。确认无误后加 --apply 回写 reminders.json。")
        return 0

    reminders_file = data / "reminders.json"
    backup = data / f"reminders.json.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    if reminders_file.exists():
        shutil.copyfile(reminders_file, backup)
    changed = apply_plan(tasks, plan, now)
    reminders_file.write_text(json.dumps(tasks, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"\n[已写入] 回写 {changed} 条任务，reminders.json 现有 {len(tasks)} 条。"
          + (f"原文件备份在 {backup.name}" if backup.exists() else ""))
    return 0

if __name__ == "__main__":
    sys.exit(main())
