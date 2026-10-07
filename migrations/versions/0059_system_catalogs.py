"""Business system operation catalogs.

`systems.sitemap_url` becomes `openapi_url`. The old column stays for one version so processes
still on the previous release keep reading it during a rolling upgrade; the new code writes both
and a later migration drops `sitemap_url`. `token_delivery` is added now (always `env`) so the
proxy mode can be switched on per system later without another schema change.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None

OLD_PURPOSES = ("chat", "cron", "health", "access_test")
PURPOSES = (*OLD_PURPOSES, "catalog")


def _purpose_check(values: tuple[str, ...]) -> str:
    return f"purpose IN ({','.join(repr(p) for p in values)})"


def upgrade():
    op.add_column("systems", sa.Column("openapi_url", sa.Text()))
    op.execute("UPDATE systems SET openapi_url = sitemap_url")
    op.add_column(
        "systems", sa.Column("token_delivery", sa.Text(), nullable=False, server_default="env")
    )
    op.create_check_constraint("token_delivery", "systems", "token_delivery IN ('env','proxy')")
    op.create_table(
        "system_catalogs",
        sa.Column(
            "system_key",
            sa.Text(),
            sa.ForeignKey("systems.key", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("spec_url", sa.Text(), nullable=False),
        sa.Column("etag", sa.Text()),
        sa.Column("spec_sha256", sa.Text()),
        sa.Column("spec_bytes", sa.Integer()),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("operation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hidden_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("module_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "lint",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("compiled", postgresql.JSONB()),
        sa.Column("compiler_version", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("status IN ('ok','stale','error')", name="status"),
    )
    op.drop_constraint(
        op.f("ck_business_token_issues_purpose"), "business_token_issues", type_="check"
    )
    op.create_check_constraint("purpose", "business_token_issues", _purpose_check(PURPOSES))


def downgrade():
    op.execute("DELETE FROM business_token_issues WHERE purpose = 'catalog'")
    op.drop_constraint(
        op.f("ck_business_token_issues_purpose"), "business_token_issues", type_="check"
    )
    op.create_check_constraint("purpose", "business_token_issues", _purpose_check(OLD_PURPOSES))
    op.drop_table("system_catalogs")
    op.drop_constraint(op.f("ck_systems_token_delivery"), "systems", type_="check")
    op.drop_column("systems", "token_delivery")
    op.execute("UPDATE systems SET sitemap_url = openapi_url")
    op.drop_column("systems", "openapi_url")
