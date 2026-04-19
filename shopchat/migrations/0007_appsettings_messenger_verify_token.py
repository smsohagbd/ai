# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shopchat", "0006_conversation_messenger"),
    ]

    operations = [
        migrations.AddField(
            model_name="appsettings",
            name="messenger_verify_token",
            field=models.CharField(
                blank=True,
                help_text="Your chosen secret. Enter the exact same value in Meta as the webhook Verify Token. If empty, MESSENGER_VERIFY_TOKEN from the environment is used.",
                max_length=256,
            ),
        ),
    ]
