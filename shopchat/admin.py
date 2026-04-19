from django.contrib import admin
from django.utils.html import format_html

from shopchat.models import (
    AppSettings,
    ChatMessage,
    Conversation,
    GeminiApiCredential,
    GeminiCredentialUsageBucket,
    ProductImage,
)
from shopchat.usage_stats import credential_usage_summary


@admin.register(AppSettings)
class AppSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not AppSettings.objects.exists()


@admin.register(GeminiCredentialUsageBucket)
class GeminiCredentialUsageBucketAdmin(admin.ModelAdmin):
    list_display = (
        "credential",
        "starts_at",
        "granularity",
        "embed_calls",
        "chat_calls",
        "total_calls",
    )
    list_filter = ("granularity", "credential")
    date_hierarchy = "starts_at"
    ordering = ("-starts_at", "granularity")
    readonly_fields = (
        "credential",
        "starts_at",
        "granularity",
        "embed_calls",
        "chat_calls",
    )

    @admin.display(description="Total")
    def total_calls(self, obj):
        return obj.embed_calls + obj.chat_calls

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(GeminiApiCredential)
class GeminiApiCredentialAdmin(admin.ModelAdmin):
    list_display = (
        "label",
        "enabled",
        "auto_disable_notice",
        "sort_order",
        "created_at",
        "key_hint",
        "usage_summary",
    )
    list_filter = ("enabled",)
    ordering = ("sort_order", "id")
    readonly_fields = ("created_at", "auto_disabled_at", "auto_disable_reason")
    fieldsets = (
        (None, {"fields": ("label", "api_key", "enabled", "sort_order")}),
        (
            "Auto-disable log",
            {
                "classes": ("collapse",),
                "description": (
                    "Filled when the app disables this key after errors such as quota tier / "
                    "project access. Saving with Enabled checked clears this log."
                ),
                "fields": ("auto_disabled_at", "auto_disable_reason"),
            },
        ),
    )

    def save_model(self, request, obj, form, change):
        if obj.enabled:
            obj.auto_disabled_at = None
            obj.auto_disable_reason = ""
        super().save_model(request, obj, form, change)

    @admin.display(description="Auto-off / error")
    def auto_disable_notice(self, obj):
        if not obj.auto_disabled_at:
            return "—"
        r = (obj.auto_disable_reason or "").strip()
        snippet = (r[:120] + "…") if len(r) > 120 else r
        return format_html(
            '<span style="color:#b91c1c" title="{}"><b>{}</b><br><small>{}</small></span>',
            r,
            obj.auto_disabled_at.strftime("%Y-%m-%d %H:%M UTC"),
            snippet,
        )

    @admin.display(description="Key")
    def key_hint(self, obj):
        k = (obj.api_key or "").strip()
        if len(k) <= 8:
            return "—"
        return f"…{k[-4:]}"

    @admin.display(description="Usage (UTC): 60m / 24h / today (e=embed, c=chat)")
    def usage_summary(self, obj):
        s = credential_usage_summary(obj.pk)
        return format_html(
            "<span style=\"white-space:nowrap\">60m: <b>{}</b> "
            "<small>(e{}·c{})</small></span><br>"
            "<span style=\"white-space:nowrap\">24h: <b>{}</b> "
            "<small>(e{}·c{})</small></span><br>"
            "<span style=\"white-space:nowrap\">Today: <b>{}</b> "
            "<small>(e{}·c{})</small></span>",
            s["total_60m"],
            s["embed_60m"],
            s["chat_60m"],
            s["total_24h"],
            s["embed_24h"],
            s["chat_24h"],
            s["total_today"],
            s["embed_today"],
            s["chat_today"],
        )


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "channel", "display_title_field", "updated_at")
    list_filter = ("channel",)
    search_fields = ("title", "psid", "web_session_key")
    ordering = ("-updated_at",)

    @admin.display(description="Title")
    def display_title_field(self, obj):
        return obj.display_title()


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("conversation", "role", "created_at", "had_image", "messenger_mid")
    list_filter = ("role",)
    search_fields = ("text", "messenger_mid", "conversation__psid")
    ordering = ("-created_at",)


@admin.register(ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at", "embedding_error")
    readonly_fields = ("embedding", "embedding_error", "created_at")
