"""Inbound Messenger → AI pipeline.

Behavior:
- Each inbound customer message is stored as its OWN ChatMessage bubble (Messenger-like).
- A short debounce window collapses bursts so the AI is invoked at most once per burst.
- The AI only replies to the **latest** message in the burst; earlier messages in the same
  burst are saved (visible in the inbox) but receive no individual AI reply.
- ``reply_to`` (Messenger "reply to message") is applied only to the latest message context.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.http import HttpRequest

from shopchat import messenger_client
from shopchat.chat_turn import _image_filename_suffix, run_chat_turn
from shopchat.models import ChatMessage, ChatUserImage, Conversation

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


def _merge_pending(psid: str, *, items: list[dict[str, Any]], reply_to_mid: str | None) -> None:
    """Append each inbound message to the per-PSID pending queue (preserves message boundaries)."""
    key = _cache_key(psid)
    with _lock:
        raw = cache.get(key)
        if raw is None:
            pending: dict[str, Any] = {"items": [], "reply_to_mid": None}
        else:
            pending = raw
        seen_mids = {it.get("mid") for it in pending["items"] if it.get("mid")}
        for it in items:
            mid = (it.get("mid") or "").strip()
            text = (it.get("text") or "").strip()
            urls = list(it.get("image_urls") or [])
            if not text and not urls:
                continue
            if mid and mid in seen_mids:
                continue
            pending["items"].append({"text": text, "image_urls": urls, "mid": mid})
            if mid:
                seen_mids.add(mid)
        if reply_to_mid:
            pending["reply_to_mid"] = reply_to_mid
        cache.set(key, pending, 180)


def _fetch_images(image_urls: list[str]) -> list[tuple[bytes, str]]:
    out: list[tuple[bytes, str]] = []
    for u in image_urls:
        try:
            data, mime = messenger_client.fetch_url_bytes(u)
            if data:
                out.append((data, mime))
        except Exception:
            logger.exception("Failed to fetch Messenger image %s", (u or "")[:80])
    return out


def _save_user_message_no_ai(
    conversation: Conversation,
    *,
    text: str,
    user_images: list[tuple[bytes, str]],
    messenger_mid: str,
) -> None:
    """Persist a customer message bubble without invoking the AI (used for non-latest messages in a burst)."""
    mid = (messenger_mid or "").strip()
    if mid and ChatMessage.objects.filter(conversation=conversation, messenger_mid=mid).exists():
        return
    if not text and not user_images:
        return
    msg = ChatMessage.objects.create(
        conversation=conversation,
        role=ChatMessage.Role.USER,
        text=text or "",
        had_image=bool(user_images),
        messenger_mid=mid,
    )
    for i, (data, mime) in enumerate(user_images):
        att = ChatUserImage(message=msg, sort_order=i)
        name = f"u{i}{_image_filename_suffix(mime)}"
        att.image.save(name, ContentFile(data), save=True)


def _flush_pending(psid: str, django_request: HttpRequest | None) -> None:
    key = _cache_key(psid)
    with _lock:
        pending = cache.get(key)
        if pending:
            cache.delete(key)
    if not pending:
        return

    items: list[dict[str, Any]] = list(pending.get("items") or [])
    if not items:
        return
    reply_to_mid = pending.get("reply_to_mid") or None

    conversation, _ = Conversation.objects.get_or_create(
        channel=Conversation.Channel.MESSENGER,
        psid=str(psid),
        defaults={"title": ""},
    )
    messenger_client.ensure_messenger_title_for_conversation(conversation)

    # Save every message except the last as a plain bubble (no AI reply).
    for it in items[:-1]:
        imgs = _fetch_images(it.get("image_urls") or [])
        _save_user_message_no_ai(
            conversation,
            text=it.get("text") or "",
            user_images=imgs,
            messenger_mid=it.get("mid") or "",
        )

    # Run AI only on the latest message.
    last = items[-1]
    last_text = last.get("text") or ""
    last_images = _fetch_images(last.get("image_urls") or [])
    last_mid = (last.get("mid") or "").strip()

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
            prefix = "[Customer replied to an earlier message (Messenger ref).]\n\n"

    if not last_text and not last_images:
        return

    run_chat_turn(
        conversation=conversation,
        text=prefix + last_text,
        user_images=last_images,
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


def _items_for_sender(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    """Return one item per inbound message (preserves boundaries) plus the latest reply_to_mid."""
    items: list[dict[str, Any]] = []
    reply_to_mid: str | None = None
    for event in events:
        msg = event.get("message") or {}
        t, iu, mid, rt = _extract_message_parts(msg)
        if not t and not iu:
            continue
        items.append({"text": t, "image_urls": iu, "mid": mid})
        if rt:
            reply_to_mid = rt
    return items, reply_to_mid


def enqueue_messenger_ingest(
    psid: str,
    *,
    items: list[dict[str, Any]],
    reply_to_mid: str | None,
    django_request: HttpRequest | None,
) -> None:
    if not items:
        return
    _merge_pending(psid, items=items, reply_to_mid=reply_to_mid)
    _schedule_flush(psid, django_request)


def process_webhook_payload(payload: dict[str, Any], django_request: HttpRequest | None) -> None:
    """Group events by sender (same POST), preserve per-message boundaries, then debounce."""
    by_sender: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in messenger_client.iter_messaging_events(payload):
        if event.get("message", {}).get("is_echo"):
            continue
        sid = (event.get("sender") or {}).get("id")
        if not sid:
            continue
        by_sender[str(sid)].append(event)

    for psid, events in by_sender.items():
        items, reply_to_mid = _items_for_sender(events)
        if not items:
            continue
        enqueue_messenger_ingest(
            psid,
            items=items,
            reply_to_mid=reply_to_mid,
            django_request=django_request,
        )
