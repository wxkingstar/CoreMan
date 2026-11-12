"""审批闸门：把助理的写操作暂存成审批单，并跟着 Tower 的决定走完它的一生。

状态：pending →（批准）approved →（worker 执行）executed / failed / stale；
pending →（拒绝）rejected、（助理或停用流程撤回）withdrawn、（72 小时无人处理）expired。
只有 pending 的单会被决定、改稿或撤回；只有 approved 的单会被执行，且只执行一次。

带文件的写操作：提交时把中转文件复制到临时桶（`attachments.py`），参数里的中转编号换成引用；
单子有了结论之后由 scheduler 删掉桶里的对象。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any

from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
from coreman.core.assistants.approvals import attachments, catalog, checks, content, tower
from coreman.core.assistants.kinds import APPROVAL_EXECUTE_KIND, APPROVAL_RESULT_KIND
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.cloud_proxy.resume import clean_reason
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantApproval, Bot, InboundEvent, User, UserIdentity
from coreman.core.db.models.storage import StoredObject
from coreman.core.feishu_personal.files import FileRelay
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.feishu_personal.toolbase import Arguments, Call, Identifier, Uuid
from coreman.core.feishu_personal.tools import TOOLS as PERSONAL_TOOLS
from coreman.core.feishu_personal.toolsets import files as file_tools
from coreman.core.feishu_personal.toolsets import mail as mail_tools
from coreman.core.logging import get_logger
from coreman.core.timeutils import utcnow

log = get_logger(__name__)
# 与云签名代理的 `cm-<任务号>-…` 同在 `cm-` 之下（对账只认这个前缀），又不会与它相撞。
EXTERNAL_PREFIX = "cm-pa-"
META = "approval"
TTL = timedelta(hours=72)
MAX_PENDING = 10
HASH_PURPOSE = "assistant_approvals.args_hash"
LIVE = ("pending", "approved")
UNAVAILABLE = "approval_service_unavailable"
PENDING_HINT = (
    "已提交审批，尚未执行。不要等待也不要重复提交，继续处理别的事；有结果时平台会在本会话里告诉你。"
)
NO_FILE_STORE = {
    "error": "attachments_not_supported",
    "hint": "这个部署没有配置审批期间存放附件的临时桶：去掉附件，或请本人自己发送文件。",
}
FILE_STORE_DOWN = {
    "error": "attachment_store_unavailable",
    "hint": "存放附件的临时桶暂时不可用：这次操作没有提交，也没有执行。稍后重新调用同一个工具。",
}


class Recipient(Arguments):
    receive_id: Identifier = Field(description="chat_id of another chat to send the same text to")
    anchor_message_id: Identifier | None = None
    uuid: Uuid


class Meta(Arguments):
    """写类工具入参里的 `approval`：平台检查与审批单要用、但不属于工具本身的信息。"""

    chat_id: Identifier | None = Field(
        default=None, description="Required for message tools: the chat this acts in"
    )
    anchor_message_id: Identifier | None = Field(
        default=None, description="The last message you read in that chat (or the one replied to)"
    )
    thread_id: Identifier | None = None
    topic: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] = Field(
        default="", description="Short label to group related approvals; no names or content"
    )
    background: Annotated[str, StringConstraints(max_length=1000)] = Field(
        default="", description="Why this is needed; shown to the owner as written by you"
    )
    research: Annotated[str, StringConstraints(max_length=1500)] = Field(
        default="", description="What you checked before drafting; shown as written by you"
    )
    more_recipients: list[Recipient] = Field(
        default_factory=list,
        max_length=9,
        description="feishu_send_message only: other chats that get the same text in one approval",
    )


class _Refused(Exception):
    """暂存不了：`payload` 原样交给助理。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("error")))
        self.payload = payload


@dataclass(frozen=True)
class Upload:
    """一个要复制进临时桶的中转文件，以及它在审批单里的引用。"""

    ref: dict[str, Any]
    row: StoredObject


@dataclass(frozen=True)
class Staged:
    tool: str
    calls: list[dict[str, Any]]
    meta: Meta
    facts: list[content.Facts]
    args_hash: str
    abstract: str
    content: str
    body: bytes
    uploads: list[Upload]
    store: attachments.Store | None

    def stored(self) -> dict[str, Any]:
        """进 `args_enc` 的内容：执行要用的参数，加上给助理回看的摘要与它自己写的说明。"""
        return {
            "calls": self.calls,
            "abstract": self.abstract,
            "background": self.meta.background,
            "research": self.meta.research,
        }


