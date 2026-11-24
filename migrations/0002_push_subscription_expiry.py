"""Caducidad de la activación de dispositivos Web Push (NULL = para siempre)."""


def upgrade(ctx):
    ctx.exec_sql("ALTER TABLE push_subscriptions ADD COLUMN IF NOT EXISTS expires_at_utc DATETIME NULL AFTER active")
