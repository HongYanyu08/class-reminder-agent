"""Local web configuration UI backend.

The HTTP layer is deliberately thin: all routing and validation live in
plain functions here so tests can exercise the API without opening a port.

Safety model:
- The server is for the machine that owns the data files. serve_ui binds
  127.0.0.1 by default and there is no authentication, so it must never be
  exposed to a LAN or the public internet.
- Preference validation uses the same field semantics and limits as
  src/config_edit.py: minute fields are integers in [0, 180], clock fields
  are strict HH:MM, and reminder types must be one of the four known types.
- A POST is validated as a whole before anything is written. If one field
  is invalid, the whole request is rejected and neither data file changes.
- A successful write first creates a timestamped backup, mirroring the CLI
  tools (configure.py / handle_reply.py / parse_notice.py).
- The 发送渠道 endpoints never return a full webhook URL or gateway
  command: only masked renderings (src/sender_settings.py). A submitted
  masked/empty secret keeps the stored value, so the masked display can
  round-trip through the form without destroying the real secret. The
  same treatment covers the session channel's fallback provider
  (fallbackChannel / fallbackKey / fallbackAfterMin, src/fallback.py);
  the test endpoint takes target="main" (default) or "fallback".
- The 导入课表 endpoints reuse the deterministic parser in
  src/timetable_import.py and mirror scripts/import_timetable.py:
  preview parses only and never writes; apply re-parses the same raw
  input (deterministic, so identical to the preview — no server-side
  session state), then writes with a backup and the CLI's dedupe key.
  Courses flagged "_issues" are imported exactly like the CLI does:
  flagged fields stay blank (never guessed) and "_issues" itself is
  stripped from the stored entries.
"""
import base64
import binascii
import json
import os
import re
import shutil
import tempfile
from copy import deepcopy
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config_edit import TYPE_LABELS, TYPE_ORDER
from .expand import expand
from .query import _exception_notes, _fmt_location
from .sender import CHANNELS, SenderConfigError, SenderError
from .sender_settings import (
    SETTING_FIELDS,
    SenderSettingsError,
    build_sender_from_config,
    load_settings_file,
    looks_masked,
    mask_secret,
    mask_webhook_url,
    resolve_sender_config,
    sanitize_text,
    settings_path,
    validate_settings,
)
from .timetable_import import (
    merge_courses,
    parse_csv_text,
    parse_json_text,
    parse_text,
    parse_xlsx,
)
from .week_calc import teaching_week

ROOT = Path(__file__).resolve().parent.parent
UI_FILE = ROOT / "ui" / "index.html"

MIN_MINUTES, MAX_MINUTES = 0, 180
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_MINUTE_FIELDS = (
    "preClassLeadMin",
    "defaultTravelMin",
    "defaultBufferMin",
    "rainExtraMin",
    "severeExtraMin",
    "followUpGraceMin",
)
_CONFIG_FIELDS = {
    "nightBeforeTime",
    *_MINUTE_FIELDS,
    "quietHours",
    "disabledTypes",
}
_OVERRIDE_FIELDS = {"courseId", "departureLeadMin", "disabledTypes"}
_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class WebUIError(Exception):
    """An API error with an HTTP status and user-readable field errors."""

    def __init__(self, message, status=400, errors=None):
        super().__init__(message)
        self.status = int(status)
        self.errors = list(errors or [message])


def _as_date(value=None):
    if value is None:
        return datetime.now().astimezone().date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _load_json(path, default):
    if not Path(path).exists():
        return deepcopy(default)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_all(data_dir):
    data = Path(data_dir)
    return {
        "term": _load_json(data / "term.json", {}),
        "courses": _load_json(data / "courses.json", []),
        "exceptions": _load_json(data / "exceptions.json", []),
        "user": _load_json(data / "user.json", {}),
        "reminders": _load_json(data / "reminders.json", []),
    }


def _type_states(disabled_types):
    disabled = set(disabled_types or [])
    return [
        {
            "type": reminder_type,
            "label": TYPE_LABELS[reminder_type],
            "enabled": reminder_type not in disabled,
        }
        for reminder_type in TYPE_ORDER
    ]


def _sorted_types(types):
    return [item for item in TYPE_ORDER if item in set(types or [])]


def _course_view(course):
    view = deepcopy(course)
    override = deepcopy(view.get("reminderOverride") or {})
    disabled = _sorted_types(override.get("disabledTypes") or [])
    override["departureLeadMin"] = override.get("departureLeadMin")
    override["disabledTypes"] = disabled
    view["reminderOverride"] = override
    view["typeStates"] = _type_states(disabled)
    view["weekdayLabel"] = (
        _WEEKDAY_LABELS[int(view["weekday"]) - 1]
        if str(view.get("weekday", "")).isdigit() and 1 <= int(view["weekday"]) <= 7
        else ""
    )
    return view


