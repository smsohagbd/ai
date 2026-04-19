"""Meta Messenger Platform: verify webhook, parse payloads, send messages."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger("shopchat.messenger")


def _graph_api_base() -> str:
    ver = (getattr(settings, "MESSENGER_GRAPH_API_VERSION", None) or "v21.0").strip()
    if not ver.startswith("v"):
        ver = f"v{ver}"
    return f"https://graph.facebook.com/{ver}"


def _messenger_graph_params(extra: dict[str, str] | None = None) -> dict[str, str]:
    """access_token plus appsecret_proof when MESSENGER_APP_SECRET is set (Meta server-side best practice)."""
    token = (getattr(settings, "MESSENGER_PAGE_ACCESS_TOKEN", "") or "").strip()
    params: dict[str, str] = dict(extra or {})
    params["access_token"] = token
    secret = (getattr(settings, "MESSENGER_APP_SECRET", "") or "").strip()
    if secret and token:
        params["appsecret_proof"] = hmac.new(
            secret.encode("utf-8"),
            token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    return params


def verify_webhook_get(mode: str | None, token: str | None, challenge: str | None) -> str | None:
    """Return hub.challenge string if verify token matches, else None."""
    from shopchat.services import get_app_settings

    app = get_app_settings()
    expected = (app.messenger_verify_token or "").strip()
    if not expected:
        expected = (getattr(settings, "MESSENGER_VERIFY_TOKEN", "") or "").strip()
    if mode == "subscribe" and token and expected and token == expected and challenge:
        return challenge
    return None


def verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    secret = getattr(settings, "MESSENGER_APP_SECRET", "") or ""
    if not secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    digest = signature_header[7:]
    mac = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, digest)


def split_message_chunks(text: str, max_len: int = 1900) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    rest = text
    while rest:
        chunks.append(rest[:max_len])
        rest = rest[max_len:]
    return chunks


def send_messenger_text(psid: str, text: str) -> None:
    token = getattr(settings, "MESSENGER_PAGE_ACCESS_TOKEN", "") or ""
    if not token:
        raise ValueError("MESSENGER_PAGE_ACCESS_TOKEN is not set.")
    url = f"{_graph_api_base()}/me/messages"
    for chunk in split_message_chunks(text):
        payload = {
            "recipient": {"id": psid},
            "messaging_type": "RESPONSE",
            "message": {"text": chunk},
        }
        r = requests.post(url, params=_messenger_graph_params(), json=payload, timeout=60)
        if r.status_code >= 400:
            logger.error("Messenger send failed %s: %s", r.status_code, r.text[:500])
            r.raise_for_status()
        logger.info("Messenger send ok to …%s", psid[-6:])


def fetch_messenger_user_profile(psid: str) -> dict[str, Any] | None:
    """
    Page-scoped user profile for someone who has messaged the Page.
    Uses Messenger-supported fields only (not composite ``name``), Page access token,
    and optional appsecret_proof when MESSENGER_APP_SECRET is set.
    """
    token = (getattr(settings, "MESSENGER_PAGE_ACCESS_TOKEN", "") or "").strip()
    if not token or not psid:
        return None
    url = f"{_graph_api_base()}/{psid}"
    r = requests.get(
        url,
        params=_messenger_graph_params(
            {
                # User Profile API: first_name, last_name, profile_pic, … — ``name`` is not supported here.
                "fields": "first_name,last_name",
            }
        ),
        timeout=30,
    )
    if r.status_code != 200:
        body = (r.text or "")[:600]
        hint = (
            "Verify Page token for THIS Page: developers.facebook.com/tools/debug/accesstoken/ — "
            "Live traffic needs pages_messaging (+ profile) Advanced Access. "
            "https://developers.facebook.com/docs/messenger-platform/identity/user-profile/"
        )
        try:
            j = r.json()
            err = (j or {}).get("error") or {}
            if err.get("code") == 100 and err.get("error_subcode") == 33:
                hint = (
                    "Meta error 100 + error_subcode 33: this PSID cannot be read as a Graph node with your "
                    "current token/permissions. Fix in Meta (not in app code): generate a fresh "
                    "**Page** access token for the **same Page** that receives these messages; in App "
                    "Dashboard → App Review / Permissions, ensure **pages_messaging** (and any required "
                    "profile access) has **Advanced** access for production. Standard-only apps often "
                    "get 100/33 for real users. Token Debugger must show type=PAGE and your Page id."
                )
        except (ValueError, TypeError, AttributeError):
            pass
        logger.warning(
            "Graph user profile failed psid=…%s status=%s. %s body=%s",
            psid[-8:],
            r.status_code,
            hint,
            body,
        )
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("error"):
        err = data.get("error")
        logger.warning(
            "Graph user profile error psid=…%s: %s",
            psid[-8:],
            err,
        )
        return None
    return data


def ensure_messenger_title_for_conversation(conversation) -> bool:
    """
    If this is a Messenger thread with no title, fetch name from Graph and save.
    Returns True if title was updated.
    """
    from shopchat.models import Conversation

    if conversation.channel != Conversation.Channel.MESSENGER:
        return False
    psid = (conversation.psid or "").strip()
    if not psid or (conversation.title or "").strip():
        return False
    data = fetch_messenger_user_profile(psid)
    if not data:
        return False
    first = (data.get("first_name") or "").strip()
    last = (data.get("last_name") or "").strip()
    name = f"{first} {last}".strip()
    if not name:
        logger.warning(
            "Messenger Graph profile returned no name psid=…%s fields=%s",
            psid[-8:],
            {k: data.get(k) for k in ("first_name", "last_name")},
        )
        return False
    Conversation.objects.filter(pk=conversation.pk).update(title=name[:255])
    conversation.title = name[:255]
    logger.info("Messenger display name set psid=…%s name=%s", psid[-8:], name[:40])
    return True


def fetch_url_bytes(url: str) -> tuple[bytes, str]:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip() or "image/jpeg"
    return r.content, mime


def iter_messaging_events(payload: dict[str, Any]):
    if payload.get("object") != "page":
        return
    for entry in payload.get("entry") or []:
        for event in entry.get("messaging") or []:
            yield event
