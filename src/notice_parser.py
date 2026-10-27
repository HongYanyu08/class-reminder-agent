"""Notice parser: turn a Chinese class-group notice （调课/停课通知） into a
CourseException preview for data/exceptions.json.

Design rules (from skills/notice-parser/SKILL.md):
- Deterministic parsing only; the LLM may OCR/forward the notice, but the
  anchoring is done here so it can be unit-tested.
- An exception MUST anchor on courseId + originalDate. Anything that cannot
  be anchored exactly (unknown course, unanchorable relative date like
  "下周三" without a reference date, a date that is not that course's class
  day) is reported in the preview's "_issues" list as 待确认 — never guessed.
- Only a single occurrence is ever affected; the weekly template in
  courses.json is never read for writing and never modified.
- Parsing produces a PREVIEW only (confirmed=False). Writing happens through
  merge_exceptions + the CLI's explicit --apply, mirroring timetable_import.

Anchor sources for originalDate, in priority order (all deterministic):
  1. an explicit date in the notice (2026年9月30日 / 2026-09-30 / 9月30日；
     a month/day without a year takes the term's year, rolling to the next
     year for Jan–Jun dates that would fall before the term starts)
  2. "第N周周X" resolved against term["firstWeekMonday"]
  3. "本周/这周/下周/上周 + 周X" or 今天/明天/后天， resolved against the
     caller-supplied reference date (the day the notice was received)
A bare weekday ("周三停课") is NOT an anchor: without knowing which week is
meant it is flagged 待确认.
"""
import hashlib, re
from datetime import date, timedelta

from .week_calc import teaching_week, effective_weekday, is_holiday, weeks_match
from .timetable_import import WEEKDAY_MAP, split_location

ONLINE_LOCATION = {"campus": "", "building": "", "room": "线上"}
TYPE_LABELS = {"cancel": "停课", "reschedule": "调课",
               "location_change": "换教室", "online": "改线上"}

_RESCHED_KW = re.compile(r"(改到|改至|调到|调至|顺延到|延期到|改在)")
# Strict on purpose: a bare "1-2" must never be read as a date (it is far
# more likely to be section numbers 第1-2节）. Only full YMD forms count.
_EXPLICIT_DATE = re.compile(
    r"(?:(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]"
    r"|(\d{4})[-/](\d{1,2})[-/](\d{1,2})"
    r"|(\d{1,2})\s*月\s*(\d{1,2})\s*[日号])")
_WEEK_WEEKDAY = re.compile(r"第\s*(\d+)\s*周\s*(?:周|星期)\s*([一二三四五六日天])")
_REL_WEEKDAY = re.compile(r"(本|这|下|上)\s*周\s*(?:周|星期)?\s*([一二三四五六日天])")
_BARE_WEEKDAY = re.compile(r"(?:周|星期)\s*([一二三四五六日天])")
_REL_DAY = {"今天": 0, "明天": 1, "后天": 2, "昨天": -1}
_LOCATION_KW = re.compile(
    r"(?:换教室|教室(?:改|换|调整)?|上课地点(?:改|换|调整)?|地点(?:改|换|调整)?)"
    r"\s*(?:到|为|至)\s*([^\s，,。；;、]+)")

# ---------------------------------------------------------------- helpers ----
def _d(s):
    return date.fromisoformat(s) if isinstance(s, str) else s

def _infer_year(month, day, term):
    """Year for a bare "M月D日": the term's start year; Jan–Jun dates that
    would land before the term starts belong to the following year."""
    start = _d(term["firstWeekMonday"])
    try:
        cand = date(start.year, month, day)
    except ValueError:
        return None
    if cand < start and month <= 6:
        cand = date(start.year + 1, month, day)
    return cand

def _explicit_date(segment, term):
    m = _EXPLICIT_DATE.search(segment)
    if not m:
        return None
    g = m.groups()
    if g[0] is not None:        # 2026年9月30日
        year, month, day = int(g[0]), int(g[1]), int(g[2])
    elif g[3] is not None:      # 2026-09-30 / 2026/09/30
        year, month, day = int(g[3]), int(g[4]), int(g[5])
    else:                       # 9月30日 (year comes from the term)
        return _infer_year(int(g[6]), int(g[7]), term)
    try:
        return date(year, month, day)
    except ValueError:
        return None

def _week_monday(d):
    return d - timedelta(days=d.weekday())

