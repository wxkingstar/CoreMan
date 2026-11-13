"""调度留在目标员工名下的那一轮：所有者看得到全文，管理者只看得到发生过，其余人看不到。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api import bot_auth
from coreman.core.assistants.kinds import BOT_HELP_KIND
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import chat_logs
from coreman.core.chat.chat_logs import ChatLogEntry
from coreman.core.config import reset_settings_cache
from coreman.core.db.models import Bot, BotMember, User
from tests.api.conftest import login_as, login_existing
from tests.api.test_runtime_nodes import _private_session
from tests.integration.assistant_helpers import SECRET, seed_standard

KEY = "assistant-help:00000000-0000-4000-8000-0000000000b1"
HIDDEN = ("message_preview", "response_preview")
HIDDEN_FULL = (*HIDDEN, "message_content", "quoted_content", "file_info", "response_content")


@pytest.fixture
def dev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """dev 路由只在 COREMAN_ENV=dev 时挂载：必须在 app 建好之前生效。"""
    monkeypatch.setenv("COREMAN_ENV", "dev")
    reset_settings_cache()


def _entry(bot: Bot, owner: User, **over: object) -> ChatLogEntry:
    base: dict[str, object] = dict(
        bot_id=bot.id,
        bot_key=bot.bot_key,
        platform="feishu",
        chat_type="single",
        message_type=BOT_HELP_KIND,
        status="success",
        request_at=datetime.now(UTC),
        user_id=owner.id,
        user_login=owner.login_name,
        user_name=owner.display_name,
        chat_id=KEY,
        session_key=KEY,
        relay_session_id=uuid.uuid4(),
        task_id=4242,
        message_content=f"问：{SECRET}",
        response_content=f"答：{SECRET}",
        tools_used=["Bash"],
        error_message=f"片段 {SECRET}",
        latency_ms=1200,
        input_tokens=180,
        output_tokens=20,
        private=True,
        reach=False,
    )
    base.update(over)
    return ChatLogEntry(**base)  # type: ignore[arg-type]


async def dispatched(client: httpx.AsyncClient, session: AsyncSession) -> tuple[Bot, User]:
    """目标员工名下的一条调度记录，以及发起它的所有者。"""
    bot = await seed_standard(session)
    owner = await login_as(client, session, role="member", display_name="张三")
    assert await chat_logs.insert_turn(session, _entry(bot, owner))
    await session.commit()
    return bot, owner


async def _viewer(client: httpx.AsyncClient, session: AsyncSession, bot: Bot, who: str) -> User:
    if who == "creator":
        creator = await session.get(User, bot.created_by)
        assert creator is not None
        return await login_existing(client, session, creator)
    if who == "member":
        user = await login_as(client, session, role="member", display_name="王五")
        session.add(BotMember(bot_id=bot.id, user_id=user.id, role="admin"))
        await session.commit()
        return user
    return await login_as(client, session, role="platform_admin")


async def _items(client: httpx.AsyncClient, query: str = "") -> list[dict[str, Any]]:
    response = await client.get("/api/admin/chat-logs" + query)
    assert response.status_code == 200, response.text
    return list(response.json()["data"]["items"])


async def test_the_owner_reads_the_whole_round(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    _, owner = await dispatched(client, db_session)
    await login_existing(client, db_session, owner)
    (item,) = await _items(client)
    assert item["message_preview"] == f"问：{SECRET}" and "content_withheld" not in item
    detail = (await client.get(f"/api/admin/chat-logs/{item['id']}")).json()["data"]
    assert detail["message_content"] == f"问：{SECRET}"
    assert detail["response_content"] == f"答：{SECRET}" and detail["tools_used"] == ["Bash"]
    assert len(await _items(client, "?keyword=机密")) == 1


@pytest.mark.parametrize("who", ["admin", "creator", "member"])
async def test_managers_see_that_it_happened_and_nothing_more(
    client: httpx.AsyncClient, db_session: AsyncSession, who: str
) -> None:
    bot, owner = await dispatched(client, db_session)
    # 所有者自己与这位员工的一条普通私聊：管理者照旧整行看不到。
    assert await chat_logs.insert_turn(
        db_session, _entry(bot, owner, message_type="text", private=False, task_id=4243)
    )
    await db_session.commit()
    await _viewer(client, db_session, bot, who)
    listing = await client.get("/api/admin/chat-logs")
    (item,) = listing.json()["data"]["items"]
    # 元数据：哪位员工、谁发起的、什么时候、状态、用量。
    assert item["bot_id"] == str(bot.id) and item["user_id"] == str(owner.id)
    assert item["message_type"] == BOT_HELP_KIND and item["status"] == "success"
    assert (item["input_tokens"], item["output_tokens"], item["latency_ms"]) == (180, 20, 1200)
    assert item["request_at"] and item["content_withheld"] is True
    assert [item[key] for key in HIDDEN] == [None, None] and item["tools_used"] == []
    detail = await client.get(f"/api/admin/chat-logs/{item['id']}")
    data = detail.json()["data"]
    assert [data[key] for key in HIDDEN_FULL] == [None] * len(HIDDEN_FULL)
    assert data["error_message"] is None and data["content_withheld"] is True
    stats = await client.get("/api/admin/chat-logs/stats")
    assert stats.json()["data"]["total"] == 1
    assert stats.json()["data"]["tokens"]["input"] == 180
    # 按正文搜索时这条记录不参与：不能用「搜得到 / 搜不到」探内容。
    assert await _items(client, "?keyword=机密") == []
    searched = await client.get("/api/admin/chat-logs/stats?keyword=机密")
    assert searched.json()["data"]["total"] == 0
    for response in (listing, detail, stats, searched):
        assert SECRET not in response.text


async def test_unrelated_members_and_bot_tokens_see_nothing(
    client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, owner = await dispatched(client, db_session)
    row_id = (await _items(client))[0]["id"]
    await login_as(client, db_session, role="member", display_name="路人")
    assert await _items(client) == []
    assert (await client.get(f"/api/admin/chat-logs/{row_id}")).status_code == 404

    # 机器人令牌可能来自群里的智能体，哪怕它背后的人就是所有者也不放行。
    async def token_user(request: Any, session: Any) -> User:
        request.state.bot_token_authenticated = True
        request.state.admin_session = None
        return owner

    monkeypatch.setattr(bot_auth, "token_user", token_user)
    client.cookies.clear()
    client.cookies.set("bot_token", "verified-owner-token")
    assert await _items(client) == []
    assert (await client.get(f"/api/admin/chat-logs/{row_id}")).status_code == 404


async def test_statistics_and_the_task_list_carry_numbers_only(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await dispatched(client, db_session)
    queued = await tasks.enqueue(
        db_session,
        NewTask(
            bot_id=bot.id,
            kind=BOT_HELP_KIND,
            user_id=owner.id,
            session_key=KEY,
            payload={"job_id": KEY.removeprefix("assistant-help:"), "round": 1},
        ),
    )
    await db_session.commit()
    assert queued is not None
    await login_as(client, db_session, role="platform_admin")
    stats = await client.get("/api/admin/statistics")
    assert stats.status_code == 200 and SECRET not in stats.text
    listed = await client.get("/api/admin/runtime/tasks?status=queued")
    assert listed.status_code == 200 and SECRET not in listed.text
    assert [item["id"] for item in listed.json()["data"]] == [queued.id]


async def test_the_dev_task_view_is_the_owners_only(
    dev_env: None, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot = await seed_standard(db_session)
    owner = await login_as(client, db_session, role="member", display_name="张三")
    queued = await tasks.enqueue(
        db_session,
        NewTask(bot_id=bot.id, kind=BOT_HELP_KIND, user_id=owner.id, session_key=KEY),
    )
    assert queued is not None
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner, task_id=queued.id))
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    assert (await client.get(f"/api/dev/tasks/{queued.id}")).status_code == 404
    owner.role = "platform_admin"
    await db_session.commit()
    await login_existing(client, db_session, owner)
    seen = await client.get(f"/api/dev/tasks/{queued.id}")
    assert seen.status_code == 200
    assert seen.json()["data"]["chat_log"]["message_preview"] == f"问：{SECRET}"


async def test_the_session_viewer_is_the_owners_only(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    # 借现成的私聊会话夹具，把那条记录改成调度留下的样子。
    _, _, owner, row, url = await _private_session(client, db_session)
    row.message_type, row.private = BOT_HELP_KIND, True
    row.chat_id = row.session_key = KEY
    await db_session.commit()
    for suffix in ("", "/events"):
        assert (await client.get(url + suffix)).status_code == 404
    owner.role = "platform_admin"
    await db_session.commit()
    await login_existing(client, db_session, owner)
    assert (await client.get(url)).status_code == 200


async def test_the_dev_task_view_hides_a_round_that_has_no_record_yet(
    dev_env: None, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """还没开流的一轮没有对话记录可判断归属：按任务类型与发起人把关，流也不给别人看。"""
    bot = await seed_standard(db_session)
    owner = await login_as(client, db_session, role="member", display_name="张三")
    queued = await tasks.enqueue(
        db_session,
        NewTask(bot_id=bot.id, kind=BOT_HELP_KIND, user_id=owner.id, session_key=KEY),
    )
    assert queued is not None
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    assert (await client.get(f"/api/dev/tasks/{queued.id}")).status_code == 404
    owner.role = "platform_admin"
    await db_session.commit()
    await login_existing(client, db_session, owner)
    assert (await client.get(f"/api/dev/tasks/{queued.id}")).status_code == 200


async def test_a_cli_session_acting_as_the_owner_does_not_read_the_round(
    dev_env: None, client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """管理命令以所有者身份建的会话不是本人在场：这一轮明文存在目标员工名下，同样不给内容。"""
    from tests.api.my_assistant_helpers import as_cli
    from tests.api.test_assistant_chat_logs import _access
    from tests.integration.assistant_helpers import into_assistant

    bot = await seed_standard(db_session)
    owner = await login_as(client, db_session, role="platform_admin", display_name="张三")
    queued = await tasks.enqueue(
        db_session,
        NewTask(bot_id=bot.id, kind=BOT_HELP_KIND, user_id=owner.id, session_key=KEY),
    )
    assert queued is not None
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner, task_id=queued.id))
    await db_session.commit()
    assistant = await into_assistant(db_session, await seed_standard(db_session), owner)
    assistant_id = assistant.id
    # 本人登录时照常看得到（对照）。
    (item,) = await _items(client)
    assert item["message_preview"] == f"问：{SECRET}"
    assert await _access(db_session) == []

    await as_cli(client, db_session, owner)
    listing = await client.get("/api/admin/chat-logs")
    assert listing.status_code == 200 and SECRET not in listing.text
    (item,) = listing.json()["data"]["items"]
    assert item["content_withheld"] is True and item["tools_used"] == []
    assert all(item[key] is None for key in HIDDEN)
    detail = await client.get(f"/api/admin/chat-logs/{item['id']}")
    assert detail.status_code == 200 and SECRET not in detail.text
    assert all(detail.json()["data"][key] is None for key in HIDDEN_FULL)
    # 按正文搜索与统计同样不含它：搜得到搜不到也是内容。
    assert await _items(client, "?keyword=机密") == []
    searched = await client.get("/api/admin/chat-logs/stats?keyword=机密")
    assert searched.json()["data"]["total"] == 0
    # 开发页的任务详情：只剩元数据。
    seen = await client.get(f"/api/dev/tasks/{queued.id}")
    assert seen.status_code == 200 and SECRET not in seen.text
    data = seen.json()["data"]
    assert data["content_withheld"] is True and data["stream"] is None
    assert data["task"]["id"] == queued.id and data["task"]["error_message"] is None
    assert data["chat_log"]["content_withheld"] is True
    assert data["chat_log"]["message_preview"] is None
    # 每次都在所有者的助理名下记一条 system，不留操作人。
    recorded = await _access(db_session)
    assert [(action, kind, who) for action, _, kind, who in recorded] == [
        ("cli.chatlogs", "system", None),
        ("cli.chatlogs", "system", None),
        ("cli.tasks", "system", None),
    ]
    assert {target for _, target, _, _ in recorded} == {"dispatch=1"}
    from sqlalchemy import text as sql

    bots = await db_session.scalars(sql("SELECT DISTINCT bot_id FROM assistant_access_log"))
    assert list(bots) == [assistant_id]
