"""运行时实例与模型目录（spec §5.3）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, TimestampMixin

RUNTIME_ENVS = ("host", "chroot", "nspawn")
HEALTH_STATUSES = ("healthy", "down", "auth_fail", "timeout", "unknown")


class RelayServer(TimestampMixin, Base):
    """运行时实例：每个运行时节点注册时按 AI 类型各建一行，请求一律经该节点的反向通道。"""

    __tablename__ = "relay_servers"
    __table_args__ = (
        CheckConstraint("runtime_env IN ('host','chroot','nspawn')", name="runtime_env"),
        CheckConstraint(
            "supported_models_mode IN ('inherit','restricted')", name="supported_models_mode"
        ),
        CheckConstraint("visibility IN ('all','admins')", name="visibility"),
        CheckConstraint(
            "health_status IN ('healthy','down','auth_fail','timeout','unknown')",
            name="health_status",
        ),
        # 唯一约束在这里给全名（不再在列上写 unique=True），保证 alembic check 只看到一个同名约束。
        UniqueConstraint("name", name="uq_relay_servers_name"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    runtime_node_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runtime_nodes.id"), index=True
    )
    name: Mapped[str] = mapped_column(Text)
    model_provider: Mapped[str] = mapped_column(Text)
    supported_models_mode: Mapped[str] = mapped_column(Text, server_default=text("'inherit'"))
    supported_models: Mapped[list[str] | None] = mapped_column(ARRAY(Text()))
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("teams.id"))
    visibility: Mapped[str] = mapped_column(Text, server_default=text("'all'"))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # 节点按 AI 类型派生的上报令牌（密文），用于 /api/infra/relay/* 与记忆上报的 Bearer 校验。
    agent_token_enc: Mapped[str | None] = mapped_column(Text, comment="enc")
    rate_limit_5h_used_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    rate_limit_5h_resets_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rate_limit_7d_used_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    rate_limit_7d_resets_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rate_limit_probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health_status: Mapped[str] = mapped_column(Text, server_default=text("'unknown'"))
    health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health_detail: Mapped[str | None] = mapped_column(Text)
    health_latency_ms: Mapped[int | None] = mapped_column(Integer)
    health_fail_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    relay_version: Mapped[str | None] = mapped_column(Text)
    relay_mode: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    # 已废弃，下个版本删除：独立中继实例的专用列。代码不再读写，仅为滚动升级窗口内
    # 旧版本进程仍会 SELECT 它们而保留（迁移 0022 已放宽为可空）。
    host: Mapped[str | None] = mapped_column(Text)
    clawrelay_port: Mapped[int | None] = mapped_column(Integer)
    agent_port: Mapped[int | None] = mapped_column(Integer)
    ssh_user: Mapped[str | None] = mapped_column(Text)
    runtime_env: Mapped[str | None] = mapped_column(Text, server_default=text("'host'"))
    chroot_path: Mapped[str | None] = mapped_column(Text)
    runtime_user: Mapped[str | None] = mapped_column(Text)
    # 乐观锁，语义同 PlatformApp.version。
    __mapper_args__ = {"version_id_col": version}

    @property
    def relay_url(self) -> str:
        """实例标识（展示与会话链接用），不参与请求路由；未绑定节点时为空串。"""
        if self.runtime_node_id is None:
            return ""
        return f"runtime://{self.runtime_node_id}/{self.model_provider}"


class ModelCatalog(TimestampMixin, Base):
    """provider 下的可用模型；替代代码常量 MODEL_PROVIDERS / RETIRED_MODELS。"""

    __tablename__ = "model_catalog"
    __table_args__ = (PrimaryKeyConstraint("provider", "model"),)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    retired: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    supports_xhigh: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
