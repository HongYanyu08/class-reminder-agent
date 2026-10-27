"""Reply acknowledgement: turn a user's reply （收到 / 请假了 / snooze …)
into ReminderTask state changes, closing the loop from AGENTS.md dataflow 3.

Design rules (same spirit as notice_parser: deterministic, never guess):
- The reply TYPE is classified by fixed keyword rules only — ack / leave /
  snooze. Anything else is reported as 待确认 (unrecognised), never forced.
- The reply TARGET is a single Occurrence, resolved deterministically:
  1. a course named in the reply wins;
  2. otherwise the occurrence whose outstanding (sent / snoozed) reminder
     was sent most recently;
  3. a tie, or several occurrences for the named course, is 待确认 —
     candidates are listed and NOTHING is written.
- Effects per type (see plan_reply / apply_plan):
  ack     -> every outstanding task of the occurrence: status=acknowledged
  leave   -> outstanding tasks: acknowledged + note="leave"; the occurrence's
             remaining pending tasks: status=cancelled (most conservative
             reading of AGENTS.md: the class will not be attended, so its
             later reminders must not fire — the raw reply is kept on every
             touched task so the decision stays auditable)
  snooze  -> outstanding tasks: status=snoozed + snoozeUntil; the scheduler
             reactivates them to pending when snoozeUntil passes.
- Parsing/planning NEVER mutates tasks. apply_plan is the only mutator and
  it refuses plans with _issues; the CLI previews by default and writes only
  with --apply (after a timestamped backup), mirroring parse_notice.py.
"""
import re
from datetime import datetime, timedelta

DEFAULT_SNOOZE_MIN = 10

KIND_LABELS = {"ack": "确认收到", "leave": "请假", "snooze": "稍后提醒"}

# leave is checked first: "请假了，不用再提醒了" must not be read as a snooze
# just because it contains 再提醒， and "今天不去上课了" is decisive.
_LEAVE_RE = re.compile(
    r"(请假|不去上课|不去了|不上课了|不上这节|翘课|不用提醒|别提醒了|不要提醒)")
_SNOOZE_WORD_RE = re.compile(r"(snooze|稍后|晚点|晚一会儿|过一会儿|等一会儿|再提醒)", re.I)
_ACK_RE = re.compile(r"(收到|确认|知道了|明白了|了解了|好的|好嘞|嗯嗯|ok|👍)", re.I)

_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_DUR_MIN_RE = re.compile(r"(\d+|[一二两三四五六七八九十]+)\s*分钟")
_DUR_HOUR_RE = re.compile(r"(\d+(?:\.\d+)?|[一二两三四五六七八九十]+)\s*(?:小时|钟头)")

OUTSTANDING_STATUSES = ("sent", "snoozed")


def _num(token):
    if token.isdigit():
        return int(token)
    if token in _CN_NUM:
        return _CN_NUM[token]
    if token == "半":
        return 0.5
    return None


def _duration_minutes(text):
    """Explicit snooze duration in minutes, or None. 半小时 counts as 30."""
    if "半小时" in text or "半个小时" in text:
        return 30
    m = _DUR_MIN_RE.search(text)
    if m:
        n = _num(m.group(1))
        if n is not None:
            return int(n)
    m = _DUR_HOUR_RE.search(text)
    if m:
        n = _num(m.group(1))
        if n is not None:
            return int(n * 60)
    return None


def _course_aliases(course):
    """Deterministic name variants a reply might use for a course:
    the full name, the name without a 大学/高等 prefix, without a 基础 suffix."""
    name = course.get("name", "")
    out = {name} if name else set()
    short = name
    for prefix in ("大学", "高等"):
        if short.startswith(prefix):
            short = short[len(prefix):]
            break
    if short:
        out.add(short)
    if short.endswith("基础"):
        out.add(short[:-2])
    return {a for a in out if len(a) >= 2}


def parse_reply(text, courses=None):
    """Classify a reply. Returns a preview dict; never mutates anything.
    snoozeMinutes is None when the reply asks to be reminded later without
    saying when (the caller substitutes its configured default)."""
    raw = (text or "").strip()
    parsed = {"kind": None, "snoozeMinutes": None, "courseHint": None,
              "courseIdHint": None, "raw": raw, "_issues": []}
    if not raw:
        parsed["_issues"].append("回复为空，无法处理")
        return parsed

    if courses:
        matched = [c for c in courses
                   if any(alias in raw for alias in _course_aliases(c))]
        if len(matched) == 1:
            parsed["courseHint"] = matched[0]["name"]
            parsed["courseIdHint"] = matched[0]["id"]
        # >1 named courses: no hint — location falls back to recency and
        # plan_reply reports the ambiguity instead of guessing here.

    if _LEAVE_RE.search(raw):
        parsed["kind"] = "leave"
    elif _SNOOZE_WORD_RE.search(raw) or (
            _duration_minutes(raw) is not None and "提醒" in raw):
        parsed["kind"] = "snooze"
        parsed["snoozeMinutes"] = _duration_minutes(raw)
    elif _ACK_RE.search(raw):
        parsed["kind"] = "ack"
    else:
        parsed["_issues"].append(
            "无法识别回复类型：只支持确认类（收到/确认/知道了）、"
            "请假类（请假了/今天不去上课）、稍后类（晚点提醒/snooze/过X分钟再提醒）")
    return parsed


def _course_of(task, occurrences_by_id, courses_by_id):
    occ = occurrences_by_id.get(task.get("occurrenceId")) or {}
    course_id = occ.get("courseId") or str(task.get("occurrenceId", "")).split("@")[0]
    course = courses_by_id.get(course_id) or {}
    return course_id, occ.get("courseName") or course.get("name", ""), occ


