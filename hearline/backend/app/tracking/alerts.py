"""Alerts for whoever runs Hearline. Raised by the worker when:

    job_dead     a job failed for good (after its retries, or a permanent error)
    budget_cap   the daily USD budget cap was reached (new jobs pause until tomorrow UTC)
    plan_failed  the daily plan raised an error (retried next interval)

Each alert is
  1. an ERROR log line "ALERT <kind>: <message>": in production one CloudWatch metric filter on
     "ALERT" turns all of them into an alarm (hearlineCDK/README.md), and
  2. if ALERT_WEBHOOK_URL is set, a POST of {"text": "..."}: the JSON Slack incoming webhooks,
     Microsoft Teams workflows and Discord (webhook URL + "/slack") accept.
The webhook gets the same kind at most once per `alert_cooldown_minutes` per process, so a burst
of failures is one message; every occurrence is still logged.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)
_lock = threading.Lock()
_last_sent: dict[str, float] = {}


def alert(kind: str, message: str) -> None:
    log.error("ALERT %s: %s", kind, message)
    settings = get_settings()
    if not settings.alert_webhook_url:
        return
    with _lock:
        now = time.monotonic()
        if now - _last_sent.get(kind, -1e9) < settings.alert_cooldown_minutes * 60:
            return
        _last_sent[kind] = now
    try:
        httpx.post(settings.alert_webhook_url, timeout=10,
                   json={"text": f"Hearline alert ({kind}): {message}"[:3000]})
    except httpx.HTTPError as exc:  # an alert must never break the worker
        log.warning("Alert webhook failed: %s", exc)
