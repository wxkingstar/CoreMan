"""个人助理所有者在场时经云签名代理的低风险写：暂存、发确认卡，所有者确认后平台原样执行一次。

反向用例：取消、过期、非所有者点击、重复点击与并发点击、确认前后风险变高、助理停用、代理配置
缺失都不执行；放不下的不暂存；普通员工的低风险写、助理的读与高风险写与原来一致。
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bus import tasks
from coreman.core.cloud_proxy import confirm
from coreman.core.cloud_proxy import service as cloud_service
from coreman.core.db.models import (
    AssistantApproval,
    Bot,
    InboundEvent,
    InteractionState,
    OutboxItem,
    Task,
    User,
    UserIdentity,
)
from coreman.core.db.models.cloud_proxy import CloudCall, CloudPendingApproval
from coreman.core.systems_catalog import confirm as systems_confirm
from coreman.core.timeutils import utcnow
from coreman.runtime.worker import cloud_write_cards
from coreman.runtime.worker.card_actions import CardActionHandler
from tests.api.test_assistant_proxied_writes import (
    ACL,
    HIGH,
    LOW,
    SLB,
    SLB_HOST,
    cloud_bearer,
    configure,
    slb_form,
    writes,
)
from tests.integration.credential_helpers import owner as standard_owner
from tests.integration.lifecycle_helpers import APP_ID, TENANT, active_assistant
from tests.integration.worker_helpers import build_ctx

pytestmark = pytest.mark.usefixtures("assistant_master")

CHAT = "oc_private"


@pytest.fixture
def aliyun_mock(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    with respx.MockRouter(assert_all_called=False) as router:
        monkeypatch.setattr(
            cloud_service,
            "transport_factory",
            lambda url: httpx.MockTransport(router.async_handler),
        )
        router.post(SLB_HOST).mock(return_value=httpx.Response(200, json={"RequestId": "r-ok"}))
        yield router


@pytest.fixture
def settings(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """代理配置齐全；worker 读的设置与 API 的同一份。"""
    configure(app)
    monkeypatch.setattr(cloud_write_cards, "get_settings", lambda: app.state.settings)


async def owner_present(session: AsyncSession) -> tuple[Bot, User, Task]:
    """开通好的助理与所有者，所有者在私聊里打字发来的一轮（正在跑）。"""
    bot, owner, task = await active_assistant(session)
    event = await session.get(InboundEvent, task.inbound_event_id)
    assert event is not None
    event.sender_open_id = "ou_owner"
    event.payload = {
        **event.payload,
        "sender": {**event.payload["sender"], "sender_type": "user"},
        "raw": {
            "header": {"tenant_key": TENANT, "app_id": APP_ID},
            "event": {
                "sender": {
                    "sender_type": "user",
                    "sender_id": {"user_id": "owner_pid", "open_id": "ou_owner"},
                },
                "message": {"chat_type": "p2p", "chat_id": event.chat_id},
            },
        },
    }
    task.status, task.user_id = "running", owner.id
    await session.commit()
    return bot, owner, task


async def post(client: httpx.AsyncClient, app: Any, bot: Bot, owner: User, task: Task, body: bytes):  # type: ignore[no-untyped-def]
    return await client.post(SLB, content=body, headers=cloud_bearer(app, bot, owner, task))


async def batch(session: AsyncSession) -> InteractionState | None:
    return await session.scalar(
        select(InteractionState)
        .where(InteractionState.kind == confirm.KIND)
        .execution_options(populate_existing=True)
    )


async def release(app: Any, task: Task, *, send: bool = True) -> bool:
    async with app.state.session_factory() as session:
        sent = await confirm.release(session, bot_id=task.bot_id, task_id=task.id, send=send)
        await session.commit()
    return sent


async def sent_card(session: AsyncSession) -> OutboxItem:
    card = await session.scalar(
        select(OutboxItem)
        .where(OutboxItem.kind == "send")
        .order_by(OutboxItem.id.desc())
        .execution_options(populate_existing=True)
    )
    assert card is not None
    return card


def card_text(card: OutboxItem) -> str:
    return str(card.payload["card"]["body"]["elements"][0]["text"]["content"])


async def press(
    session: AsyncSession,
    bot: Bot,
    origin: Task,
    state_id: uuid.UUID,
    verb: str = "confirm",
    *,
    user_id: str = "owner_pid",
    open_id: str = "ou_owner",
) -> Task:
    """在发出的确认卡上点一下：回调按网关的样子落库并排成任务。"""
    card = await sent_card(session)
    if card.status != "sent":
        card.status = "sent"
        card.payload = {**card.payload, "_feishu_message_id": "om_card"}
    action = {
        "task_id": f"personal:{origin.id}",
        "event_key": confirm.CARD_EVENT,
        "level": f"{verb}:{state_id}",
    }
    event = InboundEvent(
        bot_id=bot.id,
        platform="feishu",
        platform_msg_id=str(uuid.uuid4()),
        kind="card_action",
        chat_type="single",
        chat_id=CHAT,
        sender_platform_user_id=user_id,
        sender_open_id=open_id,
        reply_context={"chat_id": CHAT, "message_id": "om_card"},
        payload={
            "raw": {
                "header": {
                    "event_type": "card.action.trigger",
                    "app_id": APP_ID,
                    "tenant_key": TENANT,
                },
                "event": {
                    "operator": {"user_id": user_id, "open_id": open_id},
                    "context": {"open_chat_id": CHAT, "open_message_id": "om_card"},
                    "action": {"value": action},
                },
            }
        },
    )
    session.add(event)
    await session.flush()
    task = await tasks.enqueue(
        session,
        tasks.NewTask(
            bot_id=bot.id,
            kind="card_action",
            lane="fast",
            payload={"card_action": action, "platform_user_id": user_id},
            session_key=CHAT,
            inbound_event_id=event.id,
            dedupe_key=f"click:{event.id}",
        ),
    )
    assert task is not None
    task.status = "running"
    await session.commit()
    return task


async def run_click(session: AsyncSession, engine: AsyncEngine, pressed: Task) -> str:
    await CardActionHandler().run(build_ctx(engine, pressed))
    done = await session.get(Task, pressed.id, populate_existing=True)
    assert done is not None and done.result is not None
    return str(done.result["card"])


async def cloud_calls(session: AsyncSession) -> list[tuple[str, str, int | None]]:
    rows = await session.scalars(
        select(CloudCall).order_by(CloudCall.id).execution_options(populate_existing=True)
    )
    return [(row.risk, row.outcome, row.status_code) for row in rows]


async def resumed(session: AsyncSession, state: InteractionState) -> str | None:
    task = await session.scalar(
        select(Task)
        .where(Task.dedupe_key == f"cloud-write:{state.id}:resume")
        .execution_options(populate_existing=True)
    )
    if task is None:
        return None
    assert task.kind == "chat" and task.payload["serialize_session"] is True
    return str(task.payload[confirm.RESULT_KEY])


async def last_update(session: AsyncSession) -> str:
    item = await session.scalar(
        select(OutboxItem)
        .where(OutboxItem.kind == "card_update")
        .order_by(OutboxItem.id.desc())
        .execution_options(populate_existing=True)
    )
    assert item is not None
    return str(item.payload["card"]["body"]["elements"][0]["text"]["content"])


async def staged_and_sent(
    client: httpx.AsyncClient, app: Any, session: AsyncSession, *bodies: bytes
) -> tuple[Bot, User, Task, InteractionState]:
    bot, owner, task = await owner_present(session)
    for body in bodies or (LOW,):
        r = await post(client, app, bot, owner, task, body)
        assert r.status_code == 202, r.text
    assert await release(app, task)
    state = await batch(session)
    assert state is not None
    return bot, owner, task, state


# 暂存 ------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("settings")
async def test_a_present_low_risk_write_is_staged_not_sent(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, aliyun_mock: respx.MockRouter
) -> None:
    bot, owner, task = await owner_present(db_session)
    r = await post(client, app, bot, owner, task, LOW)
    assert r.status_code == 202, r.text
    data = r.json()
    assert data["status"] == "pending_confirmation" and data["pending"] == 1
    assert "确认卡" in data["hint"] and "不要重试" in data["hint"]
    assert writes(aliyun_mock) == []
    state = await batch(db_session)
    assert state is not None and state.status == "open" and not state.state.get("sent")
    [item] = state.state["items"]
    assert item["product"] == "slb" and item["params"] == dict(urllib.parse.parse_qsl(LOW.decode()))
    # 暂存的请求在库里是所有者数据密钥的密文：原始 SQL 看不到参数。
    stored = await db_session.scalar(
        select(InteractionState.state).where(InteractionState.id == state.id)
    )
    assert "8.8.8.8" not in json.dumps(stored) and "_sealed" in stored
    rows = list(
        await db_session.scalars(
            select(CloudCall).order_by(CloudCall.id).execution_options(populate_existing=True)
        )
    )
    assert [(c.risk, c.outcome, c.status_code) for c in rows] == [("write_low", "proposed", 202)]
    assert rows[0].request_hash == item["digest"] and rows[0].approval_id is None
    # 不碰 Tower：不建挂起单、不建个人助理审批单。
    assert await db_session.scalar(select(CloudPendingApproval)) is None
    assert await db_session.scalar(select(AssistantApproval)) is None
    # 同一轮重复调用同一请求：不重复暂存，卡片上只出现一次。
    again = await post(client, app, bot, owner, task, LOW)
    assert again.status_code == 202 and again.json()["duplicate"] is True
    state = await batch(db_session)
    assert state is not None and len(state.state["items"]) == 1
    assert writes(aliyun_mock) == []


@pytest.mark.usefixtures("settings")
async def test_the_card_goes_out_when_the_turn_ends_and_lists_every_parameter(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, aliyun_mock: respx.MockRouter
) -> None:
    bot, owner, task = await owner_present(db_session)
    for body in (LOW, LOW, slb_form("8.8.4.4")):
        await post(client, app, bot, owner, task, body)
    assert await release(app, task)
    assert not await release(app, task)  # 只发一次
    card = await sent_card(db_session)
    assert card.target == {"chat_id": CHAT}
    assert card.payload["card"]["task_id"] == f"personal:{task.id}"
    text = card_text(card)
    assert "AddAccessControlListEntry" in text and "slb" in text
    assert f'AclId="{ACL}"' in text
    assert "8.8.8.8" in text and "8.8.4.4" in text and text.count("1. ") == 1
    assert "确认：执行 2 个云服务写操作" in json.dumps(card.payload["card"], ensure_ascii=False)
    state = await batch(db_session)
    assert state is not None and state.state["sent"] is True
    assert writes(aliyun_mock) == []


@pytest.mark.usefixtures("settings")
async def test_a_turn_that_did_not_end_well_drops_the_batch(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, aliyun_mock: respx.MockRouter
) -> None:
    bot, owner, task = await owner_present(db_session)
    await post(client, app, bot, owner, task, LOW)
    assert not await release(app, task, send=False)
    state = await batch(db_session)
    assert state is not None and state.status == "cancelled"
    assert await db_session.scalar(select(OutboxItem).where(OutboxItem.kind == "send")) is None


@pytest.mark.usefixtures("settings")
async def test_hidden_characters_are_shown_escaped(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    aliyun_mock: respx.MockRouter,
) -> None:
    """行分隔符、方向覆盖、零宽字符在卡片上转义成可见的 \\uXXXX；执行的仍是原样那一份。"""
    comment = "第一行 AclId=forged‮evil​end\r\nx"
    entries = json.dumps([{"entry": "8.8.8.8", "comment": comment}], ensure_ascii=False)
    params = {"Action": "AddAccessControlListEntry", "AclId": ACL, "AclEntrys": entries}
    body = urllib.parse.urlencode(sorted(params.items())).encode()
    bot, _, task, state = await staged_and_sent(client, app, db_session, body)
    text = card_text(await sent_card(db_session))
    for char in (" ", "‮", "​", "\r"):
        assert char not in text
    assert "\\u2028" in text and "\\u202E" in text and "\\u200B" in text
    # 一个参数一行：伪造的 AclId 不会单独成行。
    lines = [line.strip() for line in text.split("\n")]
    assert [line for line in lines if line.startswith("AclId=")] == [f'AclId="{ACL}"']
    assert await run_click(db_session, db_engine, await press(db_session, bot, task, state.id)) == (
        "cloud_write_done"
    )
    [form] = writes(aliyun_mock)
    assert form["AclEntrys"] == entries


@pytest.mark.usefixtures("settings")
async def test_a_request_too_large_for_the_card_is_not_staged(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, aliyun_mock: respx.MockRouter
) -> None:
    bot, owner, task = await owner_present(db_session)
    entries = json.dumps([{"entry": "8.8.8.8", "comment": "c" * 13000}])
    params = {"Action": "AddAccessControlListEntry", "AclId": ACL, "AclEntrys": entries}
    body = urllib.parse.urlencode(sorted(params.items())).encode()
    r = await post(client, app, bot, owner, task, body)
    assert r.status_code == 403 and r.json()["error"] == confirm.TOO_LARGE
    assert await batch(db_session) is None and writes(aliyun_mock) == []
    assert await cloud_calls(db_session) == [("write_low", "denied", 403)]
    # 字数不超、字节超（卡片按序列化后的大小算）：同样不暂存。
    wide = json.dumps([{"entry": "8.8.8.8", "comment": "云" * 9000}], ensure_ascii=False)
    params["AclEntrys"] = wide
    body = urllib.parse.urlencode(sorted(params.items())).encode()
    r = await post(client, app, bot, owner, task, body)
    assert r.status_code == 403 and r.json()["error"] == confirm.TOO_LARGE
    assert await batch(db_session) is None


@pytest.mark.usefixtures("settings")
async def test_a_turn_the_owner_cannot_confirm_on_the_spot_sends_nothing(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, aliyun_mock: respx.MockRouter
) -> None:
    """在场的一轮但不是所有者本人在私聊里打字（如插话、协作等带 origin 的轮次）：不执行也不暂存。"""
    bot, owner, task = await owner_present(db_session)
    task.payload = {**task.payload, "origin": "dispatch"}
    await db_session.commit()
    r = await post(client, app, bot, owner, task, LOW)
    assert r.status_code == 403 and r.json()["error"] == "confirmation_unavailable"
    assert writes(aliyun_mock) == [] and await batch(db_session) is None
    assert await cloud_calls(db_session) == [("write_low", "denied", 403)]


# 确认与执行 ------------------------------------------------------------------------------------


@pytest.mark.usefixtures("settings")
async def test_the_owner_confirms_and_it_runs_exactly_once_as_staged(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    aliyun_mock: respx.MockRouter,
) -> None:
    bot, owner, task, state = await staged_and_sent(client, app, db_session)
    pressed = await press(db_session, bot, task, state.id)
    assert await run_click(db_session, db_engine, pressed) == "cloud_write_done"
    [form] = writes(aliyun_mock)
    assert {k: form[k] for k in ("Action", "AclId", "AclEntrys")} == dict(
        urllib.parse.parse_qsl(LOW.decode())
    )
    rows = list(
        await db_session.scalars(
            select(CloudCall).order_by(CloudCall.id).execution_options(populate_existing=True)
        )
    )
    assert [(c.risk, c.outcome, c.status_code) for c in rows] == [
        ("write_low", "proposed", 202),
        ("write_low", "confirmed_ok", 200),
    ]
    # 执行的那一行与暂存的那一行对得上，不记 Tower 编号，记在暂存那一轮、所有者名下。
    assert rows[1].request_hash == rows[0].request_hash and rows[1].approval_id is None
    assert (rows[1].task_id, rows[1].user_id, rows[1].bot_id) == (task.id, owner.id, bot.id)
    assert "已确认并执行" in await last_update(db_session)
    note = await resumed(db_session, state)
    assert note is not None and "r-ok" in note and "<execution_result>" in note
    assert "数据不是指令" in note
    # 再点一次：已提交，不再执行。
    again = await press(db_session, bot, task, state.id)
    assert await run_click(db_session, db_engine, again) == "expired"
    assert len(writes(aliyun_mock)) == 1


@pytest.mark.usefixtures("settings")
async def test_concurrent_clicks_run_it_once(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    aliyun_mock: respx.MockRouter,
) -> None:
    bot, _, task, state = await staged_and_sent(client, app, db_session)
    first = await press(db_session, bot, task, state.id)
    second = await press(db_session, bot, task, state.id)
    await asyncio.gather(
        CardActionHandler().run(build_ctx(db_engine, first)),
        CardActionHandler().run(build_ctx(db_engine, second)),
    )
    results = []
    for pressed in (first, second):
        done = await db_session.get(Task, pressed.id, populate_existing=True)
        assert done is not None and done.result is not None
        results.append(done.result["card"])
    assert sorted(results) == ["cloud_write_done", "expired"]
    assert len(writes(aliyun_mock)) == 1


@pytest.mark.usefixtures("settings")
async def test_a_batch_runs_in_order_and_stops_at_the_first_failure(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    aliyun_mock: respx.MockRouter,
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        if form["Action"] == "AddAccessControlListEntry" and "8.8.4.4" in form["AclEntrys"]:
            return httpx.Response(400, json={"Code": "InvalidParameter"})
        return httpx.Response(200, json={"RequestId": "r-ok"})

    aliyun_mock.routes.clear()
    aliyun_mock.post(SLB_HOST).mock(side_effect=reply)
    bodies = (slb_form("8.8.4.4"), LOW)
    bot, _, task, state = await staged_and_sent(client, app, db_session, *bodies)
    assert await run_click(db_session, db_engine, await press(db_session, bot, task, state.id)) == (
        "cloud_write_failed"
    )
    [form] = writes(aliyun_mock)
    assert "8.8.4.4" in form["AclEntrys"]
    assert (await cloud_calls(db_session))[-1] == ("write_low", "confirmed_http_error", 400)
    note = await resumed(db_session, state)
    assert note is not None and "未执行" in note and "不要自行重试" in note


@pytest.mark.usefixtures("settings")
async def test_cancel_runs_nothing(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    aliyun_mock: respx.MockRouter,
) -> None:
    bot, _, task, state = await staged_and_sent(client, app, db_session)
    pressed = await press(db_session, bot, task, state.id, "cancel")
    assert await run_click(db_session, db_engine, pressed) == "cloud_write_cancelled"
    assert writes(aliyun_mock) == []
    reloaded = await batch(db_session)
    assert reloaded is not None and reloaded.status == "cancelled"
    note = await resumed(db_session, state)
    assert note is not None and "取消" in note
    # 取消之后再点确认也不执行。
    late = await press(db_session, bot, task, state.id)
    assert await run_click(db_session, db_engine, late) == "expired"
    assert writes(aliyun_mock) == []


@pytest.mark.usefixtures("settings")
async def test_an_expired_card_runs_nothing(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    aliyun_mock: respx.MockRouter,
) -> None:
    bot, _, task, state = await staged_and_sent(client, app, db_session)
    state.expires_at = utcnow() - timedelta(seconds=1)
    await db_session.commit()
    assert await run_click(db_session, db_engine, await press(db_session, bot, task, state.id)) == (
        "expired"
    )
    assert writes(aliyun_mock) == [] and "过期" in await last_update(db_session)
    assert await resumed(db_session, state) is None


@pytest.mark.usefixtures("settings")
async def test_an_expired_card_is_resent_by_the_owner_and_runs_once_confirmed(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    aliyun_mock: respx.MockRouter,
) -> None:
    bot, _, task, state = await staged_and_sent(client, app, db_session)
    state.expires_at = utcnow() - timedelta(seconds=1)
    await db_session.commit()
    late = await press(db_session, bot, task, state.id)
    assert await run_click(db_session, db_engine, late) == "expired"
    assert "重新发起" in await last_update(db_session) and writes(aliyun_mock) == []
    resend = await press(db_session, bot, task, state.id, systems_confirm.RESEND)
    assert await run_click(db_session, db_engine, resend) == "cloud_write_resent"
    card = await sent_card(db_session)
    assert card.dedupe_key == f"cloud-write:{state.id}:resend:1"
    assert "AddAccessControlListEntry" in card_text(card)
    pressed = await press(db_session, bot, task, state.id)
    assert await run_click(db_session, db_engine, pressed) == "cloud_write_done"
    assert len(writes(aliyun_mock)) == 1


@pytest.mark.usefixtures("settings")
async def test_only_the_owner_can_confirm(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    aliyun_mock: respx.MockRouter,
) -> None:
    bot, _, task, state = await staged_and_sent(client, app, db_session)
    other = User(login_name="lisi", display_name="李四", source="sync")
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        UserIdentity(
            user_id=other.id, platform="feishu", platform_user_id="lisi_pid", open_id="ou_lisi"
        )
    )
    await db_session.commit()
    pressed = await press(db_session, bot, task, state.id, user_id="lisi_pid", open_id="ou_lisi")
    assert await run_click(db_session, db_engine, pressed) == "ignored"
    # 所有者的身份但不是平台收到的那张卡上的会话：同样无效。
    forged = await press(db_session, bot, task, state.id)
    event = await db_session.get(InboundEvent, forged.inbound_event_id)
    assert event is not None
    event.payload = {
        "raw": {
            **event.payload["raw"],
            "header": {**event.payload["raw"]["header"], "app_id": "cli_other"},
        }
    }
    await db_session.commit()
    assert await run_click(db_session, db_engine, forged) == "ignored"
    assert writes(aliyun_mock) == []
    reloaded = await batch(db_session)
    assert reloaded is not None and reloaded.status == "open"


@pytest.mark.usefixtures("settings")
async def test_a_write_that_became_high_risk_is_not_executed(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    aliyun_mock: respx.MockRouter,
) -> None:
    bot, _, task, state = await staged_and_sent(client, app, db_session, slb_form("8.8.4.4"), LOW)
    # 确认之前部署策略变了：这个 ACL 不再受管，现在执行算高风险。
    configure(app, risk_policy=None)
    assert await run_click(db_session, db_engine, await press(db_session, bot, task, state.id)) == (
        "cloud_write_refused"
    )
    assert writes(aliyun_mock) == []
    assert (await cloud_calls(db_session))[-1] == ("write_high", "confirmed_denied", None)
    note = await resumed(db_session, state)
    assert note is not None and "risk_changed" in note and "整批都没有执行" in note


@pytest.mark.usefixtures("settings")
async def test_a_stopped_assistant_does_not_execute(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    aliyun_mock: respx.MockRouter,
) -> None:
    bot, _, task, state = await staged_and_sent(client, app, db_session)
    bot.assistant_state = "deprovisioning"
    await db_session.commit()
    assert await run_click(db_session, db_engine, await press(db_session, bot, task, state.id)) == (
        "cloud_write_unavailable"
    )
    assert writes(aliyun_mock) == []
    assert [o for _, o, _ in await cloud_calls(db_session)] == ["proposed"]


@pytest.mark.usefixtures("settings")
async def test_an_unconfigured_proxy_does_not_execute(
    client: httpx.AsyncClient,
    app: Any,
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    aliyun_mock: respx.MockRouter,
) -> None:
    bot, _, task, state = await staged_and_sent(client, app, db_session)
    app.state.settings = app.state.settings.model_copy(
        update={"aliyun_proxy_access_key_secret": None}
    )
    assert await run_click(db_session, db_engine, await press(db_session, bot, task, state.id)) == (
        "cloud_write_unavailable"
    )
    assert writes(aliyun_mock) == []
    note = await resumed(db_session, state)
    assert note is not None and "云签名代理的配置不全" in note


# 不变的路径 ------------------------------------------------------------------------------------


@pytest.mark.usefixtures("settings")
async def test_assistant_reads_and_high_risk_writes_are_unchanged(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, aliyun_mock: respx.MockRouter
) -> None:
    bot, owner, task = await owner_present(db_session)
    read = urllib.parse.urlencode(
        {"Action": "DescribeAccessControlListAttribute", "AclId": ACL}
    ).encode()
    r = await post(client, app, bot, owner, task, read)
    assert r.status_code == 200 and r.json() == {"RequestId": "r-ok"}
    r = await post(client, app, bot, owner, task, HIGH)
    assert r.status_code == 428 and r.json()["error"] == "approval_required"
    assert writes(aliyun_mock) == [] and await batch(db_session) is None
    assert [(risk, o) for risk, o, _ in await cloud_calls(db_session)] == [
        ("read", "ok"),
        ("write_high", "approval_required"),
    ]


@pytest.mark.usefixtures("settings")
async def test_an_ordinary_employee_low_risk_write_goes_straight_through(
    client: httpx.AsyncClient, app: Any, db_session: AsyncSession, aliyun_mock: respx.MockRouter
) -> None:
    """范围边界：不是个人助理的员工在同样的私聊里，低风险写与 main 一致——直接签名转发。"""
    bot, user, task, _ = await standard_owner(db_session)
    r = await post(client, app, bot, user, task, LOW)
    assert r.status_code == 200 and r.json() == {"RequestId": "r-ok"}
    [form] = writes(aliyun_mock)
    assert form["AclEntrys"] == dict(urllib.parse.parse_qsl(LOW.decode()))["AclEntrys"]
    rows = list(await db_session.scalars(select(CloudCall)))
    assert [(c.risk, c.outcome, c.status_code, c.request_hash, c.approval_id) for c in rows] == [
        ("write_low", "ok", 200, None, None)
    ]
    assert await batch(db_session) is None
    assert await db_session.scalar(select(OutboxItem)) is None
