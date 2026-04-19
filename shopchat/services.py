from __future__ import annotations

import json
import logging
import mimetypes
import os
from typing import TYPE_CHECKING

import numpy as np
from django.db import transaction
from django.http import HttpRequest
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

if TYPE_CHECKING:
    from shopchat.models import AppSettings, ProductImage

from shopchat.gemini_rate_limit import (
    wait_gemini_chat_model_slot,
    wait_gemini_embedding_slot,
)
from shopchat.models import (
    CHAT_HISTORY_MAX_MESSAGES,
    GEMINI_EMBEDDING_MODEL_ID,
    GEMINI_GEMMA_CHAT_MODELS,
    ChatMessage,
)

logger = logging.getLogger("shopchat.services")

# Always appended so the model uses real catalog URLs, not invented links.
SYSTEM_INSTRUCTION_CATALOG_URL_RULES = (
    "\n\nCatalog images: When the customer asks for product photos, pictures, or "
    "\"chobi\", you MUST only use the exact full `image_url` values from the retrieval "
    "block in the current user turn—copy them character-for-character, including the "
    "file extension (.jpg, .jpeg, .png, etc.) and without truncating. "
    "Use valid Markdown only: ![short label](full_url) with a closing `)` after the URL. "
    "Never invent URLs and never use bit.ly, imgur, or other external image links unless "
    "that exact URL appears in the retrieval block. "
    "If there is no suitable row in the retrieval block, say you do not have that "
    "photo in the catalog."
)


def _api_key_hint(key: str) -> str:
    k = (key or "").strip()
    if len(k) <= 8:
        return "(key too short to hint)"
    return f"…{k[-4:]}"


def _gemini_try_next_api_key(exc: BaseException) -> bool:
    """True if another API key might succeed (quota, denied project, bad key, etc.)."""
    if isinstance(exc, genai_errors.ClientError):
        return exc.code in (401, 403, 429)
    return False


def _gemini_error_blob(exc: BaseException) -> str:
    parts: list[str] = [str(exc)]
    if isinstance(exc, genai_errors.ClientError):
        if exc.message:
            parts.append(str(exc.message))
        if exc.status:
            parts.append(str(exc.status))
        if exc.details is not None:
            try:
                parts.append(
                    json.dumps(exc.details)
                    if not isinstance(exc.details, str)
                    else exc.details
                )
            except (TypeError, ValueError):
                parts.append(str(exc.details))
    return " ".join(parts).lower()


def _gemini_error_triggers_credential_disable(exc: BaseException) -> bool:
    """
    Permanent-ish key/project problems: auto-disable this credential so rotation skips it.
    """
    if not isinstance(exc, genai_errors.ClientError):
        return False
    code = exc.code
    blob = _gemini_error_blob(exc)
    project_access_markers = (
        "quota tier unavailable",
        "contact your project administrator",
        "your project has been denied access",
        "please contact support",
        "billing has not been enabled",
    )
    key_markers = (
        "api key not valid",
        "invalid api key",
    )
    if code == 401:
        return True
    if code == 403:
        return True
    if code == 400:
        return any(m in blob for m in project_access_markers + key_markers)
    # 429 is usually RPM/RPD limits — recovers after a wait or next day. Do not match
    # generic "billing" / "resource exhausted" (Google often suggests billing on any429).
    if code == 429:
        return any(m in blob for m in project_access_markers)
    if 400 <= code < 500:
        return any(m in blob for m in project_access_markers + key_markers)
    return False


def _maybe_auto_disable_gemini_credential(
    credential_id: int | None,
    exc: BaseException,
) -> None:
    if credential_id is None:
        return
    if not _gemini_error_triggers_credential_disable(exc):
        return
    from django.utils import timezone

    from shopchat.models import GeminiApiCredential

    reason = str(exc)[:8000]
    n = GeminiApiCredential.objects.filter(pk=credential_id, enabled=True).update(
        enabled=False,
        auto_disabled_at=timezone.now(),
        auto_disable_reason=reason,
    )
    if n:
        logger.error(
            "Gemini credential id=%s auto-disabled after API error: %s",
            credential_id,
            exc,
        )


