#!/usr/bin/env python3
"""Scheduler tick (called by HEARTBEAT / cron every 5 minutes).
Usage:
  python3 scripts/run_scheduler.py --expand --from 2026-09-07 --days 14
  python3 scripts/run_scheduler.py --tick --now 2026-09-13T21:00:00+08:00   # dry-run: prints due reminders
Without --now it uses the current time. Sending is stubbed as print in MVP;
wire it to the OpenClaw channel in the next step.
"""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.expand import expand
from src.scheduler import generate_reminders, find_due, compose_message

DATA = Path(__file__).resolve().parent.parent / "data"
def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tick", action="store_true")
    ap.add_argument("--expand", action="store_true")
    ap.add_argument("--from", dest="from_date", default="2026-09-07")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--now", default=None)
    args = ap.parse_args()

    term, courses, exceptions, user = load("term.json"), load("courses.json"), load("exceptions.json"), load("user.json")
    occs = expand(term, courses, exceptions, args.from_date, args.days)
    (DATA / "occurrences.json").write_text(json.dumps(occs, ensure_ascii=False, indent=2), encoding="utf-8")
    tasks = generate_reminders(occs, courses, user.get("preferences", {}))
    (DATA / "reminders.json").write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"occurrences: {len(occs)}  reminder tasks: {len(tasks)}")
    if args.tick:
        from datetime import datetime
        now = args.now or datetime.now().astimezone().isoformat()
        by_id = {o["id"]: o for o in occs}
        for t in find_due(tasks, now):
            print("[DUE]", t["type"], compose_message(by_id[t["occurrenceId"]], t["type"]))

if __name__ == "__main__":
    main()
