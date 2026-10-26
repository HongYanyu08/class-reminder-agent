#!/usr/bin/env python3
"""Import a timetable into data/courses.json.

Default is PREVIEW ONLY (nothing is written) — this mirrors the agent rule
"show the parsed result, write only after the user confirms".

Usage:
  python3 scripts/import_timetable.py examples/sample_timetable.txt
  python3 scripts/import_timetable.py my.csv --apply
  python3 scripts/import_timetable.py export.xlsx --apply --data-dir /path/to/data

Supported inputs: .txt (free-form lines / OCR text), .csv, .json, .xlsx
Images: turn them into text first (agent vision / OCR), then import the text.
"""
import argparse, json, shutil, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.timetable_import import load_input, merge_courses

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--apply", action="store_true", help="actually write into courses.json (default: preview only)")
    ap.add_argument("--data-dir", default=str(Path(__file__).resolve().parent.parent / "data"))
    args = ap.parse_args()

    data = Path(args.data_dir)
    term = json.loads((data / "term.json").read_text(encoding="utf-8"))
    existing = json.loads((data / "courses.json").read_text(encoding="utf-8"))

    parsed = load_input(args.input, term)
    print(f"解析出 {len(parsed)} 门课：")
    for c in parsed:
        sec = c.get("sections") or {}
        loc = c.get("location", {})
        line = (f"- {c['name']}｜周{c['weekday']} 第{sec.get('start')}-{sec.get('end')}节 "
                f"{c['startTime']}-{c['endTime']}｜{json.dumps(c['weeks'], ensure_ascii=False)}"
                f"｜{loc.get('building','')}{loc.get('room','')}｜{c['teacher'] or '教师缺失'}")
        print(line)
        for issue in c.get("_issues", []):
            print(f"    ⚠ 待确认：{issue}")

    merged, added, duplicates = merge_courses(existing, parsed)
    if duplicates:
        print(f"跳过 {len(duplicates)} 门重复课程：" + "、".join(d["name"] for d in duplicates))

    if not args.apply:
        print(f"\n[预览模式] 将新增 {len(added)} 门课，未写入。确认无误后加 --apply 正式导入。")
        return
    if added:
        backup = data / f"courses.json.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copyfile(data / "courses.json", backup)
        (data / "courses.json").write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[已导入] 新增 {len(added)} 门课，courses.json 现有 {len(merged)} 门。原文件备份在 {backup.name}")
    else:
        print("\n[已导入] 没有新增课程，未修改文件。")

if __name__ == "__main__":
    main()
