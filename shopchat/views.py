import json
import logging
import uuid

from django.conf import settings as django_settings
from django.db.models import OuterRef, Q, Subquery
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from shopchat.chat_turn import run_chat_turn
from shopchat.forms import (
    AppSettingsForm,
    GeminiApiCredentialForm,
    ProductImageForm,
)
from shopchat.models import (
    CHAT_HISTORY_MAX_MESSAGES,
    GEMINI_GEMMA_CHAT_MODELS,
    AppSettings,
    ChatMessage,
    Conversation,
    GeminiApiCredential,
    ProductImage,
)
from shopchat import messenger_client
from shopchat.messenger_client import (
    ensure_messenger_title_for_conversation,
    verify_signature,
    verify_webhook_get,
)
from shopchat.messenger_pipeline import process_webhook_payload
from shopchat.services import active_key_count, gemini_keys_usage_info, get_app_settings

logger = logging.getLogger("shopchat.views")

CHAT_BATCH_IDLE_MS = 5000


def _messenger_webhook_display_url() -> str:
    explicit = (getattr(django_settings, "MESSENGER_WEBHOOK_PUBLIC_URL", "") or "").strip()
    if explicit:
        return explicit.rstrip("/") + "/"
    base = (getattr(django_settings, "PUBLIC_WEBHOOK_BASE", "") or "").strip().rstrip("/")
    path = getattr(django_settings, "MESSENGER_WEBHOOK_PATH", "/api/webhook/")
    if not path.startswith("/"):
        path = "/" + path
    if not path.endswith("/"):
        path = path + "/"
    if base:
        return base + path
    return ""


MAX_CHAT_IMAGES = 12

# Back-compat: older inbox code used this name. Same cap as INBOX_RECENT_CONVERSATIONS_LIMIT.
INBOX_CONVERSATIONS_PAGE_MAX = getattr(
    django_settings,
    "INBOX_RECENT_CONVERSATIONS_LIMIT",
    80,
)


def _ensure_chat_session(request) -> str:
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def _web_client_keys(request) -> list[str]:
    """UUID / opaque keys from the browser (localStorage), comma-separated in X-Web-Client-Keys."""
    raw = (request.headers.get("X-Web-Client-Keys") or "").strip()
    if not raw:
        return []
    keys: list[str] = []
    for part in raw.split(","):
        k = part.strip()
        if 8 <= len(k) <= 64:
            keys.append(k)
    return keys[:20]


def _owns_web_conversation(request, conv: Conversation) -> bool:
    if conv.channel != Conversation.Channel.WEB_TEST:
        return True
    return conv.web_session_key in _web_client_keys(request)


def _serialize_message(m: ChatMessage, request=None) -> dict:
    user_image_urls: list[str] = []
    if m.role == ChatMessage.Role.USER and m.had_image:
        for att in m.user_attachments.all():
            url = att.image.url
            if request is not None:
                url = request.build_absolute_uri(url)
            user_image_urls.append(url)
    return {
        "id": m.pk,
        "role": m.role,
        "text": m.text,
        "had_image": m.had_image,
        "user_image_urls": user_image_urls,
        "time": m.created_at.isoformat(),
    }


def _preview_for_conversation_row(c: Conversation) -> str:
    """Sidebar preview; uses annotate fields when present to avoid N+1 queries."""
    if hasattr(c, "_last_preview_text"):
        t = c._last_preview_text
        had = getattr(c, "_last_had_image", False)
        if t is not None and str(t).strip():
            return str(t).strip().replace("\n", " ")[:120]
        if had:
            return "(photo)"
        return ""
    last = c.messages.order_by("-created_at").first()
    if not last:
        return ""
    if last.text:
        return last.text.strip().replace("\n", " ")[:120]
    if last.had_image:
        return "(photo)"
    return ""


def _conversation_row(c: Conversation, request) -> dict:
    preview = _preview_for_conversation_row(c)
    can_compose = (
        c.channel == Conversation.Channel.WEB_TEST
        and c.web_session_key in _web_client_keys(request)
    )
    return {
        "id": c.pk,
        "title": c.display_title(),
        "channel": c.channel,
        "channel_label": c.get_channel_display(),
        "can_compose": can_compose,
        "preview": preview,
        "updated_at": c.updated_at.isoformat(),
    }


