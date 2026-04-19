from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _configure_sqlite_connection(sender, connection, **kwargs):
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")
        cursor.execute("PRAGMA synchronous=NORMAL;")


class ShopchatConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "shopchat"

    def ready(self):
        connection_created.connect(
            _configure_sqlite_connection,
            dispatch_uid="shopchat_sqlite_wal",
        )
        import shopchat.signals  # noqa: F401
