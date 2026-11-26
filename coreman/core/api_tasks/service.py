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
from coreman.core.timeutils import aware_utc, utcnow
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
    bot = await bot_by_key(session, key)
    return await check_bot(session, bot_ids, bot, user, denied)


async def bot_by_key(session: AsyncSession, key: str) -> Bot:
    bot = await session.scalar(select(Bot).where(Bot.bot_key == key))
    if bot is None:
        raise TokenError(404, "AI 员工不存在")
    return bot


async def check_bot(
    session: AsyncSession, bot_ids: list[uuid.UUID], bot: Bot, user: User | None, denied: str
) -> Bot:
    """发起方和 AI 员工两边都放行。`user` 为 None（群承接模式）时不看使用白名单。"""
    if bot.id not in bot_ids:
        raise TokenError(403, denied)
    if not bot.enabled:
        raise TokenError(409, "AI 员工已停用")
    if bot.platform != "feishu":
        raise TokenError(422, "目前只支持飞书 AI 员工")
    if not bot.api_access:
        raise TokenError(403, "该 AI 员工没有开启「允许 API 驱动」，请联系它的负责人")
    if user is not None and not await allowed_to_use(session, bot, user.id):
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
    return await start_turn(
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


async def start_turn(
    session: AsyncSession,
    row: ApiTask,
    *,
    bot: Bot,
    user: User,
    pid: str,
    trigger: str | None,
    session_key: str,
    extra: dict[str, Any],
    images: list[uuid.UUID] | None = None,
) -> ApiTask:
    """任务行已写入：写合成入站事件、在承接会话里发触发消息（`trigger` 为 None 时不发）、
    入队 chat 任务。"""
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
            sender_platform_user_id=pid or None,
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
    if trigger is not None:
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
# 静默执行的业务系统任务（含续接轮）在 chat 任务载荷里的标记：这一轮不往飞书发任何东西。
SILENT_KEY = "api_silent"
# 静默执行的一轮想向员工提问、发表单（会进入 waiting_user）：按失败结束，用这个错误码。
SILENT_ASK_CODE = "user_input_required"
SILENT_ASK_MESSAGE = "静默执行时不能向员工提问或请员工填写，AI 员工需要的信息不够"
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


# 一次请求里 `allowed_writes` 最多几项。
ALLOWED_WRITES_MAX = 100
WRITE_REF_MAX = 200


def write_ref(system_key: str, operation_id: str) -> str:
    """写操作在调用方静默清单和 `allowed_writes` 里的写法。"""
    return f"{system_key}.{operation_id}"


def parse_write_ref(raw: str) -> str:
    """`<业务系统标识>.<operationId>`：两边都不能为空，按第一个点拆开；不合格就 422。"""
    ref = raw.strip()
    system_key, dot, operation_id = ref.partition(".")
    if not dot or not system_key or not operation_id or len(ref) > WRITE_REF_MAX:
        raise TokenError(422, f"「{ref[:80]}」格式不对，应为 <业务系统标识>.<operationId>")
    return ref


def normalize_writes(raw: list[str] | None) -> list[str] | None:
    """请求里的 `allowed_writes`：去重保序。不带是 None（不限）；带了就不能为空。

    空列表不参与签名，和不带签出来一样：允许它表示「一个都不许写」，中间人删掉它就成了不限，
    所以直接拒绝。"""
    if raw is None:
        return None
    if not raw:
        raise TokenError(422, "allowed_writes 不能为空列表；不限制写操作时不要带这个字段")
    if len(raw) > ALLOWED_WRITES_MAX:
        raise TokenError(422, f"allowed_writes 最多 {ALLOWED_WRITES_MAX} 项")
    return list(dict.fromkeys(parse_write_ref(item) for item in raw))


def writes_key(allowed: list[str] | None) -> tuple[str, ...] | None:
    """幂等与同内容去重时比较 `allowed_writes`：不看顺序。"""
    return None if allowed is None else tuple(sorted(set(allowed)))


# 要求静默却回退成普通模式的原因（任务视图 `silent_fallback.reason`）。
SILENT_FALLBACK_NOT_REGISTERED = "not_registered"


def check_silent(client: ApiClient, silent: bool, allowed: list[str] | None) -> list[str] | None:
    """静默执行要列明写操作。返回没登记进调用方静默清单的项：有就整个任务回退成普通模式
    （发私聊、写操作发确认卡），不按项拆成部分静默；None 表示没要求静默或全部登记过。"""
    if not silent:
        return None
    if not allowed:
        raise TokenError(422, "silent 为 true 时必须带 allowed_writes，列出这次允许执行的写操作")
    registered = set(client.silent_writes or [])
    missing = [ref for ref in allowed if ref not in registered]
    return missing or None


def fingerprint_of(row: ApiTask) -> tuple[Any, ...]:
    # 比较的是请求内容：要求了静默但回退了的任务，仍按 silent=true 比。
    return (
        row.user_id,
        row.bot_id,
        row.prompt,
        list(row.image_ids or []),
        bool(row.silent) or row.silent_unregistered is not None,
        writes_key(row.allowed_writes),
    )


async def client_replay(
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
    if fingerprint_of(row) != fingerprint:
        raise TokenError(409, "request_id 已用于内容不同的任务")
    return row


async def open_duplicate(
    session: AsyncSession, client: ApiClient, fingerprint: tuple[Any, ...]
) -> ApiTask | None:
    """同样内容（员工、AI 员工、prompt、图片、静默与写操作范围）还没结束的任务。"""
    user_id, bot_id, prompt = fingerprint[:3]
    rows = await session.scalars(
        select(ApiTask)
        .join(Task, Task.id == ApiTask.task_id)
        .where(
            ApiTask.client_key == client.app_key,
            ApiTask.user_id == user_id,
            ApiTask.bot_id == bot_id,
            ApiTask.prompt == prompt,
            Task.status.in_(tasks.OPEN),
            Task.cancel_requested_at.is_(None),
        )
        .order_by(ApiTask.created_at)
    )
    return next((row for row in rows if fingerprint_of(row) == fingerprint), None)


async def create_for_client(
    session: AsyncSession,
    *,
    client: ApiClient,
    subject: str | None,
    bot_key: str,
    prompt: str,
    request_id: str | None,
    images: list[uuid.UUID] | None = None,
    chat_id: str | None = None,
    silent: bool = False,
    allowed_writes: list[str] | None = None,
) -> tuple[ApiTask, bool]:
    """业务系统发起一轮。调用方负责提交。

    调用方为这个 AI 员工配了群承接，就以配置的服务账号在群里跑（`groups.create`）；否则代员工
    发起：独立会话，结果发在员工和 AI 员工的私聊里。`silent` 时什么都不往私聊发，结果只经查询
    接口返回（见 docs/superpowers/specs/2026-11-26-api-tasks-silent-writes.md）。"""
    if not prompt.strip() or len(prompt) > PROMPT_MAX:
        raise TokenError(422, f"prompt 须非空且不超过 {PROMPT_MAX} 个字符")
    from coreman.core.api_tasks import groups

    allowed = normalize_writes(allowed_writes)
    target_bot = await bot_by_key(session, bot_key)
    routes = await groups.routes_for(session, client.app_key, target_bot.id)
    if routes:
        if silent:
            raise TokenError(
                422, "这个 AI 员工走群承接模式：结果本来就发在群里，不支持 silent", "silent_group"
            )
        return await groups.create(
            session,
            client=client,
            bot=target_bot,
            routes=routes,
            subject=subject,
            prompt=prompt,
            request_id=request_id,
            chat_id=chat_id,
            images=images,
            allowed_writes=allowed,
        )
    if chat_id is not None:
        raise TokenError(422, "调用方没有为这个 AI 员工配置群承接，不能指定 chat_id")
    if subject is None:
        raise TokenError(422, "需要 user（员工邮箱前缀）")
    user = await resolve_member(session, subject)
    bot = await check_bot(session, client.bot_ids, target_bot, user, "调用方没有授权驱动该 AI 员工")
    unregistered = check_silent(client, silent, allowed)
    wanted = list(dict.fromkeys(images or []))
    fingerprint = (user.id, bot.id, prompt, wanted, silent, writes_key(allowed))
    if request_id is not None:
        replay = await client_replay(session, client, request_id, fingerprint)
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
        replay = await client_replay(session, client, request_id, fingerprint)
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
    same = await open_duplicate(session, client, fingerprint)
    if same is not None:
        # 同样的内容上一条还没做完（排队、等额度、执行中）：不再建一条。
        return same, False
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
        silent=silent and unregistered is None,
        allowed_writes=allowed,
        silent_unregistered=unregistered,
    )
    session.add(row)
    await session.flush()
    return await start_turn(
        session,
        row,
        bot=bot,
        user=user,
        pid=pid,
        # 静默：私聊里连「在某系统里发起」这句也不发。
        trigger=None
        if row.silent
        else client_trigger_text(user, client.name, prompt, len(image_ids)),
        session_key=client_session_key(row.id),
        extra={"api_client": client.app_key, **({SILENT_KEY: True} if row.silent else {})},
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
        # 业务系统发起的：私聊模式回报是哪位员工（承接固定是他的私聊、独立会话），群承接模式
        # 回报是哪个群。
        from coreman.core.api_tasks import writes

        user = await session.get(User, row.user_id)
        waiting = (
            status == "queued"
            and row.resume_at is not None
            and task is not None
            and task.run_after is not None
            and aware_utc(task.run_after) > utcnow()
        )
        if waiting:
            status = "waiting_quota"
        from coreman.core.api_tasks import groups

        identity: dict[str, Any] = (
            # 群承接模式：没有员工，回报在哪个群里承接。
            {"conversation": {"target": "chat", "chat_id": row.chat_id}}
            if groups.is_group_row(row)
            else {"user": (user.email or "").split("@", 1)[0].lower() if user else None}
        )
        who = {
            **identity,
            # 运行时额度用完、排队等恢复时的预计重跑时间。
            "resume_at": row.resume_at.isoformat() if waiting and row.resume_at else None,
            # 写操作要员工在私聊里点确认卡：任务结束不等于数据已经写进业务系统。
            "writes": await writes.view(session, row),
            # 实际是否静默执行；要求了静默但有写操作没登记时为 false，原因见 silent_fallback。
            "silent": bool(row.silent),
            "silent_fallback": (
                {
                    "reason": SILENT_FALLBACK_NOT_REGISTERED,
                    "unregistered": row.silent_unregistered,
                }
                if row.silent_unregistered is not None
                else None
            ),
            "allowed_writes": row.allowed_writes,
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