@require_GET
def inbox_conversations_api(request):
    """Return only the most recently updated threads (see INBOX_RECENT_CONVERSATIONS_LIMIT)."""
    limit = getattr(
        django_settings,
        "INBOX_RECENT_CONVERSATIONS_LIMIT",
        80,
    )
    keys = _web_client_keys(request)
    if keys:
        conv_filter = Q(channel=Conversation.Channel.MESSENGER) | Q(
            channel=Conversation.Channel.WEB_TEST,
            web_session_key__in=keys,
        )
    else:
        conv_filter = Q(channel=Conversation.Channel.MESSENGER)
    last_msg = ChatMessage.objects.filter(conversation_id=OuterRef("pk")).order_by(
        "-created_at"
    )
    qs = (
        Conversation.objects.filter(conv_filter)
        .annotate(
            _last_preview_text=Subquery(last_msg.values("text")[:1]),
            _last_had_image=Subquery(last_msg.values("had_image")[:1]),
        )
        .order_by("-updated_at")[:limit]
    )
    rows = [_conversation_row(c, request) for c in qs]
    return JsonResponse(
        {
            "ok": True,
            "conversations": rows,
            "recent_limit": limit,
        }
    )


@require_GET
def inbox_messages_api(request, pk: int):
    conv = get_object_or_404(Conversation, pk=pk)
    if conv.channel == Conversation.Channel.WEB_TEST:
        if not _owns_web_conversation(request, conv):
            return JsonResponse({"ok": False, "error": "Not your thread."}, status=403)
    if conv.channel == Conversation.Channel.MESSENGER:
        if ensure_messenger_title_for_conversation(conv):
            conv.refresh_from_db()
    qs = conv.messages.order_by("created_at").prefetch_related("user_attachments")
    app = get_app_settings()
    messenger_manual_send = (
        conv.channel == Conversation.Channel.MESSENGER
        and app.deployment_mode == AppSettings.DeploymentMode.TESTING
    )
    return JsonResponse(
        {
            "ok": True,
            "conversation": _conversation_row(conv, request),
            "messenger_manual_send": messenger_manual_send,
            "messages": [_serialize_message(m, request) for m in qs],
        }
    )


@require_http_methods(["POST"])
def inbox_messenger_send_reply(request, pk: int):
    """Testing mode: send one stored assistant reply to the customer on Messenger."""
    conv = get_object_or_404(Conversation, pk=pk)
    if conv.channel != Conversation.Channel.MESSENGER:
        return JsonResponse(
            {"ok": False, "error": "Not a Messenger thread."},
            status=400,
        )
    if not (conv.psid or "").strip():
        return JsonResponse({"ok": False, "error": "Missing PSID."}, status=400)
    app_settings = get_app_settings()
    if app_settings.deployment_mode != AppSettings.DeploymentMode.TESTING:
        return JsonResponse(
            {
                "ok": False,
                "error": "Manual send is only for Testing mode (Production sends automatically).",
            },
            status=400,
        )
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON."}, status=400)
    raw_mid = body.get("message_id")
    try:
        mid = int(raw_mid)
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "message_id required."}, status=400)
    msg = get_object_or_404(ChatMessage, pk=mid, conversation=conv)
    if msg.role != ChatMessage.Role.ASSISTANT:
        return JsonResponse(
            {"ok": False, "error": "Only assistant messages can be sent."},
            status=400,
        )
    text = (msg.text or "").strip()
    if not text:
        return JsonResponse({"ok": False, "error": "Empty message."}, status=400)
    try:
        messenger_client.send_messenger_text(conv.psid, text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Messenger manual send failed")
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)
    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
