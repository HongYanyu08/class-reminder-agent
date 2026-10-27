"""Course query engine: answer natural-language timetable questions with
deterministic code only.

Design rules (same口径 as notice_parser):
- The LLM/agent only forwards the user's question; every answer is computed
  here from term/courses/exceptions via expand(), so it is unit-testable and
  the model can never invent a class.
- Queries are READ-ONLY: nothing in this module writes any file.
- Course matching reuses notice_parser's口径 (full name, or the deterministic
  core name 大学英语→英语 / 高等数学→数学 / 程序设计基础→程序设计).
  Multiple matches -> a "clarify" result listing candidates, never a guess.
- Date anchoring reuses notice_parser's rules: explicit dates, 第N周周X,
  本周/下周+周X and 今天/明天/后天 resolve against the reference date;
  a bare weekday ("周三") is NOT an anchor -> clarify, do not guess.
- Exceptions are reported honestly: a cancelled class is mentioned as 已停课
  (it must not silently vanish), a rescheduled class appears on its new date
  and its original date says it was moved away.
"""
import re
from datetime import date, datetime, time, timedelta

from .expand import expand
from .week_calc import teaching_week, effective_weekday, is_holiday
from .notice_parser import (_explicit_date, _WEEK_WEEKDAY, _REL_WEEKDAY,
                            _BARE_WEEKDAY, _REL_DAY)
from .timetable_import import WEEKDAY_MAP

_WEEKDAY_CN = {v: k for k, v in WEEKDAY_MAP.items()}
_EXAMPLES = [
    "明天有什么课",
    "本周三有什么课",
    "第3周周五有什么课",
    "高等数学什么时候上",
    "英语课在哪上",
    "程序设计要带什么",
    "数学老师是谁",
    "下节课是什么",
    "这周还有什么课",
]

# ------------------------------------------------------------------ helpers ----
def _d(s):
    return date.fromisoformat(s) if isinstance(s, str) else s

def _fmt_date(d):
    return f"{d.month}月{d.day}日（周{_WEEKDAY_CN[d.isoweekday()]}）"

def _fmt_location(occ):
    if occ.get("status") == "online":
        return "线上"
    loc = occ.get("location", {})
    if loc.get("room") == "线上":
        return "线上"
    place = "".join(str(loc.get(k, "")) for k in ("campus", "building", "room"))
    return place or "地点待确认"

def _fmt_items(occ):
    items = occ.get("requiredItems") or []
    return "、".join(items) if items else "无"

def _occ_line(occ):
    return (f"{occ['startTime']}-{occ['endTime']} {occ['courseName']}"
            f"｜{_fmt_location(occ)}｜{occ.get('teacher', '')}｜带：{_fmt_items(occ)}")

def _course_names(course):
    """Full name + deterministic core name, same derivation as notice_parser."""
    names = {course["name"]}
    core = re.sub(r"^(大学|高等)", "", course["name"])
    core = re.sub(r"基础$", "", core)
    if len(core) >= 2:
        names.add(core)
    return names

def _match_courses(text, courses):
    return [c for c in courses if any(n in text for n in _course_names(c))]

def _weeks_desc(spec):
    mode = spec.get("mode", "all")
    if mode == "list":
        return "第" + "、".join(str(w) for w in spec.get("list", [])) + "周"
    base = {"odd": "单周", "even": "双周"}.get(mode, "每周")
    start, end = spec.get("startWeek"), spec.get("endWeek")
    if start and end:
        return f"{base}，第{start}-{end}周"
    return base

def _exception_notes(on_date, courses, exceptions):
    """Human notes for confirmed exceptions anchored on / landing on on_date."""
    by_id = {c["id"]: c for c in courses}
    notes = []
    for e in exceptions:
        if not e.get("confirmed", True):
            continue
        course = by_id.get(e.get("courseId"))
        if not course:
            continue
        name = course["name"]
        if e.get("originalDate") == on_date.isoformat():
            if e["type"] == "cancel":
                notes.append(f"⚠ 《{name}》原定这天的课已停课")
            elif e["type"] == "reschedule" and e.get("newDate"):
                nd = _d(e["newDate"])
                where = "（线上）" if (e.get("newLocation") or {}).get("room") == "线上" else ""
                notes.append(f"⚠ 《{name}》这天的课已调到{_fmt_date(nd)}{where}")
            elif e["type"] == "location_change":
                loc = e.get("newLocation") or {}
                place = "".join(str(loc.get(k, "")) for k in ("building", "room"))
                notes.append(f"⚠ 《{name}》这天已换教室到{place}")
            elif e["type"] == "online":
                notes.append(f"⚠ 《{name}》这天改为线上上课")
        elif e["type"] == "reschedule" and e.get("newDate") == on_date.isoformat():
            od = _d(e["originalDate"])
            notes.append(f"⚠ 《{name}》这天的课是由{_fmt_date(od)}调来的")
    return notes

