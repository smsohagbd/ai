from django.db import models

CHAT_HISTORY_MAX_MESSAGES = 20

# Every API key uses this pair for chat (round-robin across keys × models).
GEMINI_GEMMA_CHAT_MODELS = (
    "gemma-4-26b-a4b-it",
    "gemma-4-31b-it",
)

# Image embeddings (catalog + user photos) always use this Gemini embedding model.
GEMINI_EMBEDDING_MODEL_ID = "gemini-embedding-2-preview"


class AppSettings(models.Model):
    """Singleton row (pk=1) for app-wide configuration."""

    class DeploymentMode(models.TextChoices):
        TESTING = "testing", "Testing — inbox only (no send to Messenger)"
        PRODUCTION = "production", "Production — send AI replies to Messenger"

    system_prompt = models.TextField(
        default=(
            "You are a knowledgeable shop assistant. Use the retrieved product "
            "information when it helps answer the customer. Be concise and helpful."
        )
    )
    deployment_mode = models.CharField(
        max_length=20,
        choices=DeploymentMode.choices,
        default=DeploymentMode.TESTING,
        help_text="Testing: webhook still receives and stores messages, but outbound Messenger sends are skipped.",
    )
    messenger_verify_token = models.CharField(
        max_length=256,
        blank=True,
        help_text=(
            "Your chosen secret. Enter the exact same value in Meta as the webhook "
            "Verify Token. If empty, MESSENGER_VERIFY_TOKEN from the environment is used."
        ),
    )
    embedding_output_dimensionality = models.PositiveIntegerField(
        default=768,
        help_text="Embedding vector size (e.g. 768). Must match for cosine search.",
    )
    similarity_top_k = models.PositiveIntegerField(
        default=5,
        help_text="How many closest product images to inject into context.",
    )
    chat_rr_seq = models.PositiveBigIntegerField(
        default=0,
        help_text="Internal counter for chat key/model round-robin.",
    )
    embed_rr_seq = models.PositiveBigIntegerField(
        default=0,
        help_text="Internal counter for embedding API key round-robin.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "App settings"
        verbose_name_plural = "App settings"

    def __str__(self):
        return "App settings"


class GeminiApiCredential(models.Model):
    """Google AI (Gemini) API key. Chat cycles Gemma 4 26B + 31B per key; add multiple keys to raise free-tier throughput."""

    label = models.CharField(max_length=120, blank=True)
    api_key = models.CharField(max_length=512)
    enabled = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(
        default=0,
        help_text="Lower runs earlier in the rotation.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    auto_disabled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set when the app turns this key off after a Google API error.",
    )
    auto_disable_reason = models.TextField(
        blank=True,
        help_text="Last API error that triggered auto-disable. Cleared when you save with Enabled checked.",
    )

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "Gemini API credential"
        verbose_name_plural = "Gemini API credentials"

    def __str__(self):
        return self.label or f"Key #{self.pk}"


class GeminiCredentialUsageBucket(models.Model):
    """Per API key: embed + chat counts in UTC minute / hour / day buckets."""

    class Granularity(models.TextChoices):
        MINUTE = "minute", "Minute"
        HOUR = "hour", "Hour"
        DAY = "day", "Day"

    credential = models.ForeignKey(
        GeminiApiCredential,
        on_delete=models.CASCADE,
        related_name="usage_buckets",
    )
    starts_at = models.DateTimeField(
        db_index=True,
        help_text="UTC bucket start (minute: ss=0; hour: mm=ss=0; day: 00:00:00).",
    )
    granularity = models.CharField(max_length=10, choices=Granularity.choices)
    embed_calls = models.PositiveIntegerField(default=0)
    chat_calls = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-starts_at", "granularity"]
        verbose_name = "Gemini key usage bucket"
        verbose_name_plural = "Gemini key usage buckets"
        constraints = [
            models.UniqueConstraint(
                fields=["credential", "starts_at", "granularity"],
                name="uniq_gemini_cred_usage_bucket",
            ),
        ]
        indexes = [
            models.Index(fields=["credential", "granularity", "starts_at"]),
        ]

    def __str__(self):
        return f"{self.credential} · {self.granularity} @ {self.starts_at}"


class Conversation(models.Model):
    """One thread per web test session or per Messenger PSID."""

    class Channel(models.TextChoices):
        WEB_TEST = "web_test", "Web test"
        MESSENGER = "messenger", "Messenger"

    channel = models.CharField(max_length=20, choices=Channel.choices)
    psid = models.CharField(max_length=128, blank=True, default="", db_index=True)
    web_session_key = models.CharField(max_length=64, blank=True, default="", db_index=True)
    title = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["psid"],
                condition=models.Q(channel="messenger") & ~models.Q(psid=""),
                name="uniq_conversation_messenger_psid",
            ),
            models.UniqueConstraint(
                fields=["web_session_key"],
                condition=models.Q(channel="web_test") & ~models.Q(web_session_key=""),
                name="uniq_conversation_web_session",
            ),
        ]

    def display_title(self) -> str:
        if self.title:
            return self.title
        if self.channel == self.Channel.MESSENGER and self.psid:
            return f"Messenger · …{self.psid[-8:]}" if len(self.psid) > 8 else f"Messenger {self.psid}"
        if self.web_session_key:
            return f"Web test · {self.web_session_key[:10]}…"
        return f"Conversation #{self.pk}"

    def __str__(self):
        return self.display_title()


class ProductImage(models.Model):
    name = models.CharField(max_length=255, blank=True)
    image = models.ImageField(upload_to="products/%Y/%m/")
    notes = models.TextField(
        blank=True,
        help_text="Optional text stored with the product (shown to the model).",
    )
    embedding = models.JSONField(null=True, blank=True)
    embedding_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name or f"Product #{self.pk}"


class ChatMessage(models.Model):
    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    text = models.TextField(blank=True)
    had_image = models.BooleanField(default=False)
    messenger_mid = models.CharField(
        max_length=256,
        blank=True,
        default="",
        db_index=True,
        help_text="Facebook message id (dedup inbound).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["conversation", "created_at"]),
        ]

    def __str__(self):
        return f"{self.role} @ {self.created_at:%H:%M}"


class ChatUserImage(models.Model):
    """Stored copy of images the user sent (web or Messenger) so the thread can render them."""

    message = models.ForeignKey(
        ChatMessage,
        on_delete=models.CASCADE,
        related_name="user_attachments",
    )
    image = models.ImageField(upload_to="chat_user/%Y/%m/%d/")
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return f"ChatUserImage #{self.pk} → message {self.message_id}"