def _credential_key_slots() -> list[tuple[str, int | None]]:
    """(api_key, credential_pk). credential_pk is None when using GEMINI_API_KEY env only."""
    from shopchat.models import GeminiApiCredential

    qs = GeminiApiCredential.objects.filter(enabled=True).order_by("sort_order", "id")
    slots: list[tuple[str, int | None]] = []
    for c in qs:
        k = (c.api_key or "").strip()
        if k:
            slots.append((k, c.pk))
    if slots:
        logger.debug("Active API keys from DB: %s", len(slots))
        return slots
    env_k = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_k:
        logger.debug("Using GEMINI_API_KEY from environment (no DB credentials).")
        return [(env_k, None)]
    return []


def _require_credential_slots() -> list[tuple[str, int | None]]:
    slots = _credential_key_slots()
    if not slots:
        raise ValueError(
            "No Gemini API keys configured. Add one under Settings (Gemini API keys) "
            "or set GEMINI_API_KEY in your environment."
        )
    return slots


def active_key_count() -> int:
    """Keys that will be used (DB credentials or env fallback)."""
    return len(_credential_key_slots())


def gemini_keys_usage_info() -> dict:
    """
    Report which key source the app uses (for Settings UI).
    If any non-empty enabled DB credential exists, .env GEMINI_API_KEY is ignored.
    """
    from shopchat.models import GeminiApiCredential

    db_keys = [
        c
        for c in GeminiApiCredential.objects.filter(enabled=True).order_by(
            "sort_order", "id"
        )
        if (c.api_key or "").strip()
    ]
    n_db = len(db_keys)
    env_set = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    if n_db:
        return {
            "source": "database",
            "count": n_db,
            "env_present_but_ignored": env_set,
        }
    if env_set:
        return {
            "source": "environment",
            "count": 1,
            "env_present_but_ignored": False,
        }
    return {
        "source": "none",
        "count": 0,
        "env_present_but_ignored": False,
    }


def _consume_embed_rr_start() -> tuple[list[tuple[str, int | None]], int]:
    """Advance embed round-robin; return (ordered slots, first index for this turn)."""
    from shopchat.models import AppSettings

    slots = _require_credential_slots()
    with transaction.atomic():
        s = AppSettings.objects.select_for_update().get(pk=1)
        start = s.embed_rr_seq % len(slots)
        s.embed_rr_seq += 1
        s.save(update_fields=["embed_rr_seq"])
    return slots, start


def pick_embed_api_key() -> tuple[str, int | None]:
    slots, start = _consume_embed_rr_start()
    chosen, cred_id = slots[start]
    logger.info(
        "embed: picked key %s (index %s/%s), model %s",
        _api_key_hint(chosen),
        start,
        len(slots),
        GEMINI_EMBEDDING_MODEL_ID,
    )
    return chosen, cred_id


def _consume_chat_rr_start() -> tuple[list[tuple[str, str, int | None]], int]:
    from shopchat.models import AppSettings

    cred_slots = _require_credential_slots()
    slots: list[tuple[str, str, int | None]] = []
    for key, cred_id in cred_slots:
        for model_id in GEMINI_GEMMA_CHAT_MODELS:
            slots.append((key, model_id, cred_id))
    with transaction.atomic():
        s = AppSettings.objects.select_for_update().get(pk=1)
        start = s.chat_rr_seq % len(slots)
        s.chat_rr_seq += 1
        s.save(update_fields=["chat_rr_seq"])
    return slots, start


def pick_chat_api_key_and_model() -> tuple[str, str, int | None]:
    slots, start = _consume_chat_rr_start()
    return slots[start]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    na = np.linalg.norm(va)
    nb = np.linalg.norm(vb)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def embed_image_bytes(
    client: genai.Client,
    *,
    data: bytes,
    mime_type: str,
    settings: AppSettings,
    credential_id: int | None = None,
) -> list[float]:
    part = types.Part.from_bytes(data=data, mime_type=mime_type)
    cfg = types.EmbedContentConfig(
        output_dimensionality=settings.embedding_output_dimensionality,
    )
    wait_gemini_embedding_slot()
    resp = client.models.embed_content(
        model=GEMINI_EMBEDDING_MODEL_ID,
        contents=[part],
        config=cfg,
    )
    if not resp.embeddings or not resp.embeddings[0].values:
        raise RuntimeError("Embedding API returned no vector.")
    vec = list(resp.embeddings[0].values)
    logger.info(
        "embed_content ok: model=%s dim=%s mime=%s bytes=%s",
        GEMINI_EMBEDDING_MODEL_ID,
        len(vec),
        mime_type,
        len(data),
    )
    from shopchat.usage_stats import record_gemini_usage

    record_gemini_usage(credential_id, embed_delta=1)
    return vec