def _resolve_day(text, term, ref_date):
    """Resolve the day a question asks about.
    Returns (date|None, clarify_issue|None, matched: bool)."""
    d = _explicit_date(text, term)
    if d:
        return d, None, True
    m = _WEEK_WEEKDAY.search(text)
    if m:
        monday = _d(term["firstWeekMonday"]) + timedelta(weeks=int(m.group(1)) - 1)
        return monday + timedelta(days=WEEKDAY_MAP[m.group(2)] - 1), None, True
    m = _REL_WEEKDAY.search(text)
    if m:
        offset = {"本": 0, "这": 0, "下": 1, "上": -1}[m.group(1)]
        monday = ref_date - timedelta(days=ref_date.weekday()) + timedelta(weeks=offset)
        return monday + timedelta(days=WEEKDAY_MAP[m.group(2)] - 1), None, True
    for word, delta in _REL_DAY.items():
        if word in text:
            return ref_date + timedelta(days=delta), None, True
    m = _BARE_WEEKDAY.search(text)
    if m:
        return None, (f"只说了“周{m.group(1)}”没说哪一周，我不能猜——"
                       f"你可以问“本周{_WEEKDAY_CN[WEEKDAY_MAP[m.group(1)]]}有什么课”"
                       f"或“下周{_WEEKDAY_CN[WEEKDAY_MAP[m.group(1)]]}有什么课”"), True
    return None, None, False

def _day_answer(on_date, term, courses, exceptions):
    """(answer_text, occurrences) for one day, exceptions honestly reported."""
    occs = expand(term, courses, exceptions, on_date, days=1)
    notes = _exception_notes(on_date, courses, exceptions)
    week = teaching_week(on_date, term)
    head = _fmt_date(on_date) + (f"（第{week}周）" if week else "（不在本学期周次内）")
    lines = []
    if is_holiday(on_date, term):
        lines.append(f"{head}是假期，没有排课。")
    elif not occs:
        lines.append(f"{head}没有课。")
    else:
        lines.append(f"{head}共 {len(occs)} 节课：")
        lines += [f"- {_occ_line(o)}" for o in occs]
    lines += notes
    return "\n".join(lines), occs

def _term_end(term):
    return _d(term["firstWeekMonday"]) + timedelta(weeks=int(term.get("totalWeeks", 18)))

def _upcoming(term, courses, exceptions, from_date, ref_moment, course_id=None):
    days = max((_term_end(term) - from_date).days + 1, 1)
    out = []
    for occ in expand(term, courses, exceptions, from_date, days=days):
        if course_id and occ["courseId"] != course_id:
            continue
        start = datetime.fromisoformat(occ["startAt"]).replace(tzinfo=None)
        if start > ref_moment:
            out.append(occ)
    return out

# ------------------------------------------------------------- query kinds ----
def _query_day(on_date, term, courses, exceptions, course=None):
    if course is not None:
        occs = [o for o in expand(term, courses, exceptions, on_date, days=1)
                if o["courseId"] == course["id"]]
        notes = [n for n in _exception_notes(on_date, courses, exceptions)
                 if course["name"] in n]
        if occs:
            answer = f"{_fmt_date(on_date)}有《{course['name']}》：{_occ_line(occs[0])}"
        else:
            answer = f"{_fmt_date(on_date)}没有《{course['name']}》这门课。"
        if notes:
            answer += "\n" + "\n".join(notes)
        return {"ok": True, "kind": "day", "date": on_date.isoformat(),
                "courseId": course["id"], "occurrences": occs, "answer": answer}
    answer, occs = _day_answer(on_date, term, courses, exceptions)
    return {"ok": True, "kind": "day", "date": on_date.isoformat(),
            "occurrences": occs, "answer": answer}

def _query_course(text, course, term, courses, exceptions, ref_date, ref_moment):
    upcoming = _upcoming(term, courses, exceptions, ref_date, ref_moment, course["id"])
    nxt = upcoming[0] if upcoming else None
    wd = _WEEKDAY_CN[int(course["weekday"])]
    sched = (f"周{wd} {course['startTime']}-{course['endTime']}"
             f"（{_weeks_desc(course.get('weeks', {}))}）")
    loc = course.get("location", {})
    place = "".join(str(loc.get(k, "")) for k in ("campus", "building", "room")) or "地点待确认"
    nxt_txt = (f"下一次：{_fmt_date(_d(nxt['date']))} {nxt['startTime']}，{_fmt_location(nxt)}"
               if nxt else "本学期后面没有这门课了")
    if any(w in text for w in ("老师", "谁教", "哪位")):
        answer = f"《{course['name']}》的老师是{course.get('teacher', '待确认')}。"
    elif any(w in text for w in ("带什么", "带啥", "要带", "准备", "携带")):
        items = course.get("requiredItems") or []
        answer = (f"上《{course['name']}》要带：{'、'.join(items)}。" if items
                  else f"《{course['name']}》没有登记要带的东西。")
        answer += nxt_txt + "。"
    elif any(w in text for w in ("在哪", "哪里", "教室", "地点")):
        answer = f"《{course['name']}》在{place}上课，{sched}。{nxt_txt}。"
        if nxt and _fmt_location(nxt) != place:
            answer += f"注意：下一次课地点是{_fmt_location(nxt)}。"
    else:  # 什么时候上 / 几点 / 没指明方面 -> 课程概况
        answer = (f"《{course['name']}》每{sched}，地点{place}，{course.get('teacher', '')}，"
                  f"带：{_fmt_items(course)}。{nxt_txt}。")
    return {"ok": True, "kind": "course", "courseId": course["id"],
            "occurrences": [nxt] if nxt else [], "answer": answer}