def _occurrence_view(occurrence):
    view = deepcopy(occurrence)
    view["locationText"] = _fmt_location(view)
    view["statusLabel"] = "线上" if view.get("status") == "online" else "正常"
    return view


def get_state(data_dir, today=None):
    """Return every value the single-page UI needs in one response.

    `today` is injectable for deterministic tests. Occurrences are expanded
    for the 14 days starting today. Cancelled/rescheduled exceptions do not
    silently disappear: their notes are attached to the relevant day, using
    the same presentation helper as src/query.py.
    """
    loaded = _load_all(data_dir)
    term = loaded["term"]
    courses = loaded["courses"]
    exceptions = loaded["exceptions"]
    user = loaded["user"]
    reminders = loaded["reminders"]

    today = _as_date(today)
    occurrences = [
        _occurrence_view(item)
        for item in expand(term, courses, exceptions, today, days=14)
    ]
    by_date = {}
    for occurrence in occurrences:
        by_date.setdefault(occurrence["date"], []).append(occurrence)

    upcoming_days = []
    for offset in range(14):
        day = today + timedelta(days=offset)
        day_key = day.isoformat()
        upcoming_days.append(
            {
                "date": day_key,
                "weekdayLabel": _WEEKDAY_LABELS[day.weekday()],
                "week": teaching_week(day, term),
                "isToday": offset == 0,
                "occurrences": by_date.get(day_key, []),
                "notes": _exception_notes(day, courses, exceptions),
            }
        )

    prefs = deepcopy(user.get("preferences") or {})
    disabled = _sorted_types(prefs.get("disabledTypes") or [])
    prefs["disabledTypes"] = disabled
    prefs["typeStates"] = _type_states(disabled)
    user_view = deepcopy(user)
    user_view["preferences"] = prefs

    reminder_counts = {}
    for task in reminders if isinstance(reminders, list) else []:
        status = str(task.get("status") or "unknown")
        reminder_counts[status] = reminder_counts.get(status, 0) + 1

    next_7_count = sum(
        len(day["occurrences"]) for day in upcoming_days[:7]
    )
    current_week = teaching_week(today, term)
    term_view = {
        "id": term.get("id"),
        "name": term.get("name"),
        "timezone": term.get("timezone"),
        "firstWeekMonday": term.get("firstWeekMonday"),
        "totalWeeks": term.get("totalWeeks"),
        "currentWeek": current_week,
        "today": today.isoformat(),
        "holidays": deepcopy(term.get("holidays") or []),
        "makeupDays": deepcopy(term.get("makeupDays") or []),
    }
    summary = {
        "today": today.isoformat(),
        "currentWeek": current_week,
        "courseCount": len(courses),
        "upcoming7DaysCount": next_7_count,
        "upcoming14DaysCount": len(occurrences),
        "reminderTotal": len(reminders) if isinstance(reminders, list) else 0,
        "reminderStatusCounts": reminder_counts,
    }
    return {
        "ok": True,
        "term": term_view,
        "user": user_view,
        "preferences": prefs,
        "courses": [_course_view(course) for course in courses],
        "occurrences": occurrences,
        "upcomingDays": upcoming_days,
        "reminders": {
            "total": summary["reminderTotal"],
            "statusCounts": reminder_counts,
        },
        "summary": summary,
    }


# --------------------------------------------------------------- validation ----
def _validate_time(field, value, errors):
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
        errors.append(f"{field} 必须是合法的 HH:MM 时间（00:00–23:59）")
        return None
    return value


def _validate_minutes(field, value, errors):
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{field} 必须是整数分钟")
        return None
    if not MIN_MINUTES <= value <= MAX_MINUTES:
        errors.append(
            f"{field} 必须在 {MIN_MINUTES}–{MAX_MINUTES} 分钟之间"
        )
        return None
    return value


def _validate_disabled_types(field, value, errors):
    if not isinstance(value, list):
        errors.append(f"{field} 必须是数组")
        return None
    invalid = [item for item in value if item not in TYPE_ORDER]
    if invalid:
        errors.append(
            f"{field} 含未知提醒类型：{', '.join(str(item) for item in invalid)}"
        )
        return None
    if len(set(value)) != len(value):
        errors.append(f"{field} 不能包含重复的提醒类型")
        return None
    return _sorted_types(value)


