"""Inbound Messenger → AI pipeline."""

from __future__ import annotations

import logging
from typing import Any

from django.http import HttpRequest

from shopchat.chat_turn import run_chat_turn
from shopchat import messenger_client
from shopchat.models import Conversation

logger = logging.getLogger("shopchat.messenger_pipeline")


def handle_messaging_event(event: dict[str, Any], django_request: HttpRequest | None) -> None:
    if event.get("message", {}).get("is_echo"):
        return
    sender_id = (event.get("sender") or {}).get("id")
    if not sender_id:
        return
    msg = event.get("message") or {}
    mid = msg.get("mid") or ""
    text = (msg.get("text") or "").strip()
    image_urls: list[str] = []
    for att in msg.get("attachments") or []:
        if att.get("type") == "image":
            payload = att.get("payload") or {}
            u = payload.get("url")
            if u:
                image_urls.append(u)

    user_images: list[tuple[bytes, str]] = []
    for u in image_urls:
        try:
            data, mime = messenger_client.fetch_url_bytes(u)
            if data:
                user_images.append((data, mime))
        except Exception:
            logger.exception("Failed to fetch Messenger image %s", u[:80])

    if not text and not user_images:
        return

    conversation, _ = Conversation.objects.get_or_create(
        channel=Conversation.Channel.MESSENGER,
        psid=str(sender_id),
        defaults={"title": ""},
    )
    messenger_client.ensure_messenger_title_for_conversation(conversation)

    run_chat_turn(
        conversation=conversation,
        text=text,
        user_images=user_images,
        request=django_request,
        messenger_mid=mid,
        allow_messenger_outbound=True,
    )


def process_webhook_payload(payload: dict[str, Any], django_request: HttpRequest | None) -> None:
    for event in messenger_client.iter_messaging_events(payload):
        try:
            handle_messaging_event(event, django_request)
        except Exception:
            logger.exception("messaging event failed")