def _query_next(term, courses, exceptions, ref_date, ref_moment):
    upcoming = _upcoming(term, courses, exceptions, ref_date, ref_moment)
    if not upcoming:
        return {"ok": True, "kind": "next", "occurrences": [],
                "answer": "本学期后面没有课了。"}
    occ = upcoming[0]
    d = _d(occ["date"])
    when = "今天" if d == ref_date else "明天" if d == ref_date + timedelta(days=1) else _fmt_date(d)
    answer = f"下节课是《{occ['courseName']}》：{when} {occ['startTime']}-{occ['endTime']}，{_occ_line(occ).split('｜', 1)[1]}"
    return {"ok": True, "kind": "next", "occurrences": [occ], "answer": answer}

def _query_week(term, courses, exceptions, ref_date):
    days_left = 7 - ref_date.weekday()
    occs = expand(term, courses, exceptions, ref_date, days=days_left)
    by_day = {}
    for occ in occs:
        by_day.setdefault(occ["date"], []).append(occ)
    lines = [f"本周（第{teaching_week(ref_date, term)}周）从今天起还有这些课："
             if teaching_week(ref_date, term) else "本周从今天起还有这些课："]
    if not occs and not any(
            _exception_notes(ref_date + timedelta(days=i), courses, exceptions)
            for i in range(days_left)):
        lines.append("没有课了。")
    for i in range(days_left):
        d = ref_date + timedelta(days=i)
        day_occs = by_day.get(d.isoformat(), [])
        notes = _exception_notes(d, courses, exceptions)
        if not day_occs and not notes:
            continue
        label = "今天" if i == 0 else _fmt_date(d)
        if day_occs or notes:
            lines.append(f"{label}：")
            lines += [f"- {_occ_line(o)}" for o in day_occs]
        lines += notes
    return {"ok": True, "kind": "week", "occurrences": occs,
            "answer": "\n".join(lines)}

# ------------------------------------------------------------------- main ----
def answer_query(text, term, courses, exceptions, reference=None):
    """Answer one natural-language question. Pure/read-only.

    reference: date | datetime | 'YYYY-MM-DD' | 'YYYY-MM-DDTHH:MM:SS' —
    "today" for relative expressions and for 下节课. Defaults to now.
    Returns a dict: {ok, kind, answer, occurrences, ...}; kind is one of
    day / course / next / week / clarify / unknown.
    """
    text = str(text or "").strip()
    if reference is None:
        ref_moment = datetime.now().astimezone().replace(tzinfo=None)
        ref_date = ref_moment.date()
    elif isinstance(reference, datetime):
        ref_moment, ref_date = reference, reference.date()
    elif isinstance(reference, date):
        ref_date, ref_moment = reference, datetime.combine(reference, time.min)
    else:
        s = str(reference)
        if "T" in s:
            ref_moment = datetime.fromisoformat(s).replace(tzinfo=None)
            ref_date = ref_moment.date()
        else:
            ref_date = date.fromisoformat(s)
            ref_moment = datetime.combine(ref_date, time.min)

    # 下节课
    if "下节课" in text or "下一节课" in text or "接下来" in text and "课" in text:
        return _query_next(term, courses, exceptions, ref_date, ref_moment)

    # 本周概览（"本周三/下周一有什么课"带具体星期，是按日查，不走概览）
    if ("这周" in text or "本周" in text) and "课" in text \
            and not _REL_WEEKDAY.search(text) and not _WEEK_WEEKDAY.search(text):
        return _query_week(term, courses, exceptions, ref_date)

    matched = _match_courses(text, courses)
    if len(matched) > 1:
        names = "、".join(f"《{c['name']}》" for c in matched)
        return {"ok": False, "kind": "clarify", "occurrences": [],
                "candidates": [c["id"] for c in matched],
                "answer": f"你问的课同时匹配到 {names}，你指的是哪一门？把全名告诉我就行。"}

    on_date, issue, has_date_expr = _resolve_day(text, term, ref_date)
    if issue:
        return {"ok": False, "kind": "clarify", "occurrences": [],
                "answer": issue}

    course = matched[0] if matched else None
    if on_date is not None and "课" in text:
        return _query_day(on_date, term, courses, exceptions, course)
    if course is not None:
        return _query_course(text, course, term, courses, exceptions, ref_date, ref_moment)

    return {"ok": False, "kind": "unknown", "occurrences": [],
            "answer": "这个问题我没听懂，我只会查课表。你可以这样问："
                      + "；".join(f"“{e}”" for e in _EXAMPLES) + "。"}