def _backup(path):
    path = Path(path)
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    counter = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{stamp}-{counter}")
        counter += 1
    shutil.copyfile(path, backup)
    return backup.name


def _write_json(path, value):
    path = Path(path)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temp.replace(path)


def save_config(data_dir, payload):
    """Validate and save a global-preferences payload as one transaction."""
    if not isinstance(payload, dict):
        raise WebUIError("请求体必须是 JSON 对象")
    unknown = sorted(set(payload) - _CONFIG_FIELDS)
    if unknown:
        raise WebUIError(
            "发现未知字段，整单拒绝",
            errors=[f"未知字段：{', '.join(unknown)}"],
        )
    if not payload:
        raise WebUIError("没有提交任何要保存的字段")

    errors = []
    cleaned = {}
    if "nightBeforeTime" in payload:
        value = _validate_time(
            "nightBeforeTime", payload["nightBeforeTime"], errors
        )
        if value is not None:
            cleaned["nightBeforeTime"] = value
    for field in _MINUTE_FIELDS:
        if field in payload:
            value = _validate_minutes(field, payload[field], errors)
            if value is not None:
                cleaned[field] = value
    if "quietHours" in payload:
        quiet = payload["quietHours"]
        if not isinstance(quiet, dict):
            errors.append("quietHours 必须是包含 start 和 end 的对象")
        else:
            extra = sorted(set(quiet) - {"start", "end"})
            missing = sorted({"start", "end"} - set(quiet))
            if extra:
                errors.append(f"quietHours 含未知字段：{', '.join(extra)}")
            if missing:
                errors.append(f"quietHours 缺少字段：{', '.join(missing)}")
            if not extra and not missing:
                start = _validate_time(
                    "quietHours.start", quiet["start"], errors
                )
                end = _validate_time("quietHours.end", quiet["end"], errors)
                if start is not None and end is not None:
                    cleaned["quietHours"] = {"start": start, "end": end}
    if "disabledTypes" in payload:
        value = _validate_disabled_types(
            "disabledTypes", payload["disabledTypes"], errors
        )
        if value is not None:
            cleaned["disabledTypes"] = value
    if errors:
        raise WebUIError("配置校验失败，整单拒绝、未写入任何文件", errors=errors)

    data = Path(data_dir)
    user_path = data / "user.json"
    user = _load_json(user_path, {})
    updated = deepcopy(user)
    updated.setdefault("preferences", {}).update(cleaned)
    backup = _backup(user_path)
    _write_json(user_path, updated)
    return {
        "ok": True,
        "message": "提醒设置已保存",
        "backup": backup,
        "user": updated,
        "preferences": updated.get("preferences") or {},
    }


def save_course_override(data_dir, payload):
    """Validate and save one course's reminderOverride."""
    if not isinstance(payload, dict):
        raise WebUIError("请求体必须是 JSON 对象")
    unknown = sorted(set(payload) - _OVERRIDE_FIELDS)
    if unknown:
        raise WebUIError(
            "发现未知字段，整单拒绝",
            errors=[f"未知字段：{', '.join(unknown)}"],
        )
    course_id = payload.get("courseId")
    if not isinstance(course_id, str) or not course_id:
        raise WebUIError("courseId 必须是非空字符串")
    if "departureLeadMin" not in payload and "disabledTypes" not in payload:
        raise WebUIError("没有提交单课覆盖字段")

    errors = []
    has_lead = "departureLeadMin" in payload
    lead = payload.get("departureLeadMin")
    if has_lead and lead is not None:
        lead = _validate_minutes("departureLeadMin", lead, errors)
    disabled = None
    if "disabledTypes" in payload:
        disabled = _validate_disabled_types(
            "disabledTypes", payload["disabledTypes"], errors
        )
    if errors:
        raise WebUIError("单课覆盖校验失败，整单拒绝、未写入任何文件", errors=errors)

    data = Path(data_dir)
    courses_path = data / "courses.json"
    courses = _load_json(courses_path, [])
    course = next((item for item in courses if item.get("id") == course_id), None)
    if course is None:
        raise WebUIError(f"课程不存在：{course_id}", status=404)

    updated = deepcopy(courses)
    target = next(item for item in updated if item.get("id") == course_id)
    override = target.setdefault("reminderOverride", {})
    if has_lead:
        if lead is None:
            override.pop("departureLeadMin", None)
        else:
            override["departureLeadMin"] = lead
    if disabled is not None:
        if disabled:
            override["disabledTypes"] = disabled
        else:
            override.pop("disabledTypes", None)

    backup = _backup(courses_path)
    _write_json(courses_path, updated)
    return {
        "ok": True,
        "message": "单课覆盖已保存",
        "backup": backup,
        "course": _course_view(target),
    }


