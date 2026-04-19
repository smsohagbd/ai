import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from django.utils import timezone

from shopchat.models import ChatMessage, ChatUserImage, ProductImage

logger = logging.getLogger("shopchat.signals")


@receiver(post_save, sender=ChatMessage)
def bump_conversation_on_message(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    from shopchat.models import Conversation

    Conversation.objects.filter(pk=instance.conversation_id).update(
        updated_at=timezone.now()
    )


@receiver(post_save, sender=ProductImage)
def schedule_product_embedding(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    pk = instance.pk

    def run():
        from shopchat.services import embed_product_image

        logger.debug("on_commit: scheduling embed for product_image pk=%s", pk)
        embed_product_image(pk)

    transaction.on_commit(run)


@receiver(post_delete, sender=ProductImage)
def delete_product_image_file(sender, instance, **kwargs):
    if instance.image:
        instance.image.delete(save=False)


@receiver(post_delete, sender=ChatUserImage)
def delete_chat_user_image_file(sender, instance, **kwargs):
    if instance.image:
        instance.image.delete(save=False)
