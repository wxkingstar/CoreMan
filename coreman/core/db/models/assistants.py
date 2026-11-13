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
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
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


ASSISTANT_STATES = ("provisioning", "active", "deprovisioning", "deprovisioned")
LIFECYCLE_ACTIONS = ("provision", "deprovision")
LIFECYCLE_STEPS = (
    # 开通：发了开通卡片等本人点 → 建记录与密钥、选节点 → 节点上建目录 → 发起飞书授权 → 等授权完成
    "offered",
    "create",
    "init_workspace",
    "start_grant",
    "await_grant",
    # 停用：（成员离开时）宽限 → 停下在跑的轮次 → 撤回待批 → 撤销授权 → 销毁密钥并停用记录
    "grace",
    "drain",
    "withdraw",
    "revoke",
    "destroy",
)
LIFECYCLE_TRIGGERS = ("owner", "member_left", "admin", "abandoned")
LIFECYCLE_OUTCOMES = ("done", "expired", "cancelled", "failed", "ineligible")


class AssistantLifecycle(Base):
    """一次开通或停用：做到哪一步、下一次什么时候接着做、出过什么错（只有代码）。

    一人同时只有一条没结束的（`finished_at` 为空）：开通没走完不能再点开通，停用没走完不能重新开通
    （新的开通会签发新一代密钥，停用最后一步要销毁的是旧的那一代）。看护进程按 `next_attempt_at`
    一步一步推进，每一步一个事务；任何一步失败都停在原地等下次重试，不会留下能跑轮次的半成品。
    """

    __tablename__ = "assistant_lifecycles"
    __table_args__ = (
        CheckConstraint(enum_check("action", LIFECYCLE_ACTIONS), name="action"),
        CheckConstraint(enum_check("step", LIFECYCLE_STEPS), name="step"),
        CheckConstraint(
            f"trigger IS NULL OR {enum_check('trigger', LIFECYCLE_TRIGGERS)}", name="trigger"
        ),
        CheckConstraint(
            f"outcome IS NULL OR {enum_check('outcome', LIFECYCLE_OUTCOMES)}", name="outcome"
        ),
        Index(
            "assistant_lifecycles_live_uq",
            "owner_id",
            unique=True,
            postgresql_where=text("finished_at IS NULL"),
        ),
        Index(
            "assistant_lifecycles_due_idx",
            "next_attempt_at",
            postgresql_where=text("finished_at IS NULL"),
        ),
        Index("assistant_lifecycles_bot_idx", "bot_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE", name="fk_assistant_lifecycles_owner")
    )
    platform_app_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("platform_apps.id", ondelete="CASCADE", name="fk_assistant_lifecycles_app")
    )
    bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="SET NULL", name="fk_assistant_lifecycles_bot")
    )
    action: Mapped[str] = mapped_column(Text)
    step: Mapped[str] = mapped_column(Text)
    trigger: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL", name="fk_assistant_lifecycles_actor")
    )
    # 开通卡片：发给谁（飞书 user_id）、卡片令牌的摘要（点过即清）、卡片所在的消息与私聊。
    # 点过开通之后 offer_message_id 改记最近一张授权卡片的消息。
    platform_user_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    offer_hash: Mapped[str | None] = mapped_column(Text)
    offer_message_id: Mapped[str | None] = mapped_column(Text)
    offer_chat_id: Mapped[str | None] = mapped_column(Text)
    # 点开通的人在飞书里的身份（来自卡片回调里飞书给的操作人，不来自卡片内容），授权要核对它。
    open_id: Mapped[str | None] = mapped_column(Text)
    tenant_key: Mapped[str | None] = mapped_column(Text)
    # 这一步的期限（卡片过期、等授权、等轮次停下、成员离开的宽限）。
    deadline: Mapped[datetime | None] = _ts()
    # 本步骤连续失败的次数（进入下一步时清零）；发过几次授权链接。
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    issued: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = _ts()
    error_code: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))
    finished_at: Mapped[datetime | None] = _ts()


