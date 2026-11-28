"""Dispatcher knowledge base: `knowledge_runs` and `knowledge_items`.

An item is one fact extracted from an allowed source, already stripped of quotes and personal
data; the wiki in the dispatcher's working directory cites items as `K-<id>`. A run records one
extraction and compilation pass. Additive only: the previous version never reads either table.
See docs/superpowers/specs/2026-11-28-dispatcher-knowledge-base.md.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0138"
down_revision = "0137"
branch_labels = None
depends_on = None

KINDS = "kind IN ('system', 'role', 'bot', 'decision', 'process', 'term')"
CONFIDENCES = "confidence IN ('stated', 'inferred')"
SOURCES = "source_kind IN ('task', 'rules', 'catalog', 'roster', 'group')"
ITEM_STATUSES = "status IN ('pending', 'compiled', 'dropped', 'purged')"
MODES = "mode IN ('daily', 'cold_start', 'purge')"
RUN_STATUSES = "status IN ('running', 'ok', 'skipped', 'needs_review', 'failed')"


def _now(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=None if nullable else sa.text("now()"),
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "knowledge_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'running'")),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("batches", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed_batches", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("extracted", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("dropped", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("compiled", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "detail",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("summary", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("error", sa.Text(), nullable=True),
        _now("started_at"),
        _now("finished_at", nullable=True),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(MODES, name="mode"),
        sa.CheckConstraint(RUN_STATUSES, name="status"),
    )
    op.create_index("knowledge_runs_started_idx", "knowledge_runs", ["started_at"])
    op.create_table(
        "knowledge_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=True),
        sa.Column("confidence", sa.Text(), nullable=False, server_default=sa.text("'stated'")),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("dropped_reason", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        _now("compiled_at", nullable=True),
        _now("created_at"),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["knowledge_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(KINDS, name="kind"),
        sa.CheckConstraint(CONFIDENCES, name="confidence"),
        sa.CheckConstraint(SOURCES, name="source_kind"),
        sa.CheckConstraint(ITEM_STATUSES, name="status"),
    )
    op.create_index("knowledge_items_bot_status_idx", "knowledge_items", ["bot_id", "status", "id"])
    op.create_index("knowledge_items_source_idx", "knowledge_items", ["source_kind", "source_ref"])


def downgrade() -> None:
    op.drop_table("knowledge_items")
    op.drop_table("knowledge_runs")