def _problems(exc: ValidationError) -> list[str]:
    # 只有字段名和规则说明：pydantic 的这两项不回显取值。
    return [
        ".".join(str(part) for part in error["loc"]) + ": " + error["msg"]
        for error in exc.errors(include_url=False, include_input=False)[:5]
    ]


def _dedupe_text(tool: str, calls: list[dict[str, Any]]) -> str:
    """去重用的内容：工具、参数（去掉重试编号）、会话与锚点。同一件事只换重试编号仍算重复。

    文件按内容算（文件名、大小、SHA-256）：同一份文件重新上传一次仍是同一件事。
    """
    slim = [
        {
            **call,
            "arguments": {
                k: v
                for k, v in attachments.public(call["arguments"]).items()
                if k not in catalog.RETRY_KEYS
            },
        }
        for call in calls
    ]
    return json.dumps({"tool": tool, "calls": slim}, ensure_ascii=False, sort_keys=True)


def _tower_failure(exc: tower.TowerError) -> dict[str, Any]:
    if exc.code == "unknown_approvers":
        # 发起人（所有者）未登记或已停用时 Tower 也用这个代码。
        return {
            "error": "tower_account_missing",
            "hint": "所有者在审批系统里还没有账号：请本人先登录一次审批系统，再重试。",
        }
    if exc.code == "already_final":
        # 改稿时那张单在 Tower 里已有结论（或已到期），决定正在送达的路上。
        return {
            "error": "not_pending",
            "hint": "这张单在审批系统里已有结论，不能再改：等平台把结果送到本会话。",
        }
    return {
        "error": UNAVAILABLE,
        "detail": exc.code,
        "hint": "审批系统暂时不可用：这次操作没有提交，也没有执行。稍后重新调用同一个工具。",
    }


def _oversized(tool: str, arguments: dict[str, Any], uploads: list[Upload]) -> bool:
    """文件超过原工具的上限：现在就拒绝，不让所有者批准一张注定执行失败的单。"""
    sizes = [int(item.ref["size"]) for item in uploads]
    if tool == "feishu_send_file":
        limit = file_tools.IMAGE_LIMIT if arguments.get("as_image") else file_tools.IM_FILE_LIMIT
        return max(sizes) > limit
    if tool == "feishu_save_to_drive":
        return max(sizes) > file_tools.DRIVE_LIMIT
    return sum(sizes) > mail_tools.MAIL_ATTACHMENT_LIMIT


async def _attach(
    session: AsyncSession,
    scope: Scope,
    tool: str,
    raw: dict[str, Any],
    files: FileRelay | None,
) -> tuple[dict[str, Any], list[Upload], attachments.Store | None]:
    """把参数里的中转文件编号换成临时桶里的引用。这里只定下对象名，还不复制文件。

    Raises:
        _Refused: 没配临时桶、编号无效或已过期、文件超过原工具的上限
        PersonalError: 所有者的授权失效
    """
    names = sorted(name for name in attachments.ARGUMENTS if raw.get(name))
    if not names:
        return raw, [], None
    store = attachments.store(files.settings) if files is not None else None
    if files is None or store is None:
        raise _Refused(dict(NO_FILE_STORE))
    out, uploads = dict(raw), []
    for name in names:
        value = raw[name]
        refs = []
        for upload_id in value if isinstance(value, list) else [value]:
            try:
                row = await files.uploaded(session, scope, upload_id)
            except PersonalError as exc:
                if exc.code != "upload_not_found":
                    raise
                hint = "upload_id 无效或已过期（一小时内有效）：重新上传文件后再提交。"
                raise _Refused({"error": exc.code, "hint": hint}) from None
            ref = attachments.reference(
                store,
                filename=row.filename,
                mime=row.content_type,
                size=row.size,
                sha256=row.sha256,
            )
            refs.append(ref)
            uploads.append(Upload(ref, row))
        out[name] = refs if isinstance(value, list) else refs[0]
    if _oversized(tool, raw, uploads):
        raise _Refused({"error": "file_too_large", "hint": "文件超过这个工具允许的大小。"})
    return out, uploads, store


