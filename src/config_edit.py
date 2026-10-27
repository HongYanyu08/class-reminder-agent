"""Conversational reminder-config editing: turn what the user says
（“课前提前20分钟提醒” / “关掉高数的前晚提醒” …) into a concrete,
previewable change plan for data/user.json and data/courses.json.

Design rules (same spirit as ack.py / notice_parser.py: deterministic,
never guess):
- Only fixed keyword rules are recognised. A request that cannot be
  anchored (no concrete number, an out-of-range value, an invalid time,
  a course alias matching several courses, or a global-only field with a
  course named) becomes 待确认 with the reason spelled out — the user is
  asked, never silently assigned a value.
- Additive phrasing （多提前 / 再提前 / 减少） is read literally as
  old ± X, and the preview always shows old → new, so nothing is applied
  that the user has not seen. Plain phrasing sets the new value.
- Planning NEVER mutates user/courses. apply_plan is the only mutator and
  it refuses plans with _issues; the CLI previews by default and writes
  only with --apply (after a timestamped backup), mirroring
  handle_reply.py / parse_notice.py.

Data model (kept minimal, compatible with src/scheduler.py):
- Global switches live in user.json preferences.disabledTypes: a list of
  reminder types (night_before / departure / pre_class / follow_up) that
  generate_reminders must not generate. Missing/empty means all on.
- Per-course switches live in courses.json reminderOverride.disabledTypes
  (same shape); per-course departure lead is the existing
  reminderOverride.departureLeadMin.
"""
import re
from copy import deepcopy

from .ack import _course_aliases as _ack_aliases

MIN_MINUTES, MAX_MINUTES = 0, 180

TYPE_ORDER = ("night_before", "departure", "pre_class", "follow_up")
TYPE_LABELS = {"night_before": "前晚提醒", "departure": "出门提醒",
               "pre_class": "课前提醒", "follow_up": "追问提醒"}

# Well-known deterministic abbreviations beyond ack's alias口径
# (prefix-stripped core names): 高数 for 高等数学, 大英 for 大学英语.
_EXTRA_ALIASES = {"高等数学": "高数", "大学英语": "大英"}

_EXAMPLES = [
    "前晚提醒改到20:30",
    "课前提前20分钟提醒",
    "路程改成20分钟",
    "缓冲改成5分钟",
    "静默时间改成23:30到7:30",
    "关掉前晚提醒",
    "打开课前提醒",
    "高数出门提前50分钟",
    "关掉高数的前晚提醒",
    "我现在的提醒设置是怎样的",
]

_FIELD_LABELS = {
    "nightBeforeTime": "前晚提醒时间",
    "preClassLeadMin": "课前提前量",
    "defaultTravelMin": "默认路程",
    "defaultBufferMin": "缓冲时间",
    "rainExtraMin": "雨天加量",
    "severeExtraMin": "恶劣天气加量",
    "followUpGraceMin": "追问宽限",
    "quietHours": "静默时段",
    "departureLeadMin": "出门提前量",
    "disabledTypes": "提醒类型开关",
}
_MINUTE_FIELDS = ("preClassLeadMin", "defaultTravelMin", "defaultBufferMin",
                  "rainExtraMin", "severeExtraMin", "followUpGraceMin",
                  "departureLeadMin")

_OFF_RE = re.compile(r"(关掉|关闭|关了|关上|不要|别发|取消|停掉|停止)")
_ON_RE = re.compile(r"(打开|开启|开一下|恢复|启用|重新开)")

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUM = r"(\d+(?:\.\d+)?|[零一二两三四五六七八九十百]+)"
_MIN_RE = re.compile(_NUM + r"\s*分钟")
_HOUR_RE = re.compile(_NUM + r"\s*(?:小时|钟头)")
_TIME_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{1,2})|(\d{1,2})\s*点(?:\s*(\d{1,2})\s*分?|\s*(半))?")


