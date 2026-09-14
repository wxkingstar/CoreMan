"""M2：运行时总线（process_instances…user_reached）与对话日志 chat_logs

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# 与模型侧 bus._in() 的渲染逐字一致（逗号后带空格），其余短的直接写在建表里。
CK_CONNECTION_STATE = (
    "connection_state IN ('disconnected', 'connecting', 'subscribed', 'kicked', 'auth_failed')"
)
CK_TASK_STATUS = (
    "status IN ('queued', 'claimed', 'running', 'succeeded', 'failed', 'cancelled', 'timed_out')"
)


def _trigger(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def _ts(name: str, *, null: bool = False, now: bool = False) -> sa.Column[sa.DateTime]:
    """timestamptz 列。与 0003 的 _ts 不同：这里默认「非空且无默认值」，now()/可空各自显式声明。"""
    kw: dict[str, Any] = {"nullable": null}
    if now:
        kw["server_default"] = sa.text("now()")
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "process_instances",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        _ts("started_at"),
        _ts("heartbeat_at"),
        sa.Column("capacity", sa.Integer()),
        sa.Column("running", sa.Integer(), server_default=sa.text("0"), nullable=False),
        _ts("drain_requested_at", null=True),
        _ts("stopped_at", null=True),
    )

    op.create_table(
        "bot_leases",
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="CASCADE", name="fk_bot_leases_bot_id_bots"),
            primary_key=True,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column(
            "holder_instance",
            sa.Text(),
            sa.ForeignKey(
                "process_instances.id",
                ondelete="SET NULL",
                name="fk_bot_leases_holder_instance_process_instances",
            ),
        ),
        sa.Column("generation", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        _ts("acquired_at", null=True),
        _ts("heartbeat_at", null=True),
        sa.Column("drain_requested_by", sa.Text()),
        _ts("drain_requested_at", null=True),
        _ts("released_at", null=True),
        sa.Column(
            "connection_state",
            sa.Text(),
            server_default=sa.text("'disconnected'"),
            nullable=False,
        ),
        sa.CheckConstraint(CK_CONNECTION_STATE, name="connection_state"),
    )

    op.create_table(
        "inbound_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="CASCADE", name="fk_inbound_events_bot_id_bots"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("platform_msg_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("chat_type", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("sender_platform_user_id", sa.Text()),
        sa.Column("sender_open_id", sa.Text()),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("reply_context", postgresql.JSONB(), nullable=False),
        _ts("received_at", now=True),
        sa.UniqueConstraint("bot_id", "platform_msg_id", name="uq_inbound_events_bot_id"),
        sa.CheckConstraint("chat_type IN ('single', 'group')", name="chat_type"),
        sa.CheckConstraint(
            "kind IN ('message', 'card_action', 'enter_chat', 'feedback', 'bot_added')",
            name="kind",
        ),
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="CASCADE", name="fk_tasks_bot_id_bots"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("lane", sa.Text(), server_default=sa.text("'normal'"), nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("session_key", sa.Text()),
        sa.Column("user_id", sa.Uuid()),
        sa.Column(
            "inbound_event_id",
            sa.BigInteger(),
            sa.ForeignKey("inbound_events.id", name="fk_tasks_inbound_event_id_inbound_events"),
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        _ts("run_after", now=True),
        sa.Column(
            "claimed_by",
            sa.Text(),
            sa.ForeignKey(
                "process_instances.id",
                ondelete="SET NULL",
                name="fk_tasks_claimed_by_process_instances",
            ),
        ),
        _ts("claimed_at", null=True),
        _ts("started_at", null=True),
        _ts("finished_at", null=True),
        _ts("heartbeat_at", null=True),
        _ts("cancel_requested_at", null=True),
        sa.Column("cancel_reason", sa.Text()),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column("dedupe_key", sa.Text()),
        sa.UniqueConstraint("dedupe_key", name="uq_tasks_dedupe_key"),
        sa.CheckConstraint("lane IN ('normal', 'fast')", name="lane"),
        sa.CheckConstraint(CK_TASK_STATUS, name="status"),
    )
    # 抢单：WHERE status='queued' ORDER BY lane, priority DESC, id ... FOR UPDATE SKIP LOCKED
    op.create_index(
        "tasks_claim_idx",
        "tasks",
        ["lane", sa.text("priority DESC"), "id"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    # 同一会话串行：入队前查这个会话有没有在跑的任务
    op.create_index(
        "tasks_active_session_idx",
        "tasks",
        ["bot_id", "session_key"],
        postgresql_where=sa.text("status IN ('claimed','running')"),
    )
    # 看门狗：捞心跳超时的 running 任务
    op.create_index(
        "tasks_heartbeat_idx",
        "tasks",
        ["heartbeat_at"],
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "task_streams",
        sa.Column(
            "task_id",
            sa.BigInteger(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE", name="fk_task_streams_task_id_tasks"),
            primary_key=True,
        ),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("stream_id", sa.Text(), nullable=False),
        sa.Column("reply_context", postgresql.JSONB(), nullable=False),
        sa.Column("lease_generation", sa.BigInteger(), nullable=False),
        sa.Column("delivery_mode", sa.Text(), server_default=sa.text("'stream'"), nullable=False),
        sa.Column("thinking_md", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("pending_text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("final_text", sa.Text()),
        sa.Column("pending_card", postgresql.JSONB()),
        sa.Column("session_url", sa.Text()),
        sa.Column(
            "segment_boundaries",
            postgresql.ARRAY(sa.Integer()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        _ts("running_since"),
        sa.Column("is_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        _ts("completed_at", null=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("pushed_version", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        _ts("finish_pushed_at", null=True),
        sa.Column(
            "background_state",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        _ts("updated_at", now=True),
        sa.CheckConstraint("delivery_mode IN ('stream', 'proactive')", name="delivery_mode"),
    )
    # 网关重启后要把「还没收尾」的流找回来续推
    op.create_index(
        "task_streams_active_idx",
        "task_streams",
        ["bot_id"],
        postgresql_where=sa.text("is_complete = false OR finish_pushed_at IS NULL"),
    )
    _trigger("task_streams")

    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="CASCADE", name="fk_outbox_bot_id_bots"),
            nullable=False,
        ),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("target", postgresql.JSONB(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("lease_generation", sa.BigInteger()),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        _ts("not_before", now=True),
        sa.Column("last_error", sa.Text()),
        _ts("sent_at", null=True),
        _ts("created_at", now=True),
        sa.UniqueConstraint("dedupe_key", name="uq_outbox_dedupe_key"),
        sa.CheckConstraint(
            "kind IN ('send', 'card_update', 'welcome', 'stream_finish')", name="kind"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'skipped')", name="status"
        ),
    )
    # 派送器：按 bot 取到点的待发项
    op.create_index(
        "outbox_pending_idx",
        "outbox",
        ["bot_id", "not_before", "id"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "chat_sessions",
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="CASCADE", name="fk_chat_sessions_bot_id_bots"),
            nullable=False,
        ),
        sa.Column("session_key", sa.Text(), nullable=False),
        sa.Column("relay_session_id", sa.Uuid(), nullable=False),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("last_speaker_user_id", sa.Uuid()),
        _ts("last_active_at", now=True),
        sa.PrimaryKeyConstraint("bot_id", "session_key", name="pk_chat_sessions"),
    )

    op.create_table(
        "user_reached",
        sa.Column(
            "bot_id",
            sa.Uuid(),
            sa.ForeignKey("bots.id", ondelete="CASCADE", name="fk_user_reached_bot_id_bots"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_user_reached_user_id_users"),
            nullable=False,
        ),
        sa.Column("platform_chat_id", sa.Text(), nullable=False),
        _ts("first_seen_at", now=True),
        sa.PrimaryKeyConstraint("bot_id", "user_id", name="pk_user_reached"),
    )

    op.create_table(
        "chat_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        # 刻意不挂 bots 外键：机器人删掉之后审计与统计还要看得到历史。
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("bot_key", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Uuid()),
        sa.Column("platform_user_id", sa.Text()),
        sa.Column("user_login", sa.Text()),
        sa.Column("user_name", sa.Text()),
        sa.Column("chat_type", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text()),
        sa.Column("session_key", sa.Text()),
        sa.Column("relay_session_id", sa.Uuid()),
        sa.Column("relay_server_id", sa.Uuid()),
        sa.Column("model", sa.Text()),
        sa.Column("stream_id", sa.Text()),
        sa.Column("task_id", sa.BigInteger()),
        sa.Column("message_type", sa.Text(), nullable=False),
        sa.Column("message_content", sa.Text()),
        sa.Column("quoted_content", sa.Text()),
        sa.Column("file_info", postgresql.JSONB()),
        sa.Column("response_content", sa.Text()),
        sa.Column(
            "tools_used",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("cache_read_tokens", sa.Integer()),
        sa.Column("cache_creation_tokens", sa.Integer()),
        sa.Column("cost_usd", sa.Numeric(12, 6)),
        _ts("request_at"),
        _ts("response_at", null=True),
        _ts("created_at", now=True),
        sa.CheckConstraint("chat_type IN ('single','group','cron')", name="chat_type"),
        sa.CheckConstraint(
            "status IN ('success','error','timeout','stopped','ask_user','failed')", name="status"
        ),
    )
    op.create_index("chat_logs_bot_time_idx", "chat_logs", ["bot_id", sa.text("request_at DESC")])
    op.create_index("chat_logs_user_time_idx", "chat_logs", ["user_id", sa.text("request_at DESC")])
    op.create_index(
        "chat_logs_session_idx",
        "chat_logs",
        ["bot_id", "session_key", "relay_session_id", "request_at"],
    )
    op.create_index("chat_logs_status_idx", "chat_logs", ["status", sa.text("request_at DESC")])

    # M1b 漏建的外键索引：删团队/删用户、以及「我创建的机器人」列表都要全表扫。
    op.create_index("bots_team_id_idx", "bots", ["team_id"])
    op.create_index("bots_created_by_idx", "bots", ["created_by"])
    op.create_index("bot_members_user_id_idx", "bot_members", ["user_id"])


def downgrade() -> None:
    op.drop_index("bot_members_user_id_idx", table_name="bot_members")
    op.drop_index("bots_created_by_idx", table_name="bots")
    op.drop_index("bots_team_id_idx", table_name="bots")
    # 新建表自己的索引/触发器随 DROP TABLE 一起走，不用逐个 drop。
    op.drop_table("chat_logs")
    op.drop_table("user_reached")
    op.drop_table("chat_sessions")
    op.drop_table("outbox")
    op.drop_table("task_streams")
    op.drop_table("tasks")
    op.drop_table("inbound_events")
    op.drop_table("bot_leases")
    op.drop_table("process_instances")
