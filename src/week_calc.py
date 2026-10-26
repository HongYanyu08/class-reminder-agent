"""Teaching-week calculation. Pure functions, no LLM, fully testable."""
from datetime import date, timedelta

def _d(s):
    return date.fromisoformat(s) if isinstance(s, str) else s

def teaching_week(on_date, term):
    """Return 1-based teaching week for a date, or None if outside the term."""
    start = _d(term["firstWeekMonday"])
    on_date = _d(on_date)
    delta = (on_date - start).days
    if delta < 0:
        return None
    week = delta // 7 + 1
    return week if week <= int(term.get("totalWeeks", 99)) else None

def effective_weekday(on_date, term):
    """Weekday (Mon=1..Sun=7) whose timetable applies on this date.
    Makeup days (调休) override: that date follows another weekday's timetable."""
    on_date = _d(on_date)
    for m in term.get("makeupDays", []):
        if _d(m["date"]) == on_date:
            return int(m["asWeekday"])
    return on_date.isoweekday()

def is_holiday(on_date, term):
    on_date = _d(on_date)
    if any(_d(m["date"]) == on_date for m in term.get("makeupDays", [])):
        return False  # makeup day: classes run despite being a weekend/holiday-adjacent day
    return on_date.isoformat() in set(term.get("holidays", []))

def weeks_match(week, spec):
    """spec: {mode: all|range|odd|even|list, startWeek, endWeek, list}"""
    if week is None:
        return False
    mode = spec.get("mode", "all")
    if mode == "list":
        return week in spec.get("list", [])
    if week < int(spec.get("startWeek", 1)) or week > int(spec.get("endWeek", 99)):
        return False
    if mode == "odd":
        return week % 2 == 1
    if mode == "even":
        return week % 2 == 0
    return True  # all / range
