"""Generate ReminderTasks for Occurrences and find due ones.
All times computed deterministically from data; message wording has a template fallback."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .weather import extra_minutes as _weather_extra

TZ = "+08:00"
DEFAULT_TZ_NAME = "Asia/Shanghai"

def _parse(dt_str):
    return datetime.fromisoformat(dt_str)

def as_aware(value, tz_name=DEFAULT_TZ_NAME):
    """Parse an ISO datetime into an aware datetime.

    Internal timestamps (Occurrence startAt, ReminderTask scheduledAt)
    always carry an offset, but a user-supplied moment — e.g.
    `run_scheduler.py --now 2026-10-27T21:05` — may be naive. A naive
    value is interpreted as wall-clock time in tz_name (the term's
    timezone) instead of crashing later with "can't compare offset-naive
    and offset-aware datetimes". Aware values pass through untouched."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name or DEFAULT_TZ_NAME))
    return dt

def normalize_now_iso(value, tz_name=DEFAULT_TZ_NAME):
    """as_aware() for a 'now' argument, returned as an ISO string so the
    normalized value can flow through the existing string-based pipeline."""
    return as_aware(value, tz_name).isoformat()

def data_tz_name(data_dir):
    """Timezone a data dir's naive user input should be read in:
    term.json's timezone, else user.json's, else Asia/Shanghai.
    Unreadable files are skipped — this only picks an interpretation
    for naive input, it never blocks a run."""
    for name in ("term.json", "user.json"):
        try:
            obj = json.loads((Path(data_dir) / name).read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("timezone"):
            return obj["timezone"]
    return DEFAULT_TZ_NAME

def generate_reminders(occurrences, courses, prefs, weather_at=None):
    """Build the three reminder tasks per occurrence.

    weather_at: optional callable(datetime) -> "clear"/"rain"/"severe"/None
    (src/weather.py WeatherProvider.level_at bound by the caller). It is
    consulted once per occurrence, at the base departure moment, and only
    ever moves the departure task earlier: rain adds prefs rainExtraMin,
    severe adds severeExtraMin (see weather.extra_minutes). The computation
    always starts from the class start time, never from a previously
    adjusted departure, so repeated ticks under the same forecast produce
    the identical departure time (idempotent — no stacking). A weathered
    departure task carries weatherExtraMin / weatherNote as an audit trail.
    night_before and pre_class times never depend on weather.

    Reminder-type switches (src/config_edit.py): prefs["disabledTypes"]
    disables types globally, a course's reminderOverride["disabledTypes"]
    disables them for that course only; a disabled type generates no task
    at all. follow_up tasks are gated the same way in generate_follow_ups."""
    by_id = {c["id"]: c for c in courses}
    global_off = set(prefs.get("disabledTypes") or [])
    tasks = []
    for occ in occurrences:
        if occ.get("status") == "cancelled":
            continue
        start = _parse(occ["startAt"])
        course = by_id.get(occ["courseId"], {})
        override = course.get("reminderOverride", {})
        off = global_off | set(override.get("disabledTypes") or [])

        night = start.replace(hour=21, minute=0, second=0) - timedelta(days=1)
        nb_time = prefs.get("nightBeforeTime", "21:00")
        night = night.replace(hour=int(nb_time[:2]), minute=int(nb_time[3:5]))

        lead = override.get("departureLeadMin",
                            int(prefs.get("defaultTravelMin", 15)) + int(prefs.get("defaultBufferMin", 10)))
        departure = start - timedelta(minutes=int(lead))
        pre = start - timedelta(minutes=int(prefs.get("preClassLeadMin", 10)))

        extra, note = (0, None)
        if weather_at is not None:
            extra, note = _weather_extra(weather_at(departure), prefs)
        departure = departure - timedelta(minutes=extra)

        for typ, when in (("night_before", night), ("departure", departure), ("pre_class", pre)):
            if typ in off:
                continue
            task = {
                "id": f"r_{occ['id']}_{typ}", "occurrenceId": occ["id"], "type": typ,
                "scheduledAt": when.isoformat(), "status": "pending", "retryCount": 0,
            }
            if typ == "departure" and extra > 0:
                task["weatherExtraMin"] = extra
                task["weatherNote"] = note
            tasks.append(task)
    return sorted(tasks, key=lambda t: t["scheduledAt"])

def find_due(tasks, now_iso):
    now = _parse(now_iso)
    return [t for t in tasks if t["status"] == "pending" and _parse(t["scheduledAt"]) <= now]

# ------------------------------------------------------------ stale expiry ----
DEFAULT_STALE_EXPIRE_MIN = 120

def expire_stale_tasks(tasks, now_iso, stale_min=DEFAULT_STALE_EXPIRE_MIN):
    """Flip long-overdue pending tasks to the terminal status "expired".

    The stale storm this prevents: on a first deployment, or after the
    laptop was off/asleep for hours, the next tick would otherwise
    dispatch every pending reminder whose moment has long passed — a
    flood of "该出门了" for classes already over. A pending task whose
    scheduledAt is more than stale_min minutes before now (preference
    staleExpireMin, default 120) is marked expired (expiredAt stamped)
    and from then on is inert: find_due only picks up pending tasks,
    merge_task_states preserves the expired status (and expiredAt)
    across regenerations, and generate_follow_ups only looks at *sent*
    tasks — so an expired task never sends and never nags.

    Only pending tasks are touched. In particular queued session tasks
    keep their own 15-minute requeue recovery (src/session_dispatch):
    expire runs before queueing in a tick, and again right after the
    session requeue step, so a task that falls back to pending after a
    long unconfirmed queue is expired in the same tick instead of being
    re-spoken. stale_min <= 0 disables expiry. Returns the count."""
    stale_min = int(stale_min)
    if stale_min <= 0:
        return 0
    now = _parse(now_iso)
    limit = timedelta(minutes=stale_min)
    count = 0
    for task in tasks:
        if task.get("status") != "pending":
            continue
        try:
            age = now - _parse(task["scheduledAt"])
        except (KeyError, TypeError, ValueError):
            continue  # unschedulable task: leave it for dispatch to fail loudly
        if age > limit:
            task["status"] = "expired"
            task["expiredAt"] = now_iso
            count += 1
    return count

# ------------------------------------------------------------ state merge ----
# run_scheduler regenerates tasks from occurrences on every tick. Runtime
# state (sent/failed/...) lives only in reminders.json, so regeneration must
# merge it back in — otherwise a task already sent would flip back to pending
# and be sent again on the next tick.
_RUNTIME_FIELDS = ("status", "sentAt", "queuedAt", "channel", "error", "retryCount", "lastAttemptAt",
                   "acknowledgedAt", "snoozedAt", "snoozeUntil", "resumedAt",
                   "reply", "note", "cancelledAt", "cancelReason", "expiredAt")

def merge_task_states(generated, existing):
    """Overlay stored runtime state onto freshly generated tasks (by id).
    Tasks no longer generated (occurrence gone) are dropped; their history
    stays in the outbox / gateway logs, not in reminders.json. Exception:
    follow_up tasks are created at runtime (src/ack.py loop), not by
    generate_reminders, so a stored follow_up is carried over verbatim as
    long as its occurrence is still in the generated set — otherwise the
    next tick would drop it and a duplicate could be generated/sent."""
    old_by_id = {t["id"]: t for t in existing}
    generated_ids = {t["id"] for t in generated}
    generated_occs = {t["occurrenceId"] for t in generated}
    merged = []
    for task in generated:
        old = old_by_id.get(task["id"])
        if old:
            for field in _RUNTIME_FIELDS:
                if field in old:
                    task[field] = old[field]
        merged.append(task)
    for old in existing:
        if (old.get("type") == "follow_up" and old["id"] not in generated_ids
                and old.get("occurrenceId") in generated_occs):
            merged.append(dict(old))
    return sorted(merged, key=lambda t: t["scheduledAt"])

# ------------------------------------------------- follow-up & snooze ----
DEFAULT_FOLLOWUP_GRACE_MIN = 10
_FOLLOWUP_SOURCES = ("departure", "pre_class")

def generate_follow_ups(tasks, occurrences_by_id, now_iso,
                        grace_min=DEFAULT_FOLLOWUP_GRACE_MIN,
                        disabled_types=(), course_disabled=None):
    """Create at most one follow_up task per occurrence.

    Type switches: follow_up in disabled_types (global preferences) or in
    course_disabled[courseId] (that course's reminderOverride) suppresses
    generation entirely, same口径 as generate_reminders.

    Rule: a departure/pre_class reminder for the occurrence was sent, the
    class start plus the grace period (prefs followUpGraceMin, default
    10 min) has passed, and the user still has not acknowledged or snoozed
    anything for that occurrence — then nag once. The follow_up is
    scheduled exactly at start+grace, so a tick at/after that moment sends
    it in the same pass. Acknowledged / leave / snoozed occurrences never
    get one, and an existing follow_up (any status, kept alive by
    merge_task_states) blocks duplicates.

    Generation is also bounded by the class end (occ endAt): a tick that
    runs long after the class is over (e.g. the first tick after a week of
    downtime) must not retroactively nag "还没确认" for a class days past.
    With the 5-minute HEARTBEAT cadence the [start+grace, end] window is
    always hit in practice."""
    if "follow_up" in set(disabled_types or ()):
        return []
    course_disabled = course_disabled or {}
    now = _parse(now_iso)
    by_occ = {}
    for t in tasks:
        by_occ.setdefault(t["occurrenceId"], []).append(t)
    out = []
    for occ_id, group in by_occ.items():
        if any(t["type"] == "follow_up" for t in group):
            continue
        occ0 = occurrences_by_id.get(occ_id) or {}
        if "follow_up" in set(course_disabled.get(occ0.get("courseId")) or ()):
            continue
        if not any(t["type"] in _FOLLOWUP_SOURCES and t["status"] == "sent"
                   for t in group):
            continue
        if any(t["status"] in ("acknowledged", "snoozed") for t in group):
            continue
        occ = occurrences_by_id.get(occ_id)
        if not occ or occ.get("status") == "cancelled":
            continue
        trigger = _parse(occ["startAt"]) + timedelta(minutes=int(grace_min))
        if now < trigger:
            continue
        if occ.get("endAt") and now > _parse(occ["endAt"]):
            continue  # class long over: no retroactive nagging
        out.append({"id": f"r_{occ_id}_follow_up", "occurrenceId": occ_id,
                    "type": "follow_up", "scheduledAt": trigger.isoformat(),
                    "status": "pending", "retryCount": 0})
    return sorted(out, key=lambda t: t["scheduledAt"])

def reactivate_snoozed(tasks, now_iso):
    """Snoozed tasks whose snoozeUntil has passed go back to pending (and
    are therefore picked up by find_due in the same tick). Returns how many
    were reactivated. snoozedAt/reply stay on the task as history."""
    now = _parse(now_iso)
    count = 0
    for t in tasks:
        if (t.get("status") == "snoozed" and t.get("snoozeUntil")
                and _parse(t["snoozeUntil"]) <= now):
            t["status"] = "pending"
            t["resumedAt"] = now_iso
            del t["snoozeUntil"]
            count += 1
    return count

# --------------------------------------------------------------- dispatch ----
def dispatch_due(tasks, occurrences_by_id, sender, now_iso, dry_run=False,
                 message_fn=None, user=None):
    """Send every due pending task through `sender`, writing the outcome back
    into the task dicts in place. Returns one result dict per due task.

    Status policy:
    - success  -> status=sent, sentAt=now, channel=sender.name, error cleared.
      A sent task is never sent again: find_due only picks up pending tasks
      and merge_task_states preserves the sent state across regenerations.
    - failure  -> status=failed, error=<reason>, retryCount += 1,
      lastAttemptAt=now. Failed tasks are NOT retried automatically by later
      ticks — a human flips status back to pending to retry. This is
      deliberate: a broken webhook must not re-spam the user every 5 minutes.
    - dry_run  -> nothing is sent and no task field is touched.
    - a due task whose occurrence is missing is failed with a clear error
      (data inconsistency must surface, not silently vanish).
    """
    message_fn = message_fn or compose_message
    results = []
    for task in find_due(tasks, now_iso):
        occ = occurrences_by_id.get(task.get("occurrenceId"))
        if occ is None:
            if not dry_run:
                task["status"] = "failed"
                task["error"] = f"occurrence 不存在：{task.get('occurrenceId')}"
                task["retryCount"] = int(task.get("retryCount", 0)) + 1
                task["lastAttemptAt"] = now_iso
            results.append({"taskId": task["id"], "type": task.get("type"),
                            "status": "dry_run" if dry_run else "failed",
                            "error": f"occurrence 不存在：{task.get('occurrenceId')}"})
            continue
        message = message_fn(occ, task["type"], task)
        if dry_run:
            results.append({"taskId": task["id"], "type": task.get("type"),
                            "status": "dry_run", "message": message})
            continue
        try:
            sender.send(task, message, {"occurrence": occ, "user": user})
        except Exception as e:  # SenderError or an unexpected channel bug
            task["status"] = "failed"
            task["error"] = f"{type(e).__name__}: {e}"
            task["retryCount"] = int(task.get("retryCount", 0)) + 1
            task["lastAttemptAt"] = now_iso
            results.append({"taskId": task["id"], "type": task.get("type"),
                            "status": "failed", "error": task["error"],
                            "message": message})
        else:
            task["status"] = "sent"
            task["sentAt"] = now_iso
            task["channel"] = sender.name
            task.pop("error", None)
            results.append({"taskId": task["id"], "type": task.get("type"),
                            "status": "sent", "channel": sender.name,
                            "message": message})
    return results

def compose_message(occ, task_type, task=None):
    loc = occ.get("location", {})
    place = "".join(str(loc.get(k, "")) for k in ("building", "room")) or "地点待确认"
    items = "、".join(occ.get("requiredItems", [])) or "无"
    head = {"night_before": "🌙 明天预告", "departure": "🎒 该出门了",
            "pre_class": "⏰ 快上课了", "follow_up": "❗ 还没确认"}.get(task_type, "🔔 提醒")
    msg = f"{head}｜{occ['courseName']} {occ['startTime']} 在{place}｜带：{items}"
    # Weather lead note: only when generation actually added minutes for
    # this departure (weatherNote is stamped on the task) — no filler text
    # on clear days or for other reminder types.
    note = (task or {}).get("weatherNote")
    if task_type == "departure" and note:
        msg += f"｜天气：{note}"
    return msg