# ------------------------------------------------------------------ helpers ----
def _cn_to_int(token):
    """Arabic or Chinese numerals up to a few hundred (e.g. 二十五)."""
    if token.replace(".", "").isdigit():
        return float(token) if "." in token else int(token)
    if token in _CN_DIGITS:
        return _CN_DIGITS[token]
    total = 0
    rest = token
    if "百" in rest:
        head, _, rest = rest.partition("百")
        total += (_CN_DIGITS.get(head, 1) if head else 1) * 100
    if "十" in rest:
        head, _, tail = rest.partition("十")
        total += (_CN_DIGITS.get(head, 1) if head else 1) * 10
        rest = tail
    if rest:
        if len(rest) == 1 and rest in _CN_DIGITS:
            total += _CN_DIGITS[rest]
        else:
            return None
    return total


def _extract_minutes(text):
    """Total explicit duration in minutes (hours+minutes, 半小时=30), or None."""
    if "半小时" in text or "半个小时" in text:
        base = 30
        rest = text.replace("半个小时", "").replace("半小时", "")
        extra = _extract_minutes(rest) if ("分钟" in rest or "小时" in rest) else None
        return base + (extra or 0)
    total, found = 0, False
    m = _HOUR_RE.search(text)
    if m:
        n = _cn_to_int(m.group(1))
        if n is not None:
            total += int(n * 60)
            found = True
    m = _MIN_RE.search(text)
    if m:
        n = _cn_to_int(m.group(1))
        if n is not None:
            total += int(n)
            found = True
    return total if found else None


def _find_times(text):
    """All clock times mentioned, as (valid ['HH:MM'], invalid [raw])."""
    valid, invalid = [], []
    for m in _TIME_RE.finditer(text):
        if m.group(1) is not None:
            hh, mm, raw = int(m.group(1)), int(m.group(2)), m.group(0)
        else:
            hh = int(m.group(3))
            mm = 30 if m.group(5) else (int(m.group(4)) if m.group(4) else 0)
            raw = m.group(0)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            valid.append(f"{hh:02d}:{mm:02d}")
        else:
            invalid.append(raw)
    return valid, invalid


def _aliases(course):
    out = set(_ack_aliases(course))
    extra = _EXTRA_ALIASES.get(course.get("name", ""))
    if extra:
        out.add(extra)
    return out


def _match_courses(text, courses):
    return [c for c in courses if any(a in text for a in _aliases(c))]


def _type_keyword(text):
    if "前晚" in text or "前夜" in text or "前一晚" in text:
        return "night_before"
    if "课前" in text or "上课前" in text:
        return "pre_class"
    if "出门" in text or "出发" in text:
        return "departure"
    if "追问" in text or "跟进" in text:
        return "follow_up"
    return None


def _fmt_value(field, value):
    if field in _MINUTE_FIELDS:
        return f"{value} 分钟"
    if field == "quietHours" and isinstance(value, dict):
        return f"{value.get('start')}–{value.get('end')}"
    if field == "disabledTypes":
        if not value:
            return "无（全部开启）"
        return "、".join(TYPE_LABELS.get(t, t) for t in value)
    return str(value)


def _sorted_types(types):
    return [t for t in TYPE_ORDER if t in set(types)]


