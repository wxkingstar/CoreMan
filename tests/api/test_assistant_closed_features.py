"""个人助理暂未开放的功能：这些功能会把对话内容明文写进别的表，本期在入口处关闭。

求助（human_collaborations / bot_collaborations）、API 驱动（api_tasks）、调度派活
（work_tasks）、记忆库（memories）、提醒与定时任务（cron_jobs / cron_runs）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.chat import dispatch
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    CronJob,
    HumanCollaboration,
    InteractionState,
    Memory,
    UserIdentity,
)
from coreman.core.reminders import handle_request
from coreman.runtime.worker.chat import collaboration as chat_collaboration
from coreman.runtime.worker.chat import schedules as chat_schedules
from tests.api.conftest import login_as, login_existing
from tests.api.test_feishu_personal import URL as PERSONAL_MCP
from tests.api.test_feishu_personal import headers, rpc, value
from tests.api.test_feishu_personal import setup as private_chat
from tests.api.test_self_reminders import direct
from tests.fakes.runtime_node import attach_node
from tests.integration.assistant_helpers import (
    MASTER_KEY,
    SECRET,
    into_assistant,
    seed_assistant,
)
from tests.integration.test_human_collaboration import call as collaborate
from tests.integration.test_human_collaboration import setup as with_colleague
from tests.integration.worker_helpers import build_ctx, seed_bot

REASON = "个人助理暂不支持"


@pytest.fixture(autouse=True)
def _master() -> Iterator[None]:
    sealed.use_master(Cipher(MASTER_KEY))
    keys.forget()
    sealed.forget()
    yield
    sealed.use_master(None)
    keys.forget()
    sealed.forget()


async def _count(session: AsyncSession, model: type) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_owner_cannot_switch_on_bot_help_or_api_access(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    owner = await login_as(client, db_session, role="member")
    bot, _ = await seed_assistant(db_session)
    bot.owner_id = bot.created_by = owner.id
    await db_session.commit()
    for path in ("bot-help", "api-access"):
        r = await client.patch(f"/api/admin/bots/{bot.id}/{path}", json={"enabled": True})
        assert r.status_code == 422 and REASON in r.text, r.text
    await db_session.refresh(bot)
    assert bot.accept_bot_help is False and bot.api_access is False


async def test_asking_a_colleague_is_refused_and_not_offered(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from tests.api.test_feishu_personal_worker import intake_for

    bot, owner, helper, _, task = await with_colleague(db_session, chat_type="single")
    await into_assistant(db_session, bot, owner)
    # 提示词与环境变量里都不出现求助入口。
    intake = await intake_for(db_session, bot, task, "库存口径是什么")
    ctx = build_ctx(db_engine, task)
    info: Any = None
    prompt, env = await chat_collaboration.configure(db_session, ctx, intake, info, "你是助手", {})
    assert prompt == "你是助手" and env == {}
    # 服务端同样拒绝，并说明原因。
    args = {"collaborator_id": f"human:{helper.id}", "question": SECRET}
    with pytest.raises(ValueError, match=REASON):
        await collaborate(db_session, task, owner, "request_collaboration", args)
    with pytest.raises(ValueError, match=REASON):
        await collaborate(db_session, task, owner, "search_collaborators", {})
    assert await _count(db_session, HumanCollaboration) == 0


async def test_dispatcher_never_assigns_work_to_an_assistant(db_session: AsyncSession) -> None:
    dispatcher, _, cipher = await seed_bot(db_session)
    dispatcher.platform, dispatcher.group_role = "feishu", "dispatcher"
    assistant, _ = await seed_assistant(db_session)
    member = Bot(
        bot_key="demo-bot",
        platform="feishu",
        name="demo-bot",
        created_by=dispatcher.created_by,
        model="m",
        working_dir="/demo",
        credentials_enc="",
    )
    db_session.add(member)
    await db_session.commit()
    everyone = AsyncMock(side_effect=lambda bots, chat_id, cipher: (bots, []))
    with patch("coreman.core.chat.collaboration_tools.present", new=everyone):
        found, _ = await dispatch.employees(db_session, dispatcher, "oc_group", None, cipher)
    assert [e.bot_id for e in found] == [member.id]


async def test_memory_library_is_closed(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    standard, relay, cipher = await seed_bot(db_session)
    token = "memory-agent-token-synthetic-long"
    relay.agent_token_enc = cipher.encrypt(token, "relay_servers.agent_token_enc")
    await attach_node(db_session, relay)
    assistant, owner = await seed_assistant(db_session)
    assistant.relay_server_id, assistant.working_dir = relay.id, "/data/skills/assistant"
    await db_session.commit()
    auth = {"Authorization": f"Bearer {token}"}
    # 节点问「该回收哪些目录」时不列助理的目录。
    r = await client.get(
        "/api/infra/memories/workspaces", params={"server_id": str(relay.id)}, headers=auth
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["working_dirs"] == [standard.working_dir]
    # 仍然发上来的（旧版本节点、换运行时的快照）：助理的丢弃，同批其他员工的照常入库。
    entry = {"file_name": "a.md", "content": SECRET, "file_mtime": datetime.now(UTC).timestamp()}
    body = {
        "server_id": str(relay.id),
        "memories": [
            entry | {"working_dir": assistant.working_dir},
            entry | {"working_dir": standard.working_dir},
        ],
    }
    r = await client.post("/api/infra/memories/collect", json=body, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["created"] == 1
    rows = list(await db_session.scalars(select(Memory)))
    assert [row.bot_id for row in rows] == [standard.id]
    # 管理台的记忆入口对所有者也关闭。
    await login_existing(client, db_session, owner)
    r = await client.get(f"/api/admin/bots/{assistant.id}/memories")
    assert r.status_code == 422 and REASON in r.text, r.text
    r = await client.post(
        f"/api/admin/bots/{assistant.id}/memories", json={"file_name": "b.md", "content": SECRET}
    )
    assert r.status_code == 422 and REASON in r.text, r.text
    assert await _count(db_session, Memory) == 1


async def test_owner_cannot_create_a_cron_job(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    assistant, owner = await seed_assistant(db_session)
    db_session.add(UserIdentity(user_id=owner.id, platform="feishu", platform_user_id="ou_owner"))
    await db_session.commit()
    await login_existing(client, db_session, owner)
    body = {
        "bot_id": str(assistant.id),
        "name": "日报",
        "cron_expression": "0 9 * * *",
        "prompt": SECRET,
    }
    r = await client.post("/api/admin/cron-jobs", json=body)
    assert r.status_code == 422 and REASON in r.text, r.text
    assert await _count(db_session, CronJob) == 0


async def test_reminder_request_is_refused(db_session: AsyncSession, app: Any) -> None:
    bot, user, task = await direct(db_session, app, "两分钟后提醒我检查接口，只提醒一次")
    await into_assistant(db_session, bot, user)
    reply = await handle_request(db_session, task, app.state.cipher)
    assert reply is not None and REASON in reply
    assert await _count(db_session, InteractionState) == 0
    assert await _count(db_session, CronJob) == 0


async def test_schedule_tool_is_refused_and_not_offered(
    client: httpx.AsyncClient, app: Any, db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    from tests.api.test_feishu_personal_worker import intake_for

    bot, user, task = await private_chat(db_session, app)
    await into_assistant(db_session, bot, user)
    intake = await intake_for(db_session, bot, task, "每天提醒我写日报")
    ctx = build_ctx(db_engine, task)
    prompt, env = await chat_schedules.configure(
        db_session, ctx, intake, uuid.uuid4(), "你是助手", {}
    )
    assert prompt == "你是助手" and env == {}
    auth = await headers(app, task, user)
    arguments = {"name": "日报", "prompt": SECRET, "cron_expression": "0 9 * * 1-5"}
    result = value(
        await client.post(PERSONAL_MCP, headers=auth, json=rpc("schedule_propose", arguments))
    )
    assert result["error"] == "assistant_unsupported" and REASON in result["message"]
    assert await _count(db_session, InteractionState) == 0
    assert await _count(db_session, CronJob) == 0


async def _owned_assistant(client: httpx.AsyncClient, db_session: AsyncSession) -> Bot:
    owner = await login_as(client, db_session, role="member")
    bot, _ = await seed_assistant(db_session)
    bot.owner_id = bot.created_by = owner.id
    await db_session.commit()
    return bot


async def test_owner_cannot_change_approvers(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot = await _owned_assistant(client, db_session)
    r = await client.put(f"/api/admin/bots/{bot.id}/approvers", json={"user_ids": []})
    assert r.status_code == 422 and REASON in r.text, r.text


async def test_alert_sources_are_closed_for_assistants(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    admin = await login_as(client, db_session, role="platform_admin")
    bot, _ = await seed_assistant(db_session)
    base = f"/api/admin/bots/{bot.id}/alert-sources"
    listed = await client.get(base)
    assert listed.status_code == 422 and REASON in listed.text, listed.text
    created = await client.post(
        base,
        json={"chat_id": "oc_x", "sender_union_id": "on_x", "service_user_id": str(admin.id)},
    )
    assert created.status_code == 422 and REASON in created.text, created.text


async def test_switch_preview_takes_no_target_directory_for_assistants(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    from tests.fakes.runtime_node import add_node_relay

    bot = await _owned_assistant(client, db_session)
    relay = await add_node_relay(db_session, name="preview")
    await db_session.commit()
    r = await client.post(
        f"/api/admin/bots/{bot.id}/workspace/switch-preview",
        json={"relay_server_id": str(relay.id), "target_directory": "/data/skills/other"},
    )
    assert r.status_code == 422 and REASON in r.text, r.text


async def test_switch_validation_requires_the_owner(db_session: AsyncSession) -> None:
    """管理者在上线前排进队列的换运行时作业：执行时也要是本人，别人的助理不改绑。"""
    from coreman.core.bots.switch_relay import SwitchError, validate_switch
    from coreman.core.db.models import User
    from tests.fakes.runtime_node import add_node_relay

    bot, _owner = await seed_assistant(db_session)
    admin = User(
        login_name=f"a{uuid.uuid4().hex[:6]}", display_name="管理员", role="platform_admin"
    )
    db_session.add(admin)
    target = await add_node_relay(db_session, name="target")
    await db_session.commit()
    with pytest.raises(SwitchError) as caught:
        await validate_switch(
            db_session, bot=bot, target=target, model=None, actor=admin, member_ids=set()
        )
    assert caught.value.status == 403
