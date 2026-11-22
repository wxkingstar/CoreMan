"""Personal credentials shared across AI employees: one value, bound to any number of bots.

- `personal_credential_items`: a member's credential, encrypted with AAD bound to (credential id,
  member) instead of (bot, member, variable), so the same value can be used by several bots.
- `personal_credential_bindings`: which credential is injected into which bot under which
  variable name; (bot, member, variable) stays unique.

Existing rows of `personal_credentials` are copied over and re-encrypted. Rows of the same member
with the same variable name, secrecy and value become one credential bound to all those bots.
The old table is left in place: the previous version keeps reading it during a rolling upgrade.
A later migration drops it.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict

import sqlalchemy as sa
from alembic import op

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "personal_credential_items",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("env_key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("secret", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("value_enc", sa.Text(), nullable=False),
        *_timestamps(),
    )
    op.create_index("personal_credential_items_user_idx", "personal_credential_items", ["user_id"])
    op.create_table(
        "personal_credential_bindings",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "credential_id",
            sa.Uuid(),
            sa.ForeignKey("personal_credential_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("env_key", sa.Text(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.UniqueConstraint(
            "bot_id", "user_id", "env_key", name="uq_personal_credential_bindings_bot_id"
        ),
    )
    op.create_index(
        "personal_credential_bindings_credential_idx",
        "personal_credential_bindings",
        ["credential_id"],
    )
    op.create_index(
        "personal_credential_bindings_user_idx", "personal_credential_bindings", ["user_id"]
    )
    _copy_existing()


def _copy_existing() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT bot_id, user_id, env_key, label, secret, value_enc, last_used_at, "
            "created_at, updated_at FROM personal_credentials ORDER BY updated_at"
        )
    ).all()
    if not rows:
        return
    from coreman.core.config import get_settings
    from coreman.core.crypto import DecryptError

    cipher = get_settings().build_cipher()
    # (用户, 变量名, 是否保密, 明文) 相同的合并成一条；按更新时间升序，标签取最新的那行。
    groups: dict[tuple[uuid.UUID, str, bool, str], list[sa.Row]] = defaultdict(list)
    skipped = 0
    for row in rows:
        aad = f"personal_credentials.value_enc:{row.bot_id}:{row.user_id}:{row.env_key}"
        try:
            value = cipher.decrypt(row.value_enc, aad)
        except DecryptError:
            # 解不开的旧行搬过去也注入不了：留在旧表里，本人让 AI 员工重新索取即可。
            skipped += 1
            continue
        groups[(row.user_id, row.env_key, row.secret, value)].append(row)
    for (user_id, env_key, secret, value), members in groups.items():
        latest = members[-1]
        credential_id = uuid.uuid4()
        connection.execute(
            sa.text(
                "INSERT INTO personal_credential_items "
                "(id, user_id, env_key, label, secret, value_enc, created_at, updated_at) "
                "VALUES (:id, :user_id, :env_key, :label, :secret, :value_enc, "
                ":created_at, :updated_at)"
            ),
            {
                "id": credential_id,
                "user_id": user_id,
                "env_key": env_key,
                "label": latest.label,
                "secret": secret,
                "value_enc": cipher.encrypt(
                    value, f"personal_credential_items.value_enc:{credential_id}:{user_id}"
                ),
                "created_at": min(m.created_at for m in members),
                "updated_at": latest.updated_at,
            },
        )
        for member in members:
            connection.execute(
                sa.text(
                    "INSERT INTO personal_credential_bindings "
                    "(credential_id, bot_id, user_id, env_key, last_used_at, created_at) "
                    "VALUES (:credential_id, :bot_id, :user_id, :env_key, :last_used_at, "
                    ":created_at)"
                ),
                {
                    "credential_id": credential_id,
                    "bot_id": member.bot_id,
                    "user_id": user_id,
                    "env_key": env_key,
                    "last_used_at": member.last_used_at,
                    "created_at": member.created_at,
                },
            )
    if skipped:
        logging.getLogger("alembic.runtime.migration").warning(
            "personal credentials: %d unreadable row(s) left in personal_credentials", skipped
        )


def downgrade() -> None:
    op.drop_table("personal_credential_bindings")
    op.drop_table("personal_credential_items")
