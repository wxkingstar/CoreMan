"""对话记录接口：所有者读到明文；其他人与密钥销毁之后读不到；运维接口不含内容。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import keys, sealed
from coreman.core.bus import outbox, streams, tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import chat_logs
from coreman.core.chat.chat_logs import ChatLogEntry
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot
from tests.api.conftest import login_as, login_existing
from tests.integration.assistant_helpers import MASTER_KEY, SECRET, seed_assistant, seed_standard


@pytest.fixture(autouse=True)
def _master() -> Iterator[None]:
    # 与接口进程从配置里取到的是同一把主密钥。
    sealed.use_master(Cipher(MASTER_KEY))
    keys.forget()
    sealed.forget()
    yield
    sealed.use_master(None)
    keys.forget()
    sealed.forget()


def _entry(bot: Bot, user_id: uuid.UUID | None, **over: object) -> ChatLogEntry:
    base: dict[str, object] = dict(
        bot_id=bot.id,
        bot_key=bot.bot_key,
        platform="feishu",
        chat_type="single",
        message_type="text",
        status="success",
        request_at=datetime.now(UTC),
        user_id=user_id,
        chat_id="oc_1",
        session_key="oc_1",
        task_id=1,
        message_content=f"问：{SECRET}",
        response_content=f"答：{SECRET}",
    )
    base.update(over)
    return ChatLogEntry(**base)  # type: ignore[arg-type]


async def _items(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    response = await client.get("/api/admin/chat-logs")
    assert response.status_code == 200, response.text
    return list(response.json()["data"]["items"])


async def test_owner_reads_plaintext(client: httpx.AsyncClient, db_session: AsyncSession) -> None:
    bot, owner = await seed_assistant(db_session)
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id))
    await db_session.commit()
    await login_existing(client, db_session, owner)
    items = await _items(client)
    assert len(items) == 1 and items[0]["message_preview"] == f"问：{SECRET}"
    assert "content_locked" not in items[0]
    detail = (await client.get(f"/api/admin/chat-logs/{items[0]['id']}")).json()["data"]
    assert detail["message_content"] == f"问：{SECRET}"
    assert detail["response_content"] == f"答：{SECRET}"
    assert "content_locked" not in detail


async def test_nobody_else_gets_the_content(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id))
    # 一条不按私聊规则隐藏的记录（定时任务类型）：管理员看得到这一行，但内容不为他解密。
    assert await chat_logs.insert_turn(
        db_session,
        _entry(
            bot,
            None,
            chat_type="cron",
            task_id=4242,
            file_info={"filename": f"{SECRET}.pdf"},
            error_message=f"出错：{SECRET}",
        ),
    )
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    items = await _items(client)
    assert [i["chat_type"] for i in items] == ["cron"]
    assert items[0]["content_locked"] is True
    assert items[0]["message_preview"] is None and items[0]["response_preview"] is None
    response = await client.get(f"/api/admin/chat-logs/{items[0]['id']}")
    assert response.status_code == 200
    assert SECRET not in response.text and "enc:v2:" not in response.text
    assert "_sealed" not in response.text
    detail = response.json()["data"]
    assert detail["content_locked"] is True
    assert detail["message_content"] is None and detail["file_info"] is None
    assert detail["error_message"] is None


async def test_owner_sees_locked_after_key_destroyed(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id))
    await keys.destroy_key(db_session, owner.id)
    await db_session.commit()
    await login_existing(client, db_session, owner)
    items = await _items(client)
    assert items[0]["content_locked"] is True and items[0]["message_preview"] is None
    detail = await client.get(f"/api/admin/chat-logs/{items[0]['id']}")
    assert detail.status_code == 200 and SECRET not in detail.text
    assert detail.json()["data"]["response_content"] is None


async def test_standard_bot_output_is_unchanged(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot = await seed_standard(db_session)
    viewer = await login_as(client, db_session, role="platform_admin")
    assert await chat_logs.insert_turn(db_session, _entry(bot, viewer.id, platform="wecom"))
    await db_session.commit()
    items = await _items(client)
    assert items[0]["message_preview"] == f"问：{SECRET}" and "content_locked" not in items[0]


async def test_keyword_search_does_not_reach_assistant_content(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    assert await chat_logs.insert_turn(db_session, _entry(bot, owner.id))
    await db_session.commit()
    await login_existing(client, db_session, owner)
    response = await client.get("/api/admin/chat-logs", params={"keyword": "片段"})
    assert response.status_code == 200 and response.json()["data"]["items"] == []


async def test_runtime_pages_carry_no_content(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, _ = await seed_assistant(db_session)
    await tasks.enqueue(
        db_session,
        NewTask(bot_id=bot.id, kind="chat", payload={"message": SECRET}, session_key="oc_1"),
    )
    await outbox.add(
        db_session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key="1:send:final",
        target={"chat_id": "oc_1"},
        payload={"markdown": SECRET},
    )
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    for path in (
        "/api/admin/runtime/tasks?status=queued",
        "/api/admin/runtime/outbox?status=pending",
    ):
        response = await client.get(path)
        assert response.status_code == 200 and len(response.json()["data"]) == 1
        assert SECRET not in response.text and "_sealed" not in response.text


async def test_dev_task_page_only_decrypts_for_the_owner(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot, owner = await seed_assistant(db_session)
    owner.role = "platform_admin"
    task = await tasks.enqueue(
        db_session, NewTask(bot_id=bot.id, kind="chat", payload={}, session_key="oc_1")
    )
    assert task is not None
    await streams.create(
        db_session,
        task_id=task.id,
        bot_id=bot.id,
        platform="feishu",
        stream_id="s1",
        reply_context={"chat_id": "oc_1"},
        lease_generation=1,
        running_since=datetime.now(UTC),
    )
    await streams.update(db_session, task.id, bot_id=bot.id, pending_text=f"半截：{SECRET}")
    assert await chat_logs.insert_turn(
        db_session, _entry(bot, None, chat_type="cron", task_id=task.id)
    )
    await db_session.commit()
    await login_as(client, db_session, role="platform_admin")
    response = await client.get(f"/api/dev/tasks/{task.id}")
    assert response.status_code == 200, response.text
    assert SECRET not in response.text and "enc:v2:" not in response.text
    data = response.json()["data"]
    assert data["stream"]["pending_text"] == "" and data["chat_log"]["content_locked"] is True
    await login_existing(client, db_session, owner)
    data = (await client.get(f"/api/dev/tasks/{task.id}")).json()["data"]
    assert data["stream"]["pending_text"] == f"半截：{SECRET}"
    assert data["chat_log"]["message_preview"] == f"问：{SECRET}"
