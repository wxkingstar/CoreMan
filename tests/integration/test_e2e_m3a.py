"""端到端：媒体消息、AskUserQuestion 全回路、限流切换，全部经真网关 + 真 worker 跑通。

与 `test_e2e_wecom.py` 同一套骨架（fake 企微 WS → 网关 → PostgreSQL → worker → 推回企微），
但 worker 挂齐五个处理器，并注入假媒体下载点。断的是三条主路径在「用户视角」的成品：

1. 图片消息：下载解密 → content parts 发给模型 → 回复推回企微，对话记录落 `image/success`。
2. AskUserQuestion：卡片发出 → 用户点选 → 卡片就地改成已答 → 同一会话续跑 → 主动推送结论。
3. 限流：额度总览表 + 切换卡片 → 用户选切换 → 机器人真的改绑到空闲实例。

这三条都跨了「worker 写库 → 网关读库推送 → 企微回调再进库」的完整来回，单元/组件测试各自
只覆盖其中一段。
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.db.models import Bot, ChatLog, InteractionState, RelayServer, User, UserIdentity
from coreman.core.db.session import make_session_factory
from coreman.core.i18n.messages import msg
from coreman.core.wecom.media import MediaFetcher
from coreman.runtime.gateway_wecom.service import GatewayWecomService
from coreman.runtime.gateway_wecom.ws_client import WsConfig
from coreman.runtime.worker.card_actions import CardActionHandler
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from coreman.runtime.worker.choice_submit import ChoiceSubmitHandler
from coreman.runtime.worker.commands import CommandHandler
from coreman.runtime.worker.relay_switch import RelaySwitchHandler
from coreman.runtime.worker.service import WorkerService
from tests.fakes.fake_media import FakeMedia
from tests.fakes.fake_relay import FakeRelay
from tests.fakes.fake_wecom_ws import FakeWeComWs
from tests.fakes.runtime_node import attach_node
from tests.integration.test_e2e_wecom import FAST_WS, _ready, _seed

KEY = "k" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x03" * 20


class _Stack:
    """一套跑起来的网关 + worker + 假企微 WS，`stop()` 负责排空收尾。"""

    def __init__(
        self,
        fake_ws: FakeWeComWs,
        gateway: GatewayWecomService,
        worker: WorkerService,
        tasks: list[asyncio.Task[None]],
    ) -> None:
        self.ws = fake_ws
        self.gateway = gateway
        self.worker = worker
        self._tasks = tasks

    async def stop(self) -> None:
        self.worker.request_stop("test")
        self.gateway.request_stop("test")
        await asyncio.wait_for(asyncio.gather(*self._tasks, return_exceptions=True), 60)
        await self.ws.stop()


async def _stack(
    relays: dict[str, FakeRelay], current: list[str], media: FakeMedia | None = None
) -> _Stack:
    fake_ws = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake_ws.start()
    gateway = GatewayWecomService(
        port=0,
        lease_interval=0.2,
        heartbeat_seconds=0.3,
        poll_interval=0.2,
        ws_config=WsConfig(url=url, **FAST_WS),
        stop_grace_seconds=10,
    )
    worker = WorkerService(
        port=0,
        handlers={
            "command": CommandHandler(),
            "chat": ChatTaskHandler(),
            "card_action": CardActionHandler(),
            "choice_submit": ChoiceSubmitHandler(),
            "relay_switch": RelaySwitchHandler(),
        },
        poll_interval=0.2,
        heartbeat_seconds=0.3,
        relay_client_factory=lambda _r: relays[current[0]].client(),
        media_fetcher=MediaFetcher((media or FakeMedia()).client()),
        stop_grace_seconds=10,
    )
    running = [asyncio.create_task(gateway.run()), asyncio.create_task(worker.run())]
    await _ready(gateway, worker)
    for _ in range(200):
        if "bot1" in fake_ws.connections:
            break
        await asyncio.sleep(0.05)
    assert "bot1" in fake_ws.connections
    return _Stack(fake_ws, gateway, worker, running)


def _finished(req_id: str) -> Any:
    def pred(frame: dict[str, Any]) -> bool:
        return (
            frame.get("cmd") == "aibot_respond_msg"
            and (frame.get("headers") or {}).get("req_id") == req_id
            and bool(((frame.get("body") or {}).get("stream") or {}).get("finish"))
        )

    return pred


def _sent(msgtype: str) -> Any:
    def pred(frame: dict[str, Any]) -> bool:
        return (
            frame.get("cmd") == "aibot_send_msg"
            and (frame.get("body") or {}).get("msgtype") == msgtype
        )

    return pred


async def _chat_logs(engine: AsyncEngine, count: int) -> list[ChatLog]:
    """等对话记录落库：`ChatLogWriter` 是后台批写，worker 返回时未必已经写完。"""
    async with make_session_factory(engine)() as session:
        for _ in range(150):
            logs = list((await session.execute(select(ChatLog))).scalars().all())
            if len(logs) >= count:
                return logs
            await asyncio.sleep(0.1)
    raise AssertionError(f"对话记录没有落到 {count} 条")


async def test_image_message_reaches_the_model_and_is_answered(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings: None
) -> None:
    await _seed(db_session)
    media = FakeMedia()
    url = media.add("/e2e-img", PNG, aeskey=KEY)
    relays = {"normal": FakeRelay("normal")}
    stack = await _stack(relays, ["normal"], media)
    try:
        req = await stack.ws.send_media("bot1", msgtype="image", url=url, aeskey=KEY, msgid="e2e-1")
        finish = await stack.ws.wait_frame(_finished(req), timeout=30)
        assert "你好，世界。" in finish["body"]["stream"]["content"]
        # 模型收到的是 content parts：提示语 + data URI 图片，原始 url/aeskey 不外传。
        content = relays["normal"].requests[0]["messages"][1]["content"]
        assert content[0] == {"type": "text", "text": msg("media_prompt_image")}
        assert content[1]["image_url"]["url"] == (
            "data:image/png;base64," + base64.b64encode(PNG).decode()
        )
        assert any(msg("downloading_image") in c for c, _ in stack.ws.stream_contents(req))
        logs = await _chat_logs(db_engine, 1)
        assert len(logs) == 1
        assert logs[0].message_type == "image" and logs[0].status == "success"
    finally:
        await stack.stop()


async def test_ask_user_question_full_loop(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings: None
) -> None:
    await _seed(db_session)
    relays = {"ask": FakeRelay("ask_user"), "submit": FakeRelay("submit_round")}
    current = ["ask"]
    stack = await _stack(relays, current)
    try:
        req = await stack.ws.send_message("bot1", text="帮我选", msgid="e2e-q")
        finish = await stack.ws.wait_frame(_finished(req), timeout=30)
        assert "**请选择**" in finish["body"]["stream"]["content"]
        await stack.ws.wait_frame(_sent("template_card"), timeout=30)
        card = stack.ws.sent_cards()[0]["template_card"]
        assert card["card_type"] == "vote_interaction"
        assert card["task_id"].startswith("choice@sales_bot@zs@")

        # 用户点了第一个选项：卡片就地换成「已回答」，答案带着同一个会话回给模型续跑。
        current[0] = "submit"
        ev = await stack.ws.send_card_event(
            "bot1",
            task_id=card["task_id"],
            selected={"choice_answer": ["opt_0"]},
            msgid="e2e-card",
        )
        upd = await stack.ws.wait_frame(
            lambda f: (
                f.get("cmd") == "aibot_respond_update_msg"
                and (f.get("headers") or {}).get("req_id") == ev
            ),
            timeout=30,
        )
        assert upd["body"]["template_card"]["main_title"]["title"] == "✅ 已回答"
        await stack.ws.wait_frame(
            lambda f: (
                _sent("markdown")(f)
                and str(f["body"]["markdown"]["content"]).startswith(msg("bg_done_prefix"))
            ),
            timeout=60,
        )
        body = relays["submit"].requests[0]
        assert body["messages"][1]["content"] == "[用户选择回答]\n1. 用哪个？ -> A"
        assert body["session_id"] == relays["ask"].requests[0]["session_id"]

        logs = await _chat_logs(db_engine, 2)
        async with make_session_factory(db_engine)() as session:
            assert (await session.execute(select(InteractionState))).scalars().all() == []
        assert sorted(log.status for log in logs) == ["ask_user", "success"]
    finally:
        await stack.stop()


async def test_rate_limit_offer_and_switch(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings: None
) -> None:
    bot = await _seed(db_session)
    creator = await db_session.get(User, bot.created_by)
    assert creator
    db_session.add(UserIdentity(user_id=creator.id, platform="wecom", platform_user_id="zs"))
    relay = await db_session.get(RelayServer, bot.relay_server_id)
    assert relay
    relay.rate_limit_5h_used_pct = Decimal("100")
    relay.rate_limit_7d_used_pct = Decimal("90")
    relay.rate_limit_probed_at = datetime.now(UTC)
    idle = RelayServer(name="idle", model_provider="claude")
    idle.rate_limit_5h_used_pct = Decimal("1")
    idle.rate_limit_7d_used_pct = Decimal("5")
    idle.rate_limit_probed_at = datetime.now(UTC)
    db_session.add(idle)
    await attach_node(db_session, idle)
    await db_session.commit()

    relays = {"limit": FakeRelay("rate_limit")}
    stack = await _stack(relays, ["limit"])
    try:
        req = await stack.ws.send_message("bot1", text="跑报表", msgid="e2e-rl")
        finish = await stack.ws.wait_frame(_finished(req), timeout=30)
        text = finish["body"]["stream"]["content"]
        assert "📊 服务器额度总览" in text and "| fake ⭐ |" in text
        await stack.ws.wait_frame(_sent("template_card"), timeout=30)
        card = stack.ws.sent_cards()[0]["template_card"]
        assert card["checkbox"]["question_key"] == "ratelimit_switch_choice"

        ev = await stack.ws.send_card_event(
            "bot1",
            task_id=card["task_id"],
            selected={"ratelimit_switch_choice": ["opt_switch"]},
            msgid="e2e-sw",
        )
        upd = await stack.ws.wait_frame(
            lambda f: (
                f.get("cmd") == "aibot_respond_update_msg"
                and (f.get("headers") or {}).get("req_id") == ev
            ),
            timeout=30,
        )
        assert upd["body"]["template_card"]["main_title"]["title"] == msg("rl_switching_title")
        await stack.ws.wait_frame(
            lambda f: (
                _sent("markdown")(f)
                and "已切换运行时" in str(f["body"]["markdown"]["content"])
            ),
            timeout=60,
        )
        async with make_session_factory(db_engine)() as session:
            fresh = await session.get(Bot, bot.id)
            assert fresh and fresh.relay_server_id == idle.id
            assert (await session.execute(select(InteractionState))).scalars().all() == []
    finally:
        await stack.stop()
