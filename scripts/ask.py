#!/usr/bin/env python3
"""Ask the timetable a question （对话查课表）.

The agent forwards the user's question verbatim; ALL anchoring and answers
are computed by src/query.py from data/*.json via expand(). Read-only:
this script never writes any data file.

Usage:
  python3 scripts/ask.py "明天有什么课"
  python3 scripts/ask.py "高等数学什么时候上" --date 2026-09-14
  python3 scripts/ask.py "下节课是什么" --date 2026-09-07T07:30:00 --json
"""
import argparse, json, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.query import answer_query

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", help="the user's question, verbatim （用户原话）")
    ap.add_argument("--date", help="reference day YYYY-MM-DD (or YYYY-MM-DDTHH:MM:SS); default: today/now")
    ap.add_argument("--json", action="store_true", help="structured JSON output for the agent")
    ap.add_argument("--data-dir", default=str(Path(__file__).resolve().parent.parent / "data"))
    args = ap.parse_args()

    data = Path(args.data_dir)
    term = json.loads((data / "term.json").read_text(encoding="utf-8"))
    courses = json.loads((data / "courses.json").read_text(encoding="utf-8"))
    exceptions = json.loads((data / "exceptions.json").read_text(encoding="utf-8"))

    reference = args.date or datetime.now().astimezone().isoformat(timespec="seconds")
    result = answer_query(args.text, term, courses, exceptions, reference=reference)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["answer"])

if __name__ == "__main__":
    main()