def _anchor_original(head, term, reference_date, issues):
    """Resolve the original class date from the notice's head segment.
    Returns a date or None (and appends a 待确认 issue when a date-like
    expression was present but could not be anchored)."""
    d = _explicit_date(head, term)
    if d:
        return d
    m = _WEEK_WEEKDAY.search(head)
    if m:  # 第N周周X — anchored on the term itself
        monday = _d(term["firstWeekMonday"]) + timedelta(weeks=int(m.group(1)) - 1)
        return monday + timedelta(days=WEEKDAY_MAP[m.group(2)] - 1)
    m = _REL_WEEKDAY.search(head)
    if m:
        if reference_date is None:
            issues.append(f"原日期待确认：相对日期“{m.group(0)}”无法定位，"
                          "需提供通知日期或写明具体日期")
            return None
        offset = {"本": 0, "这": 0, "下": 1, "上": -1}[m.group(1)]
        monday = _week_monday(reference_date) + timedelta(weeks=offset)
        return monday + timedelta(days=WEEKDAY_MAP[m.group(2)] - 1)
    for word, delta in _REL_DAY.items():
        if word in head:
            if reference_date is None:
                issues.append(f"原日期待确认：相对日期“{word}”无法定位，"
                              "需提供通知日期或写明具体日期")
                return None
            return reference_date + timedelta(days=delta)
    m = _BARE_WEEKDAY.search(head)
    if m:
        issues.append(f"原日期待确认：只写了“周{m.group(1)}”没写哪一周/具体日期")
        return None
    issues.append("原日期待确认：通知里没有写明原上课日期")
    return None

def _anchor_new(tail, original, term, issues):
    """Resolve the rescheduled date from the segment after 改到/调到 etc.
    Explicit date wins; "下周X" is the week after the original class's week;
    a bare "周X" is that weekday within the original class's week."""
    d = _explicit_date(tail, term)
    if d:
        return d
    if original is None:
        issues.append("新日期待确认：原日期未定，无法推算新日期")
        return None
    m = re.search(r"下\s*周\s*(?:周|星期)?\s*([一二三四五六日天])", tail)
    if m:
        return _week_monday(original) + timedelta(weeks=1, days=WEEKDAY_MAP[m.group(1)] - 1)
    m = _BARE_WEEKDAY.search(tail)
    if m:
        return _week_monday(original) + timedelta(days=WEEKDAY_MAP[m.group(1)] - 1)
    issues.append("新日期待确认：通知里没有写明改到哪一天")
    return None

def _find_course(text, courses, issues):
    """Match a course by full name, or by its deterministic core name
    (大学英语→英语, 高等数学→数学, 程序设计基础→程序设计). Ambiguity is flagged."""
    matched = []
    for c in courses:
        names = {c["name"]}
        core = re.sub(r"^(大学|高等)", "", c["name"])
        core = re.sub(r"基础$", "", core)
        if len(core) >= 2:
            names.add(core)
        if any(n in text for n in names):
            matched.append(c)
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        issues.append("课程待确认：通知同时匹配到多门课（"
                      + "、".join(c["name"] for c in matched) + "），需指明是哪一门")
    else:
        issues.append("课程待确认：通知中的课程未在课表中找到")
    return None

def _parse_location_token(raw):
    """Parse a new-location token. Never invents a building:
    'A301' -> room A301 with building left blank; '教三A301' -> 教三 / A301;
    anything else defers to timetable_import.split_location."""
    raw = re.sub(r"上课$", "", raw.strip().strip("()（）"))
    if "线上" in raw or "在线" in raw:
        return dict(ONLINE_LOCATION)
    m = re.match(r"^(.+?)([A-Za-z]\d{3,4})$", raw)
    if m and len(m.group(1)) > 1:
        return {"campus": "", "building": m.group(1), "room": m.group(2)}
    if re.match(r"^[A-Za-z]?\d{3,4}[A-Za-z]?$", raw):
        return {"campus": "", "building": "", "room": raw}
    loc, _ = split_location(raw)
    return loc

