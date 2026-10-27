#!/usr/bin/env python3
"""Change reminder settings conversationally （对话改提醒配置）.

The utterance is parsed deterministically by src/config_edit.py into a
change plan (which file, which field, old → new). The plan is PREVIEWED
by default; only --apply writes user.json / courses.json, each after a
timestamped backup. Ambiguous or invalid requests （待确认） are refused
even with --apply — values are never guessed.

Usage:
  python3 scripts/configure.py "课前提前20分钟提醒"            # preview only
  python3 scripts/configure.py "课前提前20分钟提醒" --apply    # write it
  python3 scripts/configure.py "关掉前晚提醒" --apply
  python3 scripts/configure.py "高数出门提前50分钟" --apply    # per-course override
  python3 scripts/configure.py "静默时间改成23:30到7:30" --apply
  python3 scripts/configure.py "我现在的提醒设置是怎样的"       # view (read-only)

Exit codes: 0 ok / preview / view · 1 refused （待确认）.
"""
import argparse, json, shutil, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config_edit import plan_config_change, apply_plan

ROOT = Path(__file__).resolve().parent.parent
_FILE_OF = {"user": "user.json", "course": "courses.json"}


def load(data, name, default):
    p = data / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save(data, name, obj):
    (data / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", help="config utterance （用户原话）")
    ap.add_argument("--apply", action="store_true",
                    help="actually write the changes (default: preview only)")
    ap.add_argument("--json", action="store_true",
                    help="print the plan as JSON (for the agent to consume)")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    args = ap.parse_args()

    data = Path(args.data_dir)
    user = load(data, "user.json", {})
    courses = load(data, "courses.json", [])
    plan = plan_config_change(args.text, user, courses)

    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 1 if plan["_issues"] else 0

    if plan["kind"] == "view":
        print(plan["answer"])
        return 0

    print(f"改配置解析：{plan['raw']!r}")
    for ch in plan["changes"]:
        print(f"- [{_FILE_OF[ch['target']]}] {ch['summary']}")
    for note in plan["notes"]:
        print(f"    ℹ {note}")
    for issue in plan["_issues"]:
        print(f"    ⚠ 待确认：{issue}")

    if plan["_issues"]:
        print("\n[未写入] 存在待确认项，绝不猜测。请补全信息（如给出具体数值/课程全名）后重试。")
        return 1
    if not plan["changes"]:
        print("\n[无改动] 设置已经是想要的状态，未写入任何文件。")
        return 0
    if not args.apply:
        print("\n[预览模式] 未写入。把计划给用户确认后，加 --apply 才写入（写入前自动备份）。")
        return 0

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    touched = sorted({_FILE_OF[ch["target"]] for ch in plan["changes"]})
    for name in touched:
        src_file = data / name
        if src_file.exists():
            shutil.copyfile(src_file, data / f"{name}.bak-{stamp}")
    changed = apply_plan(user, courses, plan)
    for name in touched:
        save(data, name, user if name == "user.json" else courses)
    backups = "、".join(f"{name}.bak-{stamp}" for name in touched)
    print(f"\n[已写入] {changed} 处变更 → {'、'.join(touched)}（原文件备份：{backups}）。"
          "下一 tick 起新生成/待发的提醒按新设置计算。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
