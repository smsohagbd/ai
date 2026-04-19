"""Backfill Conversation.title from Graph API for Messenger threads with no title."""

import time

from django.core.management.base import BaseCommand

from shopchat.messenger_client import ensure_messenger_title_for_conversation
from shopchat.models import Conversation


class Command(BaseCommand):
    help = (
        "Fetch Messenger user names (Graph API) for conversations with empty title. "
        "Requires MESSENGER_PAGE_ACCESS_TOKEN. Uses a small delay between calls to "
        "respect rate limits."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--sleep",
            type=float,
            default=0.08,
            help="Seconds to sleep between Graph calls (default 0.08).",
        )

    def handle(self, *args, **options):
        delay = max(0.0, options["sleep"])
        qs = (
            Conversation.objects.filter(channel=Conversation.Channel.MESSENGER)
            .exclude(psid="")
            .filter(title="")
        )
        total = qs.count()
        updated = 0
        for conv in qs.iterator():
            if ensure_messenger_title_for_conversation(conv):
                updated += 1
            if delay:
                time.sleep(delay)
        self.stdout.write(
            self.style.SUCCESS(
                f"Checked {total} Messenger row(s) without title; updated {updated}."
            )
        )
