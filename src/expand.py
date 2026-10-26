"""Expand recurring Course templates into concrete Occurrences.
Applies holidays, makeup days and CourseExceptions. Rolling window (default 14 days)."""
from datetime import date, timedelta
from .week_calc import teaching_week, effective_weekday, is_holiday, weeks_match

def _occ(course, on_date, week, location=None, status="scheduled"):
    return {
        "id": f"{course['id']}@{on_date.isoformat()}",
        "courseId": course["id"], "courseName": course["name"],
        "teacher": course.get("teacher", ""),
        "date": on_date.isoformat(), "week": week,
        "startAt": f"{on_date.isoformat()}T{course['startTime']}:00+08:00",
        "endAt": f"{on_date.isoformat()}T{course['endTime']}:00+08:00",
        "startTime": course["startTime"], "endTime": course["endTime"],
        "location": location or course.get("location", {}),
        "requiredItems": course.get("requiredItems", []),
        "status": status,
    }

def expand(term, courses, exceptions, from_date, days=14):
    from_date = date.fromisoformat(from_date) if isinstance(from_date, str) else from_date
    by_id = {c["id"]: c for c in courses}
    ex_by_key = {(e["courseId"], e["originalDate"]): e for e in exceptions if e.get("confirmed", True)}
    out, seen = [], set()

    for offset in range(days):
        d = from_date + timedelta(days=offset)
        if is_holiday(d, term):
            continue
        week = teaching_week(d, term)
        wd = effective_weekday(d, term)
        for course in courses:
            if course["weekday"] != wd or not weeks_match(week, course.get("weeks", {})):
                continue
            key = (course["id"], d.isoformat())
            ex = ex_by_key.get(key)
            if ex and ex["type"] == "cancel":
                continue
            if ex and ex["type"] == "reschedule":
                continue  # original slot removed; new slot added below
            loc = course.get("location", {})
            status = "scheduled"
            if ex and ex["type"] in ("location_change", "online") and ex.get("newLocation"):
                loc, status = ex["newLocation"], ("online" if ex["type"] == "online" else "scheduled")
            occ = _occ(course, d, week, loc, status)
            out.append(occ); seen.add(occ["id"])

    # rescheduled occurrences land on their new date (even if outside the weekday pattern)
    for e in exceptions:
        if e.get("type") != "reschedule" or not e.get("newDate") or not e.get("confirmed", True):
            continue
        nd = date.fromisoformat(e["newDate"])
        if not (from_date <= nd < from_date + timedelta(days=days)):
            continue
        course = by_id.get(e["courseId"])
        if not course:
            continue
        occ = _occ(course, nd, teaching_week(nd, term), e.get("newLocation") or course.get("location", {}))
        if occ["id"] not in seen:
            out.append(occ); seen.add(occ["id"])

    return sorted(out, key=lambda o: o["startAt"])