# ------------------------------------------------------------ sender config ----
_SENDER_FIELDS = set(SETTING_FIELDS)
TEST_MESSAGE = "【测试】上课提醒渠道连通性测试"


def _secret_values(data_dir, env, extra=()):
    """Every full secret value that must never appear in a response:
    stored ones, env ones, and any draft passed in `extra`."""
    values = set(extra or ())
    try:
        stored = load_settings_file(data_dir)
    except SenderSettingsError:
        stored = {}
    for field in ("webhookUrl", "gatewayCmd", "fallbackKey"):
        if stored.get(field):
            values.add(stored[field])
    env = os.environ if env is None else env
    for name in ("REMINDER_WEBHOOK_URL", "REMINDER_GATEWAY_CMD",
                 "REMINDER_FALLBACK_KEY"):
        if env.get(name):
            values.add(env[name])
    return [value for value in values if value]


def _load_stored_or_error(data_dir):
    try:
        return load_settings_file(data_dir)
    except SenderSettingsError as exc:
        raise WebUIError(
            f"现有发送渠道设置文件无法读取：{exc}",
            status=500,
        ) from exc


def _fallback_view_fields(source):
    """The fallback (session escalation) slice of a sender view, from
    either a resolved config or the raw stored settings. The key only
    ever leaves as a masked rendering plus a has-key flag."""
    return {
        "fallbackChannel": (source or {}).get("fallbackChannel") or "none",
        "fallbackKeyMasked": mask_secret((source or {}).get("fallbackKey")),
        "hasFallbackKey": bool((source or {}).get("fallbackKey")),
        "fallbackAfterMin": (source or {}).get("fallbackAfterMin"),
    }


def _sender_view_from(config, sources, stored):
    effective = {
        "channel": config.get("channel"),
        "webhookUrlMasked": mask_webhook_url(config.get("webhookUrl")),
        "hasWebhookUrl": bool(config.get("webhookUrl")),
        "gatewayCmdMasked": mask_secret(config.get("gatewayCmd")),
        "hasGatewayCmd": bool(config.get("gatewayCmd")),
        "outboxDir": config.get("outboxDir") or "",
        "webhookTimeoutSec": config.get("webhookTimeoutSec"),
        **_fallback_view_fields(config),
    }
    stored_view = None
    if stored is not None:
        stored_view = {
            "channel": stored.get("channel"),
            "webhookUrlMasked": mask_webhook_url(stored.get("webhookUrl")),
            "hasWebhookUrl": bool(stored.get("webhookUrl")),
            "gatewayCmdMasked": mask_secret(stored.get("gatewayCmd")),
            "hasGatewayCmd": bool(stored.get("gatewayCmd")),
            "outboxDir": stored.get("outboxDir") or "",
            "webhookTimeoutSec": stored.get("webhookTimeoutSec"),
            **_fallback_view_fields(stored),
        }
    return {
        "ok": True,
        "channels": list(CHANNELS),
        "effective": effective,
        "sources": sources,
        "settingsFile": stored_view,
        "testMessage": TEST_MESSAGE,
    }


def get_sender_view(data_dir, env=None):
    """Masked view of the effective sender configuration.

    Full webhook URLs / gateway commands NEVER leave this function — only
    masked renderings (scheme://host/…last4, first-12…last-4)."""
    stored = _load_stored_or_error(data_dir)
    try:
        config, sources = resolve_sender_config(env=env, data_dir=data_dir)
    except SenderConfigError as exc:
        raise WebUIError(f"发送渠道配置无效：{exc}", status=500) from exc
    view = _sender_view_from(config, sources, stored)
    view["webhookUrlMasked"] = view["effective"]["webhookUrlMasked"]
    view["gatewayCmdMasked"] = view["effective"]["gatewayCmdMasked"]
    view["fallbackKeyMasked"] = view["effective"]["fallbackKeyMasked"]
    view["settings"] = view["settingsFile"]
    return view