def inbox_chat_api(request, pk: int):
    conv = get_object_or_404(Conversation, pk=pk)
    if conv.channel != Conversation.Channel.WEB_TEST:
        return JsonResponse(
            {"ok": False, "error": "Only web test threads can be sent from here."},
            status=400,
        )
    if not _owns_web_conversation(request, conv):
        return JsonResponse({"ok": False, "error": "Not your thread."}, status=403)
    try:
        text = (request.POST.get("message") or "").strip()
        user_images: list[tuple[bytes, str]] = []
        for f in request.FILES.getlist("images"):
            raw = f.read()
            if raw:
                user_images.append((raw, f.content_type or "image/jpeg"))
        legacy = request.FILES.get("image")
        if legacy and not user_images:
            b = legacy.read()
            if b:
                user_images.append((b, legacy.content_type or "image/jpeg"))
        if len(user_images) > MAX_CHAT_IMAGES:
            return JsonResponse(
                {
                    "ok": False,
                    "error": f"At most {MAX_CHAT_IMAGES} images per message.",
                },
                status=400,
            )
        if not text and not user_images:
            return JsonResponse(
                {"ok": False, "error": "Send a message and/or an image."},
                status=400,
            )
        reply = run_chat_turn(
            conversation=conv,
            text=text,
            user_images=user_images,
            request=request,
            messenger_mid="",
            allow_messenger_outbound=False,
        )
        qs = conv.messages.order_by("created_at").prefetch_related("user_attachments")
        return JsonResponse(
            {
                "ok": True,
                "reply": reply,
                "messages": [_serialize_message(m, request) for m in qs],
            }
        )
    except ValueError as exc:
        logger.warning("inbox_chat validation: %s", exc)
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        logger.exception("inbox_chat failed")
        return JsonResponse(
            {"ok": False, "error": f"Request failed: {exc}"},
            status=500,
        )


@require_GET
def inbox_web_bootstrap(request):
    """Expose legacy session-keyed web conversation so localStorage can adopt the same key."""
    sk = _ensure_chat_session(request)
    conv = Conversation.objects.filter(
        channel=Conversation.Channel.WEB_TEST,
        web_session_key=sk,
    ).first()
    if not conv:
        return JsonResponse({"ok": True, "migrated": False})
    return JsonResponse(
        {
            "ok": True,
            "migrated": True,
            "id": conv.pk,
            "web_key": conv.web_session_key,
        }
    )


@require_http_methods(["POST"])
def inbox_new_web_conversation(request):
    """Create a new web test thread; browser must store web_key and send it in X-Web-Client-Keys."""
    key = str(uuid.uuid4())
    conv = Conversation.objects.create(
        channel=Conversation.Channel.WEB_TEST,
        web_session_key=key,
        title="Web test",
    )
    return JsonResponse({"ok": True, "id": conv.pk, "web_key": key})


@require_GET
def chat_history_api(request):
    keys = _web_client_keys(request)
    if not keys:
        return JsonResponse(
            {
                "ok": True,
                "max_messages": CHAT_HISTORY_MAX_MESSAGES,
                "messages": [],
            }
        )
    conv = Conversation.objects.filter(
        channel=Conversation.Channel.WEB_TEST,
        web_session_key=keys[0],
    ).first()
    if not conv:
        return JsonResponse(
            {
                "ok": True,
                "max_messages": CHAT_HISTORY_MAX_MESSAGES,
                "messages": [],
            }
        )
    qs = conv.messages.order_by("created_at").prefetch_related("user_attachments")
    return JsonResponse(
        {
            "ok": True,
            "max_messages": CHAT_HISTORY_MAX_MESSAGES,
            "messages": [_serialize_message(m, request) for m in qs],
        }
    )


