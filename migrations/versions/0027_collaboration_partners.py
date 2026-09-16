"""Separate configured partners from group transport proofs; retain all histories."""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("bot_collaboration_partners"):
        op.create_table(
            "bot_collaboration_partners",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "source_bot_id",
                sa.Uuid(),
                sa.ForeignKey("bots.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "target_bot_id",
                sa.Uuid(),
                sa.ForeignKey("bots.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
            sa.Column(
                "timeout_seconds", sa.Integer(), server_default=sa.text("300"), nullable=False
            ),
            sa.UniqueConstraint("source_bot_id", "target_bot_id"),
        )
    bind = op.get_bind()
    pairs = (
        bind.execute(
            sa.text("""SELECT source_bot_id, target_bot_id,
        bool_or(enabled AND NOT archived) AS enabled, bool_and(archived) AS archived,
        min(timeout_seconds) AS timeout_seconds FROM bot_collaboration_routes
        GROUP BY source_bot_id, target_bot_id""")
        )
        .mappings()
        .all()
    )
    for pair in pairs:
        pid = uuid.uuid4()
        bind.execute(
            sa.text("""INSERT INTO bot_collaboration_partners
            (id, source_bot_id, target_bot_id, enabled, archived, timeout_seconds)
            VALUES (:id, :source_bot_id, :target_bot_id, :enabled, :archived, :timeout_seconds)
            ON CONFLICT (source_bot_id, target_bot_id) DO NOTHING"""),
            {**pair, "id": pid},
        )
        pid = bind.execute(
            sa.text("""SELECT id FROM bot_collaboration_partners
            WHERE source_bot_id=:source_bot_id AND target_bot_id=:target_bot_id"""),
            {"source_bot_id": pair["source_bot_id"], "target_bot_id": pair["target_bot_id"]},
        ).scalar_one()
        bind.execute(
            sa.text("""UPDATE bot_collaboration_routes
            SET setup = setup || jsonb_build_object('partner_id', CAST(:id AS text))
            WHERE source_bot_id=:source_bot_id AND target_bot_id=:target_bot_id"""),
            {
                "id": str(pid),
                "source_bot_id": pair["source_bot_id"],
                "target_bot_id": pair["target_bot_id"],
            },
        )


def downgrade():
    op.execute("UPDATE bot_collaboration_routes SET setup = setup - 'partner_id'")
    op.drop_table("bot_collaboration_partners")