def _clean_sender_draft(payload, stored):
    """Merge a submitted sender payload over the stored settings.

    Masked-placeholder or empty secret fields keep the stored value
    (that is how the masked display round-trips without destroying the
    real secret); an explicit JSON null clears a field. Returns the
    merged, validated settings dict."""
    if not isinstance(payload, dict):
        raise WebUIError("请求体必须是 JSON 对象")
    unknown = sorted(set(payload) - _SENDER_FIELDS)
    if unknown:
        raise WebUIError(
            "发现未知字段，整单拒绝",
            errors=[f"未知字段：{', '.join(unknown)}"],
        )
    if not payload:
        raise WebUIError("没有提交任何要保存的字段")
    merged = dict(stored or {})
    for field in ("webhookUrl", "gatewayCmd", "fallbackKey"):
        if field not in payload:
            continue
        value = payload[field]
        if value is None:
            merged.pop(field, None)
        elif isinstance(value, str) and (not value.strip() or looks_masked(value)):
            pass  # keep the stored secret untouched
        else:
            merged[field] = value
    for field in ("channel", "webhookTimeoutSec", "outboxDir",
                  "fallbackChannel", "fallbackAfterMin"):
        if field not in payload:
            continue
        value = payload[field]
        if value is None or (isinstance(value, str) and not value.strip()):
            merged.pop(field, None)
        else:
            merged[field] = value
    try:
        return validate_settings(merged, source="发送渠道设置")
    except SenderSettingsError as exc:
        raise WebUIError("发送渠道设置校验失败，未写入任何文件",
                         errors=[str(exc)]) from exc


def save_sender_settings(data_dir, payload, env=None):
    """Validate a sender payload and persist it to
    data/sender_settings.json (gitignored), backing the old file up
    first. Environment variables still override the file at resolve
    time; the returned view says so per field via `sources`."""
    stored = _load_stored_or_error(data_dir)
    cleaned = _clean_sender_draft(payload, stored)
    path = settings_path(data_dir)
    backup = _backup(path)
    _write_json(path, cleaned)
    view = get_sender_view(data_dir, env=env)
    return {
        "ok": True,
        "message": "发送渠道设置已保存（环境变量如已设置仍会优先生效）",
        "backup": backup,
        "sender": view,
        "effective": view["effective"],
        "sources": view["sources"],
    }


def send_sender_test(data_dir, payload, env=None, transport=None,
                     sender_factory=None):
    """Send one fixed test message through a DRAFT sender configuration
    (the submitted payload merged over the stored settings; nothing is
    written to disk). Send failures are reported, never raised and never
    hidden; every string in the result is scrubbed of full secrets.

    The payload may carry "target": "main" (default — the primary
    channel, the historical behaviour) or "target": "fallback" (the
    Server酱/PushPlus escalation provider from the draft's
    fallbackChannel/fallbackKey). "target" is a test selector, not a
    setting; it is stripped before the draft merge so it never reaches
    validate_settings."""
    if not isinstance(payload, dict):
        raise WebUIError("请求体必须是 JSON 对象")
    target = payload.get("target") or "main"
    if target not in ("main", "fallback"):
        raise WebUIError(f"未知测试目标 target：{target!r}（可用：main / fallback）")
    payload = {k: v for k, v in payload.items() if k != "target"}
    stored = _load_stored_or_error(data_dir)
    if payload:
        draft = _clean_sender_draft(payload, stored)
    else:
        # No draft fields at all: the test targets the stored settings
        # exactly as saved (the save path's "empty submission" guard
        # does not apply — nothing is being written here).
        draft = validate_settings(dict(stored), source="发送渠道设置")
    config = {
        "channel": draft.get("channel") or "console",
        "webhookUrl": draft.get("webhookUrl"),
        "webhookTimeoutSec": draft.get("webhookTimeoutSec", 10),
        "gatewayCmd": draft.get("gatewayCmd"),
        "outboxDir": draft.get("outboxDir"),
        "fallbackChannel": draft.get("fallbackChannel"),
        "fallbackKey": draft.get("fallbackKey"),
        "fallbackAfterMin": draft.get("fallbackAfterMin"),
    }
    secrets = _secret_values(data_dir, env, extra=(
        draft.get("webhookUrl"), draft.get("gatewayCmd"),
        draft.get("fallbackKey")))

    def scrub(text):
        return sanitize_text(text, secrets)

    if target == "fallback":
        return _send_fallback_test(draft, scrub, transport)

    try:
        if sender_factory is not None:
            sender = sender_factory(dict(config))
        else:
            sender = build_sender_from_config(
                config, {"channel": "draft"}, data_dir=data_dir,
                transport=transport)
    except SenderConfigError as exc:
        return {"ok": False, "channel": config["channel"],
                "error": scrub(str(exc))}
    user = _load_json(Path(data_dir) / "user.json", {})
    task = {"id": "channel-test", "occurrenceId": None, "type": "test",
            "scheduledAt": None}
    try:
        result = sender.send(task, TEST_MESSAGE, {"user": user}) or {}
    except SenderError as exc:
        return {"ok": False, "channel": sender.name,
                "error": scrub(str(exc))}
    except Exception as exc:  # a channel bug must surface, not crash the API
        return {"ok": False, "channel": getattr(sender, "name", config["channel"]),
                "error": scrub(f"发送失败：{type(exc).__name__}: {exc}")}
    return {
        "ok": True,
        "channel": sender.name,
        "message": f"测试消息已通过「{sender.name}」通道发送",
        "detail": scrub(result.get("detail", "")),
        "testMessage": TEST_MESSAGE,
    }


