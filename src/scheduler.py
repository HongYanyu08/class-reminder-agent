"""Generate ReminderTasks for Occurrences and find due ones.
All times computed deterministically from data; message wording has a template fallback."""
from datetime import datetime, timedelta

TZ = "+08:00"

def _parse(dt_str):
    return datetime.fromisoformat(dt_str)

def generate_reminders(occurrences, courses, prefs):
    by_id = {c["id"]: c for c in courses}
    tasks = []
    for occ in occurrences:
        if occ.get("status") == "cancelled":
            continue
        start = _parse(occ["startAt"])
        course = by_id.get(occ["courseId"], {})
        override = course.get("reminderOverride", {})

        night = start.replace(hour=21, minute=0, second=0) - timedelta(days=1)
        nb_time = prefs.get("nightBeforeTime", "21:00")
        night = night.replace(hour=int(nb_time[:2]), minute=int(nb_time[3:5]))

        lead = override.get("departureLeadMin",
                            int(prefs.get("defaultTravelMin", 15)) + int(prefs.get("defaultBufferMin", 10)))
        departure = start - timedelta(minutes=int(lead))
        pre = start - timedelta(minutes=int(prefs.get("preClassLeadMin", 10)))

        for typ, when in (("night_before", night), ("departure", departure), ("pre_class", pre)):
            tasks.append({
                "id": f"r_{occ['id']}_{typ}", "occurrenceId": occ["id"], "type": typ,
                "scheduledAt": when.isoformat(), "status": "pending", "retryCount": 0,
            })
    return sorted(tasks, key=lambda t: t["scheduledAt"])

def find_due(tasks, now_iso):
    now = _parse(now_iso)
    return [t for t in tasks if t["status"] == "pending" and _parse(t["scheduledAt"]) <= now]

def compose_message(occ, task_type):
    loc = occ.get("location", {})
    place = "".join(str(loc.get(k, "")) for k in ("building", "room")) or "地点待确认"
    items = "、".join(occ.get("requiredItems", [])) or "无"
    head = {"night_before": "🌙 明天预告", "departure": "🎒 该出门了",
            "pre_class": "⏰ 快上课了", "follow_up": "❗ 还没确认"}.get(task_type, "🔔 提醒")
    return f"{head}｜{occ['courseName']} {occ['startTime']} 在{place}｜带：{items}"
