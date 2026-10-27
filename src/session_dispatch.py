"""Session (main-session) delivery: the two-phase send used by the
微信 ClawBot deployment.

Why two phases: measured on the user's OpenClaw install (2026-10-27,
OpenClaw 2026.10.5 + the official openclaw-weixin plugin), the raw
`openclaw message send` CLI and isolated-session cron announce delivery
are both rejected by the iLink backend with
"sendMessage ret=-3 invalid arguments" — those paths do not attach the
per-account context_token that iLink issues with an inbound message.
The only proactive send that works is the agent speaking inside the
main session (the WeChat DM routes into it), e.g. after a heartbeat or
a `--session main --system-event` cron wake. So delivery is split:

  phase 1 (deterministic, here): a due pending task is "sent" through
      the session channel = one record appended to
      <data-dir>/session_outbox.jsonl, task status -> queued with
      queuedAt + channel="session". queued is NOT sent: the follow_up
      generator only looks at sent tasks, so nothing downstream treats
      an unspoken reminder as delivered.
  phase 2 (the agent, driven by HEARTBEAT.md): the woken agent speaks
      each queued message verbatim into the current conversation, then
      runs run_scheduler.py --confirm-sent <ids>, which flips exactly
      those queued tasks to sent (+sentAt). Ids that are not currently
      queued are reported as problems and left untouched — the agent
      cannot confirm what it did not queue/speak.

Stale recovery: a queued task that stays unconfirmed for more than
SESSION_REQUEUE_MIN minutes (agent crashed, session lost, ...) is
reverted to pending at the next tick and queued again in the same
pass, so a reminder is re-spoken at most once per stale cycle instead
of being lost. The LLM never computes any of this; it only reads the
queued wording aloud.
"""
from datetime import timedelta

from .scheduler import _parse, compose_message, find_due

SESSION_REQUEUE_MIN = 15


def queue_due_for_session(tasks, occurrences_by_id, sender, now_iso,
                          user=None, message_fn=None):
    """Phase 1: queue every due pending task through the session sender.

    Mirrors scheduler.dispatch_due's loop and failure口径 (a due task
    whose occurrence is missing, or whose queue-write fails, is marked
    failed with the same fields), except success lands on the honest
    intermediate state: status=queued, queuedAt=now, channel=sender.name.
    Returns one result dict per due task, with status "queued"/"failed".
    """
    message_fn = message_fn or compose_message
    results = []
    for task in find_due(tasks, now_iso):
        occ = occurrences_by_id.get(task.get("occurrenceId"))
        if occ is None:
            task["status"] = "failed"
            task["error"] = f"occurrence 不存在：{task.get('occurrenceId')}"
            task["retryCount"] = int(task.get("retryCount", 0)) + 1
            task["lastAttemptAt"] = now_iso
            results.append({"taskId": task["id"], "type": task.get("type"),
                            "status": "failed", "error": task["error"]})
            continue
        message = message_fn(occ, task["type"], task)
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
            task["status"] = "queued"
            task["queuedAt"] = now_iso
            task["channel"] = sender.name
            task.pop("error", None)
            results.append({"taskId": task["id"], "type": task.get("type"),
                            "status": "queued", "channel": sender.name,
                            "message": message})
    return results


def confirm_session_sent(tasks, task_ids, now_iso):
    """Phase 2: flip queued tasks to sent after the agent has spoken them.

    Only tasks currently in status "queued" are confirmed (status=sent,
    sentAt=now). Every other id — unknown, still pending, already sent,
    anything else — is returned in the problems list and its task (if
    any) is left byte-for-byte untouched. Returns (confirmed_ids,
    problems): two lists of strings, in input order."""
    by_id = {t["id"]: t for t in tasks}
    confirmed, problems = [], []
    for task_id in task_ids:
        task = by_id.get(task_id)
        if task is None:
            problems.append(f"未知任务 id：{task_id}")
            continue
        if task.get("status") != "queued":
            problems.append(
                f"任务 {task_id} 当前状态为 {task.get('status')}，不是 queued，未改动")
            continue
        task["status"] = "sent"
        task["sentAt"] = now_iso
        confirmed.append(task_id)
    return confirmed, problems


def requeue_stale_session_tasks(tasks, now_iso, stale_min=SESSION_REQUEUE_MIN):
    """Revert queued tasks whose queuedAt is more than stale_min minutes
    before now back to pending, so queue_due_for_session picks them up
    again in the same tick. queuedAt is dropped (the next queue stamps a
    fresh one) and note records why. A queued task with a missing or
    unparseable queuedAt counts as stale — it can never be confirmed
    sensibly, so recovering it beats stranding it. Returns the count."""
    now = _parse(now_iso)
    limit = timedelta(minutes=int(stale_min))
    count = 0
    for task in tasks:
        if task.get("status") != "queued":
            continue
        try:
            stale = now - _parse(task["queuedAt"]) > limit
        except (KeyError, TypeError, ValueError):
            stale = True
        if not stale:
            continue
        task["status"] = "pending"
        task.pop("queuedAt", None)
        task["note"] = "session requeue"
        count += 1
    return count
