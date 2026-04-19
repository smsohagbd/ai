# Generated manually for Conversation + ChatMessage FK migration from session_key.

from django.db import migrations, models
import django.db.models.deletion


def forwards_migrate_sessions(apps, schema_editor):
    ChatMessage = apps.get_model("shopchat", "ChatMessage")
    Conversation = apps.get_model("shopchat", "Conversation")

    session_keys = (
        ChatMessage.objects.values_list("session_key", flat=True)
        .distinct()
        .order_by("session_key")
    )
    for sk in session_keys:
        if not sk:
            continue
        conv, _ = Conversation.objects.get_or_create(
            channel="web_test",
            web_session_key=sk,
            defaults={"title": ""},
        )
        ChatMessage.objects.filter(session_key=sk).update(conversation=conv)


def backwards_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("shopchat", "0005_embedding_model_fixed"),
    ]

    operations = [
        migrations.AddField(
            model_name="appsettings",
            name="deployment_mode",
            field=models.CharField(
                choices=[
                    (
                        "testing",
                        "Testing (monitor in inbox; do not send to Messenger)",
                    ),
                    (
                        "production",
                        "Production (send AI replies to Messenger users)",
                    ),
                ],
                default="testing",
                help_text="Testing: webhook still receives and stores messages, but outbound Messenger sends are skipped.",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="Conversation",
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
                    "channel",
                    models.CharField(
                        choices=[
                            ("web_test", "Web test"),
                            ("messenger", "Messenger"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "psid",
                    models.CharField(
                        blank=True, db_index=True, default="", max_length=128
                    ),
                ),
                (
                    "web_session_key",
                    models.CharField(
                        blank=True, db_index=True, default="", max_length=64
                    ),
                ),
                ("title", models.CharField(blank=True, max_length=255)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-updated_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="conversation",
            constraint=models.UniqueConstraint(
                fields=["psid"],
                condition=models.Q(channel="messenger") & ~models.Q(psid=""),
                name="uniq_conversation_messenger_psid",
            ),
        ),
        migrations.AddConstraint(
            model_name="conversation",
            constraint=models.UniqueConstraint(
                fields=["web_session_key"],
                condition=models.Q(channel="web_test")
                & ~models.Q(web_session_key=""),
                name="uniq_conversation_web_session",
            ),
        ),
        migrations.AddField(
            model_name="chatmessage",
            name="conversation",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="messages",
                to="shopchat.conversation",
            ),
        ),
        migrations.AddField(
            model_name="chatmessage",
            name="messenger_mid",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="",
                help_text="Facebook message id (dedup inbound).",
                max_length=256,
            ),
        ),
        migrations.RunPython(forwards_migrate_sessions, backwards_noop),
        migrations.RemoveIndex(
            model_name="chatmessage",
            name="shopchat_ch_session_8a3ee0_idx",
        ),
        migrations.RemoveField(
            model_name="chatmessage",
            name="session_key",
        ),
        migrations.AlterField(
            model_name="chatmessage",
            name="conversation",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="messages",
                to="shopchat.conversation",
            ),
        ),
        migrations.AddIndex(
            model_name="chatmessage",
            index=models.Index(
                fields=["conversation", "created_at"],
                name="shopchat_ch_convers_0f8b2f_idx",
            ),
        ),
    ]
