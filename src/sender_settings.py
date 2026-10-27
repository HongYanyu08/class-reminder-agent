"""Persistent sender (channel) settings: data/sender_settings.json.

Historically the sending channel was configured by environment variables
ONLY (REMINDER_CHANNEL / REMINDER_WEBHOOK_URL / REMINDER_GATEWAY_CMD / ...),
because a webhook URL is effectively a secret. The Web UI needs somewhere
to store what the user picks on its 发送渠道 page, so this module owns one
settings file next to the other data files:

    data/sender_settings.json        (gitignored — NEVER committed)

Resolution order for every field (highest first):

    CLI argument (--channel)  >  environment variable  >  settings file
    >  built-in default (channel console, webhook timeout 10 s)

The scheduler CLI resolves through the same code path (sender.build_sender
delegates here), so what the UI saves is what --tick --send will use,
unless an env var deliberately overrides it.

A settings file that exists but is corrupt or invalid is a hard error
(SenderSettingsError, a SenderConfigError): silently falling back to
another channel could send reminders somewhere the user never intended,
or quietly drop them onto the console. Fix or delete the file instead.

Secrets hygiene: this module masks webhook URLs / gateway commands for
display (mask_webhook_url / mask_secret), never logs them, and validation
error messages never echo secret values back.
"""
import json
import os
import shlex
import string
from pathlib import Path
from urllib.parse import urlparse

from .sender import CHANNELS, SenderConfigError

SETTINGS_FILENAME = "sender_settings.json"
SETTING_FIELDS = ("channel", "webhookUrl", "webhookTimeoutSec",
                  "gatewayCmd", "outboxDir")

# Which environment variable supplies each field (highest priority source).
ENV_FOR_FIELD = {
    "channel": "REMINDER_CHANNEL",
    "webhookUrl": "REMINDER_WEBHOOK_URL",
    "webhookTimeoutSec": "REMINDER_WEBHOOK_TIMEOUT",
    "gatewayCmd": "REMINDER_GATEWAY_CMD",
    "outboxDir": "REMINDER_OUTBOX_DIR",
}

DEFAULTS = {"channel": "console", "webhookUrl": None, "webhookTimeoutSec": 10,
            "gatewayCmd": None, "outboxDir": None}

# Placeholders the gateway command template may use (src/sender.py contract).
GATEWAY_PLACEHOLDERS = ("text", "task_id", "type", "user_id")

MIN_TIMEOUT_SEC, MAX_TIMEOUT_SEC = 1, 120


class SenderSettingsError(SenderConfigError):
    """The settings file / a settings payload is invalid. Raised loudly."""


def settings_path(data_dir):
    return Path(data_dir) / SETTINGS_FILENAME


def _default_data_dir():
    return Path(__file__).resolve().parent.parent / "data"


# ------------------------------------------------------------------ masking ----
def mask_secret(value):
    """Mask a secret-ish string: first 12 chars + last 4 for long values,
    fully masked when short (where 12+4 would reveal nearly everything)."""
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 20:
        return "••••••••"
    return f"{value[:12]}…{value[-4:]}"


def mask_webhook_url(url):
    """Mask a webhook URL as scheme://host/…last4. Falls back to
    mask_secret when the value does not parse as a URL."""
    url = str(url or "")
    if not url:
        return ""
    parts = urlparse(url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.hostname or parts.netloc}/…{url[-4:]}"
    return mask_secret(url)


def looks_masked(value):
    """True when a submitted value is a masked placeholder produced by
    mask_* (bullet or ellipsis marker) rather than a real secret. Used by
    the save path so re-submitting the masked display value never
    overwrites the stored secret."""
    return isinstance(value, str) and ("•" in value or "…" in value)


def sanitize_text(text, secrets):
    """Replace any full secret value occurring in `text` with its masked
    form (longest first). Used on every error/detail string that leaves
    this layer, so a transport error echoing the URL cannot leak it."""
    out = str(text or "")
    candidates = sorted({str(s) for s in secrets if s},
                        key=len, reverse=True)
    for secret in candidates:
        if secret in out:
            if secret.startswith(("http://", "https://")):
                replacement = mask_webhook_url(secret)
            else:
                replacement = mask_secret(secret)
            out = out.replace(secret, replacement)
    return out


