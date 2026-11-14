"""Assistant inbox kind `owner_said` and the focus-people list.

`owner_said` rows hold the owner's own outgoing messages so the assistant can extract
promises and requests from them. `focus_people_enc` is the sealed list of open_ids whose
messages are always handed over immediately.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None

OLD = "kind IN ('message', 'approval_todo', 'reminder')"
NEW = "kind IN ('message', 'approval_todo', 'reminder', 'owner_said')"


def upgrade() -> None:
    # 改的是在用的表：5 秒拿不到锁就报错回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("kind", "assistant_inbox", type_="check")
    op.create_check_constraint("kind", "assistant_inbox", NEW)
    op.add_column(
        "assistant_settings",
        sa.Column("focus_people_enc", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("assistant_settings", "focus_people_enc")
    # 降级后的代码不认识 owner_said：这类条目没有可交回的内容，直接删掉。
    op.execute("DELETE FROM assistant_inbox WHERE kind = 'owner_said'")
    op.drop_constraint("kind", "assistant_inbox", type_="check")
    op.create_check_constraint("kind", "assistant_inbox", OLD)
