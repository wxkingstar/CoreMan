"""加密存储用例的公共件：造助理记录与普通机器人，按原始 SQL 看库里到底存了什么。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.records import new_assistant_bot
from coreman.core.db.models import (
    AssistantSettings,
    Bot,
    PlatformApp,
    RelayServer,
    RuntimeNode,
    User,
)
from coreman.core.runtime_nodes.transport import now

MASTER_KEY = b"\x07" * 32
# 断言「库里不含明文」用的片段：不会碰巧出现在 base64 密文里。
SECRET = "机密-片段-7f3a"


async def seed_assistant(session: AsyncSession) -> tuple[Bot, User]:
    owner = User(login_name=f"u{uuid.uuid4().hex[:8]}", display_name="张三")
    app = PlatformApp(
        platform="feishu",
        name=f"个人助理-{uuid.uuid4().hex[:6]}",
        capabilities=["assistant"],
        app_id=f"cli_{uuid.uuid4().hex[:10]}",
        secret_enc="x",
    )
    session.add_all([owner, app])
    await session.flush()
    bot = new_assistant_bot(
        owner, app, relay_server_id=None, model="m", working_dir="/w", credentials_enc=""
    )
    # 各功能的用例要的是一位已经开通好的助理（开通流程本身见 test_assistant_provisioning）。
    bot.assistant_state = "active"
    session.add(bot)
    await session.commit()
    return bot, owner


async def seed_standard(session: AsyncSession) -> Bot:
    creator = User(login_name=f"c{uuid.uuid4().hex[:8]}", display_name="李四")
    session.add(creator)
    await session.flush()
    bot = Bot(
        bot_key=f"demo-bot-{uuid.uuid4().hex[:6]}",
        platform="feishu",
        name="demo-bot",
        created_by=creator.id,
        model="m",
        working_dir="/d",
        credentials_enc="",
    )
    session.add(bot)
    await session.commit()
    return bot


async def raw(session: AsyncSession, sql: str, **params: Any) -> Any:
    """原始 SQL 取一个值：绕开会话的自动还原，看到的就是库里存的。"""
    return (await session.execute(text(sql), params)).scalar_one()


async def into_assistant(session: AsyncSession, bot: Bot, owner: User) -> Bot:
    """把现成的飞书普通机器人改成 `owner` 的个人助理：复用各功能已有的造数函数。

    之前写下的行保持明文；调用方在这之后不再经 ORM 改内容列（否则要先签发数据密钥）。
    """
    from coreman.core.assistants import sealed
    from coreman.core.assistants.records import ASSISTANT_FIXED

    app = PlatformApp(
        platform="feishu",
        name=f"个人助理-{uuid.uuid4().hex[:6]}",
        capabilities=["assistant"],
        app_id=f"cli_{uuid.uuid4().hex[:10]}",
        secret_enc="x",
    )
    session.add(app)
    await session.flush()
    bot.kind, bot.owner_id, bot.created_by, bot.platform_app_id = (
        "assistant",
        owner.id,
        owner.id,
        app.id,
    )
    bot.team_id = None
    bot.assistant_state = "active"
    for name, value in ASSISTANT_FIXED.items():
        setattr(bot, name, value)
    await sandbox_ready(session, bot)
    await session.commit()
    sealed.forget()
    return bot


async def sandbox_ready(session: AsyncSession, bot: Bot) -> None:
    """助理绑的实例放进助理池，所在节点在线并声明能隔离运行助理（1E 起助理只在这样的节点上跑）。"""
    relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    if relay is None or relay.runtime_node_id is None:
        return
    relay.assistant_pool = True
    node = await session.get(RuntimeNode, relay.runtime_node_id)
    assert node is not None
    capabilities = dict(node.capabilities or {})
    capabilities[relay.model_provider] = {
        **capabilities.get(relay.model_provider, {}),
        "installed": True,
        "login": "ready",
        "assistant_sandbox_v1": True,
    }
    node.capabilities = capabilities
    node.heartbeat_at = now()
    # 助理专用实例上报本机指纹与系统用户（不是 root），每轮都按它们核对节点上没有普通员工。
    node.machine_fingerprint = node.machine_fingerprint or f"fp-{node.id.hex}"
    node.uid = node.uid or 1001
    await session.flush()


async def identified(
    session: AsyncSession,
    bot: Bot,
    *,
    open_id: str = "ou_owner",
    tenant_key: str = "tenant-example",
    platform_user_id: str = "owner_pid",
    chat_id: str = "oc_private",
) -> AssistantSettings:
    """开通时记下的所有者身份（平台来源的轮次凭它拿个人工具）。

    默认值与 `lifecycle_helpers.active_assistant` 造出的通讯录身份、授权与私聊一致。
    """
    from coreman.core.assistants import settings

    row = await settings.record_identity(
        session,
        bot.id,
        open_id=open_id,
        tenant_key=tenant_key,
        platform_user_id=platform_user_id,
        chat_id=chat_id,
    )
    await session.commit()
    return row
