#!/usr/bin/env python3
"""Speak trigger: after an OS-scheduler tick, wake the main session
only if reminders are queued for it to say (src/speak_trigger.py).

Usage:
  py scripts/speak_trigger.py             # fire the one-shot wake if needed
  py scripts/speak_trigger.py --dry-run   # print the wake command, run nothing

Exit codes: 0 ok / nothing queued / throttled · 1 wake command failed
· 2 reminders.json unreadable. Run by scripts/tick_task.cmd right
after `run_scheduler.py --tick --send`; safe to run by hand any time.
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.speak_trigger import run_speak_trigger

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the wake command without executing it "
                         "and without writing the throttle state")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    args = ap.parse_args()
    return run_speak_trigger(args.data_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
