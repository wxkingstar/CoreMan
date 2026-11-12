"""Personal assistant node isolation: encrypted transcripts, assistant pool, sandboxed calls."""

import sqlalchemy as sa
from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 改的是在用的表，要拿排他锁：排在长事务后面会让之后对这张表的读写全部跟着排队。
    # 5 秒拿不到锁就报错，整次迁移回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "assistant_transcripts",
        sa.Column(
            "bot_id", sa.Uuid(), sa.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("blob_enc", sa.LargeBinary(), nullable=True),
        sa.Column("size", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("pending_transfer", sa.Uuid(), nullable=True),
        sa.Column(
            "pending_node_id",
            sa.Uuid(),
            sa.ForeignKey("runtime_nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("bot_id", "session_id"),
        sa.UniqueConstraint("pending_transfer", name="uq_assistant_transcripts_pending_transfer"),
    )
    op.create_index("assistant_transcripts_updated_idx", "assistant_transcripts", ["updated_at"])
    op.add_column(
        "relay_servers",
        sa.Column("assistant_pool", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "runtime_calls",
        sa.Column("sandbox", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("runtime_nodes", sa.Column("machine_fingerprint", sa.Text(), nullable=True))
    op.add_column("runtime_nodes", sa.Column("uid", sa.BigInteger(), nullable=True))
    op.create_index(
        "ix_runtime_nodes_machine_fingerprint_uid",
        "runtime_nodes",
        ["machine_fingerprint", "uid"],
    )


def downgrade() -> None:
    # 改的是在用的表，要拿排他锁：排在长事务后面会让之后对这张表的读写全部跟着排队。
    # 5 秒拿不到锁就报错，整次迁移回滚，稍后重跑即可。
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_runtime_nodes_machine_fingerprint_uid", table_name="runtime_nodes")
    op.drop_column("runtime_nodes", "uid")
    op.drop_column("runtime_nodes", "machine_fingerprint")
    op.drop_column("runtime_calls", "sandbox")
    op.drop_column("relay_servers", "assistant_pool")
    op.drop_index("assistant_transcripts_updated_idx", table_name="assistant_transcripts")
    op.drop_table("assistant_transcripts")
