"""Move notification destinations to individual scheduled jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cron_jobs", sa.Column("notify_webhook_url_enc", sa.Text(), nullable=True, comment="enc")
    )
    op.alter_column("cron_jobs", "notify_webhook", server_default=sa.text("false"))
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT j.id, b.notify_webhook_url FROM cron_jobs j JOIN bots b ON b.id=j.bot_id "
            "WHERE b.notify_webhook_url IS NOT NULL AND b.notify_webhook_url <> ''"
        )
    ).all()
    if rows:
        from coreman.core.config import get_settings
        from coreman.core.crypto import Cipher

        cipher = Cipher(get_settings().master_key_bytes)
        for job_id, url in rows:
            connection.execute(
                sa.text("UPDATE cron_jobs SET notify_webhook_url_enc=:value WHERE id=:id"),
                {"id": job_id, "value": cipher.encrypt(url, "notifications.webhook_url")},
            )
    # 原先未配置地址的默认开关不应变成启用但无地址。
    connection.execute(
        sa.text("UPDATE cron_jobs SET notify_webhook=false WHERE notify_webhook_url_enc IS NULL")
    )


def downgrade() -> None:
    op.drop_column("cron_jobs", "notify_webhook_url_enc")
    op.alter_column("cron_jobs", "notify_webhook", server_default=sa.text("true"))
