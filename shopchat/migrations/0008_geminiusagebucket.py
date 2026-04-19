# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shopchat", "0007_appsettings_messenger_verify_token"),
    ]

    operations = [
        migrations.CreateModel(
            name="GeminiUsageBucket",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "starts_at",
                    models.DateTimeField(
                        db_index=True,
                        help_text="UTC start of this bucket (second=0; hour also has minute=0).",
                    ),
                ),
                (
                    "granularity",
                    models.CharField(
                        choices=[
                            ("minute", "Per minute"),
                            ("hour", "Per hour"),
                        ],
                        max_length=10,
                    ),
                ),
                (
                    "embed_calls",
                    models.PositiveIntegerField(
                        default=0,
                        help_text="Gemini embedding API calls in this bucket.",
                    ),
                ),
                (
                    "chat_calls",
                    models.PositiveIntegerField(
                        default=0,
                        help_text="Gemini generate_content (chat) calls in this bucket.",
                    ),
                ),
            ],
            options={
                "verbose_name": "Gemini API usage bucket",
                "verbose_name_plural": "Gemini API usage (minute & hour)",
                "ordering": ["-starts_at", "granularity"],
            },
        ),
        migrations.AddConstraint(
            model_name="geminiusagebucket",
            constraint=models.UniqueConstraint(
                fields=("starts_at", "granularity"),
                name="uniq_gemini_usage_bucket",
            ),
        ),
    ]