async def _store_uploads(files: FileRelay | None, staged: Staged) -> None:
    """把中转文件复制进临时桶；任何一个失败都抛 `attachments.Unavailable`。"""
    if not staged.uploads:
        return
    assert files is not None and staged.store is not None
    for item in staged.uploads:
        try:
            content = await files.read(item.row)
        except Exception as exc:  # noqa: BLE001 中转区读不出来与临时桶写不进去同样处理
            log.warning("assistant_attachment_read_failed", type=type(exc).__name__)
            raise attachments.Unavailable from None
        await attachments.put(staged.store, item.ref["key"], content)


async def _discard(staged: Staged) -> None:
    """提交没成：把刚复制进临时桶的对象删掉（删不掉由桶的自动清理兜底）。"""
    if staged.store is not None:
        await attachments.delete(staged.store, [item.ref["key"] for item in staged.uploads])


async def _stage(
    session: AsyncSession,
    cipher: Cipher,
    scope: Scope,
    tool: str,
    arguments: dict[str, Any],
    files: FileRelay | None = None,
) -> Staged | dict[str, Any]:
    """校验参数、重新读会话、生成审批单内容。返回字典表示不能提交（原样交给助理）。"""
    item = PERSONAL_TOOLS.get(tool)
    if item is None or not catalog.is_write(tool):
        return {"error": "invalid_tool_or_arguments"}
    raw = dict(arguments)
    try:
        meta = Meta.model_validate(raw.pop(META, {}))
        item.args.model_validate(raw)
    except ValidationError as exc:
        return {"error": "invalid_tool_or_arguments", "invalid": _problems(exc)}
    try:
        raw, uploads, store = await _attach(session, scope, tool, raw, files)
    except _Refused as exc:
        return exc.payload
    if meta.more_recipients and tool not in catalog.MERGEABLE:
        return {"error": "merge_not_supported", "hint": "只有 feishu_send_message 能合并多个会话。"}
    calls: list[dict[str, Any]] = [
        {
            "arguments": raw,
            "chat_id": meta.chat_id,
            "anchor_message_id": meta.anchor_message_id,
            "thread_id": meta.thread_id,
        }
    ]
    for extra in meta.more_recipients:
        # 合并的每个会话只换接收方与重试编号，正文与格式和第一条相同。
        merged = {
            **raw,
            "receive_id": extra.receive_id,
            "receive_id_type": "chat_id",
            "uuid": extra.uuid,
        }
        calls.append(
            {
                "arguments": merged,
                "chat_id": extra.receive_id,
                "anchor_message_id": extra.anchor_message_id,
                "thread_id": None,
            }
        )
    if len({one["chat_id"] for one in calls}) != len(calls):
        return {"error": "duplicate_recipient", "hint": "合并的会话里有重复的 chat_id。"}
    reader = Call(session, cipher, scope)
    facts: list[content.Facts] = []
    try:
        for one in calls:
            found = await checks.check_call(reader, tool, one, owner_open_id=scope.open_id)
            one["anchor_message_id"] = found.anchor_message_id
            facts.append(found)
    except checks.Rejected as exc:
        return {"error": exc.code, "hint": exc.hint}
    template = catalog.entry(tool)
    # 审批人在批准前要能看到文件：链接与审批单同一时刻失效。
    links = {
        item.ref["key"]: attachments.link(store, item.ref, int(TTL.total_seconds()))
        for item in uploads
        if store is not None
    }
    try:
        text = content.render(
            template,
            calls,
            facts,
            background=meta.background,
            research=meta.research,
            links=links,
        )
    except content.TooLong:
        return {
            "error": "content_too_long",
            "limit": content.LIMIT,
            "hint": "审批单正文超过上限，要执行的内容不会被截断：缩短内容或拆成几次操作。",
        }
    try:
        digest = await sealed.content_mac(
            session, scope.bot.id, HASH_PURPOSE, _dedupe_text(tool, calls)
        )
    except sealed.ContentLocked:
        return {"error": sealed.LOCKED_CODE}
    return Staged(
        tool=tool,
        calls=calls,
        meta=meta,
        facts=facts,
        args_hash=digest,
        abstract=content.abstract(template, facts),
        content=text,
        body=tower.request_body(tool, calls),
        uploads=uploads,
        store=store,
    )


