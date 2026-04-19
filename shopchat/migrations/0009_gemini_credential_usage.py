# Generated manually: per-key usage; remove global bucket.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("shopchat", "0008_geminiusagebucket"),
    ]

    operations = [
        migrations.DeleteModel(name="GeminiUsageBucket"),
        migrations.CreateModel(
            name="GeminiCredentialUsageBucket",
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
                        help_text="UTC bucket start (minute: ss=0; hour: mm=ss=0; day: 00:00:00).",
                    ),
                ),
                (
                    "granularity",
                    models.CharField(
                        choices=[
                            ("minute", "Minute"),
                            ("hour", "Hour"),
                            ("day", "Day"),
                        ],
                        max_length=10,
                    ),
                ),
                ("embed_calls", models.PositiveIntegerField(default=0)),
                ("chat_calls", models.PositiveIntegerField(default=0)),
                (
                    "credential",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="usage_buckets",
                        to="shopchat.geminiapicredential",
                    ),
                ),
            ],
            options={
                "verbose_name": "Gemini key usage bucket",
                "verbose_name_plural": "Gemini key usage buckets",
                "ordering": ["-starts_at", "granularity"],
            },
        ),
        migrations.AddConstraint(
            model_name="geminicredentialusagebucket",
            constraint=models.UniqueConstraint(
                fields=("credential", "starts_at", "granularity"),
                name="uniq_gemini_cred_usage_bucket",
            ),
        ),
        migrations.AddIndex(
            model_name="geminicredentialusagebucket",
            index=models.Index(
                fields=["credential", "granularity", "starts_at"],
                name="shopchat_ge_cred_id_2f1b0d_idx",
            ),
        ),
    ]
