from django.urls import path

from shopchat import views

urlpatterns = [
    path("", views.inbox_page, name="inbox"),
    path(
        "api/inbox/conversations/",
        views.inbox_conversations_api,
        name="inbox_conversations_api",
    ),
    path(
        "api/inbox/<int:pk>/messages/",
        views.inbox_messages_api,
        name="inbox_messages_api",
    ),
    path(
        "api/inbox/<int:pk>/chat/",
        views.inbox_chat_api,
        name="inbox_chat_api",
    ),
    path(
        "api/inbox/web/bootstrap/",
        views.inbox_web_bootstrap,
        name="inbox_web_bootstrap",
    ),
    path(
        "api/inbox/web/new/",
        views.inbox_new_web_conversation,
        name="inbox_new_web_conversation",
    ),
    path("api/chat/history/", views.chat_history_api, name="chat_history_api"),
    path("api/chat/", views.chat_api, name="chat_api"),
    path("api/webhook/", views.messenger_webhook, name="messenger_webhook"),
    path("api/webhook", views.messenger_webhook),
    path("settings/", views.settings_page, name="settings"),
]
