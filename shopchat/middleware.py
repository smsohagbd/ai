"""HTTP middleware for shopchat."""

from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse

logger = logging.getLogger("shopchat.middleware")


def _client_ip(request) -> str:
    xff = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    if xff:
        return xff.split(",")[0].strip()[:64] or "unknown"
    return (request.META.get("REMOTE_ADDR") or "unknown")[:64]


def _should_rate_limit(path: str) -> bool:
    if not path.startswith("/api/"):
        return False
    # Meta sends bursts to the webhook; do not throttle subscription verification.
    if path.startswith("/api/webhook"):
        return False
    return True


class ApiRateLimitMiddleware:
    """
    Limit API routes per client IP per minute. /api/webhook is excluded (Messenger).
    /api/inbox/* uses API_INBOX_RATE_LIMIT_PER_MINUTE (default 300); other /api/* uses
    API_RATE_LIMIT_PER_MINUTE (default 15). Set either limit to 0 to disable that bucket.
    Uses Django cache (Redis recommended for multi-worker).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            return self.get_response(request)
        path = request.path or ""
        if not _should_rate_limit(path):
            return self.get_response(request)

        if path.startswith("/api/inbox/"):
            limit = getattr(settings, "API_INBOX_RATE_LIMIT_PER_MINUTE", 300)
            bucket = "inbox"
        else:
            limit = getattr(settings, "API_RATE_LIMIT_PER_MINUTE", 15)
            bucket = "general"
        if limit <= 0:
            return self.get_response(request)

        ip = _client_ip(request)
        window = int(time.time() // 60)
        key = f"shopchat:api_rl:{bucket}:{ip}:{window}"
        try:
            count = cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=120)
            count = 1

        if count > limit:
            logger.warning("API rate limit exceeded ip=%s path=%s count=%s", ip, path, count)
            return JsonResponse(
                {
                    "ok": False,
                    "error": "Too many requests. Limit is "
                    f"{limit} per minute. Try again shortly.",
                },
                status=429,
                headers={"Retry-After": "60"},
            )

        return self.get_response(request)
