"""API 调用方的群承接模式：以配置里的服务账号身份，在群的主会话里跑一轮。

调用方为某个 AI 员工配了群承接（`api_client_group_routes`），驱动它时就只走这条路：请求里
不能指定员工，执行身份锁死为配置里的服务账号；不要求飞书身份、私聊过和使用白名单。结果直接
发在群里、进群主会话的上下文。写操作的确认卡也发到群里，由群里有权限的人点，按点卡人身份
执行（见 `writes.card_clicker`）。
见 docs/superpowers/specs/2026-11-25-api-client-group-delivery.md。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.api_tasks.tokens import TokenError
from coreman.core.db.models import ApiClient, ApiClientGroupRoute, ApiTask, Bot, Task, User
from coreman.core.prompting.system_prompt import Speaker

# 任务载荷里的标记：这一轮（及其续接轮）是群承接模式发起的。
PAYLOAD_KEY = "api_group"
# 每个（AI 员工, 群）里群模式发起、还在排队的任务最多这么多条；真人提问不计。
QUEUE_MAX = 5
QUEUE_FULL = "queue_full"
SPEAKER_PREFIX = "api-group:"
PREVIEW = 500


def is_group_payload(payload: dict[str, Any] | None) -> bool:
    payload = payload or {}
    return payload.get(PAYLOAD_KEY) is True and bool(payload.get("api_client"))


def is_group_row(row: ApiTask) -> bool:
    return row.client_key is not None and row.target == "chat"


def service_usable(user: User | None) -> bool:
    return user is not None and user.status == "active" and user.source == "service"


async def routes_for(
    session: AsyncSession, client_key: str, bot_id: uuid.UUID
) -> list[ApiClientGroupRoute]:
    return list(
        await session.scalars(
            select(ApiClientGroupRoute)
            .where(
                ApiClientGroupRoute.client_key == client_key,
                ApiClientGroupRoute.bot_id == bot_id,
            )
            .order_by(ApiClientGroupRoute.created_at, ApiClientGroupRoute.id)
        )
    )


async def route_of(session: AsyncSession, row: ApiTask) -> ApiClientGroupRoute | None:
    """任务发起时用的那条配置，仍在且仍绑同一个服务账号；否则 None。"""
    if not is_group_row(row):
        return None
    route: ApiClientGroupRoute | None = await session.scalar(
        select(ApiClientGroupRoute).where(
            ApiClientGroupRoute.client_key == row.client_key,
            ApiClientGroupRoute.bot_id == row.bot_id,
            ApiClientGroupRoute.chat_id == row.chat_id,
        )
    )
    if route is None or route.service_user_id != row.user_id:
        return None
    return route


def speaker(route: ApiClientGroupRoute, user: User) -> Speaker:
    """群模式这一轮的发言者：服务账号，没有飞书身份（同告警回合，不伪造）。"""
    return Speaker(f"{SPEAKER_PREFIX}{route.id}", user.id, user.login_name, user.display_name)


def trigger_text(client_name: str, prompt: str, images: int = 0) -> str:
    """群里的第一句：在哪个系统里发起、要做什么、附了几张图。"""
    body = prompt if len(prompt) <= PREVIEW else prompt[:PREVIEW] + f"…（共 {len(prompt)} 字）"
    quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in body.splitlines())
    attached = f"（附 {images} 张图片）" if images else ""
    return f"在「{client_name}」里发起{attached}\n{quoted}"


def _pick(routes: list[ApiClientGroupRoute], chat_id: str | None) -> ApiClientGroupRoute:
    if chat_id is None:
        if len(routes) > 1:
            raise TokenError(422, "这个 AI 员工配了多个承接群，请用 chat_id 指定在哪个群发起")
        return routes[0]
    found = next((route for route in routes if route.chat_id == chat_id), None)
    if found is None:
        raise TokenError(404, "调用方没有为这个 AI 员工配置该群的群承接")
    return found


async def queued(session: AsyncSession, bot_id: uuid.UUID, chat_id: str) -> int:
    """这个群里群模式发起、还在排队（不含正在执行）的任务数。"""
    count = await session.scalar(
        select(func.count())
        .select_from(ApiTask)
        .join(Task, Task.id == ApiTask.task_id)
        .where(
            ApiTask.client_key.is_not(None),
            ApiTask.target == "chat",
            ApiTask.bot_id == bot_id,
            ApiTask.chat_id == chat_id,
            Task.status == "queued",
            Task.cancel_requested_at.is_(None),
        )
    )
    return int(count or 0)


async def create(
    session: AsyncSession,
    *,
    client: ApiClient,
    bot: Bot,
    routes: list[ApiClientGroupRoute],
    subject: str | None,
    prompt: str,
    request_id: str | None,
    chat_id: str | None,
    images: list[uuid.UUID] | None,
) -> tuple[ApiTask, bool]:
    """群模式发起一轮；返回 (任务, 是否新建)。调用方负责提交。"""
    from coreman.core.api_tasks import service

    if subject is not None:
        raise TokenError(
            422,
            "这个调用方驱动该 AI 员工走群承接模式，执行身份固定为配置的服务账号，请求里不能带 user",
        )
    route = _pick(routes, chat_id)
    user = await session.get(User, route.service_user_id, populate_existing=True)
    if user is None or not service_usable(user):
        raise TokenError(409, "群承接配置的服务账号不存在、已停用或不是服务账号")
    await service.check_bot(session, client.bot_ids, bot, None, "调用方没有授权驱动该 AI 员工")
    # 和私聊模式同一口径：图片按顺序去重后算进幂等指纹和同内容去重。
    wanted = list(dict.fromkeys(images or []))
    fingerprint: tuple[Any, ...] = (user.id, bot.id, prompt, wanted)
    if request_id is not None:
        replay = await service.client_replay(session, client, request_id, fingerprint)
        if replay is not None:
            return replay, False
    await session.execute(
        select(ApiClient.app_key).where(ApiClient.app_key == client.app_key).with_for_update()
    )
    # 同一个群的排队数跨调用方统计：按（AI 员工, 群）串行数名额，不会被并发挤破。
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"api-group:{bot.id}:{route.chat_id}"},
    )
    if request_id is not None:
        replay = await service.client_replay(session, client, request_id, fingerprint)
        if replay is not None:
            return replay, False
    same = await service.open_duplicate(session, client, user.id, bot.id, prompt, wanted)
    if same is not None and same.chat_id == route.chat_id and is_group_row(same):
        return same, False
    if await queued(session, bot.id, route.chat_id) >= QUEUE_MAX:
        raise TokenError(
            429,
            f"这个群里已有 {QUEUE_MAX} 条 API 任务在排队，这条没有接收，请稍后再发起",
            QUEUE_FULL,
        )
    from coreman.core.api_tasks import uploads

    # 上传归属和私聊模式同一口径：(调用方, 执行身份)，这里的执行身份是配置里的服务账号。
    image_ids = await uploads.claim(session, client_key=client.app_key, user_id=user.id, ids=wanted)
    row = ApiTask(
        id=uuid.uuid4(),
        client_key=client.app_key,
        image_ids=image_ids,
        user_id=user.id,
        bot_id=bot.id,
        request_id=request_id,
        prompt=prompt,
        target="chat",
        chat_id=route.chat_id,
    )
    session.add(row)
    await session.flush()
    return await service.start_turn(
        session,
        row,
        bot=bot,
        user=user,
        pid="",
        trigger=trigger_text(client.name, prompt, len(image_ids)),
        session_key=route.chat_id,
        # 群里有人的轮次在跑就排在后面，不打断人。
        extra={"api_client": client.app_key, PAYLOAD_KEY: True, "serialize_session": True},
        images=image_ids,
    ), True


async def upload_owner(
    session: AsyncSession, client: ApiClient, bot_key: str, chat_id: str | None
) -> User:
    """群承接模式上传图片的归属：这个调用方为这个 AI 员工配的（那个群的）服务账号。

    和发起时认领用同一个执行身份，别的员工名下的上传认领不了。"""
    from coreman.core.api_tasks import service

    bot = await service.bot_by_key(session, bot_key)
    routes = await routes_for(session, client.app_key, bot.id)
    if not routes:
        raise TokenError(422, "调用方没有为这个 AI 员工配置群承接，上传图片请带 user")
    route = _pick(routes, chat_id)
    user = await session.get(User, route.service_user_id)
    if user is None or not service_usable(user):
        raise TokenError(409, "群承接配置的服务账号不存在、已停用或不是服务账号")
    return user


async def options(session: AsyncSession, bot_id: uuid.UUID | None) -> dict[str, Any]:
    """建配置时可选的群和服务账号：该 AI 员工告警来源里已有的，以及所有在用的服务账号。"""
    from coreman.core.db.models import BotAlertSource

    sources = (
        list(
            await session.scalars(
                select(BotAlertSource)
                .where(BotAlertSource.bot_id == bot_id)
                .order_by(BotAlertSource.created_at)
            )
        )
        if bot_id is not None
        else []
    )
    users = list(
        await session.scalars(
            select(User)
            .where(User.source == "service", User.status == "active")
            .order_by(User.login_name)
        )
    )
    return {
        "alert_sources": [
            {"chat_id": s.chat_id, "service_user_id": str(s.service_user_id)} for s in sources
        ],
        "service_users": [
            {"id": str(u.id), "login_name": u.login_name, "display_name": u.display_name}
            for u in users
        ],
    }