def _send_fallback_test(draft, scrub, transport):
    """The target="fallback" half of send_sender_test: push the fixed
    test message through the draft's fallback provider. An unconfigured
    provider/key is a clean ok=False result (the user is mid-setup),
    never an exception; the real send uses src/fallback.py with the
    injected transport in tests."""
    from .fallback import FallbackSender

    provider = draft.get("fallbackChannel") or "none"
    key = draft.get("fallbackKey")
    name = f"fallback:{provider}" if provider != "none" else "fallback"
    if provider == "none":
        return {"ok": False, "channel": name,
                "error": "备用通道未启用：先在上面选 Server酱 或 PushPlus 再测试"}
    if not key:
        return {"ok": False, "channel": name,
                "error": f"备用通道 {provider} 还没有填写 key，无法发送测试"}
    try:
        sender = FallbackSender(provider, key, transport=transport)
    except SenderConfigError as exc:
        return {"ok": False, "channel": name, "error": scrub(str(exc))}
    task = {"id": "channel-test", "occurrenceId": None, "type": "test",
            "scheduledAt": None}
    try:
        result = sender.send(task, TEST_MESSAGE, {}) or {}
    except SenderError as exc:
        return {"ok": False, "channel": sender.name,
                "error": scrub(str(exc))}
    except Exception as exc:  # a channel bug must surface, not crash the API
        return {"ok": False, "channel": sender.name,
                "error": scrub(f"发送失败：{type(exc).__name__}: {exc}")}
    return {
        "ok": True,
        "channel": sender.name,
        "message": f"测试消息已通过备用通道「{sender.name}」发送",
        "detail": scrub(result.get("detail", "")),
        "testMessage": TEST_MESSAGE,
    }


# -------------------------------------------------------- timetable import ----
IMPORT_EXTENSIONS = {".txt", ".csv", ".xlsx", ".json"}
IMPORT_MAX_BYTES = 2 * 1024 * 1024
_IMPORT_FIELDS = {"filename", "contentBase64", "text"}
IMPORT_PATHS = {"/api/import/preview", "/api/import/apply"}


def _import_term(data_dir):
    term_path = Path(data_dir) / "term.json"
    if not term_path.exists():
        raise WebUIError(
            "缺少 data/term.json，无法解析课表（节次换算需要学期数据）",
            status=500,
        )
    return _load_json(term_path, {})


def _parse_import_bytes(raw, ext, term):
    """Dispatch decoded file bytes to the same parsers load_input uses."""
    if ext == ".xlsx":
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".xlsx", delete=False
            ) as handle:
                handle.write(raw)
                tmp = handle.name
            return parse_xlsx(tmp, term)
        except Exception as exc:
            raise WebUIError(f"XLSX 解析失败：{exc}") from exc
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    try:
        if ext == ".csv":
            return parse_csv_text(raw.decode("utf-8-sig"), term)
        if ext == ".json":
            return parse_json_text(raw.decode("utf-8"), term)
        return parse_text(raw.decode("utf-8"), term)
    except Exception as exc:
        raise WebUIError(f"解析失败：{exc}") from exc