def embed_text_query(
    client: genai.Client,
    *,
    text: str,
    settings: AppSettings,
    credential_id: int | None = None,
) -> list[float]:
    q = (text or "").strip()
    if not q:
        raise ValueError("Empty text for embedding.")
    cfg = types.EmbedContentConfig(
        output_dimensionality=settings.embedding_output_dimensionality,
    )
    wait_gemini_embedding_slot()
    resp = client.models.embed_content(
        model=GEMINI_EMBEDDING_MODEL_ID,
        contents=[q],
        config=cfg,
    )
    if not resp.embeddings or not resp.embeddings[0].values:
        raise RuntimeError("Embedding API returned no vector for text.")
    vec = list(resp.embeddings[0].values)
    logger.info(
        "embed_content ok (text): model=%s dim=%s chars=%s",
        GEMINI_EMBEDDING_MODEL_ID,
        len(vec),
        len(q),
    )
    from shopchat.usage_stats import record_gemini_usage

    record_gemini_usage(credential_id, embed_delta=1)
    return vec


def embed_text_query_resilient(
    settings: AppSettings,
    *,
    text: str,
) -> list[float]:
    """Try each API key (starting at round-robin) until embed succeeds or errors exhaust."""
    slots, start = _consume_embed_rr_start()
    n = len(slots)
    for off in range(n):
        idx = (start + off) % n
        key, cred_id = slots[idx]
        if off == 0:
            logger.info(
                "embed: picked key %s (index %s/%s), model %s",
                _api_key_hint(key),
                idx,
                n,
                GEMINI_EMBEDDING_MODEL_ID,
            )
        client = genai.Client(api_key=key)
        try:
            return embed_text_query(
                client,
                text=text,
                settings=settings,
                credential_id=cred_id,
            )
        except BaseException as exc:
            _maybe_auto_disable_gemini_credential(cred_id, exc)
            if off + 1 < n and _gemini_try_next_api_key(exc):
                logger.warning(
                    "embed_text_query failed for key %s, trying next key (%s/%s): %s",
                    _api_key_hint(key),
                    off + 1,
                    n,
                    exc,
                )
                continue
            raise


def embed_image_bytes_resilient(
    settings: AppSettings,
    *,
    data: bytes,
    mime_type: str,
) -> list[float]:
    slots, start = _consume_embed_rr_start()
    n = len(slots)
    for off in range(n):
        idx = (start + off) % n
        key, cred_id = slots[idx]
        if off == 0:
            logger.info(
                "embed: picked key %s (index %s/%s), model %s",
                _api_key_hint(key),
                idx,
                n,
                GEMINI_EMBEDDING_MODEL_ID,
            )
        client = genai.Client(api_key=key)
        try:
            return embed_image_bytes(
                client,
                data=data,
                mime_type=mime_type,
                settings=settings,
                credential_id=cred_id,
            )
        except BaseException as exc:
            _maybe_auto_disable_gemini_credential(cred_id, exc)
            if off + 1 < n and _gemini_try_next_api_key(exc):
                logger.warning(
                    "embed_image_bytes failed for key %s, trying next key (%s/%s): %s",
                    _api_key_hint(key),
                    off + 1,
                    n,
                    exc,
                )
                continue
            raise


def guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "image/jpeg"


def embed_product_image(product_id: int) -> None:
    from shopchat.models import AppSettings, ProductImage

    settings = get_app_settings()
    try:
        p = ProductImage.objects.get(pk=product_id)
    except ProductImage.DoesNotExist:
        return
    if not p.image:
        ProductImage.objects.filter(pk=product_id).update(
            embedding=None,
            embedding_error="No image file.",
        )
        return
    path = p.image.path
    try:
        logger.info("product_image embed start id=%s path=%s", product_id, path)
        with open(path, "rb") as f:
            data = f.read()
        vec = embed_image_bytes_resilient(
            settings,
            data=data,
            mime_type=guess_mime(path),
        )
        ProductImage.objects.filter(pk=product_id).update(
            embedding=vec,
            embedding_error="",
        )
        logger.info("product_image embed done id=%s dim=%s", product_id, len(vec))
    except Exception as exc:  # noqa: BLE001 — surface API errors in admin/UI
        logger.exception("product_image embed failed id=%s", product_id)
        ProductImage.objects.filter(pk=product_id).update(
            embedding=None,
            embedding_error=str(exc)[:2000],
        )


