"""开通个人助理：私聊助理应用 → 开通卡片 → 本人点「开通」→ 建记录与密钥、选节点、建目录 → 飞书授权
→ 授权完成才可用。

入口两处，都在网关的应答窗口里、只写库：
- `request_offer`：没有助理的人私聊（或打开与应用的私聊）时，登记一条「要发开通卡片」的流程；
- `accept_offer`：卡片回调。认人只认飞书给的操作人（网关已按它找到本人），卡片里的令牌只用来认
  「是不是发给这个人的那一张、还没过期、没点过」：令牌只存摘要、点过即作废，还要求回调里的消息与
  会话正是那张卡片所在的私聊消息（转发出去的卡片是另一条消息）。

之后的各步由看护进程推进（`lifecycle_runner`），每步一个事务。记录建好之后、授权完成之前它的状态
是 provisioning：`runtime.turn_blocker` 挡住一切轮次。任何一步放弃时走一遍停用，把已经建了的
记录、密钥、目录与授权收干净。
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import uuid
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Any, cast

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import eligibility, keys, lifecycle, runtime, sealed
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.apps import app_credentials, is_assistant_app
from coreman.core.assistants.lifecycle import StepFailed, StepRetry, advance, finish, token_hash
from coreman.core.assistants.records import new_assistant_bot
from coreman.core.bots.secrets import CREDENTIALS_AAD, encrypt_json
from coreman.core.bots.workspace import reserve_workspace
from coreman.core.bots.workspace_transfer import instruction_content
from coreman.core.config import get_settings
from coreman.core.crypto import Cipher, DecryptError
from coreman.core.db.models import (
    AssistantKey,
    AssistantLifecycle,
    Bot,
    FeishuPersonalGrant,
    PlatformApp,
    RelayServer,
    RuntimeNode,
    Task,
    User,
)
from coreman.core.errors import ApiError
from coreman.core.feishu_personal import service as personal
from coreman.core.feishu_personal.policy import Scope
from coreman.core.relay.agent_client import AgentError, call_agent
from coreman.core.relay.models import default_model, load_catalog
from coreman.core.timeutils import utcnow

OFFER_TTL = timedelta(minutes=30)
# 同一张卡片发出这么久之内，再发消息不重发卡片。
OFFER_RESEND = timedelta(minutes=10)
# 刚被拒（不在范围）或刚失败过：这段时间里再发消息不再提醒。
QUIET = {"ineligible": timedelta(hours=24), "failed": timedelta(minutes=10)}
# 授权链接最多发这么多次（每次十来分钟有效）还没完成，就放弃这次开通。
GRANT_ROUNDS = 3
GRANT_LEVEL = "all"
# 数据密钥销毁之后多久才能再开通：各进程的密钥缓存都过期，再留一点余量。
KEY_COOLING = timedelta(seconds=keys.KEY_TTL_SECONDS + 15)
GRANT_POLL = timedelta(seconds=5)
NODE_SECONDS = 120
TOASTS = ("provision_started", "provision_expired")
FAILURES = {
    **eligibility.REASONS,
    "no_pool_runtime": "个人助理暂时没有可用的运行时，这次开通没有完成，请稍后再试或联系管理员。",
    "no_model": "个人助理还没有可用的模型，这次开通没有完成，请联系管理员。",
    "app_unavailable": "个人助理应用暂时不可用，这次开通没有完成，请联系管理员。",
    "app_permission_missing": "个人助理应用的飞书权限不足，这次开通没有完成，请联系管理员。",
    "bot_key_taken": "个人助理这次没能开通，请联系管理员处理。",
    "already_provisioned": "你已经有一个个人助理了。",
    "grant_not_completed": "飞书授权一直没有完成，这次开通已取消；想用的时候再给我发条消息即可。",
}
GENERIC_FAILURE = "个人助理这次没能开通，已经收拾干净；稍后再给我发条消息可以重新开通。"


def approval_system(*, markdown: bool = False, link: bool = True) -> str:
    """审批系统在文案里的叫法：部署配了名称就用名称，配了网页入口就带上链接。"""
    settings = get_settings()
    name = (settings.tower_display_name or "").strip() or "审批系统"
    url = (settings.tower_web_url or "").strip()
    if not link or not url.startswith(("https://", "http://")):
        return name
    return f"[{name}]({url})" if markdown else f"{name}（{url}）"


def offer_card(token: str) -> dict[str, Any]:
    """开通卡片：说清楚它是什么、数据放在哪、要先做什么；按钮的值只有一个随机令牌。"""
    name = approval_system(link=False)
    text = (
        "这是你的个人助理：以你的飞书身份帮你看消息、起草回复、处理文档与审批。\n"
        "- 只在这个私聊里工作，对话与个人数据加密保存，只有你本人看得到。\n"
        "- 对外有影响的操作（发消息、改文档、处理审批……）都先提交审批，"
        f"由你本人在{approval_system(markdown=True)}里批准后才执行；审批单正文保存在{name}里。\n"
        f"- 开通前请先登录一次{name}，否则第一次提交审批时会提示没有账号。\n"
        "- 开通后还要授权一次飞书；随时发「停用助理」可以停用，停用后个人数据不可恢复。"
    )
    return {
        "schema": "2.0",
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": "开通个人助理"},
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": text},
                {
                    "tag": "button",
                    "type": "primary",
                    "text": {"tag": "plain_text", "content": "开通"},
                    "behaviors": [
                        {"type": "callback", "value": {"assistant": "provision", "token": token}}
                    ],
                },
            ]
        },
    }


def accepted_card() -> dict[str, Any]:
    """点了「开通」之后换上的卡片：没有按钮，免得再点。"""
    return {
        "schema": "2.0",
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": "开通个人助理"},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "正在开通，好了会在这里告诉你；接下来还要授权一次飞书。",
                }
            ]
        },
    }


def grant_card(url: str) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"template": "turquoise", "title": {"tag": "plain_text", "content": "飞书授权"}},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "最后一步：授权助理以你的身份使用飞书（读消息与文档、起草；"
                        "发出去之前都要你批准）。授权完成后我会在这里告诉你。\n"
                        f"{personal.RETENTION_NOTICE}"
                    ),
                },
                {
                    "tag": "button",
                    "type": "primary",
                    "text": {"tag": "plain_text", "content": "一键授权"},
                    "behaviors": [{"type": "open_url", "default_url": url}],
                },
            ]
        },
    }


def granted_card() -> dict[str, Any]:
    """授权完成后换上的卡片：没有按钮。"""
    return {
        "schema": "2.0",
        "header": {"template": "turquoise", "title": {"tag": "plain_text", "content": "飞书授权"}},
        "body": {"elements": [{"tag": "markdown", "content": "已完成授权。"}]},
    }


def welcome_text() -> str:
    name = approval_system(link=False)
    return (
        "个人助理开通好了，直接在这里说要做的事就行。\n"
        f"对外有影响的操作会先提交审批，在{approval_system()}里批准后才执行。"
        f"要是提示你在{name}里还没有账号，先登录一次{name}再让我重试。\n"
        "发「停用助理」可以随时停用。"
    )


# ---- 网关：登记与点击 --------------------------------------------------------------------------


async def request_offer(
    session: AsyncSession, *, app_id: uuid.UUID, owner_id: uuid.UUID, platform_user_id: str
) -> None:
    """没有助理的人发来私聊：登记一条开通流程，由看护进程判断范围并发卡片。只写库，调用方提交。"""
    now = utcnow()
    # 网关的应答窗口只有 1.5 秒：看护进程正锁着这条流程（一步最长 150 秒）就是正在处理，不等它。
    row = await lifecycle.live(session, owner_id, skip_locked=True)
    if row is None and await session.scalar(
        select(AssistantLifecycle.id).where(
            AssistantLifecycle.owner_id == owner_id, AssistantLifecycle.finished_at.is_(None)
        )
    ):
        return
    if row is not None:
        sent_at = row.deadline - OFFER_TTL if row.deadline else None
        if (
            row.action == "provision"
            and row.step == "offered"
            and row.next_attempt_at is None
            and (sent_at is None or sent_at <= now - OFFER_RESEND)
        ):
            row.platform_user_id, row.next_attempt_at = platform_user_id, now
        return
    for outcome, quiet in QUIET.items():
        recent = await session.scalar(
            select(AssistantLifecycle.id)
            .where(
                AssistantLifecycle.owner_id == owner_id,
                AssistantLifecycle.action == "provision",
                AssistantLifecycle.outcome == outcome,
                AssistantLifecycle.finished_at > now - quiet,
            )
            .limit(1)
        )
        if recent is not None:
            return
    await session.execute(
        insert(AssistantLifecycle)
        .values(
            id=uuid.uuid4(),
            owner_id=owner_id,
            platform_app_id=app_id,
            action="provision",
            step="offered",
            trigger="owner",
            actor_id=owner_id,
            platform_user_id=platform_user_id,
            next_attempt_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[AssistantLifecycle.owner_id],
            index_where=AssistantLifecycle.finished_at.is_(None),
        )
    )


def _obj(container: object, key: str) -> dict[str, Any]:
    value = container.get(key) if isinstance(container, dict) else None
    return value if isinstance(value, dict) else {}


async def accept_offer(
    session: AsyncSession, *, app_id: uuid.UUID, owner_id: uuid.UUID, raw: dict[str, Any]
) -> str:
    """本人点了开通卡片。`owner_id` 是网关按回调里的操作人认出的人，不来自卡片。调用方提交。

    返回回调应答用的结果（`TOASTS` 之一）。
    """
    header, event = _obj(raw, "header"), _obj(raw, "event")
    value = _obj(_obj(event, "action"), "value")
    context, operator = _obj(event, "context"), _obj(event, "operator")
    token = value.get("token")
    open_id = operator.get("open_id")
    tenant_key = header.get("tenant_key") or operator.get("tenant_key")
    # 令牌只能用一次，这里要真的锁住；等锁算在网关事务的 statement_timeout（1.5 秒）里。
    row = await lifecycle.live(session, owner_id)
    if row is None or row.action != "provision" or row.platform_app_id != app_id:
        return "provision_expired"
    if row.step != "offered":
        return "provision_started"
    if not (
        value.get("assistant") == "provision"
        and isinstance(token, str)
        and row.offer_hash is not None
        and hmac.compare_digest(row.offer_hash, token_hash(token))
        and context.get("open_message_id") == row.offer_message_id
        and context.get("open_chat_id") == row.offer_chat_id
        and row.deadline is not None
        and row.deadline > utcnow()
        and isinstance(open_id, str)
        and open_id
        and isinstance(tenant_key, str)
        and tenant_key
    ):
        return "provision_expired"
    row.offer_hash, row.open_id, row.tenant_key = None, open_id, tenant_key
    advance(row, "create")
    return "provision_started"


# ---- 看护进程：各步 ---------------------------------------------------------------------------


async def _owner(session: AsyncSession, row: AssistantLifecycle) -> User:
    owner = await session.get(User, row.owner_id)
    if owner is None:
        raise StepFailed("inactive")
    return owner


async def _bot(session: AsyncSession, row: AssistantLifecycle) -> Bot:
    """开通中的那条记录；本人的账号这期间被停用了就放弃（走停用把记录收掉）。"""
    if (await _owner(session, row)).status != "active":
        raise StepFailed("inactive")
    bot = await session.get(Bot, row.bot_id, with_for_update=True) if row.bot_id else None
    if bot is None or bot.owner_id != row.owner_id:
        raise StepFailed("bot_missing")
    return bot


async def step_offered(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """判断范围；不在范围就说一声并结束，在范围就发开通卡片（换一个新令牌）。"""
    reason = await eligibility.refusal(session, await _owner(session, row))
    if reason is not None:
        finish(row, "ineligible", reason)
        return [eligibility.REASONS[reason]]
    token = secrets.token_urlsafe(32)
    digest = token_hash(token)
    message_id, chat_id = await messenger.card(
        session,
        row.platform_app_id,
        row.platform_user_id,
        offer_card(token),
        key=f"assistant-offer:{row.id}:{digest[:16]}",
    )
    now = utcnow()
    row.offer_hash, row.offer_message_id, row.offer_chat_id = digest, message_id, chat_id
    row.deadline, row.next_attempt_at, row.attempts, row.updated_at = now + OFFER_TTL, None, 0, now
    return []


async def _pick(session: AsyncSession) -> tuple[RelayServer, RuntimeNode]:
    """按设置的 AI 顺序，在助理池里挑一个现在能跑、且与普通员工隔开的节点。

    一种 AI 都没配：再等也不会有，当场放弃并告诉本人。配了而眼下没有可用节点：稍后再试。
    """
    providers = await eligibility.backends(session)
    if not providers:
        raise StepFailed("no_pool_runtime")
    for provider in providers:
        relay = await runtime.pick_pool_relay(session, provider)
        if relay is None or relay.runtime_node_id is None:
            continue
        node = await session.get(RuntimeNode, relay.runtime_node_id)
        if node is not None and await runtime.node_isolated(session, node):
            return relay, node
    raise StepRetry("no_pool_runtime")


async def _free_bot_key(session: AsyncSession, key: str, owner_id: uuid.UUID) -> None:
    """本人以前停用的助理记录占着同一个 bot_key：给它换一个不会再用到的名字。还在用的记录不动。

    占着这个名字的是别人的记录或普通员工：一律不碰，这次开通失败。
    """
    old = await session.scalar(select(Bot).where(Bot.bot_key == key).with_for_update())
    if old is None:
        return
    if old.kind != "assistant" or old.owner_id != owner_id:
        raise StepFailed("bot_key_taken")
    if old.enabled:
        raise StepFailed("already_provisioned")
    old.bot_key = f"{key}-x{old.id.hex[:8]}"
    await session.flush()


async def step_create(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """建助理记录（provisioning）与数据密钥，绑定池里的一个节点，定下全新的工作目录。一个事务。"""
    owner = await _owner(session, row)
    reason = await eligibility.refusal(session, owner)
    if reason is not None:
        raise StepFailed(reason)
    app = await session.get(PlatformApp, row.platform_app_id)
    if app is None or not is_assistant_app(app):
        raise StepFailed("app_unavailable")
    # 看记录的状态而不是启停开关：停用完之前（含被关掉的、状态为空的旧记录）都算已经有一个。
    taken = await session.scalar(
        select(Bot.id)
        .where(
            Bot.kind == "assistant",
            Bot.owner_id == owner.id,
            or_(Bot.assistant_state.is_(None), Bot.assistant_state != "deprovisioned"),
        )
        .limit(1)
    )
    if taken is not None:
        raise StepFailed("already_provisioned")
    # 刚销毁过数据密钥：别的进程（网关等）可能还缓存着那一把，会用它封存新记录收到的内容，
    # 新一代密钥就再也解不开。等所有缓存都过期再建记录。
    cooling = await session.scalar(
        select(AssistantKey.user_id).where(
            AssistantKey.user_id == owner.id,
            AssistantKey.destroyed_at > func.now() - KEY_COOLING,
        )
    )
    if cooling is not None:
        raise StepRetry("key_cooling")
    relay, node = await _pick(session)
    model = default_model(relay.model_provider, await load_catalog(session))
    if model is None:
        raise StepFailed("no_model")
    try:
        credentials = encrypt_json(cipher, app_credentials(cipher, app), CREDENTIALS_AAD)
    except DecryptError:
        # 应用密钥暂时解不开（例如主密钥轮换中）：过会儿再试，不当成再也做不成。
        raise StepRetry("app_secret_unreadable") from None
    except ValueError:
        raise StepFailed("app_unavailable") from None
    bot_id = uuid.uuid4()
    # 目录名带记录编号：每次开通都是一个从没用过的新目录，节点上留着的旧目录不会被接管。
    directory = str(PurePosixPath(node.workspace_root) / f"assistant-{bot_id.hex}")
    bot = new_assistant_bot(
        owner,
        app,
        relay_server_id=relay.id,
        model=model,
        working_dir=directory,
        credentials_enc=credentials,
    )
    bot.id = bot_id
    await _free_bot_key(session, bot.bot_key, owner.id)
    try:
        await reserve_workspace(session, relay.id, directory, bot_id=bot_id)
    except ApiError:
        raise StepRetry("workspace_reserved") from None
    session.add(bot)
    await session.flush()
    # 上一次停用销毁过的密钥在这里签发下一代；旧一代加密的内容一律按已锁定处理。
    await keys.ensure_key(session, sealed.master(), owner.id)
    row.bot_id = bot.id
    advance(row, "init_workspace")
    return []


async def step_init_workspace(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """节点上建这个全新的目录。重试安全：助理专用实例对已带本记录标记的目录直接返回。"""
    bot = await _bot(session, row)
    relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    if relay is None:
        raise StepFailed("runtime_missing")
    try:
        async with asyncio.timeout(NODE_SECONDS):
            await call_agent(
                relay,
                cipher,
                "workspace-init",
                {
                    "working_dir": bot.working_dir,
                    "bot_id": str(bot.id),
                    "content": instruction_content(bot),
                },
            )
    except (AgentError, TimeoutError) as exc:
        raise StepRetry(getattr(exc, "code", None) or "workspace_init_failed") from None
    bot.workspace_state, bot.workspace_phase, bot.workspace_error = "ready", None, None
    advance(row, "start_grant")
    return []


# 飞书暂时不可用或限频：这一步稍后再试，不算授权被拒。
_TRANSIENT = ("upstream_unavailable", "rate_limited")


def _grant_scope(bot: Bot, row: AssistantLifecycle, app_id: str) -> Scope:
    """发起与查询授权用的身份：飞书给的操作人（点开通时记下的），不来自任何请求内容。

    开通时还没有任何一轮；`begin_in_flow` 与 `authorization_status` 不读 `scope.task`。
    """
    return Scope(
        cast(Task, None),
        bot,
        row.owner_id,
        row.tenant_key or "",
        app_id,
        row.open_id or "",
        row.platform_user_id,
        row.offer_chat_id or "",
        None,
    )


async def _app_id(session: AsyncSession, row: AssistantLifecycle) -> str:
    app = await session.get(PlatformApp, row.platform_app_id)
    if app is None or not app.app_id:
        raise StepFailed("app_unavailable")
    return app.app_id


async def step_start_grant(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """发起飞书个人授权（全部权限、带离线访问），把授权卡片私信给本人。"""
    bot = await _bot(session, row)
    scope = _grant_scope(bot, row, await _app_id(session, row))
    try:
        begun = await personal.begin_in_flow(session, cipher, scope, GRANT_LEVEL)
    except personal.PersonalError as exc:
        if exc.code in _TRANSIENT:
            raise StepRetry(exc.code) from None
        raise StepFailed("app_permission_missing") from None
    row.issued += 1
    # 开通卡片点过之后这一栏空出来，改记授权卡片，授权完成时好把按钮去掉。
    row.offer_message_id, _ = await messenger.card(
        session,
        row.platform_app_id,
        row.platform_user_id,
        grant_card(begun["authorization_url"]),
        key=f"assistant-grant:{row.id}:{row.issued}",
    )
    grant = await session.get(FeishuPersonalGrant, (bot.id, row.owner_id))
    advance(row, "await_grant", at=utcnow() + GRANT_POLL)
    row.deadline = grant.pending_expires_at if grant is not None else None
    return []


def _connected(grant: FeishuPersonalGrant | None) -> bool:
    return (
        grant is not None
        and grant.status == "connected"
        and bool(grant.token_enc)
        and grant.authorization_level == GRANT_LEVEL
    )


async def step_await_grant(
    session: AsyncSession, cipher: Cipher, row: AssistantLifecycle, messenger: lifecycle.Messenger
) -> list[str]:
    """替本人查授权结果，完成就开通好了（记录变成 active）；链接过期再发，最多三次。"""
    bot = await _bot(session, row)
    now = utcnow()
    key = (bot.id, row.owner_id)
    grant = await session.get(FeishuPersonalGrant, key, populate_existing=True)
    if (
        not _connected(grant)
        and grant is not None
        and grant.status == "pending"
        and grant.pending_expires_at is not None
        and grant.pending_expires_at > now
        and (grant.next_poll_at is None or grant.next_poll_at <= now)
    ):
        scope = _grant_scope(bot, row, await _app_id(session, row))
        try:
            await personal.authorization_status(session, cipher, scope)
        except personal.PersonalError as exc:
            if exc.code in _TRANSIENT:
                raise StepRetry(exc.code) from None
            # 授权被拒或身份对不上：授权行已清空，下面按「链接已失效」再发一次。
        grant = await session.get(FeishuPersonalGrant, key, populate_existing=True)
    if _connected(grant):
        # 和置为 active 同一个事务：平台自己发起的轮次（交回、子任务结果）凭这份身份拿个人工具。
        # 取自点开通时飞书给的操作人与开通卡片所在的私聊，不来自任何请求内容。
        await assistant_settings.record_identity(
            session,
            bot.id,
            open_id=row.open_id or "",
            tenant_key=row.tenant_key or "",
            platform_user_id=row.platform_user_id,
            chat_id=row.offer_chat_id or "",
        )
        if row.offer_message_id:
            await messenger.update_card(
                session, row.platform_app_id, row.offer_message_id, granted_card()
            )
        bot.assistant_state = "active"
        finish(row, "done")
        return [welcome_text()]
    if (
        grant is not None
        and grant.status == "pending"
        and grant.pending_expires_at is not None
        and grant.pending_expires_at > now
    ):
        wait = max(GRANT_POLL, timedelta(seconds=grant.poll_interval or 5))
        row.next_attempt_at, row.updated_at = now + wait, now
        return []
    if row.issued >= GRANT_ROUNDS:
        raise StepFailed("grant_not_completed")
    advance(row, "start_grant")
    return []


STEPS = {
    "offered": step_offered,
    "create": step_create,
    "init_workspace": step_init_workspace,
    "start_grant": step_start_grant,
    "await_grant": step_await_grant,
}


def failure_text(code: str) -> str:
    return FAILURES.get(code, GENERIC_FAILURE)
