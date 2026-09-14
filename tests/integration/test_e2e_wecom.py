"""端到端：fake 企微 WS → 网关 → PostgreSQL → worker（fake relay）→ task_streams → 推送回企微。

真起两个进程级服务（`GatewayWecomService` / `WorkerService`）+ 一个本地 WS 服务端，
中间只有测试库这一条总线；断的是用户视角能看见的东西：思考过程、工具行、实时链接、
完成后缀、对话记录落库，以及 `stop` 同时打断在途任务的那条组合路径。
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, ENV_AAD
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, ChatLog, RelayServer, User
from coreman.core.db.session import make_session_factory
from coreman.core.i18n.messages import msg
from coreman.runtime.gateway_wecom.service import GatewayWecomService
from coreman.runtime.gateway_wecom.ws_client import WsConfig
from coreman.runtime.worker.chat_handler import ChatTaskHandler
from coreman.runtime.worker.commands import CommandHandler
from coreman.runtime.worker.service import WorkerService
from tests.fakes.fake_relay import FakeRelay
from tests.fakes.fake_wecom_ws import FakeWeComWs
from tests.integration.worker_helpers import MASTER

FAST_WS = dict(
    ping_interval=0.5,
    watchdog_interval=0.2,
    idle_timeout=5.0,
    reconnect_base=0.05,
    reconnect_max=0.2,
    subscribe_timeout=1.0,
)


async def _seed(session: AsyncSession) -> Bot:
    cipher = Cipher(MASTER)
    u = User(login_name="c", display_name="c")
    session.add(u)
    await session.flush()
    relay = RelayServer(name="fake", host="relay.test", clawrelay_port=80, model_provider="claude")
    session.add(relay)
    await session.flush()
    bot = Bot(
        bot_key="sales_bot",
        platform="wecom",
        name="小助手",
        created_by=u.id,
        relay_server_id=relay.id,
        model="vllm/claude-sonnet-4-6",
        working_dir="/d",
        system_prompt="你是销售",
        merged_system_prompt="你是销售",
        credentials_enc=cipher.encrypt(
            json.dumps({"bot_id": "bot1", "secret": "sec"}), CREDENTIALS_AAD
        ),
        env_vars_enc=cipher.encrypt(json.dumps({}), ENV_AAD),
    )
    session.add(bot)
    await session.commit()
    return bot


async def _ready(*services) -> None:  # type: ignore[no-untyped-def]
    for _ in range(200):
        if all(s.ready for s in services):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("服务未就绪")


def _is_push(frame: dict, req_id: str) -> bool:
    return (
        frame.get("cmd") == "aibot_respond_msg"
        and (frame.get("headers") or {}).get("req_id") == req_id
    )


async def test_wecom_message_round_trip_and_stop(
    db_engine: AsyncEngine, db_session: AsyncSession, runtime_settings
) -> None:  # type: ignore[no-untyped-def]
    await _seed(db_session)
    fake_ws = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake_ws.start()
    relays = {"normal": FakeRelay("normal"), "slow": FakeRelay("slow", chunk_delay=0.4)}
    current = ["normal"]
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
        handlers={"command": CommandHandler(), "chat": ChatTaskHandler()},
        poll_interval=0.2,
        heartbeat_seconds=0.3,
        relay_client_factory=lambda _r: relays[current[0]].client(),
        stop_grace_seconds=10,
    )
    tg, tw = asyncio.create_task(gateway.run()), asyncio.create_task(worker.run())
    try:
        await _ready(gateway, worker)
        for _ in range(100):
            if "bot1" in fake_ws.connections:
                break
            await asyncio.sleep(0.05)
        assert "bot1" in fake_ws.connections

        # ① 一条普通消息：思考过程 → 工具行 → 正文 → 完成后缀，中间至少推过一次增量。
        # 用 `slow`（帧间隔 0.4 秒）跑这一条：网关 0.2 秒轮询一次，无延迟的 `normal` 整条流
        # 几十毫秒就跑完，轮询第一眼常常看到的就是已完成的行，只推一帧 finish，
        # 下面「至少一帧非 finish」的存在性断言就落空。0.4 > 0.2 时首个 flush(force=True)
        # 的思考行必然先以 finish=false 推出。① 正常完成不计 abort，②的断言不受影响。
        current[0] = "slow"
        req = await fake_ws.send_message("bot1", text="你好", msgid="m1")
        finish = await fake_ws.wait_frame(
            lambda f: _is_push(f, req) and f["body"]["stream"]["finish"], timeout=15
        )
        content = finish["body"]["stream"]["content"]
        assert content.startswith("<think>\n")
        assert "</think>" in content
        assert "你好，世界。" in content
        assert content.endswith(msg("done_suffix"))
        assert "🔧 **Bash**" in content
        assert "📎 查看实时聊天记录" in content
        assert any(not f["body"]["stream"]["finish"] for f in fake_ws.frames if _is_push(f, req))

        async with make_session_factory(db_engine)() as s:
            logs: list[ChatLog] = []
            for _ in range(50):
                logs = list((await s.execute(select(ChatLog))).scalars().all())
                if logs:
                    break
                await asyncio.sleep(0.1)
            assert len(logs) == 1
            assert logs[0].status == "success"
            assert logs[0].message_content == "你好"

        # ② 慢任务在途时发 stop：命令立刻回执，在途任务被打断并带上「已停止」后缀。
        req2 = await fake_ws.send_message("bot1", text="慢一点", msgid="m2")
        await fake_ws.wait_frame(
            lambda f: _is_push(f, req2) and "你好" in f["body"]["stream"]["content"], timeout=15
        )
        req3 = await fake_ws.send_message("bot1", text="stop", msgid="m3")
        stopped = await fake_ws.wait_frame(
            lambda f: _is_push(f, req3) and f["body"]["stream"]["finish"], timeout=15
        )
        assert stopped["body"]["stream"]["content"] == msg("stopped")
        old_finish = await fake_ws.wait_frame(
            lambda f: _is_push(f, req2) and f["body"]["stream"]["finish"], timeout=15
        )
        assert old_finish["body"]["stream"]["content"].endswith(msg("task_stopped_suffix"))
        assert relays["slow"].aborted == 1
    finally:
        worker.request_stop("test")
        gateway.request_stop("test")
        await asyncio.wait_for(asyncio.gather(tg, tw, return_exceptions=True), 30)
        await fake_ws.stop()
