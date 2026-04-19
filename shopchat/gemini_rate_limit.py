"""Hard per-minute caps on Gemini API calls (free-tier safety). UTC minute buckets.

Counters are per OS process (thread-safe). With multiple Gunicorn workers, each worker has its
own counts — set lower caps or use one worker if you must stay under a strict global quota.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.utils import timezone as django_timezone

logger = logging.getLogger("shopchat.gemini_rate_limit")

_lock = threading.Lock()
_counts: dict[tuple[str, datetime], int] = {}


def _floor_minute_utc() -> datetime:
    now = django_timezone.now()
    return now.astimezone(dt_timezone.utc).replace(second=0, microsecond=0)


def _prune_counts(current_minute: datetime) -> None:
    cutoff = current_minute - timedelta(minutes=2)
    dead = [k for k in _counts if k[1] < cutoff]
    for k in dead:
        _counts.pop(k, None)


def _wait_minute_slot(slot_prefix: str, limit: int, max_wait_sec: float) -> None:
    if limit <= 0:
        return
    deadline = time.monotonic() + max_wait_sec
    logged_wait = False
    while time.monotonic() < deadline:
        m = _floor_minute_utc()
        with _lock:
            _prune_counts(m)
            key = (slot_prefix, m)
            c = _counts.get(key, 0)
            if c < limit:
                _counts[key] = c + 1
                return
            at_cap = c
        if not logged_wait:
            logger.info(
                "Gemini rate limit: waiting for slot %r (UTC minute full: %s/%s)",
                slot_prefix,
                at_cap,
                limit,
            )
            logged_wait = True
        now = django_timezone.now().astimezone(dt_timezone.utc)
        sec_left = 60.0 - now.second - now.microsecond / 1_000_000 + 0.05
        time.sleep(min(max(sec_left, 0.05), 1.0))
    raise TimeoutError(
        f"Gemini rate limit: exceeded max wait {max_wait_sec}s for {slot_prefix!r} "
        f"(cap {limit} calls/min UTC)"
    )


def wait_gemini_embedding_slot() -> None:
    if not getattr(settings, "GEMINI_RATE_LIMIT_ENABLED", True):
        return
    lim = getattr(settings, "GEMINI_RL_EMBEDDING_PER_MINUTE", 25)
    mx = float(getattr(settings, "GEMINI_RL_MAX_WAIT_SEC", 180.0))
    _wait_minute_slot("embed:gemini-embedding", lim, mx)


def wait_gemini_chat_model_slot(model_id: str) -> None:
    if not getattr(settings, "GEMINI_RATE_LIMIT_ENABLED", True):
        return
    lim = getattr(settings, "GEMINI_RL_CHAT_PER_MODEL_PER_MINUTE", 10)
    mx = float(getattr(settings, "GEMINI_RL_MAX_WAIT_SEC", 180.0))
    safe_id = (model_id or "unknown").replace(":", "_")
    _wait_minute_slot(f"chat:{safe_id}", lim, mx)
