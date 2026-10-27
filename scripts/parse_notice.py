#!/usr/bin/env python3
"""Parse a class-group notice （调课/停课） into a CourseException.

Default is PREVIEW ONLY (nothing is written) — this mirrors the agent rule
"show the parsed result, write only after the user confirms". A preview with
待确认 (_issues) items can never be applied: anchors are never guessed.

Usage:
  python3 scripts/parse_notice.py "大学英语 2026年9月30日停课一次"
  python3 scripts/parse_notice.py --file notice.txt --date 2026-09-14   # --date anchors 本周/下周
  python3 scripts/parse_notice.py "高等数学 2026年9月21日停课一次" --apply

Screenshot notices: OCR them to text first (agent vision), then feed the text.
"""
import argparse, json, shutil, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.notice_parser import parse_notice, merge_exceptions, TYPE_LABELS

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", help="notice text （班群通知原文）")
    ap.add_argument("--file", help="read the notice text from a file instead")
    ap.add_argument("--date", help="reference date YYYY-MM-DD (notice arrival day; anchors 本周/下周/明天）")
    ap.add_argument("--apply", action="store_true", help="actually write into exceptions.json (default: preview only)")
    ap.add_argument("--data-dir", default=str(Path(__file__).resolve().parent.parent / "data"))
    args = ap.parse_args()

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.text:
        text = args.text
    else:
        ap.error("需要给出通知原文（位置参数或 --file）")

    data = Path(args.data_dir)
    term = json.loads((data / "term.json").read_text(encoding="utf-8"))
    courses = json.loads((data / "courses.json").read_text(encoding="utf-8"))
    existing = json.loads((data / "exceptions.json").read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in courses}

    p = parse_notice(text, courses, term, reference_date=args.date)
    course = by_id.get(p["courseId"] or "", {})
    print("解析出 1 条课程变更预览：")
    print(f"- {course.get('name', '课程待确认')}｜原日期 {p['originalDate'] or '待确认'}"
          f"｜{TYPE_LABELS.get(p['type'], '类型待确认')}"
          + (f"｜新日期 {p['newDate']}" if p["newDate"] else "")
          + (f"｜新地点 {json.dumps(p['newLocation'], ensure_ascii=False)}" if p["newLocation"] else ""))
    for issue in p["_issues"]:
        print(f"    ⚠ 待确认：{issue}")

    if not args.apply:
        print("\n[预览模式] 未写入。确认无误后加 --apply 写入 exceptions.json（只影响这一次课）。")
        return
    if p["_issues"]:
        print("\n[未写入] 存在待确认项，锚点不准绝不入库。请补全信息后重试。")
        sys.exit(1)

    merged, added, duplicates, conflicts = merge_exceptions(existing, [p])
    if duplicates:
        print("\n[未写入] exceptions.json 里已有完全相同的变更，跳过重复。")
        return
    if conflicts:
        print("\n[未写入] 同一门课同一天已有另一种变更，需先人工确认保留哪条。")
        sys.exit(1)
    backup = data / f"exceptions.json.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    shutil.copyfile(data / "exceptions.json", backup)
    (data / "exceptions.json").write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[已写入] 新增 1 条变更，exceptions.json 现有 {len(merged)} 条。原文件备份在 {backup.name}")

if __name__ == "__main__":
    main()
