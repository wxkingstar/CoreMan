"""Runtime owners and private runtimes: every user can enroll a runtime; a private one is
selectable only by its owner (the user who created the install link)."""

import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("runtime_nodes", "relay_servers"):
        op.add_column(
            table,
            sa.Column(
                "owner_id",
                sa.Uuid(),
                sa.ForeignKey("users.id", name=f"fk_{table}_owner_id_users"),
                nullable=True,
            ),
        )
    # 已注册的节点：安装链接的创建人就是所有者。
    op.execute(
        "UPDATE runtime_nodes n SET owner_id = l.created_by"
        " FROM runtime_install_links l WHERE l.node_id = n.id"
    )
    op.execute(
        "UPDATE relay_servers r SET owner_id = n.owner_id"
        " FROM runtime_nodes n WHERE r.runtime_node_id = n.id"
    )
    op.drop_constraint(op.f("ck_relay_servers_visibility"), "relay_servers", type_="check")
    op.create_check_constraint(
        "visibility", "relay_servers", "visibility IN ('all','admins','private')"
    )


def downgrade() -> None:
    op.execute("UPDATE relay_servers SET visibility = 'all' WHERE visibility = 'private'")
    op.execute("UPDATE runtime_nodes SET visibility = 'all' WHERE visibility = 'private'")
    op.execute("UPDATE runtime_install_links SET visibility = 'all' WHERE visibility = 'private'")
    op.drop_constraint(op.f("ck_relay_servers_visibility"), "relay_servers", type_="check")
    op.create_check_constraint("visibility", "relay_servers", "visibility IN ('all','admins')")
    op.drop_column("relay_servers", "owner_id")
    op.drop_column("runtime_nodes", "owner_id")