def _parse_import_payload(data_dir, payload):
    """Validate an import request body and parse it deterministically.

    Exactly one input form: {"text": ...} (pasted free-form lines, e.g.
    text converted from a timetable screenshot) or
    {"filename": ..., "contentBase64": ...} (an uploaded
    .txt/.csv/.xlsx/.json file; decoded size capped at 2 MB).
    Returns (courses, source) where courses may carry "_issues".
    This function never writes anything.
    """
    if not isinstance(payload, dict):
        raise WebUIError("请求体必须是 JSON 对象")
    unknown = sorted(set(payload) - _IMPORT_FIELDS)
    if unknown:
        raise WebUIError(
            "发现未知字段，拒绝解析",
            errors=[f"未知字段：{', '.join(unknown)}"],
        )
    has_text = "text" in payload
    has_file = "filename" in payload or "contentBase64" in payload
    if has_text and has_file:
        raise WebUIError("文件上传和粘贴文字只能二选一，请只保留一种输入")
    if not has_text and not has_file:
        raise WebUIError("没有提供课表内容：请选择课表文件，或粘贴课表文字")

    term = _import_term(data_dir)
    if has_text:
        text = payload["text"]
        if not isinstance(text, str) or not text.strip():
            raise WebUIError("粘贴的课表文字为空")
        if len(text.encode("utf-8")) > IMPORT_MAX_BYTES:
            raise WebUIError("课表文字超过 2 MB 上限")
        try:
            courses = parse_text(text, term)
        except Exception as exc:
            raise WebUIError(f"解析失败：{exc}") from exc
        source = {"kind": "text"}
    else:
        filename = payload.get("filename")
        content_b64 = payload.get("contentBase64")
        if not isinstance(filename, str) or not filename.strip():
            raise WebUIError(
                "filename 必须是非空字符串（上传文件的原始文件名）"
            )
        if not isinstance(content_b64, str) or not content_b64.strip():
            raise WebUIError(
                "contentBase64 必须是非空字符串（文件内容的 base64 编码）"
            )
        ext = Path(filename.strip()).suffix.lower()
        if ext not in IMPORT_EXTENSIONS:
            raise WebUIError(
                f"不支持的文件类型：{ext or '（无扩展名）'}；"
                "只接受 .txt / .csv / .xlsx / .json"
                "（.xls 老格式请先在 Excel/WPS 里另存为 .xlsx 或 .csv）"
            )
        try:
            raw = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WebUIError(
                f"contentBase64 不是合法的 base64：{exc}"
            ) from exc
        if len(raw) > IMPORT_MAX_BYTES:
            raise WebUIError(
                f"文件大小 {len(raw)} 字节超过 2 MB 上限"
                f"（{IMPORT_MAX_BYTES} 字节）"
            )
        source = {"kind": "file", "filename": filename.strip()}
        courses = _parse_import_bytes(raw, ext, term)
    if not courses:
        raise WebUIError(
            "没有解析出任何课程：请确认内容是「每行一门课」的文字，"
            "或带表头的 CSV / JSON / XLSX 课表"
        )
    return courses, source


def _import_summary(courses, existing):
    _, added, duplicates = merge_courses(existing, courses)
    return {
        "total": len(courses),
        "withIssues": sum(1 for c in courses if c.get("_issues")),
        "issueCount": sum(len(c.get("_issues") or []) for c in courses),
        "willAdd": len(added),
        "duplicates": len(duplicates),
        "duplicateNames": [c.get("name") or "" for c in duplicates],
    }


def preview_import(data_dir, payload):
    """Parse only — never writes. Mirrors import_timetable.py's preview:
    each course is returned with its "_issues" (if any) so the UI can
    flag them one by one."""
    courses, source = _parse_import_payload(data_dir, payload)
    existing = _load_json(Path(data_dir) / "courses.json", [])
    return {
        "ok": True,
        "source": source,
        "courses": courses,
        "summary": _import_summary(courses, existing),
        "issuesNote": (
            "带「待确认」的课程与命令行导入一致：会照常导入，待确认字段"
            "留空、不会被猜测填充，_issues 本身不会写进 courses.json；"
            "导入后请核对修正。"
        ),
    }


def apply_import(data_dir, payload):
    """Re-parse the same raw input (deterministic ⇒ identical to the
    preview; no server-side session state) and write, mirroring
    import_timetable.py --apply: timestamped backup first, dedupe by
    name+weekday+sections+weeks, and when nothing is new the file is
    not touched at all."""
    courses, source = _parse_import_payload(data_dir, payload)
    data = Path(data_dir)
    courses_path = data / "courses.json"
    existing = _load_json(courses_path, [])
    merged, added, duplicates = merge_courses(existing, courses)
    backup = None
    if added:
        backup = _backup(courses_path)
        _write_json(courses_path, merged)
    return {
        "ok": True,
        "message": (
            f"导入完成：新增 {len(added)} 门，"
            f"跳过重复 {len(duplicates)} 门，现有共 {len(merged)} 门"
        ),
        "source": source,
        "added": len(added),
        "addedNames": [c.get("name") or "" for c in added],
        "skippedDuplicates": len(duplicates),
        "duplicateNames": [c.get("name") or "" for c in duplicates],
        "withIssues": sum(1 for c in courses if c.get("_issues")),
        "total": len(merged),
        "backup": backup,
        "courses": [_course_view(item) for item in merged],
    }