def get_app_settings() -> AppSettings:
    from shopchat.models import AppSettings

    s, _ = AppSettings.objects.get_or_create(
        pk=1,
        defaults={},
    )
    return s


def merge_top_product_matches(
    per_query_matches: list[list[tuple[ProductImage, float]]],
    *,
    top_k: int,
) -> list[tuple[ProductImage, float]]:
    """Keep best score per product across several similarity lists (e.g. multiple photos)."""
    best: dict[int, tuple[ProductImage, float]] = {}
    for ml in per_query_matches:
        for p, score in ml:
            pid = p.pk
            prev = best.get(pid)
            if prev is None or score > prev[1]:
                best[pid] = (p, score)
    merged = sorted(best.values(), key=lambda x: x[1], reverse=True)
    out = merged[:top_k]
    if out:
        logger.info(
            "merged similarity: %s unique products, top=%.4f (from %s queries, top_k=%s)",
            len(best),
            out[0][1],
            len(per_query_matches),
            top_k,
        )
    return out


def find_similar_products(
    query_vector: list[float], *, top_k: int
) -> list[tuple[ProductImage, float]]:
    from shopchat.models import ProductImage

    scored: list[tuple[ProductImage, float]] = []
    qs = ProductImage.objects.exclude(embedding__isnull=True).iterator()
    for p in qs:
        if not p.embedding:
            continue
        scored.append((p, cosine_similarity(query_vector, p.embedding)))
    scored.sort(key=lambda x: x[1], reverse=True)
    out = scored[:top_k]
    if out:
        logger.info(
            "similarity: %s catalog vectors, top score=%.4f (top_k=%s)",
            len(scored),
            out[0][1],
            top_k,
        )
    else:
        logger.info("similarity: no scored products (top_k=%s)", top_k)
    return out


def build_retrieval_context(
    matches: list[tuple[ProductImage, float]],
    *,
    source: str,
    request: HttpRequest | None = None,
) -> str:
    lines = []
    for i, (p, score) in enumerate(matches, start=1):
        if p.image:
            rel = p.image.url
            if request is not None:
                url = request.build_absolute_uri(rel)
            else:
                url = rel
        else:
            url = ""
        note = (p.notes or "").strip()
        name = p.name or f"product_id={p.pk}"
        lines.append(
            f"{i}. name={name!r} similarity={score:.4f} image_url={url!r} notes={note!r}"
        )
    if not lines:
        return "No indexed product images matched (empty catalog or no embeddings yet)."
    header = {
        "image": "Retrieved product images (visual similarity — user sent a photo):",
        "multi_image": "Retrieved product images (visual similarity — user sent multiple photos):",
        "text": "Retrieved product images (multimodal text-to-image similarity on the user message):",
    }.get(source, "Retrieved product images:")
    return header + "\n" + "\n".join(lines)


def _history_text_for_gemini(msg: ChatMessage) -> str:
    t = (msg.text or "").strip()
    if msg.role == ChatMessage.Role.USER and msg.had_image:
        if t:
            return f"{t}\n\n[User attached one or more images in this message.]"
        return "[User sent one or more images (no text).]"
    if msg.role == ChatMessage.Role.USER and not t:
        return "(empty message)"
    return t or " "


def _prior_messages_for_turn(
    prior_db: list[ChatMessage],
) -> list[types.Content]:
    """Turn stored rows into Gemini contents (roles user / model)."""
    out: list[types.Content] = []
    for msg in prior_db:
        text = _history_text_for_gemini(msg)
        if msg.role == ChatMessage.Role.USER:
            out.append(
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=text)],
                )
            )
        else:
            out.append(
                types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=text)],
                )
            )
    return out


