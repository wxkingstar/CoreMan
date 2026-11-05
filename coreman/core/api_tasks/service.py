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
from coreman.core.bus import outbox, tasks
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    ApiTask,
    Bot,
    BotAllowedUser,
    ChatLog,
    InboundEvent,
    PersonalAccessToken,
    Task,
    User,
    UserIdentity,
    UserReached,
)
from coreman.core.wecom.messages import InboundMessage, Sender, TextPart

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


async def _bot_for(session: AsyncSession, token: PersonalAccessToken, user: User, key: str) -> Bot:
    bot = await session.scalar(select(Bot).where(Bot.bot_key == key))
    if bot is None:
        raise TokenError(404, "AI 员工不存在")
    if bot.id not in token.bot_ids:
        raise TokenError(403, "这枚令牌没有授权驱动该 AI 员工")
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
    bot = await _bot_for(session, token, user, bot_key)
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
    chat_type = "single" if target == "p2p" else "group"
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
    message = InboundMessage(
        platform="feishu",
        bot_id=bot.id,
        kind="message",
        chat_type=chat_type,
        chat_id=chat_id,
        sender=Sender(platform_user_id=pid),
        message_id=f"{SYNTHETIC_PREFIX}{row.id}",
        mentions_bot=True,
        parts=[TextPart(text=prompt)],
        reply_context={"chat_id": chat_id, "chat_type": chat_type},
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
            payload=message.model_dump(mode="json"),
            reply_context=message.reply_context,
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
        payload={"markdown": trigger_text(user, token.name, prompt)},
    )
    task = await tasks.enqueue(
        session,
        tasks.NewTask(
            bot_id=bot.id,
            kind="chat",
            session_key=chat_id,
            user_id=user.id,
            inbound_event_id=event_id,
            dedupe_key=f"api-task:{row.id}",
            payload={
                "api_task_id": str(row.id),
                "origin": ORIGIN,
                "bot_key": bot.bot_key,
                "platform_user_id": pid,
                # 同一会话里有人的轮次在跑就排在后面，不打断人。
                "serialize_session": True,
            },
        ),
    )
    assert task is not None
    stored = await session.get(ApiTask, row.id)
    assert stored is not None
    stored.task_id = task.id
    await session.flush()
    return stored, True


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
        return "waiting_user" if log is not None and log.status == "ask_user" else "succeeded"
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
    finished = status not in ("queued", "running")
    conversation: dict[str, Any] = {"target": row.target}
    if row.alias:
        conversation["alias"] = row.alias
    return {
        "id": str(row.id),
        "request_id": row.request_id,
        "bot_key": bot.bot_key if bot else None,
        "status": status,
        "conversation": conversation,
        "reply": log.response_content if finished and log is not None else None,
        "error_code": (task.error_code if task else None) if status == "failed" else None,
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