def plan_reply(parsed, tasks, occurrences_by_id, courses_by_id, now_iso,
               default_snooze_min=DEFAULT_SNOOZE_MIN):
    """Resolve a parsed reply to a concrete write plan (pure, no mutation).

    Plan shape: {kind, raw, occurrenceId, courseName, occurrenceDate,
    snoozeMinutes, snoozeUntil, changes: [{taskId, type, action, newStatus}],
    candidates: [...], _issues: [...]}. A plan with _issues must never be
    applied."""
    plan = {"kind": parsed.get("kind"), "raw": parsed.get("raw", ""),
            "occurrenceId": None, "courseName": None, "occurrenceDate": None,
            "snoozeMinutes": None, "snoozeUntil": None,
            "changes": [], "candidates": [], "_issues": list(parsed.get("_issues", []))}
    if plan["kind"] is None:
        return plan

    by_occ = {}
    for t in tasks:
        if t.get("status") in OUTSTANDING_STATUSES:
            by_occ.setdefault(t["occurrenceId"], []).append(t)
    if not by_occ:
        plan["_issues"].append("当前没有待确认的已发送提醒（可能尚未发送，或都已确认）")
        return plan

    def describe(occ_id):
        _, name, occ = _course_of(by_occ[occ_id][0], occurrences_by_id, courses_by_id)
        latest = max(t.get("sentAt") or t["scheduledAt"] for t in by_occ[occ_id])
        return {"occurrenceId": occ_id, "courseName": name,
                "date": occ.get("date") or str(occ_id).split("@")[-1],
                "types": sorted(t["type"] for t in by_occ[occ_id]),
                "latestSentAt": latest}

    hint_id = parsed.get("courseIdHint")
    target = None
    if hint_id:
        matched = [oid for oid in by_occ
                   if _course_of(by_occ[oid][0], occurrences_by_id,
                                 courses_by_id)[0] == hint_id]
        if not matched:
            plan["_issues"].append(
                f"没有找到「{parsed.get('courseHint')}」待确认的已发送提醒")
            plan["candidates"] = [describe(oid) for oid in sorted(by_occ)]
            return plan
        if len(matched) > 1:
            plan["_issues"].append(
                f"「{parsed.get('courseHint')}」有多条待确认提醒，无法唯一确定是哪一次课")
            plan["candidates"] = [describe(oid) for oid in sorted(matched)]
            return plan
        target = matched[0]
    elif len(by_occ) == 1:
        target = next(iter(by_occ))
    else:
        markers = {oid: max(t.get("sentAt") or t["scheduledAt"] for t in ts)
                   for oid, ts in by_occ.items()}
        latest = max(markers.values())
        winners = [oid for oid, m in markers.items() if m == latest]
        if len(winners) > 1:
            plan["_issues"].append(
                "有多条待确认提醒且发送时间相同，无法确定回复针对哪条；请在回复里点名课程")
            plan["candidates"] = [describe(oid) for oid in sorted(winners)]
            return plan
        target = winners[0]

    info = describe(target)
    plan["occurrenceId"] = target
    plan["courseName"] = info["courseName"]
    plan["occurrenceDate"] = info["date"]
    outstanding = by_occ[target]
    kind = plan["kind"]
    if kind == "ack":
        for t in outstanding:
            plan["changes"].append({"taskId": t["id"], "type": t["type"],
                                    "action": "acknowledge", "newStatus": "acknowledged"})
    elif kind == "leave":
        for t in outstanding:
            plan["changes"].append({"taskId": t["id"], "type": t["type"],
                                    "action": "acknowledge", "newStatus": "acknowledged"})
        for t in tasks:
            if t["occurrenceId"] == target and t.get("status") == "pending":
                plan["changes"].append({"taskId": t["id"], "type": t["type"],
                                        "action": "cancel_pending",
                                        "newStatus": "cancelled"})
    elif kind == "snooze":
        minutes = parsed.get("snoozeMinutes")
        if minutes is None:
            minutes = int(default_snooze_min)
        plan["snoozeMinutes"] = minutes
        plan["snoozeUntil"] = (
            datetime.fromisoformat(now_iso) + timedelta(minutes=minutes)).isoformat()
        for t in outstanding:
            plan["changes"].append({"taskId": t["id"], "type": t["type"],
                                    "action": "snooze", "newStatus": "snoozed"})
    return plan


def apply_plan(tasks, plan, now_iso):
    """Apply a clean plan to the task dicts in place; returns how many tasks
    changed. Refuses (ValueError) any plan with _issues — anchors are never
    guessed, not even by callers in a hurry."""
    if plan.get("_issues"):
        raise ValueError("计划含待确认项，拒绝回写：" + "；".join(plan["_issues"]))
    by_id = {t["id"]: t for t in tasks}
    changed = 0
    for change in plan["changes"]:
        task = by_id.get(change["taskId"])
        if task is None:
            continue
        task["reply"] = plan["raw"]
        if change["action"] == "acknowledge":
            task["status"] = "acknowledged"
            task["acknowledgedAt"] = now_iso
            if plan["kind"] == "leave":
                task["note"] = "leave"
        elif change["action"] == "cancel_pending":
            task["status"] = "cancelled"
            task["cancelledAt"] = now_iso
            task["note"] = "leave"
            task["cancelReason"] = "用户回复请假，该次课后续提醒取消"
        elif change["action"] == "snooze":
            task["status"] = "snoozed"
            task["snoozedAt"] = now_iso
            task["snoozeUntil"] = plan["snoozeUntil"]
        changed += 1
    return changed
