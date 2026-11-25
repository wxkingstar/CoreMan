"""API 任务：校验、在飞书会话里发起一轮、查询与取消。

API 这一轮和本人在飞书里发一条消息走同一条对话流水线：这里写一条合成的入站事件、在承接会话
里发一句「谁通过 API 发起」，再按那个会话入队一个 chat 任务。见
docs/superpowers/specs/2026-11-06-api-driven-tasks.md。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.api_tasks import SYNTHETIC_PREFIX, aliases
from coreman.core.api_tasks.tokens import TokenError
from coreman.core.assistants import sealed
from coreman.core.bus import outbox, tasks
from coreman.core.chat.chat_logs import LIMITS
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    ApiClient,
    ApiTask,
    Bot,
    BotAllowedUser,
    ChatLog,
    CredentialRequest,
    InboundEvent,
    PersonalAccessToken,
    Task,
    User,
    UserIdentity,
    UserReached,
)
from coreman.core.i18n.messages import MESSAGES
from coreman.core.wecom.messages import ImagePart, InboundMessage, Sender, TextPart

PROMPT_MAX = 20000
REQUEST_ID_MAX = 128
MAX_OPEN_PER_TOKEN = 5
PREVIEW = 500
ORIGIN = "api"


async def allowed_to_use(session: AsyncSession, bot: Bot, user_id: uuid.UUID) -> bool:
    """和在飞书里跟它说话同一个口径：白名单为空所有人可用，否则要在名单里。"""
    allowed = set(
        await session.scalars(select(BotAllowedUser.user_id).where(BotAllowedUser.bot_id == bot.id))
    )
    return not allowed or user_id in allowed


async def feishu_user_id(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    ident: UserIdentity | None = await session.scalar(
        select(UserIdentity).where(
            UserIdentity.user_id == user_id, UserIdentity.platform == "feishu"
        )
    )
    return ident.platform_user_id if ident is not None and ident.platform_user_id else None


def trigger_text(user: User, token_name: str, prompt: str) -> str:
    """承接会话里的第一句：让会话里的人知道这一轮从哪来、要做什么。"""
    body = prompt if len(prompt) <= PREVIEW else prompt[:PREVIEW] + f"…（共 {len(prompt)} 字）"
    quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in body.splitlines())
    who = user.display_name or user.login_name
    return f"**{who}** 通过 API 发起（令牌：{token_name}）\n{quoted}"


def client_trigger_text(user: User, client_name: str, prompt: str, images: int = 0) -> str:
    """业务系统代员工发起时私聊里的第一句：在哪个系统里发起、要做什么、附了几张图。"""
    body = prompt if len(prompt) <= PREVIEW else prompt[:PREVIEW] + f"…（共 {len(prompt)} 字）"
    quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in body.splitlines())
    who = user.display_name or user.login_name
    attached = f"（附 {images} 张图片）" if images else ""
    return f"**{who}** 在「{client_name}」里发起{attached}\n{quoted}"


async def _bot_for(
    session: AsyncSession, bot_ids: list[uuid.UUID], user: User, key: str, denied: str
) -> Bot:
    bot = await session.scalar(select(Bot).where(Bot.bot_key == key))
    if bot is None:
        raise TokenError(404, "AI 员工不存在")
    if bot.id not in bot_ids:
        raise TokenError(403, denied)
    if not bot.enabled:
        raise TokenError(409, "AI 员工已停用")
    if bot.platform != "feishu":
        raise TokenError(422, "目前只支持飞书 AI 员工")
    if not bot.api_access:
        raise TokenError(403, "该 AI 员工没有开启「允许 API 驱动」，请联系它的负责人")
    if not await allowed_to_use(session, bot, user.id):
        raise TokenError(403, "你不在该 AI 员工的可用名单里")
    return bot


async def _conversation(
    session: AsyncSession,
    bot: Bot,
    user: User,
    pid: str,
    conversation: dict[str, Any],
    cipher: Cipher,
) -> tuple[str, str, str | None]:
    """承接会话 → (target, chat_id, alias)。群要实时确认本人还在群里。"""
    target = conversation.get("target") or "p2p"
    if target == "p2p":
        reached = await session.get(UserReached, (bot.id, user.id))
        if reached is None or not reached.platform_chat_id:
            raise TokenError(409, f"你还没有在飞书私聊过「{bot.name}」，请先私聊它一次再发起")
        return "p2p", reached.platform_chat_id, None
    if target != "chat":
        raise TokenError(422, "conversation.target 只能是 p2p 或 chat")
    alias = str(conversation.get("alias") or "").strip()
    if not alias:
        raise TokenError(422, "在群里承接需要提供 conversation.alias")
    row = await aliases.by_alias(session, bot.id, alias)
    if row is None:
        raise TokenError(404, f"「{bot.name}」没有名为「{alias}」的群别名")
    from coreman.core.chat.human_collaboration import in_group

    if not await in_group(bot, row.chat_id, pid, cipher):
        raise TokenError(403, f"无法确认你在别名「{alias}」对应的群里")
    return "chat", row.chat_id, alias


async def _replay(
    session: AsyncSession, token: PersonalAccessToken, request_id: str, fingerprint: tuple[Any, ...]
) -> ApiTask | None:
    row: ApiTask | None = await session.scalar(
        select(ApiTask).where(ApiTask.token_id == token.id, ApiTask.request_id == request_id)
    )
    if row is None:
        return None
    if (row.bot_id, row.prompt, row.target, row.alias) != fingerprint:
        raise TokenError(409, "request_id 已用于内容不同的任务")
    return row


async def create(
    session: AsyncSession,
    *,
    token: PersonalAccessToken,
    user: User,
    bot_key: str,
    prompt: str,
    request_id: str | None,
    conversation: dict[str, Any],
    cipher: Cipher,
) -> tuple[ApiTask, bool]:
    """发起一轮；返回 (任务, 是否新建)。调用方负责提交。"""
    if not prompt.strip() or len(prompt) > PROMPT_MAX:
        raise TokenError(422, f"prompt 须非空且不超过 {PROMPT_MAX} 个字符")
    bot = await _bot_for(session, token.bot_ids, user, bot_key, "这枚令牌没有授权驱动该 AI 员工")
    target = conversation.get("target") or "p2p"
    alias = (str(conversation.get("alias") or "").strip() or None) if target == "chat" else None
    if request_id is not None:
        replay = await _replay(session, token, request_id, (bot.id, prompt, target, alias))
        if replay is not None:
            return replay, False
    pid = await feishu_user_id(session, user.id)
    if pid is None:
        raise TokenError(409, "你的账号没有绑定飞书身份，无法在飞书会话里承接")
    # 群成员要问飞书（最长 10 秒），放在锁令牌之前，免得同一枚令牌的请求都排在它后面。
    target, chat_id, alias = await _conversation(session, bot, user, pid, conversation, cipher)
    # 锁住令牌行再数未结束的任务：同一枚令牌的并发发起在这里排队，名额不会被挤破。
    await session.execute(
        select(PersonalAccessToken.id).where(PersonalAccessToken.id == token.id).with_for_update()
    )
    if request_id is not None:
        # 同一个 request_id 的并发重试：先到的那个可能刚建完，拿到锁后再回放一次，
        # 不能因为它占了名额就给重试回 429。
        replay = await _replay(session, token, request_id, (bot.id, prompt, target, alias))
        if replay is not None:
            return replay, False
    open_count = await session.scalar(
        select(func.count())
        .select_from(ApiTask)
        .join(Task, Task.id == ApiTask.task_id)
        .where(
            ApiTask.token_id == token.id,
            Task.status.in_(tasks.OPEN),
            Task.cancel_requested_at.is_(None),
        )
    )
    if (open_count or 0) >= MAX_OPEN_PER_TOKEN:
        raise TokenError(429, f"这枚令牌已有 {MAX_OPEN_PER_TOKEN} 个任务未结束，请稍后再发起")
    row = ApiTask(
        id=uuid.uuid4(),
        token_id=token.id,
        user_id=user.id,
        bot_id=bot.id,
        request_id=request_id,
        prompt=prompt,
        target=target,
        alias=alias,
        chat_id=chat_id,
    )
    # 同一个 request_id 并发发起：唯一键挡住第二个，回放第一个。
    stmt = (
        insert(ApiTask)
        .values(
            id=row.id,
            token_id=row.token_id,
            user_id=row.user_id,
            bot_id=row.bot_id,
            request_id=row.request_id,
            prompt=row.prompt,
            target=row.target,
            alias=row.alias,
            chat_id=row.chat_id,
        )
        .on_conflict_do_nothing(constraint="uq_api_tasks_token_request")
        .returning(ApiTask.id)
    )
    if (await session.execute(stmt)).first() is None:
        assert request_id is not None
        replay = await _replay(session, token, request_id, (bot.id, prompt, target, alias))
        assert replay is not None
        return replay, False
    return await _start_turn(
        session,
        row,
        bot=bot,
        user=user,
        pid=pid,
        trigger=trigger_text(user, token.name, prompt),
        session_key=chat_id,
        # 同一会话里有人的轮次在跑就排在后面，不打断人。
        extra={"serialize_session": True},
    ), True


async def _start_turn(
    session: AsyncSession,
    row: ApiTask,
    *,
    bot: Bot,
    user: User,
    pid: str,
    trigger: str,
    session_key: str,
    extra: dict[str, Any],
    images: list[uuid.UUID] | None = None,
) -> ApiTask:
    """任务行已写入：写合成入站事件、在承接会话里发触发消息、入队 chat 任务。"""
    target, chat_id, prompt = row.target, row.chat_id, row.prompt
    chat_type = "single" if target == "p2p" else "group"
    message = InboundMessage(
        platform="feishu",
        bot_id=bot.id,
        kind="message",
        chat_type=chat_type,
        chat_id=chat_id,
        sender=Sender(platform_user_id=pid),
        message_id=f"{SYNTHETIC_PREFIX}{row.id}",
        mentions_bot=True,
        parts=[
            TextPart(text=prompt),
            # 图片从上传记录里取（`uploads.UploadFetcher`），ref 只认这个任务自己的合成消息。
            *(
                ImagePart(ref={"message_id": f"{SYNTHETIC_PREFIX}{row.id}", "file_key": str(i)})
                for i in images or []
            ),
        ],
        reply_context={"chat_id": chat_id, "chat_type": chat_type},
    )
    event = await sealed.seal_values(
        session,
        InboundEvent,
        bot.id,
        {"payload": message.model_dump(mode="json"), "reply_context": message.reply_context},
    )
    event_id = await session.scalar(
        insert(InboundEvent)
        .values(
            bot_id=bot.id,
            platform="feishu",
            platform_msg_id=message.message_id,
            kind="message",
            chat_type=chat_type,
            chat_id=chat_id,
            sender_platform_user_id=pid,
            sender_open_id=None,
            payload=event["payload"],
            reply_context=event["reply_context"],
        )
        .returning(InboundEvent.id)
    )
    target_info: dict[str, Any] = {"chat_id": chat_id}
    if target == "p2p":
        # 私聊带上收件人：网关发送前复核这个私聊还是本人的。
        target_info |= {"recipient_user_id": str(user.id), "recipient_platform_user_id": pid}
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=f"api-task:{row.id}:trigger",
        target=target_info,
        payload={"markdown": trigger},
    )
    task = await tasks.enqueue(
        session,
        tasks.NewTask(
            bot_id=bot.id,
            kind="chat",
            session_key=session_key,
            user_id=user.id,
            inbound_event_id=event_id,
            dedupe_key=f"api-task:{row.id}",
            payload={
                "api_task_id": str(row.id),
                "origin": ORIGIN,
                "bot_key": bot.bot_key,
                "platform_user_id": pid,
                **extra,
            },
        ),
    )
    assert task is not None
    stored = await session.get(ApiTask, row.id)
    assert stored is not None
    stored.task_id = task.id
    await session.flush()
    return stored


# ---- 业务系统代员工发起（API 调用方 + `tasks` 接口组） ----------------------------------------

CLIENT_SCOPE = "tasks"
MAX_OPEN_PER_CLIENT_USER = 20
# 独立会话的会话键：每个任务一个，不读不写员工私聊的会话，私聊里的消息也替代不了它。
CLIENT_SESSION_PREFIX = "api:"


def client_session_key(api_task_id: uuid.UUID) -> str:
    return f"{CLIENT_SESSION_PREFIX}{api_task_id}"


async def resolve_member(session: AsyncSession, subject: str) -> User:
    """邮箱前缀 → 员工，和业务令牌的 sub 同一口径：认不出唯一的一个人就拒绝，不退回登录名。"""
    from coreman.core.auth.system_access import (
        SubjectUnavailable,
        token_subject,
        user_for_subject,
    )

    user = await user_for_subject(session, subject)
    if user is not None:
        try:
            # 反查只看在职的人；和停用账号撞前缀同样认不出（签业务令牌时也会拒签）。
            if await token_subject(session, user) == subject.strip().lower():
                return user
        except SubjectUnavailable:
            pass
    raise TokenError(403, f"无法按邮箱前缀「{subject[:64]}」认出唯一一位在职员工")


async def _client_replay(
    session: AsyncSession,
    client: ApiClient,
    request_id: str,
    fingerprint: tuple[Any, ...],
) -> ApiTask | None:
    row: ApiTask | None = await session.scalar(
        select(ApiTask).where(
            ApiTask.client_key == client.app_key, ApiTask.request_id == request_id
        )
    )
    if row is None:
        return None
    if (row.user_id, row.bot_id, row.prompt, list(row.image_ids or [])) != fingerprint:
        raise TokenError(409, "request_id 已用于内容不同的任务")
    return row


async def create_for_client(
    session: AsyncSession,
    *,
    client: ApiClient,
    subject: str,
    bot_key: str,
    prompt: str,
    request_id: str | None,
    images: list[uuid.UUID] | None = None,
) -> tuple[ApiTask, bool]:
    """业务系统代员工发起一轮：独立会话，结果发在员工和 AI 员工的私聊里。调用方负责提交。"""
    if not prompt.strip() or len(prompt) > PROMPT_MAX:
        raise TokenError(422, f"prompt 须非空且不超过 {PROMPT_MAX} 个字符")
    user = await resolve_member(session, subject)
    bot = await _bot_for(session, client.bot_ids, user, bot_key, "调用方没有授权驱动该 AI 员工")
    wanted = list(dict.fromkeys(images or []))
    fingerprint = (user.id, bot.id, prompt, wanted)
    if request_id is not None:
        replay = await _client_replay(session, client, request_id, fingerprint)
        if replay is not None:
            return replay, False
    pid = await feishu_user_id(session, user.id)
    if pid is None:
        raise TokenError(409, "这位员工的账号没有绑定飞书身份，无法在飞书私聊里推送结果")
    reached = await session.get(UserReached, (bot.id, user.id))
    if reached is None or not reached.platform_chat_id:
        raise TokenError(409, f"这位员工还没有在飞书私聊过「{bot.name}」，请先私聊它一次再发起")
    # 锁住调用方行再数未结束的任务：同一调用方的并发发起在这里排队，名额不会被挤破。
    await session.execute(
        select(ApiClient.app_key).where(ApiClient.app_key == client.app_key).with_for_update()
    )
    if request_id is not None:
        replay = await _client_replay(session, client, request_id, fingerprint)
        if replay is not None:
            return replay, False
    open_count = await session.scalar(
        select(func.count())
        .select_from(ApiTask)
        .join(Task, Task.id == ApiTask.task_id)
        .where(
            ApiTask.client_key == client.app_key,
            ApiTask.user_id == user.id,
            Task.status.in_(tasks.OPEN),
            Task.cancel_requested_at.is_(None),
        )
    )
    if (open_count or 0) >= MAX_OPEN_PER_CLIENT_USER:
        raise TokenError(429, f"这位员工已有 {MAX_OPEN_PER_CLIENT_USER} 个任务未结束，请稍后再发起")
    from coreman.core.api_tasks import uploads

    image_ids = await uploads.claim(session, client_key=client.app_key, user_id=user.id, ids=wanted)
    row = ApiTask(
        id=uuid.uuid4(),
        client_key=client.app_key,
        image_ids=image_ids,
        user_id=user.id,
        bot_id=bot.id,
        request_id=request_id,
        prompt=prompt,
        target="p2p",
        chat_id=reached.platform_chat_id,
    )
    session.add(row)
    await session.flush()
    return await _start_turn(
        session,
        row,
        bot=bot,
        user=user,
        pid=pid,
        trigger=client_trigger_text(user, client.name, prompt, len(image_ids)),
        session_key=client_session_key(row.id),
        extra={"api_client": client.app_key},
        images=image_ids,
    ), True


LOG_FAILED = ("error", "timeout", "failed")
REPLY_LIMIT = LIMITS["response_content"]


# 飞书卡片的完成标记（「✅ 回复已生成」等，各语言一份）：给人看的，不返回给调用的程序。
DONE_SUFFIXES = tuple(
    {table["done_suffix"] for table in MESSAGES.values() if "done_suffix" in table}
)
# 业务系统发起的任务在私聊里带的尾注（「不在当前对话上下文中」）：同样只给人看。
PUSH_FOOTERS = tuple(
    {table["api_push_footer"] for table in MESSAGES.values() if "api_push_footer" in table}
)


def clean_reply(text: str | None) -> str | None:
    if text is None:
        return None
    for footer in PUSH_FOOTERS:
        if text.endswith(footer):
            text = text[: -len(footer)]
            break
    for suffix in DONE_SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _status(task: Task | None, log: ChatLog | None) -> str:
    if task is None:
        return "expired" if log is None else ("failed" if log.status != "success" else "succeeded")
    if task.status == "queued" and task.cancel_requested_at is not None:
        # 还没被认领就取消了：worker 认领时直接结掉，不会再执行。
        return "cancelled"
    if task.status in ("queued", "claimed"):
        return "queued"
    if task.status == "running":
        return "running"
    if task.status == "succeeded":
        # 对话记录在任务写完终态之后还可能被改成失败（例如协作在收尾时被拒）：以它为准。
        if log is not None and log.status == "ask_user":
            return "waiting_user"
        if log is not None and log.status in LOG_FAILED:
            return "failed"
        if log is not None and log.status == "stopped":
            return "cancelled"
        return "succeeded"
    if task.status == "cancelled":
        superseded = "superseded" in (task.error_code, task.cancel_reason)
        return "superseded" if superseded else "cancelled"
    return "failed"


async def view(session: AsyncSession, row: ApiTask) -> dict[str, Any]:
    task = await session.get(Task, row.task_id, populate_existing=True) if row.task_id else None
    log: ChatLog | None = (
        await session.scalar(
            select(ChatLog)
            .where(ChatLog.task_id == row.task_id, ChatLog.status != "running")
            .order_by(ChatLog.id.desc())
            .limit(1)
        )
        if row.task_id
        else None
    )
    bot = await session.get(Bot, row.bot_id)
    status = _status(task, log)
    if status == "succeeded" and row.task_id is not None:
        # 这一轮向本人发了凭证表单就收尾了：要等人在飞书里填完才会继续。
        asked = await session.scalar(
            select(CredentialRequest.id)
            .where(
                CredentialRequest.origin_task_id == row.task_id,
                CredentialRequest.status.in_(("open", "submitted")),
            )
            .limit(1)
        )
        if asked is not None:
            status = "waiting_user"
    finished = status not in ("queued", "running")
    reply = clean_reply(log.response_content) if finished and log is not None else None
    conversation: dict[str, Any] = {"target": row.target}
    if row.alias:
        conversation["alias"] = row.alias
    who: dict[str, Any] = {"conversation": conversation}
    if row.client_key is not None:
        # 业务系统代员工发起的：回报是哪位员工，承接固定是他的私聊（独立会话）。
        from coreman.core.api_tasks import writes

        user = await session.get(User, row.user_id)
        who = {
            "user": (user.email or "").split("@", 1)[0].lower() if user else None,
            # 写操作要员工在私聊里点确认卡：任务结束不等于数据已经写进业务系统。
            "writes": await writes.view(session, row),
        }
    return {
        "id": str(row.id),
        "request_id": row.request_id,
        "bot_key": bot.bot_key if bot else None,
        "status": status,
        **who,
        "reply": reply,
        # 对话记录最多存 50000 字：超长的回复在这里被截断，完整内容在飞书会话里。
        "reply_truncated": reply is not None and len(reply) >= REPLY_LIMIT,
        "error_code": (
            (log.error_code if log else None) or (task.error_code if task else None)
            if status == "failed"
            else None
        ),
        "error_message": (
            (log.error_message if log else None) or (task.error_message if task else None)
            if status == "failed"
            else None
        ),
        "created_at": row.created_at.isoformat(),
        "finished_at": task.finished_at.isoformat() if task and task.finished_at else None,
    }


async def cancel(session: AsyncSession, row: ApiTask) -> bool:
    """只对未结束的任务生效。"""
    if row.task_id is None:
        return False
    return await tasks.request_cancel(session, row.task_id, "api_cancel")
