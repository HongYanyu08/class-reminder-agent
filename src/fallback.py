"""Fallback (backup) delivery: the escalation path for session tasks.

Why this exists (measured incident, 2026-10-28): the session channel
can only deliver while WeChat's per-account context token is fresh —
the token is issued by an inbound message and dies after hours of
silence. The user's last inbound was 21:30 the night before, so the
08:05 departure reminder sat queued / re-queued (15-minute cycles)
undeliverable until the user happened to message the bot at 08:35 and
the backlog flushed at 08:55 — 50 minutes late (not lost, thanks to
the requeue). The gateway and the tick were healthy throughout; only
the token was stale.

The fallback removes the token from the critical path: when a session
task has been queued for at least fallbackAfterMin minutes without
being confirmed sent (src/session_dispatch.escalate_stale_queued_tasks
owns that state machine), the same reminder text is pushed through a
provider that needs no WeChat session at all:

  serverchan  Server酱: POST https://sctapi.ftqq.com/<SendKey>.send,
              form fields title / desp. Success = HTTP 2xx AND the
              JSON body's "code" == 0.
  pushplus    PushPlus: POST https://www.pushplus.plus/send, JSON
              {token, title, content}. Success = HTTP 2xx AND the
              JSON body's "code" == 200.

Configuration lives with the other sender settings
(src/sender_settings.py): fallbackChannel ("none" default /
"serverchan" / "pushplus"), fallbackKey (a secret — settings file or
REMINDER_FALLBACK_* env vars only, never logged, masked in every
string this module produces), fallbackAfterMin (default 15, 5–120).

The real provider domains appear ONLY in this module's URL constants;
tests always inject a fake transport, so no test traffic ever leaves
the machine (there is a standing rule after a past incident in which
an exploratory test call reached a real domain).
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from .sender import SenderConfigError, SenderError

PROVIDERS = ("serverchan", "pushplus")
SERVERCHAN_URL_TEMPLATE = "https://sctapi.ftqq.com/{key}.send"
PUSHPLUS_URL = "https://www.pushplus.plus/send"

DEFAULT_TIMEOUT_SEC = 10
MIN_AFTER_MIN, MAX_AFTER_MIN = 5, 120
DEFAULT_AFTER_MIN = 15

_PROVIDER_LABELS = {"serverchan": "Server酱", "pushplus": "PushPlus"}


def _title_of(message):
    """Title for the push: the reminder's first line (composed reminder
    messages are single-line, so usually the whole message)."""
    lines = str(message or "").splitlines()
    return (lines[0] if lines else "").strip()[:100]


def _default_transport(url, data, headers, timeout):
    """Default transport: POST raw bytes via urllib (stdlib only).
    Returns (status_code, body_text). Raises on transport-level errors.
    `url` may embed the secret key (Server酱) — callers must never put
    the URL or a raw exception echoing it into an error string without
    scrubbing (FallbackSender.send does exactly that)."""
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:  # a response arrived, just not 2xx
        return e.code, e.read().decode("utf-8", "replace")


class FallbackSender:
    """One fallback provider push. send() follows the sender contract:
    returns {"detail": ...} on success, raises SenderError on failure.

    name is "fallback:<provider>" — deliberately distinct from every
    main channel name, so a task delivered this way records
    channel="fallback:serverchan" etc. in reminders.json: the audit
    trail stays honest about which path actually delivered it.
    """

    def __init__(self, provider, key, timeout=DEFAULT_TIMEOUT_SEC,
                 transport=None):
        if provider not in PROVIDERS:
            raise SenderConfigError(
                f"未知备用通道 {provider!r}，可用：{', '.join(PROVIDERS)}")
        if not key:
            raise SenderConfigError(
                f"备用通道 {provider} 未配置 key：请设置环境变量 "
                "REMINDER_FALLBACK_KEY，或在 Web UI 的「发送渠道」页填写")
        self.provider = provider
        self.name = f"fallback:{provider}"
        self.key = str(key)
        self.timeout = timeout
        self.transport = transport or _default_transport

    # ------------------------------------------------------------ request ----
    def build_request(self, task, message, context=None):
        """Return (url, body_bytes, headers) for this provider. The
        Server酱 URL embeds the key — handle it as a secret."""
        title = _title_of(message)
        if self.provider == "serverchan":
            url = SERVERCHAN_URL_TEMPLATE.format(key=self.key)
            body = urllib.parse.urlencode(
                {"title": title, "desp": message}).encode("utf-8")
            return url, body, {
                "Content-Type": "application/x-www-form-urlencoded"}
        body = json.dumps({"token": self.key, "title": title,
                           "content": message},
                          ensure_ascii=False).encode("utf-8")
        return PUSHPLUS_URL, body, {"Content-Type": "application/json"}

    def _scrub(self, text):
        """Remove the raw key from any string that might leave this
        layer (transport errors can echo the request URL, which for
        Server酱 contains the key)."""
        out = str(text or "")
        if self.key and self.key in out:
            out = out.replace(self.key, "•••")
        return out

    # ---------------------------------------------------------------- send ----
    def send(self, task, message, context=None):
        url, body, headers = self.build_request(task, message, context)
        label = _PROVIDER_LABELS[self.provider]
        try:
            status, resp_body = self.transport(url, body, headers,
                                               self.timeout)
        except Exception as e:  # timeout / DNS / connection refused / ...
            raise SenderError(self._scrub(
                f"{label} 请求失败：{type(e).__name__}: {e}")) from e
        if not (200 <= int(status) < 300):
            raise SenderError(self._scrub(
                f"{label} 返回 HTTP {status}：{str(resp_body)[:200]}"))
        try:
            code = json.loads(resp_body).get("code")
        except (ValueError, AttributeError):
            raise SenderError(self._scrub(
                f"{label} 返回内容不是合法 JSON：{str(resp_body)[:200]}"))
        expected = 0 if self.provider == "serverchan" else 200
        if code != expected:
            raise SenderError(self._scrub(
                f"{label} 返回 code={code!r}（期望 {expected}）："
                f"{str(resp_body)[:200]}"))
        return {"detail": f"{label} HTTP {status}"}


def build_fallback_sender(data_dir=None, env=None, transport=None):
    """Resolve the fallback configuration and build a FallbackSender.

    Returns (sender, after_min). sender is None — escalation entirely
    off, behaviour identical to before this feature existed — when
    fallbackChannel resolves to "none" (the default) OR when a provider
    is chosen but no key is configured anywhere. A provider WITH a key
    but an invalid fallbackAfterMin raises SenderConfigError instead:
    the operator clearly intended the feature to work, so a broken
    knob must abort the tick loudly (exit 2), never silently disable
    or mis-time the escalation. Resolution priority is the usual one
    (src/sender_settings.resolve_sender_config): REMINDER_FALLBACK_*
    env > data/sender_settings.json > defaults.
    """
    from .sender_settings import resolve_sender_config

    config, _sources = resolve_sender_config(env=env, data_dir=data_dir)
    provider = config.get("fallbackChannel") or "none"
    if provider == "none":
        return None, DEFAULT_AFTER_MIN
    key = config.get("fallbackKey")
    if not key:
        # Provider picked but never given a key: treat as "not
        # configured" and stay completely out of the way.
        return None, DEFAULT_AFTER_MIN
    raw_after = config.get("fallbackAfterMin", DEFAULT_AFTER_MIN)
    try:
        after = float(raw_after)
    except (TypeError, ValueError):
        raise SenderConfigError(
            f"fallbackAfterMin 不是数字：{raw_after!r}"
            "（来源：环境变量 REMINDER_FALLBACK_AFTER_MIN 或设置文件）")
    if not MIN_AFTER_MIN <= after <= MAX_AFTER_MIN:
        raise SenderConfigError(
            f"fallbackAfterMin 必须在 {MIN_AFTER_MIN}–{MAX_AFTER_MIN} 分钟之间"
            f"（当前 {raw_after!r}，来源：环境变量 REMINDER_FALLBACK_AFTER_MIN"
            "或设置文件）")
    sender = FallbackSender(provider, key, transport=transport)
    return sender, int(after)