def _validate_anchor(preview, course, term):
    """The anchored date must really be one of this course's occurrences."""
    if not course or not preview.get("originalDate"):
        return
    d = _d(preview["originalDate"])
    issues = preview["_issues"]
    week = teaching_week(d, term)
    if week is None:
        issues.append(f"原日期待确认：{d.isoformat()} 不在本学期周次范围内")
    elif is_holiday(d, term):
        issues.append(f"原日期待确认：{d.isoformat()} 是假期，按课表当天无课")
    elif effective_weekday(d, term) != int(course["weekday"]) or \
            not weeks_match(week, course.get("weeks", {})):
        issues.append(f"原日期待确认：{d.isoformat()} 不是《{course['name']}》的上课日"
                      f"（该课在周{course['weekday']}，请核对日期）")

# ------------------------------------------------------------------ parse ----
def parse_notice(text, courses, term, reference_date=None):
    """Parse one notice into a CourseException PREVIEW dict.

    The preview carries the stored schema fields plus "_issues" (same
    convention as timetable_import). "_issues" non-empty means the preview
    must NOT be written; missing anchors stay None instead of being guessed.
    reference_date (date or 'YYYY-MM-DD') is the day the notice arrived —
    only used to anchor 本周/下周/明天 style expressions.
    """
    text = str(text or "").strip()
    reference_date = _d(reference_date) if reference_date else None
    issues = []

    course = _find_course(text, courses, issues)

    kw = _RESCHED_KW.search(text)
    head = text[:kw.start()] if kw else text
    tail = text[kw.end():] if kw else ""

    original = _anchor_original(head, term, reference_date, issues)

    new_date, new_location, typ = None, None, None
    loc_match = _LOCATION_KW.search(text)
    if kw:
        typ = "reschedule"
        new_date = _anchor_new(tail, original, term, issues)
        if new_date is not None and original is not None and new_date == original:
            issues.append("新日期待确认：改后的日期与原日期相同")
        if "线上" in tail or "在线" in tail:
            new_location = dict(ONLINE_LOCATION)
        else:
            m = re.search(r"在\s*([^\s，,。；;、]+?)\s*(?:上课|上课的)", tail)
            if m:
                new_location = _parse_location_token(m.group(1))
    elif any(w in text for w in ("停课", "取消", "不上课")):
        typ = "cancel"
    elif loc_match:
        token = loc_match.group(1)
        if "线上" in token or "在线" in token:
            typ, new_location = "online", dict(ONLINE_LOCATION)
        else:
            typ = "location_change"
            new_location = _parse_location_token(token)
            if not new_location.get("room"):
                issues.append(f"新教室待确认：地点“{token}”里没有教室号")
    elif "线上" in text or "在线" in text:
        typ, new_location = "online", dict(ONLINE_LOCATION)
    else:
        issues.append("变更类型待确认：没认出是停课 / 调课 / 换教室 / 改线上中的哪一种")

    preview = {
        "id": None,
        "courseId": course["id"] if course else None,
        "originalDate": original.isoformat() if original else None,
        "type": typ,
        "newDate": new_date.isoformat() if new_date else None,
        "newLocation": new_location,
        "sourceText": text,
        "confirmed": False,
        "_issues": issues,
    }
    _validate_anchor(preview, course, term)
    if preview["courseId"] and preview["originalDate"] and typ:
        raw = f"{preview['courseId']}|{preview['originalDate']}|{typ}"
        preview["id"] = "ex_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return preview

# ------------------------------------------------------------------ merge ----
def dedupe_key(e):
    return (e.get("courseId"), e.get("originalDate"), e.get("type"))

def merge_exceptions(existing, new_exceptions):
    """Returns (merged, added, duplicates, conflicts). Strips _issues and
    marks stored entries confirmed=True (the --apply step IS the confirmation).
    Same course+date with a different type is a conflict: never auto-applied,
    a human must resolve which change wins."""
    merged = list(existing)
    seen = {dedupe_key(e) for e in existing}
    anchors = {(e.get("courseId"), e.get("originalDate")) for e in existing}
    added, duplicates, conflicts = [], [], []
    for e in new_exceptions:
        if e.get("_issues"):
            continue  # unconfirmed previews never land; the CLI refuses earlier
        if dedupe_key(e) in seen:
            duplicates.append(e)
            continue
        if (e.get("courseId"), e.get("originalDate")) in anchors:
            conflicts.append(e)
            continue
        clean = {k: v for k, v in e.items() if not k.startswith("_")}
        clean["confirmed"] = True
        merged.append(clean); added.append(clean)
        seen.add(dedupe_key(e)); anchors.add((e.get("courseId"), e.get("originalDate")))
    return merged, added, duplicates, conflicts
