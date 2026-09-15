"""Retire standalone relay instances: every runtime backend belongs to an enrolled node.

未绑定运行时节点（runtime_node_id 为空）的实例不再受支持：先把绑在上面的机器人解绑并停用，
再删除这些实例行。旧专用列放宽为可空、去掉 host+端口唯一约束；列本身保留到下个版本——滚动
升级窗口内旧版本进程仍会 SELECT 它们。
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

LEGACY_COLUMNS = ("host", "clawrelay_port", "runtime_env")


def upgrade() -> None:
    connection = op.get_bind()
    unbound = connection.execute(
        sa.text(
            "UPDATE bots SET relay_server_id = NULL, enabled = false, version = version + 1 "
            "WHERE relay_server_id IN "
            "(SELECT id FROM relay_servers WHERE runtime_node_id IS NULL) "
            "RETURNING id"
        )
    ).scalars()
    for bot_id in unbound:
        # 与 API 同一通道：提交后网关/工作进程立即重载，不必等兜底轮询。
        connection.execute(
            sa.text("SELECT pg_notify('config_changed', :payload)"),
            {"payload": json.dumps({"table": "bots", "id": str(bot_id)})},
        )
    connection.execute(sa.text("DELETE FROM relay_servers WHERE runtime_node_id IS NULL"))
    op.drop_constraint("uq_relay_servers_host", "relay_servers", type_="unique")
    for column in LEGACY_COLUMNS:
        op.alter_column("relay_servers", column, nullable=True)
    # 节点遥测：协议版本、并发上限与当前在执行数（旧节点不上报，保持为空）。
    op.add_column("runtime_nodes", sa.Column("protocol_version", sa.Integer(), nullable=True))
    op.add_column("runtime_nodes", sa.Column("max_concurrent", sa.Integer(), nullable=True))
    op.add_column("runtime_nodes", sa.Column("active_calls", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("runtime_nodes", "active_calls")
    op.drop_column("runtime_nodes", "max_concurrent")
    op.drop_column("runtime_nodes", "protocol_version")
    # 已删除的独立实例无法恢复；节点实例补回旧版本要求的非空值（主机取节点 ID）。
    op.execute("UPDATE relay_servers SET host = runtime_node_id::text WHERE host IS NULL")
    op.execute(
        "UPDATE relay_servers SET clawrelay_port = "
        "CASE model_provider WHEN 'codex' THEN 50010 ELSE 50009 END "
        "WHERE clawrelay_port IS NULL"
    )
    op.execute("UPDATE relay_servers SET runtime_env = 'host' WHERE runtime_env IS NULL")
    for column in LEGACY_COLUMNS:
        op.alter_column("relay_servers", column, nullable=False)
    op.create_unique_constraint(
        "uq_relay_servers_host", "relay_servers", ["host", "clawrelay_port"]
    )
