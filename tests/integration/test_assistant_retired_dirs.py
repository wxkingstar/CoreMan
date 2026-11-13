"""要删的旧工作目录：登记一次、经节点删掉、节点不在就退避重试、不是这位助理的或还在用的不删；
以及看护进程经助理应用给本人发消息的那一层。"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.assistants import lifecycle, retired_dirs
from coreman.core.db.models import AssistantRetiredDir, RelayServer
from coreman.core.db.session import make_session_factory
from coreman.core.platforms.feishu import FeishuClient
from coreman.core.timeutils import utcnow
from tests.integration.lifecycle_helpers import CIPHER, FakeNode, assistant_app, pool_relay


async def retired(session: AsyncSession, relay: RelayServer, path: str = "/srv/pa/old") -> None:
    await retired_dirs.retire(session, bot_id=uuid.uuid4(), relay_id=relay.id, working_dir=path)
    await retired_dirs.retire(session, bot_id=uuid.uuid4(), relay_id=relay.id, working_dir=path)
    await session.commit()


async def rows(session: AsyncSession) -> list[AssistantRetiredDir]:
    return list(
        await session.scalars(select(AssistantRetiredDir).execution_options(populate_existing=True))
    )


async def test_a_directory_is_registered_once_and_removed_by_its_node(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = FakeNode()
    monkeypatch.setattr(retired_dirs, "call_agent", node)
    relay = await pool_relay(db_session)
    await retired(db_session, relay)
    assert len(await rows(db_session)) == 1
    counts = await retired_dirs.tick(make_session_factory(db_engine), CIPHER, utcnow())
    assert counts == {"assistant_dirs_removed": 1, "assistant_dirs_retry": 0}
    ((operation, payload),) = node.calls
    assert operation == "workspace-remove" and payload["working_dir"] == "/srv/pa/old"
    assert await rows(db_session) == []


@pytest.mark.parametrize(
    ("answer", "kept", "code"),
    [
        ({"down": True}, True, None),
        ({"removed": False, "existed": True, "busy": True}, True, "busy"),
        ({"removed": True, "existed": True}, False, None),
        ({"removed": False, "existed": True}, False, None),
        ({"removed": True, "existed": False}, False, None),
        # 节点认不准是不是这位助理的目录：不管别的字段怎么写，都留着登记再来。
        ({"removed": False, "existed": True, "unverified": True}, True, "dir_unverified"),
        ({"removed": False, "existed": False, "unverified": True}, True, "dir_unverified"),
        ({"removed": True, "existed": False, "unverified": True}, True, "dir_unverified"),
        # 不像样的回答不算定论。
        ({}, True, "bad_node_answer"),
        ({"removed": True}, True, "bad_node_answer"),
        ({"removed": "true", "existed": False}, True, "bad_node_answer"),
        ({"removed": True, "existed": 1}, True, "bad_node_answer"),
        ({"removed": True, "existed": False, "unverified": "yes"}, True, "bad_node_answer"),
    ],
)
async def test_what_the_node_says_decides_whether_to_try_again(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    answer: dict,
    kept: bool,
    code: str | None,
) -> None:
    node = FakeNode()
    node.down = bool(answer.pop("down", False))
    node.removed = answer
    monkeypatch.setattr(retired_dirs, "call_agent", node)
    relay = await pool_relay(db_session)
    await retired(db_session, relay)
    now = utcnow()
    await retired_dirs.tick(make_session_factory(db_engine), CIPHER, now)
    left = await rows(db_session)
    assert bool(left) is kept
    if kept:
        assert left[0].attempts == 1 and left[0].next_attempt_at > now
        if code is not None:
            assert left[0].error_code == code
        # 退避期内不再打扰节点。
        before = len(node.calls)
        await retired_dirs.tick(make_session_factory(db_engine), CIPHER, now)
        assert len(node.calls) == before


async def test_a_directory_in_use_again_is_never_removed(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.assistant_helpers import seed_assistant

    node = FakeNode()
    monkeypatch.setattr(retired_dirs, "call_agent", node)
    relay = await pool_relay(db_session)
    bot, _ = await seed_assistant(db_session)
    bot.relay_server_id, bot.working_dir = relay.id, "/srv/pa/live"
    await retired_dirs.retire(
        db_session, bot_id=bot.id, relay_id=relay.id, working_dir="/srv/pa/live"
    )
    await db_session.commit()
    await retired_dirs.tick(make_session_factory(db_engine), CIPHER, utcnow())
    assert node.calls == [] and await rows(db_session) == []


async def test_an_unexpected_error_postpones_only_that_directory(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: object, **_kw: object) -> dict:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(retired_dirs, "call_agent", broken)
    relay = await pool_relay(db_session)
    await retired(db_session, relay)
    now = utcnow()
    await retired_dirs.tick(make_session_factory(db_engine), CIPHER, now)
    (row,) = await rows(db_session)
    assert row.error_code == "unexpected" and row.next_attempt_at >= now + timedelta(hours=1)


async def test_deleting_the_runtime_instance_drops_its_rows(db_session: AsyncSession) -> None:
    relay = await pool_relay(db_session)
    await retired(db_session, relay)
    await db_session.delete(relay)
    await db_session.commit()
    assert await db_session.scalar(select(func.count()).select_from(AssistantRetiredDir)) == 0


async def test_the_messenger_sends_through_the_assistant_app(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = await assistant_app(db_session)
    await db_session.commit()
    sent: list[dict] = []
    reply = {"code": 0, "data": {"message_id": "om_1", "chat_id": "oc_1"}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 7200})
        sent.append({"params": dict(request.url.params), **json.loads(request.content)})
        return httpx.Response(200, json=reply)

    def client(app_id: str, secret: str) -> FeishuClient:
        assert (app_id, secret) == ("cli_example", "app-secret")
        http = httpx.AsyncClient(
            base_url="https://open.feishu.cn", transport=httpx.MockTransport(handler)
        )
        return FeishuClient(app_id, secret, http=http)

    monkeypatch.setattr(lifecycle, "FeishuClient", client)
    messenger = lifecycle.Messenger(CIPHER)
    card = {"schema": "2.0"}
    assert await messenger.card(db_session, app.id, "zhangsan", card, key="k") == ("om_1", "oc_1")
    assert sent[0]["params"] == {"receive_id_type": "user_id"}
    assert sent[0]["msg_type"] == "interactive" and json.loads(sent[0]["content"]) == card
    assert sent[0]["receive_id"] == "zhangsan" and len(sent[0]["uuid"]) == 32
    reply = {"code": 230013, "msg": "synthetic"}
    with pytest.raises(lifecycle.StepRetry) as failed:
        await messenger.text(db_session, app.id, "zhangsan", "你好", key="k2")
    assert failed.value.code == "feishu_230013"
    reply = {"code": 0, "data": {}}
    with pytest.raises(lifecycle.StepRetry):
        await messenger.text(db_session, app.id, "zhangsan", "你好", key="k3")
    with pytest.raises(lifecycle.StepFailed):
        await messenger.text(db_session, uuid.uuid4(), "zhangsan", "你好", key="k4")
    # data 不是对象：暂时失败，带固定的代码。
    for data in (["om_1"], "om_1", 5):
        reply = {"code": 0, "data": data}
        with pytest.raises(lifecycle.StepRetry) as failed:
            await messenger.text(db_session, app.id, "zhangsan", "你好", key="k5")
        assert failed.value.code == "feishu_bad_response"
    await messenger.aclose()


async def test_an_unreadable_app_secret_is_a_temporary_failure(db_session: AsyncSession) -> None:
    """应用密钥解不开（例如主密钥轮换没做完）：稍后再试，不放弃开通。"""
    from coreman.core.assistants.apps import SECRET_AAD
    from coreman.core.crypto import Cipher

    app = await assistant_app(db_session)
    app.secret_enc = Cipher(b"\x01" * 32).encrypt("app-secret", SECRET_AAD)
    await db_session.commit()
    messenger = lifecycle.Messenger(CIPHER)
    with pytest.raises(lifecycle.StepRetry) as failed:
        await messenger.text(db_session, app.id, "zhangsan", "你好", key="k")
    assert failed.value.code == "app_secret_unreadable"
    await messenger.aclose()


def test_the_node_operation_is_on_the_platform_side_list() -> None:
    from coreman.core.relay import agent_client

    assert "workspace-remove" in agent_client.OPERATIONS


async def test_a_row_another_scheduler_just_handled_is_not_sent_to_the_node_again(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """列出到期的之后、锁住之前，别的看护进程处理了它并往后推：这一轮不再调用节点。"""
    from contextlib import asynccontextmanager

    from sqlalchemy import update

    node = FakeNode()
    monkeypatch.setattr(retired_dirs, "call_agent", node)
    relay = await pool_relay(db_session)
    await retired(db_session, relay)
    real = make_session_factory(db_engine)
    opened = 0

    @asynccontextmanager
    async def interleaved():  # type: ignore[no-untyped-def]
        nonlocal opened
        opened += 1
        if opened == 2:
            async with real() as other:
                await other.execute(
                    update(AssistantRetiredDir).values(
                        attempts=1, next_attempt_at=utcnow() + timedelta(hours=1)
                    )
                )
                await other.commit()
        async with real() as session:
            yield session

    counts = await retired_dirs.tick(interleaved, CIPHER, utcnow())  # type: ignore[arg-type]
    assert node.calls == []
    assert counts == {"assistant_dirs_removed": 0, "assistant_dirs_retry": 0}
    (row,) = await rows(db_session)
    assert row.attempts == 1


async def test_one_round_stops_taking_rows_once_its_time_is_spent(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]

    class SlowNode(FakeNode):
        async def __call__(self, *args, **kw):  # type: ignore[no-untyped-def]
            clock[0] += retired_dirs.NODE_SECONDS  # 每次都等到超时那么久
            return await super().__call__(*args, **kw)

    node = SlowNode()
    node.down = True
    monkeypatch.setattr(retired_dirs, "call_agent", node)
    monkeypatch.setattr(retired_dirs, "_clock", lambda: clock[0])
    relay = await pool_relay(db_session)
    for index in range(4):
        await retired(db_session, relay, path=f"/srv/pa/old-{index}")
    counts = await retired_dirs.tick(make_session_factory(db_engine), CIPHER, utcnow())
    assert counts["assistant_dirs_retry"] == 2
    assert sorted(row.attempts for row in await rows(db_session)) == [0, 0, 1, 1]


async def test_an_unverified_directory_backs_off_up_to_the_cap(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = FakeNode()
    node.removed = {"removed": False, "existed": True, "unverified": True}
    monkeypatch.setattr(retired_dirs, "call_agent", node)
    relay = await pool_relay(db_session)
    await retired(db_session, relay)
    await db_session.execute(update(AssistantRetiredDir).values(attempts=20))
    await db_session.commit()
    now = utcnow()
    await retired_dirs.tick(make_session_factory(db_engine), CIPHER, now)
    (row,) = await rows(db_session)
    assert row.error_code == "dir_unverified" and row.attempts == 21
    assert row.next_attempt_at - now <= retired_dirs.BACKOFF_MAX + timedelta(seconds=5)
    assert row.next_attempt_at - now >= retired_dirs.BACKOFF_MAX - timedelta(seconds=5)
