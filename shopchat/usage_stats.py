"""Per Gemini API credential: count calls into UTC minute / hour / day buckets."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from django.db import IntegrityError, OperationalError, transaction
from django.db.models import F, Sum
from django.utils import timezone

from shopchat.models import GeminiCredentialUsageBucket

logger = logging.getLogger("shopchat.usage_stats")


def _floor_minute(dt):
    return dt.replace(second=0, microsecond=0)


def _floor_hour(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def _floor_day(dt):
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def record_gemini_usage(
    credential_id: int | None,
    *,
    embed_delta: int = 0,
    chat_delta: int = 0,
) -> None:
    """Increment counters for this DB credential (env keys are not tracked)."""
    if credential_id is None:
        return
    if embed_delta == 0 and chat_delta == 0:
        return
    now = timezone.now()
    for granularity, floor in (
        (GeminiCredentialUsageBucket.Granularity.MINUTE, _floor_minute),
        (GeminiCredentialUsageBucket.Granularity.HOUR, _floor_hour),
        (GeminiCredentialUsageBucket.Granularity.DAY, _floor_day),
    ):
        starts = floor(now)
        for attempt in range(8):
            try:
                with transaction.atomic():
                    obj, created = GeminiCredentialUsageBucket.objects.get_or_create(
                        credential_id=credential_id,
                        starts_at=starts,
                        granularity=granularity,
                        defaults={
                            "embed_calls": embed_delta,
                            "chat_calls": chat_delta,
                        },
                    )
                    if not created:
                        GeminiCredentialUsageBucket.objects.filter(pk=obj.pk).update(
                            embed_calls=F("embed_calls") + embed_delta,
                            chat_calls=F("chat_calls") + chat_delta,
                        )
                break
            except IntegrityError:
                logger.debug(
                    "cred usage race cred=%s g=%s attempt=%s",
                    credential_id,
                    granularity,
                    attempt,
                )
                continue
            except OperationalError as exc:
                msg = str(exc).lower()
                if ("locked" in msg or "busy" in msg) and attempt < 7:
                    time.sleep(0.03 * (2**attempt))
                    logger.debug(
                        "cred usage sqlite lock cred=%s g=%s attempt=%s",
                        credential_id,
                        granularity,
                        attempt,
                    )
                    continue
                raise


def _sum_api_calls(
    credential_id: int,
    *,
    granularity: str,
    starts_at__gte,
) -> tuple[int, int, int]:
    """Returns (embed_total, chat_total, combined)."""
    r = (
        GeminiCredentialUsageBucket.objects.filter(
            credential_id=credential_id,
            granularity=granularity,
            starts_at__gte=starts_at__gte,
        ).aggregate(
            e=Sum("embed_calls"),
            c=Sum("chat_calls"),
        )
    )
    e = r["e"] or 0
    c = r["c"] or 0
    return e, c, e + c


def credential_usage_summary(credential_id: int) -> dict:
    """
    Rolling windows in UTC for admin list columns.
    Uses minute buckets for last 60m /24h; day buckets from00:00 UTC today.
    """
    now = timezone.now()
    midnight_utc = _floor_day(now)
    h24 = now - timedelta(hours=24)
    h1 = now - timedelta(hours=1)

    e1, c1, t1 = _sum_api_calls(
        credential_id,
        granularity=GeminiCredentialUsageBucket.Granularity.MINUTE,
        starts_at__gte=h1,
    )
    e24, c24, t24 = _sum_api_calls(
        credential_id,
        granularity=GeminiCredentialUsageBucket.Granularity.MINUTE,
        starts_at__gte=h24,
    )
    ed, cd, tday = _sum_api_calls(
        credential_id,
        granularity=GeminiCredentialUsageBucket.Granularity.DAY,
        starts_at__gte=midnight_utc,
    )
    return {
        "embed_60m": e1,
        "chat_60m": c1,
        "total_60m": t1,
        "embed_24h": e24,
        "chat_24h": c24,
        "total_24h": t24,
        "embed_today": ed,
        "chat_today": cd,
        "total_today": tday,
    }