# ------------------------------------------------------------- settings view ----
def describe_settings(user, courses):
    """Read-only rendering of the currently effective reminder settings."""
    prefs = user.get("preferences", {})
    disabled = _sorted_types(prefs.get("disabledTypes") or [])

    def state(typ):
        return "已关闭" if typ in disabled else "开启"

    lines = ["当前提醒设置：", "【全局】"]
    lines.append(f"- 前晚提醒时间：{prefs.get('nightBeforeTime', '21:00')}（{state('night_before')}）")
    lines.append(f"- 课前提前量：{prefs.get('preClassLeadMin', 10)} 分钟（{state('pre_class')}）")
    travel, buf = prefs.get("defaultTravelMin", 15), prefs.get("defaultBufferMin", 10)
    lines.append(f"- 出门提醒：路程 {travel} 分钟 + 缓冲 {buf} 分钟"
                 f"（单课没覆盖时上课前 {int(travel) + int(buf)} 分钟，{state('departure')}）")
    lines.append(f"- 天气加量：雨天 +{prefs.get('rainExtraMin', 10)} 分钟，"
                 f"恶劣天气 +{prefs.get('severeExtraMin', 20)} 分钟（只加在出门提醒上）")
    lines.append(f"- 追问宽限：{prefs.get('followUpGraceMin', 10)} 分钟"
                 f"（未确认追问{state('follow_up')}）")
    quiet = prefs.get("quietHours") or {}
    lines.append(f"- 静默时段：{quiet.get('start', '23:00')}–{quiet.get('end', '07:00')}")
    lines.append(f"- 已关闭的提醒类型：{_fmt_value('disabledTypes', disabled)}")
    overridden = []
    for c in courses:
        ov = c.get("reminderOverride") or {}
        if not ov:
            continue
        bits = []
        if "departureLeadMin" in ov:
            bits.append(f"出门提前 {ov['departureLeadMin']} 分钟")
        if ov.get("disabledTypes"):
            bits.append("已关闭：" + _fmt_value("disabledTypes",
                                                _sorted_types(ov["disabledTypes"])))
        if bits:
            overridden.append(f"- 《{c['name']}》：" + "；".join(bits))
    lines.append("【单课覆盖】")
    lines += overridden or ["- 无"]
    return "\n".join(lines)


# ------------------------------------------------------------------- planning ----
def _change(target, path, field, old, new, course=None, mode="set"):
    label = _FIELD_LABELS.get(field, field)
    scope = f"《{course['name']}》" if course else "全局"
    verb = {"add": "增加", "sub": "减少"}.get(mode)
    return {
        "target": target,
        "courseId": course["id"] if course else None,
        "courseName": course["name"] if course else None,
        "path": list(path), "field": field, "label": label,
        "oldValue": deepcopy(old), "newValue": deepcopy(new),
        "summary": (f"[{scope}] {label} {field}：{_fmt_value(field, old)}"
                    f" → {_fmt_value(field, new)}"
                    + (f"（在现值基础上{verb}）" if verb else "")),
    }


def _minutes_or_issue(text, field_label):
    """(minutes, mode, issue): mode is set/add/sub."""
    minutes = _extract_minutes(text)
    if minutes is None:
        return None, "set", (f"没给出具体数值：改{field_label}需要一个 "
                             f"{MIN_MINUTES}–{MAX_MINUTES} 分钟的数字"
                             f"（比如“{field_label}改成 20 分钟”），“提前一点”这类说法我不能替你定")
    mode = "set"
    if re.search(r"(改成|设为|改为|设成|调到|调成)", text):
        mode = "set"
    elif re.search(r"(多提前|再提前|多加|增加|加上)", text):
        mode = "add"
    elif re.search(r"(少提前|减少|减掉|调少)", text):
        mode = "sub"
    return minutes, mode, None


def _resolve_minutes(old, minutes, mode, field_label):
    """(new_value, issue) after applying set/add/sub and the range check."""
    new = {"set": minutes, "add": int(old) + minutes, "sub": int(old) - minutes}[mode]
    if not (MIN_MINUTES <= new <= MAX_MINUTES):
        return None, (f"{field_label}的新值 {new} 分钟超出允许范围 "
                      f"{MIN_MINUTES}–{MAX_MINUTES} 分钟，不改")
    return new, None


