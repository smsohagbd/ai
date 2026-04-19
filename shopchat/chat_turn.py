"""Single place: prior → Gemini → save messages → prune → optional Messenger send."""

from __future__ import annotations

import logging

from django.core.files.base import ContentFile
from django.http import HttpRequest

from shopchat import messenger_client
from shopchat.models import AppSettings, ChatMessage, ChatUserImage, Conversation
from shopchat.services import generate_chat_reply, get_app_settings, prune_conversation

logger = logging.getLogger("shopchat.chat_turn")


def _image_filename_suffix(mime: str) -> str:
    base = (mime or "").split(";")[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(base, ".jpg")


def run_chat_turn(
    *,
    conversation: Conversation,
    text: str,
    user_images: list[tuple[bytes, str]],
    request: HttpRequest | None,
    messenger_mid: str = "",
    allow_messenger_outbound: bool = False,
) -> str | None:
    """
    Returns assistant reply text, or None if skipped (duplicate Messenger mid).
    """
    if messenger_mid and ChatMessage.objects.filter(
        conversation=conversation, messenger_mid=messenger_mid
    ).exists():
        logger.info("Skip duplicate messenger mid=%s", messenger_mid[:24])
        return None

    app_settings = get_app_settings()
    prior = list(conversation.messages.order_by("created_at"))
    reply = generate_chat_reply(
        user_text=text,
        user_images=user_images,
        settings=app_settings,
        prior_messages=prior,
        request=request,
    )

    user_msg = ChatMessage.objects.create(
        conversation=conversation,
        role=ChatMessage.Role.USER,
        text=text,
        had_image=bool(user_images),
        messenger_mid=messenger_mid or "",
    )
    for i, (data, mime) in enumerate(user_images):
        att = ChatUserImage(message=user_msg, sort_order=i)
        name = f"u{i}{_image_filename_suffix(mime)}"
        att.image.save(name, ContentFile(data), save=True)
    ChatMessage.objects.create(
        conversation=conversation,
        role=ChatMessage.Role.ASSISTANT,
        text=reply,
        had_image=False,
        messenger_mid="",
    )
    prune_conversation(conversation.pk)

    if (
        allow_messenger_outbound
        and conversation.channel == Conversation.Channel.MESSENGER
        and app_settings.deployment_mode == AppSettings.DeploymentMode.PRODUCTION
    ):
        try:
            messenger_client.send_messenger_text(conversation.psid, reply)
        except Exception:
            logger.exception("Messenger send failed")

    return reply