async def _duplicate(
    session: AsyncSession, bot_id: uuid.UUID, args_hash: str
) -> AssistantApproval | None:
    """同一份内容已有在途的单，或 72 小时内已经执行过。"""
    found: AssistantApproval | None = await session.scalar(
        select(AssistantApproval)
        .where(
            AssistantApproval.bot_id == bot_id,
            AssistantApproval.args_hash == args_hash,
            or_(
                AssistantApproval.status.in_(LIVE),
                and_(
                    AssistantApproval.status == "executed",
                    AssistantApproval.executed_at >= utcnow() - TTL,
                ),
            ),
        )
        .order_by(AssistantApproval.created_at.desc())
        .limit(1)
    )
    return found


async def _requester(
    session: AsyncSession, scope: Scope, cfg: tower.Config | None
) -> str | dict[str, Any]:
    """发起人在审批系统里的用户名（所有者的登录名）；不能提交时返回给助理的错误。"""
    if not is_assistant(scope.bot) or scope.bot.owner_id != scope.user_id:
        return {"error": "invalid_tool_or_arguments"}
    if cfg is None:
        return {"error": UNAVAILABLE, "hint": "这个部署没有配置审批系统，写操作不可用。"}
    owner = await session.get(User, scope.user_id)
    if owner is None or not owner.login_name:
        return {
            "error": "no_login_name",
            "hint": "所有者在平台里没有登录名，无法提交审批：请管理员在成员资料里补上。",
        }
    return str(owner.login_name)


def _apply(row: AssistantApproval, staged: Staged, cfg: tower.Config, caller: str) -> None:
    first = staged.calls[0]
    row.args_enc = staged.stored()
    row.args_hash = staged.args_hash
    row.request_hash = tower.request_hash(cfg, row.external_id, staged.body, caller)
    row.channel = staged.facts[0].channel
    row.chat_id = first["chat_id"]
    row.anchor_message_id = first["anchor_message_id"]
    row.thread_id = first["thread_id"]
    row.topic = staged.meta.topic or None
    row.expires_at = utcnow() + TTL
    row.notified_at = None
    row.has_files = bool(staged.uploads)


async def submit(
    session: AsyncSession,
    cipher: Cipher,
    scope: Scope,
    tool: str,
    arguments: dict[str, Any],
    *,
    cfg: tower.Config | None,
    files: FileRelay | None = None,
) -> dict[str, Any]:
    """暂存一次写操作并向 Tower 提交审批单；不执行。调用方负责提交事务。

    Args:
        scope: 所有者本人私聊那一轮的核验结果（`policy.capability_scope`）
        arguments: 工具参数，可带 `approval`（见 `Meta`）
        cfg: `tower.config(settings)`；None 表示部署没配审批系统
        files: 文件中转；带文件的写操作要用它读出中转文件，没有时这类操作不可用

    Returns:
        `{"status": "pending_approval", "approval_id": …}`；同一份内容已有在途或已执行的单时
        返回那一张（带 `duplicate`）；不能提交时是 `{"error": …, "hint": …}`

    Raises:
        PersonalError: 重新读会话时所有者的授权失效
    """
    requester = await _requester(session, scope, cfg)
    if isinstance(requester, dict):
        return requester
    assert cfg is not None
    staged = await _stage(session, cipher, scope, tool, arguments, files)
    if isinstance(staged, dict):
        return staged
    bot = scope.bot
    existing = await _duplicate(session, bot.id, staged.args_hash)
    if existing is not None:
        status = "pending_approval" if existing.status in LIVE else "already_executed"
        return {"status": status, "approval_id": str(existing.id), "duplicate": True}
    pending = await session.scalar(
        select(func.count())
        .select_from(AssistantApproval)
        .where(AssistantApproval.bot_id == bot.id, AssistantApproval.status == "pending")
    )
    if (pending or 0) >= MAX_PENDING:
        return {
            "error": "too_many_pending",
            "limit": MAX_PENDING,
            "hint": "同时待批的审批单已到上限：等本人处理，或用 approval_withdraw 撤回不再需要的。",
        }
    row_id = uuid.uuid4()
    try:
        # Tower 没收下就连同暂存的行一起回滚：不留一张谁也批不了的单。
        async with session.begin_nested():
            row = AssistantApproval(
                id=row_id,
                bot_id=bot.id,
                tool=tool,
                external_id=EXTERNAL_PREFIX + row_id.hex,
                origin_event_id=scope.event.id if scope.event is not None else None,
                session_key=scope.task.session_key or "",
            )
            _apply(row, staged, cfg, bot.bot_key)
            session.add(row)
            await session.flush()
            await _store_uploads(files, staged)
            sent = await tower.submit(
                cfg,
                external_id=row.external_id,
                requester=requester,
                caller=bot.bot_key,
                abstract=staged.abstract,
                content=staged.content,
                body=staged.body,
                expires_at=row.expires_at,
            )
            row.approval_id = sent.approval_id
    except tower.TowerError as exc:
        await _discard(staged)
        log.warning("assistant_approval_submit_failed", approval=str(row_id), code=exc.code)
        return _tower_failure(exc)
    except attachments.Unavailable:
        await _discard(staged)
        return dict(FILE_STORE_DOWN)
    return {"status": "pending_approval", "approval_id": str(row_id), "hint": PENDING_HINT}