def generate_chat_reply(
    *,
    user_text: str,
    user_images: list[tuple[bytes, str]],
    settings: AppSettings,
    prior_messages: list[ChatMessage],
    request: HttpRequest | None = None,
) -> str:
    n_img = len(user_images)
    logger.info(
        "chat_reply start: text_chars=%s n_images=%s prior_msgs=%s",
        len(user_text or ""),
        n_img,
        len(prior_messages),
    )
    retrieval = ""
    if user_images:
        match_lists: list[list[tuple[ProductImage, float]]] = []
        for data, mime in user_images:
            qvec = embed_image_bytes_resilient(
                settings,
                data=data,
                mime_type=mime,
            )
            match_lists.append(
                find_similar_products(qvec, top_k=settings.similarity_top_k)
            )
        merged = merge_top_product_matches(
            match_lists, top_k=settings.similarity_top_k
        )
        src = "multi_image" if n_img > 1 else "image"
        retrieval = build_retrieval_context(
            merged, source=src, request=request
        )
        logger.debug("retrieval block length=%s (%s images)", len(retrieval), n_img)
    elif user_text.strip():
        qvec = embed_text_query_resilient(
            settings,
            text=user_text,
        )
        matches = find_similar_products(
            qvec, top_k=settings.similarity_top_k
        )
        retrieval = build_retrieval_context(
            matches, source="text", request=request
        )
        logger.debug("retrieval block length=%s (text query)", len(retrieval))

    if user_text.strip():
        text_block = f"User message:\n{user_text.strip()}"
    else:
        text_block = (
            "User message: (no text; image only)"
            if n_img == 1
            else f"User message: (no text; {n_img} images)"
        )

    if retrieval:
        text_block += (
            "\n\n---\nClosest products from your catalog for this turn "
            "(use their image_url values only when sharing photos):\n"
            f"{retrieval}\n---"
        )

    current_parts: list[types.Part] = [types.Part.from_text(text=text_block)]
    for data, mime in user_images:
        current_parts.append(
            types.Part.from_bytes(data=data, mime_type=mime)
        )

    max_prior = CHAT_HISTORY_MAX_MESSAGES - 1
    trimmed = prior_messages[-max_prior:] if len(prior_messages) > max_prior else prior_messages

    contents = _prior_messages_for_turn(trimmed) + [
        types.Content(role="user", parts=current_parts),
    ]

    chat_slots, chat_start = _consume_chat_rr_start()
    n_chat = len(chat_slots)
    system_instruction = (
        (settings.system_prompt or "").strip() + SYSTEM_INSTRUCTION_CATALOG_URL_RULES
    )
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
    )
    from shopchat.usage_stats import record_gemini_usage

    for off in range(n_chat):
        idx = (chat_start + off) % n_chat
        chat_key, chat_model, chat_cred = chat_slots[idx]
        chat_client = genai.Client(api_key=chat_key)
        logger.info(
            "generate_content: model=%s contents_turns=%s (key %s)",
            chat_model,
            len(contents),
            _api_key_hint(chat_key),
        )
        wait_gemini_chat_model_slot(chat_model)
        try:
            resp = chat_client.models.generate_content(
                model=chat_model,
                contents=contents,
                config=config,
            )
        except BaseException as exc:
            _maybe_auto_disable_gemini_credential(chat_cred, exc)
            if off + 1 < n_chat and _gemini_try_next_api_key(exc):
                logger.warning(
                    "generate_content failed for key %s model %s, trying next slot (%s/%s): %s",
                    _api_key_hint(chat_key),
                    chat_model,
                    off + 1,
                    n_chat,
                    exc,
                )
                continue
            raise
        record_gemini_usage(chat_cred, chat_delta=1)
        if not resp.text:
            logger.warning("generate_content returned empty text")
            return "No text response from the model."
        logger.info("chat_reply done: reply_chars=%s", len(resp.text))
        return resp.text


def prune_conversation(conversation_id: int) -> None:
    while (
        ChatMessage.objects.filter(conversation_id=conversation_id).count()
        > CHAT_HISTORY_MAX_MESSAGES
    ):
        oldest = (
            ChatMessage.objects.filter(conversation_id=conversation_id)
            .order_by("created_at")
            .first()
        )
        if oldest is None:
            break
        oldest.delete()