# --------------------------------------------------------------- validation ----
def validate_gateway_template(template):
    """Return a list of problems with a gateway command template (empty
    list = OK), per the src/sender.py contract: shlex-split, no shell,
    placeholders drawn from GATEWAY_PLACEHOLDERS, and {text} is required
    (a command that never receives the message text cannot deliver it)."""
    errors = []
    try:
        argv = shlex.split(template)
    except ValueError as exc:
        return [f"gatewayCmd 无法解析（引号不配对等）：{exc}"]
    if not argv:
        return ["gatewayCmd 不能为空命令"]
    try:
        fields = [field for _, field, _, _ in string.Formatter().parse(template)
                  if field]
    except ValueError as exc:
        return [f"gatewayCmd 占位符括号不配对：{exc}"]
    unknown = [f for f in fields if f not in GATEWAY_PLACEHOLDERS]
    if unknown:
        errors.append(
            "gatewayCmd 含未知占位符："
            + ", ".join("{" + name + "}" for name in unknown)
            + "（可用：" + ", ".join("{" + n + "}" for n in GATEWAY_PLACEHOLDERS) + "）")
    if "text" not in fields:
        errors.append("gatewayCmd 必须包含 {text} 占位符（提醒正文要靠它传给命令）")
    return errors


def validate_settings(raw, source="设置"):
    """Validate a settings-shaped dict and return a cleaned copy holding
    only known fields (empty strings dropped). Raises SenderSettingsError
    listing every problem. Error messages never echo secret values."""
    if not isinstance(raw, dict):
        raise SenderSettingsError(f"{source}必须是 JSON 对象")
    errors = []
    unknown = sorted(set(raw) - set(SETTING_FIELDS))
    if unknown:
        errors.append(f"未知字段：{', '.join(unknown)}")

    cleaned = {}
    channel = raw.get("channel")
    if channel is not None:
        if not isinstance(channel, str) or \
                channel.strip().lower() not in CHANNELS:
            errors.append(
                f"channel 必须是以下之一：{', '.join(CHANNELS)}"
                f"（收到 {channel!r}）")
        else:
            cleaned["channel"] = channel.strip().lower()

    url = raw.get("webhookUrl")
    if url is not None:
        if not isinstance(url, str):
            errors.append("webhookUrl 必须是字符串")
        elif url.strip():
            parts = urlparse(url.strip())
            if parts.scheme != "https" or not parts.netloc:
                errors.append("webhookUrl 必须是 https 开头的完整 URL"
                              "（如 https://qyapi.weixin.qq.com/...）")
            else:
                cleaned["webhookUrl"] = url.strip()

    timeout = raw.get("webhookTimeoutSec")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            errors.append("webhookTimeoutSec 必须是数字（秒）")
        elif not MIN_TIMEOUT_SEC <= float(timeout) <= MAX_TIMEOUT_SEC:
            errors.append(
                f"webhookTimeoutSec 必须在 {MIN_TIMEOUT_SEC}–{MAX_TIMEOUT_SEC} 秒之间")
        else:
            cleaned["webhookTimeoutSec"] = timeout

    cmd = raw.get("gatewayCmd")
    if cmd is not None:
        if not isinstance(cmd, str):
            errors.append("gatewayCmd 必须是字符串")
        elif cmd.strip():
            cmd_errors = validate_gateway_template(cmd.strip())
            errors.extend(cmd_errors)
            if not cmd_errors:
                cleaned["gatewayCmd"] = cmd.strip()

    outbox = raw.get("outboxDir")
    if outbox is not None:
        if not isinstance(outbox, str):
            errors.append("outboxDir 必须是字符串")
        elif outbox.strip():
            cleaned["outboxDir"] = outbox.strip()

    if errors:
        raise SenderSettingsError(
            f"{source}不合法：" + "；".join(errors))
    return cleaned


