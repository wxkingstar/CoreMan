"""个人助理的数据密钥、审批流水、「处理中」标记与节点转录。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coreman.core.db.base import Base, enum_check

APPROVAL_STATUSES = (
    "pending",
    "approved",
    "rejected",
    "withdrawn",
    "stale",
    "executed",
    "failed",
    "expired",
)
APPROVAL_CHANNELS = ("p2p", "group", "doc", "approval", "calendar", "task", "mail", "other")


def _ts(**kw: Any) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)


class AssistantKey(Base):
    """每人一把数据密钥，用平台主密钥包裹；销毁后 dek_enc 为空，对应内容永久不可读。

    重新签发时 generation 加 1：内容密文记着自己是第几代加密的，旧一代的读不出来。
    """

    __tablename__ = "assistant_keys"
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), primary_key=True)
    dek_enc: Mapped[str | None] = mapped_column(Text)
    generation: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    destroyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssistantApproval(Base):
    """助理的一次写操作：暂存的参数、审批状态与执行结果，同时是审批流水。

    `args_enc` / `reason_enc` / `result_enc` 登记在 `sealed.COLUMNS`：库里是密文，经会话取出
    的行上是明文。其余列是调度与排障用的元数据。`external_id` 是向 Tower 提交时的幂等键
    （改稿沿用），`approval_id` 是 Tower 给的编号（取令牌、上报结果、撤回都用它）。
    """

    __tablename__ = "assistant_approvals"
    __table_args__ = (
        CheckConstraint(enum_check("status", APPROVAL_STATUSES), name="status"),
        CheckConstraint(enum_check("channel", APPROVAL_CHANNELS), name="channel"),
        UniqueConstraint("external_id", name="uq_assistant_approvals_external_id"),
        Index("assistant_approvals_bot_status_idx", "bot_id", "status"),
        # 同一助理同一份内容同时只有一张在途的单；提交时先查，这里兜住并发。
        Index(
            "assistant_approvals_live_uq",
            "bot_id",
            "args_hash",
            unique=True,
            postgresql_where=text("status IN ('pending', 'approved')"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    tool: Mapped[str] = mapped_column(Text)
    args_enc: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=False)
    args_hash: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[str | None] = mapped_column(Text)
    anchor_message_id: Mapped[str | None] = mapped_column(Text)
    thread_id: Mapped[str | None] = mapped_column(Text)
    topic: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    reason_enc: Mapped[str | None] = mapped_column(Text)
    result_enc: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # 失败、未执行的原因代码（content_locked、tower_unavailable、token_used……），不带内容。
    error_code: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    approval_id: Mapped[str | None] = mapped_column(Text)
    # 提交这张单的那一轮：结果投回助理会话时沿用它的入站事件与会话键。
    origin_event_id: Mapped[int | None] = mapped_column(BigInteger)
    session_key: Mapped[str] = mapped_column(Text, server_default=text("''"))
    # 临时桶里还留着这张单的附件；到终态后由 scheduler 删除并清掉这个标记。
    has_files: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
    expires_at: Mapped[datetime] = _ts()
    notified_at: Mapped[datetime | None] = _ts()
    decided_at: Mapped[datetime | None] = _ts()
    execute_started_at: Mapped[datetime | None] = _ts()
    executed_at: Mapped[datetime | None] = _ts()


class AssistantMark(Base):
    """助理在别人发来的消息上打的「处理中」表情；撤销时删行。第 2 期按时间提醒久未回复的。"""

    __tablename__ = "assistant_marks"
    __table_args__ = (PrimaryKeyConstraint("bot_id", "message_id"),)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    message_id: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[str] = mapped_column(Text)
    reaction_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))


class AssistantTranscript(Base):
    """节点上一个会话的 CLI 转录：每轮结束由节点上传，下一轮续聊前下发，节点上不留。

    `blob_enc` 登记在 `sealed.COLUMNS`（二进制列）：库里是用所有者数据密钥加密的 tar.gz，经会话
    取出的行上是明文字节。`pending_transfer` 是派给节点的那一轮的交接编号：派出时写上，节点上传
    时凭它对上号并清空；下一轮派出时它还在，说明上一轮的转录没传回来，这个会话不再续。
    """

    __tablename__ = "assistant_transcripts"
    __table_args__ = (
        PrimaryKeyConstraint("bot_id", "session_id"),
        UniqueConstraint("pending_transfer", name="uq_assistant_transcripts_pending_transfer"),
        Index("assistant_transcripts_updated_idx", "updated_at"),
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    blob_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    size: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    pending_transfer: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    pending_node_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runtime_nodes.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))
