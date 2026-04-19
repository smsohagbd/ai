from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("shopchat", "0009_gemini_credential_usage"),
    ]

    operations = [
        migrations.AddField(
            model_name="geminiapicredential",
            name="auto_disabled_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Set when the app turns this key off after a Google API error.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="geminiapicredential",
            name="auto_disable_reason",
            field=models.TextField(
                blank=True,
                help_text="Last API error that triggered auto-disable. Cleared when you save with Enabled checked.",
            ),
        ),
    ]
