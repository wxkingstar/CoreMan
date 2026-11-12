"""要删的旧工作目录：登记一次、经节点删掉、节点不在就退避重试、不是这位助理的或还在用的不删；
以及看护进程经助理应用给本人发消息的那一层。"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select
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
    ("answer", "kept"),
    [
        ({"down": True}, True),
        ({"removed": False, "existed": True, "busy": True}, True),
        ({"removed": False, "existed": True}, False),
        ({"removed": True, "existed": False}, False),
    ],
)
async def test_what_the_node_says_decides_whether_to_try_again(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    answer: dict,
    kept: bool,
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
    await messenger.aclose()


def test_the_node_operation_is_on_the_platform_side_list() -> None:
    from coreman.core.relay import agent_client

    assert "workspace-remove" in agent_client.OPERATIONS
