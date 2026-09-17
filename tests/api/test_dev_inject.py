import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import streams
from coreman.core.config import reset_settings_cache
from coreman.core.db.models import Bot, InboundEvent, Task, User


@pytest.fixture
def dev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """dev 路由只在 COREMAN_ENV=dev 时挂载：必须在 app（api_settings）建好之前生效。"""
    monkeypatch.setenv("COREMAN_ENV", "dev")
    reset_settings_cache()


async def _bot(session: AsyncSession, enabled: bool = True) -> Bot:
    u = User(login_name="c", display_name="c")
    session.add(u)
    await session.flush()
    b = Bot(
        bot_key="sales_bot",
        platform="wecom",
        name="b",
        created_by=u.id,
        model="m",
        working_dir="/d",
        credentials_enc="enc:v1:x",
        enabled=enabled,
    )
    session.add(b)
    await session.commit()
    return b


async def test_dev_routes_absent_in_prod(
    prod_env: None, app: FastAPI, admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """prod 下 dev 路由整个不注册。

    用一个**真实存在且已启用**的 bot 来打：dev 下这一发必然 200 并落一行 task，
    所以 404/405 + 零 task 才真的说明路由没挂上（拿不存在的 bot_key 打，dev 自己
    也回 404，两种环境分不开）。
    """
    await _bot(db_session)
    assert [p for p in app.openapi()["paths"] if p.startswith("/api/dev/")] == []
    r = await admin_client.post(
        "/api/dev/inject-message", json={"bot_key": "sales_bot", "text": "你好"}
    )
    assert r.status_code in (404, 405), r.text
    assert (await db_session.execute(select(Task))).scalars().all() == []


async def test_inject_and_read_back(
    dev_env: None, admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    bot = await _bot(db_session)
    r = await admin_client.post(
        "/api/dev/inject-message", json={"bot_key": "sales_bot", "text": "你好"}
    )
    assert r.status_code == 200, r.text
    task_id = r.json()["data"]["task_id"]
    task = (await db_session.execute(select(Task))).scalar_one()
    ev = (await db_session.execute(select(InboundEvent))).scalar_one()
    assert task.id == task_id and task.kind == "chat"
    assert ev.reply_context["gateway_instance"] == "dev" and ev.platform_msg_id.startswith("dev-")
    d = (await admin_client.get(f"/api/dev/tasks/{task_id}")).json()["data"]
    assert d["task"]["status"] == "queued" and d["stream"] is None and d["chat_log"] is None
    from datetime import UTC, datetime

    await streams.create(
        db_session,
        task_id=task_id,
        bot_id=bot.id,
        platform="wecom",
        stream_id="s",
        reply_context=ev.reply_context,
        lease_generation=0,
        running_since=datetime.now(UTC),
    )
    await streams.update(db_session, task_id, thinking_md="🤔 x", pending_text="正文")
    await db_session.commit()
    d = (await admin_client.get(f"/api/dev/tasks/{task_id}")).json()["data"]
    assert d["stream"]["rendered"].startswith("<think>\n🤔 x\n</think>\n\n正文")
    r = await admin_client.post(
        "/api/dev/inject-message", json={"bot_key": "sales_bot", "text": "stop"}
    )
    assert r.json()["data"]["task_id"] > task_id
    assert (
        await db_session.execute(select(Task).where(Task.kind == "command"))
    ).scalar_one().lane == "fast"
    r = await admin_client.post("/api/dev/inject-message", json={"bot_key": "nope", "text": "hi"})
    assert r.status_code == 404
    bot.enabled = False
    await db_session.commit()
    r = await admin_client.post(
        "/api/dev/inject-message", json={"bot_key": "sales_bot", "text": "hi"}
    )
    assert r.status_code == 409


async def test_inject_mixed_parts_and_reject_oversized_references(admin_client, db_session):
    await _bot(db_session)
    parts = [
        {"type": "image", "ref": {"url": "https://test.example/image"}},
        {"type": "audio", "transcript": "语音转写"},
        {"type": "quote", "kind": "text", "text": "被引用的内容"},
    ]
    r = await admin_client.post(
        "/api/dev/inject-message", json={"bot_key": "sales_bot", "parts": parts}
    )
    assert r.status_code == 200, r.text
    event = (await db_session.execute(select(InboundEvent))).scalar_one()
    assert event.payload["parts"][0]["type"] == "image"
    assert event.payload["parts"][1]["transcript"] == "语音转写"
    body = {"bot_key": "sales_bot", "parts": [{"type": "image", "ref": {"url": "x" * 70000}}]}
    assert (await admin_client.post("/api/dev/inject-message", json=body)).status_code == 422
    assert (
        await admin_client.post("/api/dev/inject-message", json={"bot_key": "sales_bot"})
    ).status_code == 422


async def test_dev_task_private_stream_requires_owner(dev_env, admin_client, db_session):
    await _bot(db_session)
    result = await admin_client.post(
        "/api/dev/inject-message", json={"bot_key": "sales_bot", "text": "private"}
    )
    task_id = result.json()["data"]["task_id"]
    event = (await db_session.scalars(select(InboundEvent))).one()
    event.platform = "feishu"
    event.chat_type = "single"
    await db_session.commit()
    assert (await admin_client.get(f"/api/dev/tasks/{task_id}")).status_code == 404
