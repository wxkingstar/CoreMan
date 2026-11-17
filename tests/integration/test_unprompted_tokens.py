"""没人点名的轮次（旁听插话、调度）：env / mcp 系统的令牌不签发，平台代理的照常；告警轮次不变。"""

from __future__ import annotations

import json

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.system_access import (
    ENV_WRITE_REMINDER,
    MCP_MOUNTS_ENV,
    UNPROMPTED_NOTE,
    build_system_access,
)
from coreman.core.db.models import (
    Bot,
    BusinessSystem,
    BusinessTokenIssue,
    Task,
    User,
    UserIdentity,
)
from coreman.core.prompting.system_prompt import Speaker
from coreman.core.systems_catalog import policy as catalog_policy
from tests.integration import test_alert_sources as alerts
from tests.integration.test_group_dispatch import _dispatch_turn, plan
from tests.integration.test_group_observe import _interjection_turn
from tests.integration.worker_helpers import build_ctx, seed_bot

WITHHELD = UNPROMPTED_NOTE.format(names="库存")


def stock(**over: object) -> BusinessSystem:
    values: dict[str, object] = {
        "key": "stock",
        "name": "库存",
        "base_url": "https://stock.example.com",
        "default_for_all_bots": True,
        "allowed_bot_ids": None,
    }
    return BusinessSystem(**(values | over))


async def member_u1(session: AsyncSession) -> User:
    """群里发言的 u1 对应一位有邮箱的成员：认得出人，令牌才签得出来。"""
    person = User(login_name="lilei", display_name="李雷", email="lilei@example.test")
    session.add(person)
    await session.flush()
    session.add(UserIdentity(user_id=person.id, platform="feishu", platform_user_id="u1"))
    await session.commit()
    return person


async def exempt(bot: Bot, session: AsyncSession) -> None:
    bot.unprompted_system_tokens = True
    await session.commit()


async def issues(session: AsyncSession) -> list[str]:
    return list(await session.scalars(select(BusinessTokenIssue.system_key)))


async def test_unprompted_turn_keeps_proxy_and_withholds_env_and_mcp(
    db_session: AsyncSession,
) -> None:
    bot, _, cipher = await seed_bot(db_session)
    creator = await db_session.get(User, bot.created_by)
    assert creator
    db_session.add_all(
        [
            stock(sort_order=1),
            stock(
                key="lookup",
                name="检索",
                base_url=None,
                token_delivery="mcp",
                mcp_url="https://mcp.example.com/mcp",
                sort_order=2,
            ),
            stock(
                key="orders",
                name="订单",
                base_url="https://orders.example.com",
                openapi_url="https://orders.example.com/openapi.json",
                token_delivery="proxy",
                sort_order=3,
            ),
        ]
    )
    task = Task(bot_id=bot.id, kind="chat", payload={}, status="running")
    db_session.add(task)
    await db_session.commit()
    speaker = Speaker("u1", creator.id, creator.login_name, creator.display_name)

    async def access(unprompted: bool):
        return await build_system_access(
            db_session,
            cipher,
            bot=bot,
            speaker=speaker,
            issuer="coreman",
            external_key=None,
            catalog=catalog_policy.Mount(task.id, "https://coreman.example.com/mcp", 1800),
            mcp_supported=True,
            unprompted=unprompted,
        )

    quiet = await access(True)
    assert not [key for key in quiet.env if key.startswith("BOT_TOKEN_")]
    assert quiet.issued == []
    # mcp 系统让驱动显式禁用，而不是留着上一轮的挂载。
    assert json.loads(quiet.env[MCP_MOUNTS_ENV]) == [{"key": "lookup", "url": ""}]
    # 平台代理的系统、目录凭据和业务系统账号照常。
    assert [c["key"] for c in json.loads(quiet.env["COREMAN_SYSTEMS"])] == ["orders"]
    assert catalog_policy.TOKEN_ENV in quiet.env and catalog_policy.URL_ENV in quiet.env
    assert quiet.env["COREMAN_USER_SUBJECT"] == "creator"
    assert UNPROMPTED_NOTE.format(names="库存、检索") in quiet.note
    assert ENV_WRITE_REMINDER not in quiet.note
    # 「业务系统访问」段不随轮次变：同一会话里逐字一致。
    normal = await access(False)
    assert quiet.prompt == normal.prompt
    assert {"BOT_TOKEN_STOCK", "BOT_TOKEN_LOOKUP"} <= set(normal.env)
    assert [r.system_key for r in normal.issued] == ["stock", "lookup"]
    assert "没人点名" not in normal.note