class AssistantRetiredDir(Base):
    """要从节点上删掉的助理工作目录：停用时的当前目录、换运行时换走的旧目录。

    看护进程向目录所在实例的节点发 `workspace-remove`，节点按它自己保管的归属登记认目录（目录里的
    标记只用于登记出现之前初始化的目录），只删这位助理的；节点离线、目录还在用、或节点认不准
    （`error_code` 为 `dir_unverified`）就隔一段时间再试，直到节点答复「已删除」或「不是它的」。
    不引用 bots：记录删了目录也要删。
    """

    __tablename__ = "assistant_retired_dirs"
    __table_args__ = (
        UniqueConstraint("relay_server_id", "working_dir", name="uq_assistant_retired_dirs_path"),
        Index("assistant_retired_dirs_due_idx", "next_attempt_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    relay_server_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("relay_servers.id", ondelete="CASCADE", name="fk_assistant_retired_dirs_relay"),
    )
    working_dir: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = _ts(server_default=text("now()"))
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))


SHIFT_OVERRIDES = ("on", "off")
_HHMM = "'^([01][0-9]|2[0-3]):[0-5][0-9]$'"


class AssistantSettings(Base):
    """一位助理的设置：开通时记下的所有者身份，工作时段，重点群与忽略的会话。

    身份四列（`open_id`、`tenant_key`、`platform_user_id`、`chat_id`）在授权完成时由平台写入，
    取自点开通时飞书给的操作人与开通卡片所在的私聊，不来自任何请求内容；平台自己发起的轮次
    （没有入站事件）凭它们拿个人工具，见 `coreman/core/assistants/platform_turns.py`。空串表示
    没记下，这样的助理跑不了平台来源的轮次。

    `focus_chats_enc` / `ignored_chats_enc` 登记在 `sealed.COLUMNS`：库里是密文，经会话取出
    的行上是会话 id 的列表；没设置过是 None，密钥销毁后读出来是空列表。
    工作时段是所有者时区里的 `HH:MM`，不跨午夜（约束 `work_start < work_end`）；`workdays`
    是 ISO 星期（1 周一 … 7 周日）。`shift_override` 是「上班」「下班」命令的临时切换，过了
    `shift_override_until` 失效。
    """

    __tablename__ = "assistant_settings"
    __table_args__ = (
        CheckConstraint(
            f"work_start ~ {_HHMM} AND work_end ~ {_HHMM} AND work_start < work_end",
            name="work_hours",
        ),
        CheckConstraint("workdays <@ ARRAY[1,2,3,4,5,6,7]::smallint[]", name="workdays"),
        CheckConstraint(
            f"shift_override IS NULL OR {enum_check('shift_override', SHIFT_OVERRIDES)}",
            name="shift_override",
        ),
        CheckConstraint(
            "daily_turn_budget IS NULL OR daily_turn_budget BETWEEN 10 AND 2000",
            name="daily_turn_budget",
        ),
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
    )
    open_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    tenant_key: Mapped[str] = mapped_column(Text, server_default=text("''"))
    platform_user_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    chat_id: Mapped[str] = mapped_column(Text, server_default=text("''"))
    timezone: Mapped[str] = mapped_column(Text, server_default=text("'Asia/Shanghai'"))
    work_start: Mapped[str] = mapped_column(Text, server_default=text("'09:30'"))
    work_end: Mapped[str] = mapped_column(Text, server_default=text("'19:00'"))
    workdays: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger()), server_default=text("'{1,2,3,4,5}'::smallint[]")
    )
    focus_chats_enc: Mapped[list[str] | None] = mapped_column(JSONB)
    ignored_chats_enc: Mapped[list[str] | None] = mapped_column(JSONB)
    # 空则用平台默认（设置项 assistant_daily_turn_budget，2E）。
    daily_turn_budget: Mapped[int | None] = mapped_column(Integer)
    shift_override: Mapped[str | None] = mapped_column(Text)
    shift_override_until: Mapped[datetime | None] = _ts()
    cold_start_done_at: Mapped[datetime | None] = _ts()
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))
