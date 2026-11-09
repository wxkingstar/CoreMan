"""Allow users.source = service for accounts that only act through system-triggered turns."""

from alembic import op

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_users_source"), "users", type_="check")
    op.create_check_constraint(
        "source", "users", "source IN ('sync','bootstrap','manual','service')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM users WHERE source = 'service'")
    op.drop_constraint(op.f("ck_users_source"), "users", type_="check")
    op.create_check_constraint("source", "users", "source IN ('sync','bootstrap','manual')")