def load_settings_file(data_dir):
    """Return the validated settings dict from data/sender_settings.json,
    or {} when the file does not exist. A corrupt/invalid file raises
    SenderSettingsError — never a silent fallback."""
    path = settings_path(data_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SenderSettingsError(
            f"发送渠道设置文件损坏：{path} 不是合法 JSON（{exc}）。"
            "请修正或删除该文件后重试，不会静默改用其他渠道。") from exc
    return validate_settings(raw, source=f"设置文件 {path.name}")


# --------------------------------------------------------------- resolution ----
def resolve_sender_config(channel=None, env=None, data_dir=None):
    """Resolve the effective sender configuration.

    Returns (config, sources): config has every SETTING_FIELDS key;
    sources[field] is one of "argument" / "env" / "settings" / "default".
    The settings file is consulted only when data_dir is given (the
    scheduler CLI and the Web UI always pass it); with data_dir=None the
    historical env-only behaviour is preserved.
    """
    env = os.environ if env is None else env
    config = dict(DEFAULTS)
    sources = {field: "default" for field in SETTING_FIELDS}

    if data_dir is not None:
        for field, value in load_settings_file(data_dir).items():
            config[field] = value
            sources[field] = "settings"

    for field, env_name in ENV_FOR_FIELD.items():
        raw = env.get(env_name)
        if raw is None or str(raw).strip() == "":
            continue
        if field == "webhookTimeoutSec":
            try:
                config[field] = float(raw)
            except (TypeError, ValueError):
                # Match the historical SenderConfigError for a bad env
                # timeout; surfacing it even for non-webhook channels is
                # avoided by keeping the raw value and letting the webhook
                # branch of build_sender_from_config reject it if used.
                config[field] = raw
        elif field == "channel":
            config[field] = str(raw).strip().lower()
        else:
            config[field] = str(raw).strip()
        sources[field] = "env"

    if channel:
        config["channel"] = str(channel).strip().lower()
        sources["channel"] = "argument"

    if config["channel"] not in CHANNELS:
        raise SenderConfigError(
            f"未知发送通道 {config['channel']!r}"
            f"（来源：{sources['channel']}），可用通道：{', '.join(CHANNELS)}")
    return config, sources


def build_sender_from_config(config, sources=None, data_dir=None,
                             stream=None, transport=None):
    """Construct a sender object from a resolved config dict (see
    resolve_sender_config). Channel-specific missing config raises
    SenderConfigError via the sender constructors, as before."""
    from . import sender as _sender  # deferred: sender imports us lazily

    sources = sources or {}
    channel = config.get("channel") or "console"
    if channel == "console":
        return _sender.ConsoleSender(stream=stream)
    if channel == "outbox":
        outbox_dir = config.get("outboxDir")
        if not outbox_dir:
            base = Path(data_dir) if data_dir else _default_data_dir()
            outbox_dir = base / "outbox"
        return _sender.OutboxSender(outbox_dir)
    if channel == "webhook":
        raw_timeout = config.get("webhookTimeoutSec", 10)
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            origin = ("环境变量 REMINDER_WEBHOOK_TIMEOUT"
                      if sources.get("webhookTimeoutSec") == "env"
                      else "webhookTimeoutSec 设置")
            raise SenderConfigError(
                f"{origin} 不是数字：{raw_timeout!r}")
        return _sender.WebhookSender(config.get("webhookUrl"),
                                     timeout=timeout, transport=transport)
    if channel == "gateway":
        return _sender.GatewaySender(config.get("gatewayCmd"))
    if channel == "session":
        # No settings field of its own: the session outbox lives directly
        # in the data dir (session_outbox.jsonl), next to reminders.json.
        base = Path(data_dir) if data_dir else _default_data_dir()
        return _sender.SessionSender(base)
    raise SenderConfigError(
        f"未知发送通道 {channel!r}，可用通道：{', '.join(CHANNELS)}")
