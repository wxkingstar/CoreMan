"""个人助理的数据密钥、审批流水、「处理中」标记与节点转录。"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
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
    # 最近一次改稿的时刻（没改过为空）：改稿时平台重新读过会话，「之后会话动没动过」从它算起。
    revised_at: Mapped[datetime | None] = _ts()
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


class AssistantSent(Base):
    """平台经审批以所有者身份发出的消息：盯消息读到它们时不当作所有者亲自发言。只有编号。"""

    __tablename__ = "assistant_sent"
    __table_args__ = (PrimaryKeyConstraint("bot_id", "message_id"),)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    message_id: Mapped[str] = mapped_column(Text)
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
COLD_START_STEPS = ("voice", "people", "duties", "focus", "hours")
COLD_START_OUTCOMES = ("completed", "skipped", "timeout")
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

    冷启动：`cold_start_started_at` 是开始时刻（24 小时兜底从它算起），`cold_start_step` 是
    走到哪一步，`cold_start_next_at` 非空表示该平台出手（拉样本、发卡片）、空表示在等所有者，
    `cold_start_outcome` 是怎么结束的；做完、跳过、超时都写 `cold_start_done_at`。
    编译：`worked_on` 是最近一次被看到在班的那天，`compiled_on` 是已经处理过编译的那天，
    `compiled_at` 是上一次编译成功时的入队时刻（下一次的活动窗口从它算起）。日期都按所有者时区。
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
        CheckConstraint(
            f"cold_start_step IS NULL OR {enum_check('cold_start_step', COLD_START_STEPS)}",
            name="cold_start_step",
        ),
        CheckConstraint(
            "cold_start_outcome IS NULL OR "
            f"{enum_check('cold_start_outcome', COLD_START_OUTCOMES)}",
            name="cold_start_outcome",
        ),
        # 冷启动循环只看「开始了、没做完」的行：平时一行都没有。
        Index(
            "assistant_settings_cold_start_idx",
            "cold_start_started_at",
            postgresql_where=text(
                "cold_start_done_at IS NULL AND cold_start_started_at IS NOT NULL"
            ),
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
    cold_start_started_at: Mapped[datetime | None] = _ts()
    cold_start_step: Mapped[str | None] = mapped_column(Text)
    cold_start_next_at: Mapped[datetime | None] = _ts()
    cold_start_outcome: Mapped[str | None] = mapped_column(Text)
    worked_on: Mapped[date | None] = mapped_column(Date)
    compiled_on: Mapped[date | None] = mapped_column(Date)
    compiled_at: Mapped[datetime | None] = _ts()
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))


PAGE_AUTHORS = ("owner", "assistant", "import")


class AssistantPage(Base):
    """个人知识库的一页。

    `path_enc` / `title_enc` / `summary_enc` / `body_enc` 登记在 `sealed.COLUMNS`：库里是密文，
    经会话取出并还原的行上是明文。`path_hmac` 是规范化后的路径用所有者数据密钥算的带密钥
    摘要，同一位助理内唯一；`size` 是正文的 UTF-8 字节数，合计上限按它求和；`version` 每次
    写入加 1，写入时带上读到的版本做乐观锁。
    """

    __tablename__ = "assistant_pages"
    __table_args__ = (
        CheckConstraint(enum_check("updated_by", PAGE_AUTHORS), name="updated_by"),
        UniqueConstraint("bot_id", "path_hmac", name="uq_assistant_pages_path"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    path_hmac: Mapped[str] = mapped_column(Text)
    path_enc: Mapped[str] = mapped_column(Text, server_default=text("''"))
    title_enc: Mapped[str] = mapped_column(Text, server_default=text("''"))
    summary_enc: Mapped[str] = mapped_column(Text, server_default=text("''"))
    body_enc: Mapped[str] = mapped_column(Text, server_default=text("''"))
    size: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    pinned: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    updated_by: Mapped[str] = mapped_column(Text, server_default=text("'assistant'"))
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))


FOLLOWUP_CATEGORIES = ("decide", "promised", "waiting", "remind")
FOLLOWUP_STATUSES = ("open", "snoozed", "done", "dropped")
FOLLOWUP_CLOSE_CODES = ("origin_recalled",)
JOB_KINDS = ("subtask", "bot_help")
JOB_ORIGINS = ("assistant", "system")
JOB_STATUSES = ("queued", "running", "done", "failed", "cancelled", "blocked")


class AssistantFollowup(Base):
    """助理替所有者记着的一件跨天的事：等他决定、他答应了别人、在等别人、到日子提醒他。

    `title_enc` / `notes_enc` 登记在 `sealed.COLUMNS`：库里是密文，经会话取出的行上是明文。
    分类、状态、两个日期、编号是明文，按日期查询不解密。`seq` 在一位助理内递增，显示为 F012。
    `snoozed` 的条目到 `review_on` 那天由读取方改回 `open`。`close_code` 只在平台自动关闭时有值。
    `job_id` 指向引出这一条的子任务（`assistant_jobs`），子任务行删掉时置空。
    """

    __tablename__ = "assistant_followups"
    __table_args__ = (
        CheckConstraint(enum_check("category", FOLLOWUP_CATEGORIES), name="category"),
        CheckConstraint(enum_check("status", FOLLOWUP_STATUSES), name="status"),
        CheckConstraint(
            f"close_code IS NULL OR {enum_check('close_code', FOLLOWUP_CLOSE_CODES)}",
            name="close_code",
        ),
        UniqueConstraint("bot_id", "seq", name="uq_assistant_followups_bot_seq"),
        Index("assistant_followups_bot_status_idx", "bot_id", "status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    review_on: Mapped[date | None] = mapped_column(Date)
    due_on: Mapped[date | None] = mapped_column(Date)
    origin_message_id: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("assistant_jobs.id", ondelete="SET NULL")
    )
    title_enc: Mapped[str] = mapped_column(Text)
    notes_enc: Mapped[str | None] = mapped_column(Text)
    close_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))
    closed_at: Mapped[datetime | None] = _ts()


WATCH_STATES = ("idle", "polling", "backoff", "paused", "stopped")
INBOX_KINDS = ("message", "approval_todo", "reminder")
INBOX_LEVELS = ("immediate", "batch", "hold")
INBOX_STATUSES = ("pending", "delivered", "void")


class AssistantWatch(Base):
    """一位助理的盯消息状态：读到哪了、上一轮的会话顺序、下次什么时候轮询、健康状况。

    `cursor_enc` 是定位信息，只有会话 id、话题 id、消息 id 与毫秒时间；与 `head_enc` 一样登记在
    `sealed.COLUMNS`（和谁有会话、什么时候说过话本身就是所有者的私事），库里是密文：
    `{"since": <开始被盯的时刻>, "chats": {<会话>: {"t": <读到的最新一条>, "m": [<时间等于 t 的
    消息>], "k": "p2p" | "group", "o": <所有者上次在这里发言>, "th": {<话题>: <读到的最新一条>},
    "f": <连续读失败的轮数>, "x": <放下它到什么时候>}}, "todo": [<上一轮没读完的会话>]}`。
    `head_enc` 同样是密文，经会话取出的行上是上一轮列会话得到的会话 id 列表（按最近活跃倒序）。
    两列都不能在 SQL 里按内容过滤：要看里面的字段就取出来在内存里判断。
    `state`：`polling` 在轮询；`idle` 没在轮询但没有问题（下班、冷启动没做完）；`paused` 运行时
    暂停；`backoff` 出错或限频后在退避；`stopped` 已停用。`last_error` 只存代码。
    `keepalive_at` 是上次确认令牌还活着的时刻（轮询成功或保活刷新），不轮询时每 24 小时刷新一次。
    """

    __tablename__ = "assistant_watch"
    __table_args__ = (
        CheckConstraint(enum_check("state", WATCH_STATES), name="state"),
        CheckConstraint('"interval" BETWEEN 60 AND 3600', name="interval"),
        Index("assistant_watch_due_idx", "next_poll_at"),
    )
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
    )
    cursor_enc: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    head_enc: Mapped[list[str] | None] = mapped_column(JSONB)
    interval: Mapped[int] = mapped_column(Integer, server_default=text("60"))
    next_poll_at: Mapped[datetime] = _ts(server_default=text("now()"))
    state: Mapped[str] = mapped_column(Text, server_default=text("'idle'"))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_ok_at: Mapped[datetime | None] = _ts()
    failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    keepalive_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))


class AssistantInbox(Base):
    """要交回给助理的一件事。消息内容不落库：这里只有定位信息与加密的摘要。

    `summary_enc` 登记在 `sealed.COLUMNS`。`level` 是入箱时的分级；`hold_until` 为空表示可以
    立刻交回，否则攒批或压着到那个时刻。`status`：`pending` 待交回，`delivered` 已交回，`void`
    作废（`void_code`：`recalled` 对方撤回、`stopped` 助理停用）。`delivered_task_id` 由投递层
    （2E）写入：交回它的那一轮；`pending` 且非空表示正在交回，盯消息不再改这一行。
    """

    __tablename__ = "assistant_inbox"
    __table_args__ = (
        CheckConstraint(enum_check("kind", INBOX_KINDS), name="kind"),
        CheckConstraint(enum_check("level", INBOX_LEVELS), name="level"),
        CheckConstraint(enum_check("status", INBOX_STATUSES), name="status"),
        # 同一条消息只入箱一次：轮询重跑、两个 scheduler 交接时重叠都靠它兜住。
        Index(
            "assistant_inbox_message_uq",
            "bot_id",
            "kind",
            "message_id",
            unique=True,
            postgresql_where=text("message_id IS NOT NULL"),
        ),
        Index("assistant_inbox_due_idx", "bot_id", "status", "hold_until"),
        # 对账用：还绑着任务号、没记账的条目（见 assistants/delivery.py 的 reconcile）。
        Index(
            "assistant_inbox_in_flight_idx",
            "delivered_task_id",
            postgresql_where=text("delivered_task_id IS NOT NULL AND status = 'pending'"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text, server_default=text("'message'"))
    level: Mapped[str] = mapped_column(Text)
    chat_id: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(Text)
    thread_id: Mapped[str | None] = mapped_column(Text)
    message_at: Mapped[datetime | None] = _ts()
    summary_enc: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    void_code: Mapped[str | None] = mapped_column(Text)
    hold_until: Mapped[datetime | None] = _ts()
    # 不设外键：任务行按保留期清理，条目比它留得久。
    delivered_task_id: Mapped[int | None] = mapped_column(BigInteger)
    # 带着它的交回轮次真失败过几次；满 delivery.MAX_FAILURES 次作废（void_code=undeliverable）。
    failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))


class AssistantDelivery(Base):
    """投递层给每位助理记的账；没有内容列。

    `handover_seq` 只增不减，拼进交回任务的幂等键；取这一行的行锁（`FOR UPDATE SKIP LOCKED`）
    同时是「同一时刻只有一处在为这位助理入队」的互斥。两个日期都是所有者时区里的日期：当天
    已经私信过「预算用完」、已经提醒过「授权快到期」。`next_sweep_at` 是下一次顺带检查的时刻。
    """

    __tablename__ = "assistant_delivery"
    __table_args__ = (Index("assistant_delivery_sweep_idx", "next_sweep_at"),)
    bot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="CASCADE"), primary_key=True
    )
    handover_seq: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    budget_notified_on: Mapped[date | None] = mapped_column(Date)
    reauth_reminded_on: Mapped[date | None] = mapped_column(Date)
    next_sweep_at: Mapped[datetime] = _ts(server_default=text("now()"))
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))


class AssistantJob(Base):
    """助理派出去的一件事：自己的会话里跑一轮或几轮，结论回到主会话。

    `title_enc` / `brief_enc` / `message_enc` / `result_enc` / `blocked_enc` 登记在
    `sealed.COLUMNS`：库里是密文，经会话取出的行上是明文。其余列是调度与排障用的元数据。
    `last_task_id` 是在途或最近那一轮的任务号，`result_task_id` 是把结论投回主会话的那一轮；
    两列都不设外键，任务行会被过期清理。`kind = 'bot_help'` 与 `target_bot_id` 留给调度公共员工。
    """

    __tablename__ = "assistant_jobs"
    __table_args__ = (
        CheckConstraint(enum_check("kind", JOB_KINDS), name="kind"),
        CheckConstraint(enum_check("origin", JOB_ORIGINS), name="origin"),
        CheckConstraint(enum_check("status", JOB_STATUSES), name="status"),
        Index("assistant_jobs_bot_status_idx", "bot_id", "status"),
        Index(
            "assistant_jobs_live_idx",
            "deadline_at",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index(
            "assistant_jobs_unreported_idx",
            "bot_id",
            postgresql_where=text(
                "report AND reported_at IS NULL"
                " AND status IN ('done', 'failed', 'cancelled', 'blocked')"
            ),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    bot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bots.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text, server_default=text("'subtask'"))
    origin: Mapped[str] = mapped_column(Text, server_default=text("'assistant'"))
    target_bot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("bots.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    title_enc: Mapped[str] = mapped_column(Text)
    brief_enc: Mapped[str] = mapped_column(Text)
    message_enc: Mapped[str | None] = mapped_column(Text)
    result_enc: Mapped[str | None] = mapped_column(Text)
    blocked_enc: Mapped[str | None] = mapped_column(Text)
    session_key: Mapped[str] = mapped_column(Text)
    origin_session_key: Mapped[str] = mapped_column(Text)
    rounds: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_task_id: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(Text)
    report: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    result_task_id: Mapped[int | None] = mapped_column(BigInteger)
    report_seq: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    report_after: Mapped[datetime | None] = _ts()
    report_failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    reported_at: Mapped[datetime | None] = _ts()
    report_code: Mapped[str | None] = mapped_column(Text)
    deadline_at: Mapped[datetime] = _ts()
    created_at: Mapped[datetime] = _ts(server_default=text("now()"))
    updated_at: Mapped[datetime] = _ts(server_default=text("now()"))
    started_at: Mapped[datetime | None] = _ts()
    finished_at: Mapped[datetime | None] = _ts()