# ------------------------------------------------------------------- routing ----
def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def handle_request(data_dir, method, raw_path, body=b"", env=None,
                   transport=None, sender_factory=None):
    """Route one request without needing a live socket.

    Returns (status, content_type, response_bytes). `env` / `transport` /
    `sender_factory` are injectable for tests; production leaves them
    as None (real environment, real HTTP transport).
    """
    parsed = urlparse(raw_path)
    path = parsed.path
    method = str(method or "").upper()
    try:
        if method == "GET" and path == "/api/state":
            return 200, "application/json; charset=utf-8", _json_bytes(
                get_state(data_dir)
            )
        if method == "GET" and path == "/api/sender":
            return 200, "application/json; charset=utf-8", _json_bytes(
                get_sender_view(data_dir, env=env)
            )
        if method == "POST" and path in {
            "/api/config", "/api/course-override",
            "/api/sender", "/api/sender/test",
            "/api/import/preview", "/api/import/apply",
        }:
            try:
                payload = json.loads((body or b"").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WebUIError(f"请求体不是合法 JSON：{exc}") from exc
            if path == "/api/config":
                result = save_config(data_dir, payload)
            elif path == "/api/course-override":
                result = save_course_override(data_dir, payload)
            elif path == "/api/sender":
                result = save_sender_settings(data_dir, payload, env=env)
            elif path == "/api/import/preview":
                result = preview_import(data_dir, payload)
            elif path == "/api/import/apply":
                result = apply_import(data_dir, payload)
            else:
                result = send_sender_test(
                    data_dir, payload, env=env, transport=transport,
                    sender_factory=sender_factory)
            return 200, "application/json; charset=utf-8", _json_bytes(result)
        if method == "GET" and path in {"/", "/index.html"}:
            if not UI_FILE.exists():
                raise WebUIError("UI 文件不存在", status=500)
            return 200, "text/html; charset=utf-8", UI_FILE.read_bytes()
        if method == "GET" and path == "/favicon.ico":
            return 204, "text/plain; charset=utf-8", b""
        if path.startswith("/api/"):
            raise WebUIError(f"接口不存在：{method} {path}", status=404)
        raise WebUIError(f"页面不存在：{path}", status=404)
    except WebUIError as exc:
        payload = {"ok": False, "error": str(exc), "errors": exc.errors}
        return exc.status, "application/json; charset=utf-8", _json_bytes(payload)
    except (OSError, ValueError) as exc:
        payload = {
            "ok": False,
            "error": f"读取数据失败：{exc}",
            "errors": [f"读取数据失败：{exc}"],
        }
        return 500, "application/json; charset=utf-8", _json_bytes(payload)


def make_handler(data_dir):
    class WebUIHandler(BaseHTTPRequestHandler):
        server_version = "ClassReminderWebUI/1.0"

        def _send(self, status, content_type, payload):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def do_GET(self):
            status, content_type, payload = handle_request(
                data_dir, "GET", self.path
            )
            self._send(status, content_type, payload)

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            # Import uploads carry a base64 file (2 MB file ≈ 2.8 MB of
            # base64), so those two endpoints get a larger — still
            # bounded — body limit; everything else stays at 64 KB.
            limit = (
                4 * 1024 * 1024
                if urlparse(self.path).path in IMPORT_PATHS
                else 64 * 1024
            )
            if length < 0 or length > limit:
                payload = _json_bytes(
                    {
                        "ok": False,
                        "error": "请求体过大或长度无效",
                        "errors": ["请求体过大或长度无效"],
                    }
                )
                self._send(400, "application/json; charset=utf-8", payload)
                return
            body = self.rfile.read(length) if length else b""
            status, content_type, payload = handle_request(
                data_dir, "POST", self.path, body
            )
            self._send(status, content_type, payload)

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            return

    return WebUIHandler


def create_server(host, port, data_dir):
    return ThreadingHTTPServer((host, int(port)), make_handler(Path(data_dir)))


def run_server(host="127.0.0.1", port=8765, data_dir=None):
    data_dir = Path(data_dir) if data_dir is not None else ROOT / "data"
    server = create_server(host, port, data_dir)
    actual_port = server.server_address[1]
    print(
        f"上课提醒配置 UI: http://{host}:{actual_port}/ "
        f"（data-dir: {data_dir}）"
    )
    print("仅供本机使用、无鉴权；不要把端口暴露到局域网或公网。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