@dataclass(frozen=True)
class Decided:
    """known=False：不是这里发起的单；queued：排上了执行任务或结果回合。"""

    known: bool
    queued: bool = False


async def deliver(session: AsyncSession, row: AssistantApproval, bot: Bot) -> bool:
    """把这张单的结局作为一轮投回助理会话；沿用提交那一轮的入站事件与会话键。"""
    if row.origin_event_id is None or bot.owner_id is None:
        return False
    if await session.get(InboundEvent, row.origin_event_id) is None:
        return False
    identity = await session.scalar(
        select(UserIdentity.platform_user_id)
        .where(UserIdentity.user_id == bot.owner_id, UserIdentity.platform == "feishu")
        .limit(1)
    )
    task = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind=APPROVAL_RESULT_KIND,
            user_id=bot.owner_id,
            session_key=row.session_key,
            inbound_event_id=row.origin_event_id,
            dedupe_key=f"assistant-approval:{row.id}:result:{row.status}",
            payload={
                "approval_id": str(row.id),
                "status": row.status,
                # 提交的那一轮可能还没结束：同一会话串行，等它收尾再开这一轮。
                "serialize_session": True,
                "bot_key": bot.bot_key,
                "platform_user_id": identity or "",
            },
        ),
    )
    return task is not None


async def decide(
    session: AsyncSession,
    *,
    approval_id: str,
    external_id: str,
    status: str,
    reason: str | None,
    decided_at: datetime | None,
) -> Decided:
    """把 Tower 的决定落到审批单上；回调与对账共用。调用方已校验字段并负责提交。

    只从 pending 往前走，所以重复送达、撤回之后迟到的批准都是无事发生。批准时只排执行任务，
    不在这里调飞书：回调要尽快应答，执行前还要重新读会话、核销令牌。
    """
    row: AssistantApproval | None = await session.scalar(
        select(AssistantApproval)
        .where(AssistantApproval.external_id == external_id)
        .with_for_update()
    )
    if row is None or row.approval_id not in (None, approval_id):
        return Decided(known=False)
    row.approval_id = approval_id
    if row.status != "pending":
        return Decided(known=True)
    row.decided_at = decided_at or utcnow()
    bot = await session.get(Bot, row.bot_id)
    if bot is None or not bot.enabled or sealed.is_locked(row):
        # 助理已停用，或数据密钥已销毁、参数读不出来：不执行，也没有会话可以投回。
        row.status, row.error_code = "withdrawn", "assistant_inactive"
        return Decided(known=True)
    note = clean_reason(reason) if reason else None
    row.reason_enc = note or None
    if status == "approved":
        row.status = "approved"
        task = await tasks.enqueue(
            session,
            NewTask(
                bot_id=bot.id,
                kind=APPROVAL_EXECUTE_KIND,
                user_id=bot.owner_id,
                dedupe_key=f"assistant-approval:{row.id}:execute",
                payload={"approval_id": str(row.id)},
            ),
        )
        return Decided(known=True, queued=task is not None)
    row.status = status
    return Decided(known=True, queued=await deliver(session, row, bot))


RESULT_LIMIT = 20000


def bounded_result(value: Any) -> dict[str, Any]:
    """执行结果存档：太大时只留一句说明，不把整份文档塞回会话。"""
    if len(json.dumps(value, ensure_ascii=False, default=str)) > RESULT_LIMIT:
        return {"truncated": True}
    return {"results": value}