@require_http_methods(["POST"])
def chat_api(request):
    keys = _web_client_keys(request)
    if not keys:
        return JsonResponse(
            {"ok": False, "error": "Send X-Web-Client-Keys (see inbox web test flow)."},
            status=400,
        )
    conv = Conversation.objects.filter(
        channel=Conversation.Channel.WEB_TEST,
        web_session_key=keys[0],
    ).first()
    if not conv:
        return JsonResponse(
            {"ok": False, "error": "No web test conversation for this key."},
            status=400,
        )
    try:
        text = (request.POST.get("message") or "").strip()
        user_images: list[tuple[bytes, str]] = []
        for f in request.FILES.getlist("images"):
            raw = f.read()
            if raw:
                user_images.append((raw, f.content_type or "image/jpeg"))
        legacy = request.FILES.get("image")
        if legacy and not user_images:
            b = legacy.read()
            if b:
                user_images.append((b, legacy.content_type or "image/jpeg"))
        if len(user_images) > MAX_CHAT_IMAGES:
            return JsonResponse(
                {
                    "ok": False,
                    "error": f"At most {MAX_CHAT_IMAGES} images per message.",
                },
                status=400,
            )
        if not text and not user_images:
            return JsonResponse(
                {"ok": False, "error": "Send a message and/or an image."},
                status=400,
            )
        reply = run_chat_turn(
            conversation=conv,
            text=text,
            user_images=user_images,
            request=request,
            messenger_mid="",
            allow_messenger_outbound=False,
        )
        qs = conv.messages.order_by("created_at").prefetch_related("user_attachments")
        return JsonResponse(
            {
                "ok": True,
                "reply": reply,
                "messages": [_serialize_message(m, request) for m in qs],
            }
        )
    except ValueError as exc:
        logger.warning("chat_api validation: %s", exc)
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        logger.exception("chat_api failed")
        return JsonResponse(
            {"ok": False, "error": f"Request failed: {exc}"},
            status=500,
        )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def messenger_webhook(request):
    if request.method == "GET":
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge")
        out = verify_webhook_get(mode, token, challenge)
        if out is None:
            return HttpResponse("Forbidden", status=403)
        return HttpResponse(out, content_type="text/plain")

    raw = request.body
    sig = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(raw, sig):
        logger.warning("Messenger webhook: bad signature")
        return HttpResponse("Forbidden", status=403)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponse("Bad Request", status=400)
    try:
        process_webhook_payload(payload, request)
    except Exception:
        logger.exception("Messenger webhook handler error")
    return JsonResponse({"ok": True})


@require_GET
def inbox_page(request):
    get_app_settings()
    return render(
        request,
        "shopchat/inbox.html",
        {
            "batch_idle_ms": CHAT_BATCH_IDLE_MS,
            "batch_idle_sec": CHAT_BATCH_IDLE_MS // 1000,
            "messenger_webhook_url": _messenger_webhook_display_url(),
            "inbox_recent_limit": getattr(
                django_settings,
                "INBOX_RECENT_CONVERSATIONS_LIMIT",
                80,
            ),
            "inbox_poll_ms": getattr(django_settings, "INBOX_POLL_MS", 2500),
        },
    )


@require_http_methods(["GET", "POST"])
def settings_page(request):
    settings_obj = get_app_settings()
    form = AppSettingsForm(instance=settings_obj)
    pform = ProductImageForm()
    cform = GeminiApiCredentialForm()

    if request.method == "POST":
        if "save_settings" in request.POST:
            form = AppSettingsForm(request.POST, instance=settings_obj)
            if form.is_valid():
                form.save()
                return redirect("settings")
        elif "add_credential" in request.POST:
            cform = GeminiApiCredentialForm(request.POST)
            if cform.is_valid():
                cform.save()
                return redirect("settings")
        elif "delete_credential" in request.POST:
            cid = request.POST.get("credential_id")
            GeminiApiCredential.objects.filter(pk=cid).delete()
            return redirect("settings")
        elif "add_product" in request.POST:
            pform = ProductImageForm(request.POST, request.FILES)
            if pform.is_valid():
                pform.save()
                return redirect("settings")
        elif "delete_product" in request.POST:
            pid = request.POST.get("product_id")
            ProductImage.objects.filter(pk=pid).delete()
            return redirect("settings")

    credentials = GeminiApiCredential.objects.all()[:100]
    n_keys = active_key_count()
    gemini_usage = gemini_keys_usage_info()
    slots = n_keys * len(GEMINI_GEMMA_CHAT_MODELS)
    products = ProductImage.objects.all()[:200]
    return render(
        request,
        "shopchat/settings.html",
        {
            "form": form,
            "pform": pform,
            "cform": cform,
            "credentials": credentials,
            "n_keys": n_keys,
            "gemini_usage": gemini_usage,
            "gemma_models": GEMINI_GEMMA_CHAT_MODELS,
            "chat_slots": slots,
            "products": products,
            "messenger_webhook_url": _messenger_webhook_display_url(),
        },
    )