def plan_config_change(text, user, courses):
    """Parse one config utterance into a change plan (pure, no mutation).

    Plan shape: {kind: "change"|"view"|None, raw, changes: [...], notes,
    candidates, answer (view only), _issues}. A plan with _issues must
    never be applied."""
    raw = (text or "").strip()
    plan = {"kind": None, "raw": raw, "changes": [], "notes": [],
            "candidates": [], "_issues": []}
    if not raw:
        plan["_issues"].append("话是空的，没法改设置")
        return plan
    prefs = user.get("preferences", {})
    matched = _match_courses(raw, courses)
    if len(matched) > 1:
        plan["_issues"].append(
            "同时匹配到多门课，无法确定说的是哪一门："
            + "、".join(f"《{c['name']}》" for c in matched)
            + "；请用全名再说一次")
        plan["candidates"] = [c["id"] for c in matched]
        return plan
    course = matched[0] if matched else None

    def global_only_issue(field_label):
        return (f"「{field_label}」只有全局设置、没有单课版本，我不能替你决定改哪边。"
                f"想改全局：去掉课程名说“{field_label}改成 …”；"
                f"只想改《{course['name']}》的出门时间：说“{course['name']}出门提前X分钟”。")

    def finish_change():
        plan["kind"] = "change"
        return plan

    # ---- reminder-type switches （关掉/打开 … 提醒） ----
    typ = _type_keyword(raw)
    off = _OFF_RE.search(raw)
    on = _ON_RE.search(raw)
    if typ and (off or on):
        want_disabled = bool(off)
        if course:
            container = (course.get("reminderOverride") or {})
            current = _sorted_types(container.get("disabledTypes") or [])
            path = ["reminderOverride", "disabledTypes"]
            target = "course"
        else:
            current = _sorted_types(prefs.get("disabledTypes") or [])
            path = ["preferences", "disabledTypes"]
            target = "user"
        new = list(current)
        if want_disabled and typ not in new:
            new.append(typ)
        if not want_disabled and typ in new:
            new.remove(typ)
        new = _sorted_types(new)
        if new == current:
            scope = f"《{course['name']}》的" if course else ""
            plan["notes"].append(
                f"{scope}{TYPE_LABELS[typ]}本来就是"
                f"{'关闭' if want_disabled else '开启'}状态，没有改动")
            plan["kind"] = "change"
            return plan
        plan["changes"].append(_change(
            target, path, "disabledTypes", current, new, course=course))
        return finish_change()

    # ---- quiet hours ----
    if any(w in raw for w in ("静默", "免打扰", "安静时段")):
        if course:
            plan["_issues"].append(global_only_issue("静默时段"))
            return plan
        valid, invalid = _find_times(raw)
        if invalid:
            plan["_issues"].append(
                f"时间 {invalid[0]} 不合法（小时 0–23、分钟 0–59），不改")
        elif len(valid) < 2:
            plan["_issues"].append("静默时段需要起止两个时间（比如“23:30到7:30”），只给了一个或没给")
        else:
            old = dict(prefs.get("quietHours") or {"start": "23:00", "end": "07:00"})
            plan["changes"].append(_change(
                "user", ["preferences", "quietHours"], "quietHours",
                old, {"start": valid[0], "end": valid[1]}))
            return finish_change()
        return plan

    # ---- night-before time ----
    if any(w in raw for w in ("前晚", "前夜", "前一晚")) and (
            _TIME_RE.search(raw) or "改" in raw or "设" in raw):
        if course:
            plan["_issues"].append(global_only_issue("前晚提醒时间"))
            return plan
        valid, invalid = _find_times(raw)
        if invalid:
            plan["_issues"].append(
                f"时间 {invalid[0]} 不合法（小时 0–23、分钟 0–59），不改")
        elif not valid:
            plan["_issues"].append("没给出具体时间：改前晚提醒时间需要一个 HH:MM（如 20:30）")
        else:
            plan["changes"].append(_change(
                "user", ["preferences", "nightBeforeTime"], "nightBeforeTime",
                prefs.get("nightBeforeTime", "21:00"), valid[0]))
            return finish_change()
        return plan

    # ---- per-course departure lead ----
    if course and ("出门" in raw or "出发" in raw):
        minutes, mode, issue = _minutes_or_issue(raw, "出门提前量")
        if issue:
            plan["_issues"].append(issue)
            return plan
        ov = course.get("reminderOverride") or {}
        if "departureLeadMin" in ov:
            old = int(ov["departureLeadMin"])
        else:
            old = int(prefs.get("defaultTravelMin", 15)) + int(prefs.get("defaultBufferMin", 10))
        new, issue = _resolve_minutes(old, minutes, mode, "出门提前量")
        if issue:
            plan["_issues"].append(issue)
            return plan
        plan["changes"].append(_change(
            "course", ["reminderOverride", "departureLeadMin"],
            "departureLeadMin", old, new, course=course, mode=mode))
        return finish_change()

    # ---- global numeric fields ----
    field = None
    if any(w in raw for w in ("恶劣天气", "极端天气", "暴雨")):
        field = "severeExtraMin"
    elif any(w in raw for w in ("雨天", "下雨")):
        field = "rainExtraMin"
    elif "追问" in raw or "宽限" in raw:
        field = "followUpGraceMin"
    elif any(w in raw for w in ("路程", "通勤", "路上")):
        field = "defaultTravelMin"
    elif "缓冲" in raw:
        field = "defaultBufferMin"
    elif "课前" in raw or "上课前" in raw:
        field = "preClassLeadMin"
    if field:
        if course:
            plan["_issues"].append(global_only_issue(_FIELD_LABELS[field]))
            return plan
        minutes, mode, issue = _minutes_or_issue(raw, _FIELD_LABELS[field])
        if issue:
            plan["_issues"].append(issue)
            return plan
        old = int(prefs.get(field, 0))
        new, issue = _resolve_minutes(old, minutes, mode, _FIELD_LABELS[field])
        if issue:
            plan["_issues"].append(issue)
            return plan
        plan["changes"].append(_change(
            "user", ["preferences", field], field, old, new, mode=mode))
        return finish_change()

    # ---- departure without a course: it is travel + buffer, not one field ----
    if "出门" in raw or "出发" in raw:
        plan["_issues"].append(
            "全局的出门时间是“路程 + 缓冲”两项算出来的，没有单一的出门提前量字段。"
            "想改全局：分别说“路程改成X分钟”/“缓冲改成X分钟”；"
            "只改某一门课：点名说“《课程名》出门提前X分钟”。")
        return plan

    # ---- view current settings ----
    if ("设置" in raw or "配置" in raw or "偏好" in raw) and any(
            w in raw for w in ("怎样", "什么", "查看", "看看", "现在", "当前", "列出", "显示", "如何")):
        plan["kind"] = "view"
        plan["answer"] = describe_settings(user, courses)
        return plan

    plan["_issues"].append(
        "这句我没听懂，我只会改提醒设置。你可以这样说："
        + "；".join(f"“{e}”" for e in _EXAMPLES) + "。")
    return plan


# --------------------------------------------------------------------- apply ----
def apply_plan(user, courses, plan):
    """Apply a clean plan, mutating user/courses in place; returns how many
    changes were written. Refuses (ValueError) any plan with _issues."""
    if plan.get("_issues"):
        raise ValueError("计划含待确认项，拒绝写入：" + "；".join(plan["_issues"]))
    course_by_id = {c["id"]: c for c in courses}
    changed = 0
    for ch in plan.get("changes", []):
        if ch["target"] == "user":
            node = user
        else:
            node = course_by_id.get(ch["courseId"])
            if node is None:
                raise ValueError(f"找不到课程 {ch['courseId']}，拒绝写入")
        for key in ch["path"][:-1]:
            node = node.setdefault(key, {})
        node[ch["path"][-1]] = deepcopy(ch["newValue"])
        changed += 1
    return changed
