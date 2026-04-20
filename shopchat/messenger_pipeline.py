"""Inbound Messenger → AI pipeline (batch, debounce, reply-to context)."""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest

from shopchat.chat_turn import run_chat_turn
from shopchat import messenger_client
from shopchat.models import ChatMessage, Conversation

logger = logging.getLogger("shopchat.messenger_pipeline")

_lock = threading.Lock()
_debounce_timers: dict[str, threading.Timer] = {}


def _debounce_sec() -> float:
    return float(getattr(settings, "MESSENGER_INGEST_DEBOUNCE_SEC", 1.5))


def _cache_key(psid: str) -> str:
    return f"messenger_ingest:{psid}"


def _cancel_timer(psid: str) -> None:
    t = _debounce_timers.pop(psid, None)
    if t is not None:
        t.cancel()


def _schedule_flush(psid: str, django_request: HttpRequest | None) -> None:
    _cancel_timer(psid)
    delay = _debounce_sec()

    def _run() -> None:
        with _lock:
            _debounce_timers.pop(psid, None)
        try:
            _flush_pending(psid, django_request)
        except Exception:
            logger.exception("Messenger ingest flush failed psid=…%s", psid[-8:])

    timer = threading.Timer(delay, _run)
    timer.daemon = True
    timer.start()
    _debounce_timers[psid] = timer


def _merge_pending(
    psid: str,
    *,
    text: str,
    image_urls: list[str],
    mids: list[str],
    reply_to_mid: str | None,
) -> None:
    key = _cache_key(psid)
    with _lock:
        raw = cache.get(key)
        if raw is None:
            pending: dict[str, Any] = {
                "texts": [],
                "urls": [],
                "mids": [],
                "reply_to_mid": None,
            }
        else:
            pending = raw
        if text and text.strip():
            pending["texts"].append(text.strip())
        for u in image_urls:
            if u and u not in pending["urls"]:
                pending["urls"].append(u)
        for m in mids:
            if m and m not in pending["mids"]:
                pending["mids"].append(m)
        if reply_to_mid:
            pending["reply_to_mid"] = reply_to_mid
        cache.set(key, pending, 180)


def _flush_pending(psid: str, django_request: HttpRequest | None) -> None:
    key = _cache_key(psid)
    with _lock:
        pending = cache.get(key)
        if pending:
            cache.delete(key)
    if not pending:
        return

    texts = pending.get("texts") or []
    full_text = "\n\n".join(t for t in texts if t and str(t).strip())
    urls: list[str] = list(pending.get("urls") or [])
    mids: list[str] = list(pending.get("mids") or [])
    reply_to_mid = pending.get("reply_to_mid") or None

    user_images: list[tuple[bytes, str]] = []
    for u in urls:
        try:
            data, mime = messenger_client.fetch_url_bytes(u)
            if data:
                user_images.append((data, mime))
        except Exception:
            logger.exception("Failed to fetch Messenger image %s", (u or "")[:80])

    if not full_text and not user_images:
        return

    conversation, _ = Conversation.objects.get_or_create(
        channel=Conversation.Channel.MESSENGER,
        psid=str(psid),
        defaults={"title": ""},
    )
    messenger_client.ensure_messenger_title_for_conversation(conversation)

    prefix = ""
    if reply_to_mid:
        parent = ChatMessage.objects.filter(
            conversation=conversation,
            messenger_mid=str(reply_to_mid).strip(),
        ).first()
        if parent:
            snippet = (parent.text or "").strip()
            if len(snippet) > 400:
                snippet = snippet[:400] + "…"
            role = "user" if parent.role == ChatMessage.Role.USER else "assistant"
            prefix = f"[Customer replied to this earlier {role} message: {snippet}]\n\n"
        else:
            prefix = (
                f"[Customer replied to an earlier message (Messenger ref).]\n\n"
            )

    final_text = prefix + full_text
    last_mid = mids[-1] if mids else ""

    run_chat_turn(
        conversation=conversation,
        text=final_text,
        user_images=user_images,
        request=django_request,
        messenger_mid=last_mid,
        allow_messenger_outbound=True,
    )


def _extract_message_parts(msg: dict[str, Any]) -> tuple[str, list[str], str, str | None]:
    mid = (msg.get("mid") or "").strip()
    text = (msg.get("text") or "").strip()
    image_urls: list[str] = []
    for att in msg.get("attachments") or []:
        if att.get("type") == "image":
            payload = att.get("payload") or {}
            u = payload.get("url")
            if u:
                image_urls.append(u)
    reply_to = msg.get("reply_to") or {}
    reply_mid = (reply_to.get("mid") or "").strip() or None
    return text, image_urls, mid, reply_mid


def _merge_events_for_sender(events: list[dict[str, Any]]) -> tuple[str, list[str], list[str], str | None]:
    """Combine multiple messaging events from one webhook POST for the same sender."""
    texts: list[str] = []
    urls: list[str] = []
    mids: list[str] = []
    reply_to_mid: str | None = None
    for event in events:
        msg = event.get("message") or {}
        t, iu, mid, rt = _extract_message_parts(msg)
        if t:
            texts.append(t)
        for u in iu:
            if u not in urls:
                urls.append(u)
        if mid:
            mids.append(mid)
        if rt:
            reply_to_mid = rt
    merged_text = "\n\n".join(texts)
    return merged_text, urls, mids, reply_to_mid


def enqueue_messenger_ingest(
    psid: str,
    *,
    text: str,
    image_urls: list[str],
    mids: list[str],
    reply_to_mid: str | None,
    django_request: HttpRequest | None,
) -> None:
    _merge_pending(
        psid,
        text=text,
        image_urls=image_urls,
        mids=mids,
        reply_to_mid=reply_to_mid,
    )
    _schedule_flush(psid, django_request)


def process_webhook_payload(payload: dict[str, Any], django_request: HttpRequest | None) -> None:
    """Group events by sender (same POST), merge parts, then debounce across rapid webhooks."""
    by_sender: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in messenger_client.iter_messaging_events(payload):
        if event.get("message", {}).get("is_echo"):
            continue
        sid = (event.get("sender") or {}).get("id")
        if not sid:
            continue
        by_sender[str(sid)].append(event)

    for psid, events in by_sender.items():
        merged_text, urls, mids, reply_to_mid = _merge_events_for_sender(events)
        if not merged_text and not urls:
            continue
        enqueue_messenger_ingest(
            psid,
            text=merged_text,
            image_urls=urls,
            mids=mids,
            reply_to_mid=reply_to_mid,
            django_request=django_request,
        )