async def test_interjection_turn_gets_no_env_token(db_engine, db_session, monkeypatch) -> None:
    await member_u1(db_session)
    db_session.add(stock())
    await db_session.commit()
    body, _, _, _ = await _interjection_turn(db_engine, db_session, "normal", monkeypatch)
    env = body["env_vars"]
    assert "BOT_TOKEN_STOCK" not in env and env["COREMAN_USER_SUBJECT"] == "lilei"
    assert WITHHELD in json.dumps(body["messages"], ensure_ascii=False)
    assert await issues(db_session) == []


async def test_interjection_turn_of_an_exempt_bot_keeps_its_token(
    db_engine, db_session, monkeypatch
) -> None:
    await member_u1(db_session)
    db_session.add(stock())
    await db_session.commit()

    async def prepare(bot: Bot) -> None:
        await exempt(bot, db_session)

    body, _, _, _ = await _interjection_turn(
        db_engine, db_session, "normal", monkeypatch, prepare=prepare
    )
    assert "BOT_TOKEN_STOCK" in body["env_vars"]
    assert "没人点名" not in json.dumps(body["messages"], ensure_ascii=False)
    assert await issues(db_session) == ["stock"]


async def test_dispatch_turn_issues_nothing(db_engine, db_session, monkeypatch) -> None:
    await member_u1(db_session)
    db_session.add(stock())
    await db_session.commit()
    _, body, _, _, _, _ = await _dispatch_turn(
        db_engine, db_session, monkeypatch, plan(action="none", reason="闲聊")
    )
    assert "BOT_TOKEN_STOCK" not in body["env_vars"]
    assert await issues(db_session) == []


async def test_dispatch_turn_of_an_exempt_bot_issues_as_before(
    db_engine, db_session, monkeypatch
) -> None:
    await member_u1(db_session)
    db_session.add(stock())
    await db_session.commit()

    async def prepare(bot: Bot) -> None:
        await exempt(bot, db_session)

    _, body, _, _, _, _ = await _dispatch_turn(
        db_engine,
        db_session,
        monkeypatch,
        plan(action="none", reason="闲聊"),
        prepare=prepare,
    )
    # 现状：调度员只安排不干活，令牌照签但不进它的运行环境。
    assert "BOT_TOKEN_STOCK" not in body["env_vars"]
    assert await issues(db_session) == ["stock"]


async def test_alert_turn_keeps_its_service_user_token(db_engine, db_session) -> None:
    """告警轮次不算「没人点名」：它以管理员配置的服务用户身份运行，又不挂操作目录，
    停掉令牌就完全查不了业务系统，所以照常签发，与豁免开关无关。"""
    from coreman.runtime.worker.chat.handler import ChatTaskHandler
    from tests.fakes.fake_relay import FakeRelay

    bot, _, _ = await alerts.setup(db_session)
    db_session.add(stock())
    await db_session.commit()
    assert bot.unprompted_system_tokens is False

    task = await alerts.alert(db_session, bot, "m1", "告警名称: m1")
    await db_session.execute(update(Task).where(Task.id == task.id).values(status="running"))
    await db_session.commit()
    task = await db_session.get(Task, task.id, populate_existing=True)
    fake = FakeRelay("normal")
    ctx = build_ctx(db_engine, task, relay_client_factory=lambda _r: fake.client())
    await ChatTaskHandler().run(ctx)
    body = fake.requests[0]
    assert "BOT_TOKEN_STOCK" in body["env_vars"]
    assert body["env_vars"]["COREMAN_USER_SUBJECT"] == "svc-demo"
    assert WITHHELD not in json.dumps(body["messages"], ensure_ascii=False)
    assert await issues(db_session) == ["stock"]
