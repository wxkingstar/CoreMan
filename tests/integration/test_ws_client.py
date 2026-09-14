import asyncio

import pytest

from coreman.runtime.gateway_wecom.ws_client import WeComWsClient, WsConfig
from tests.fakes.fake_wecom_ws import FakeWeComWs

FAST = dict(
    ping_interval=0.2,
    watchdog_interval=0.1,
    idle_timeout=0.6,
    reconnect_base=0.05,
    reconnect_max=0.2,
    subscribe_timeout=1.0,
    kick_limit=2,
    kick_window=60.0,
)


async def _client(
    url: str, fake: FakeWeComWs, *, secret: str = "sec", **over
) -> tuple[WeComWsClient, list, list]:  # type: ignore[no-untyped-def]
    frames: list = []
    states: list = []

    async def on_frame(f: dict) -> None:
        frames.append(f)

    async def on_state(s: str) -> None:
        states.append(s)

    cfg = WsConfig(url=url, **{**FAST, **over})
    return (
        WeComWsClient(
            bot_id="bot1",
            secret=secret,
            bot_key="b",
            on_frame=on_frame,
            on_state=on_state,
            config=cfg,
        ),
        frames,
        states,
    )


async def _wait(pred, timeout: float = 3.0) -> None:  # type: ignore[no-untyped-def]  # noqa: ASYNC109
    for _ in range(int(timeout / 0.05)):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("条件未满足")


async def test_subscribe_receive_send_and_stop() -> None:
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    try:
        client, frames, states = await _client(url, fake)
        await client.start()
        await _wait(lambda: client.state == "subscribed")
        assert states[:2] == ["connecting", "subscribed"] and fake.subscribe_attempts == 1
        req = await fake.send_message("bot1", text="你好")
        await _wait(lambda: len(frames) == 1)
        assert frames[0]["cmd"] == "aibot_msg_callback" and frames[0]["headers"]["req_id"] == req
        await client.send(
            {
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": req},
                "body": {
                    "msgtype": "stream",
                    "stream": {"id": "s", "finish": True, "content": "hi"},
                },
            }
        )
        got = await fake.wait_frame(lambda f: f["cmd"] == "aibot_respond_msg")
        assert got["body"]["stream"]["content"] == "hi" and fake.stream_contents(req) == [
            ("hi", True)
        ]
        await asyncio.sleep(0.5)
        assert any(f["cmd"] == "ping" for f in fake.frames)
        await client.stop()
        assert client.state == "disconnected"
    finally:
        await fake.stop()


async def test_rejected_subscribe_backs_off_then_recovers() -> None:
    fake = FakeWeComWs(accepted={"bot1": "right"})
    url = await fake.start()
    try:
        client, _, states = await _client(url, fake, secret="wrong")
        await client.start()
        await _wait(lambda: client.state == "auth_failed")
        await asyncio.sleep(0.4)
        assert fake.subscribe_attempts >= 2 and client.connections == 0
        client.secret = "right"
        await _wait(lambda: client.state == "subscribed", timeout=3.0)
        assert client.connections == 1
        await client.stop()
    finally:
        await fake.stop()


async def test_watchdog_reconnects_when_idle() -> None:
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    try:
        client, _, _ = await _client(url, fake)
        await client.start()
        await _wait(lambda: client.connections == 1)
        fake.silence("bot1", True)
        await _wait(lambda: client.connections >= 2, timeout=4.0)
        await client.stop()
    finally:
        await fake.stop()


async def test_kick_fuse_stops_reconnecting() -> None:
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    try:
        client, frames, _ = await _client(url, fake)
        await client.start()
        await _wait(lambda: client.connections == 1)
        await fake.kick("bot1")
        await _wait(lambda: client.connections == 2, timeout=3.0)
        await fake.kick("bot1")
        await _wait(lambda: client.fused, timeout=3.0)
        assert client.state == "kicked" and not any(
            f.get("cmd") == "aibot_event_callback" for f in frames
        )
        await asyncio.sleep(0.5)
        assert client.connections == 2
        await client.stop()
    finally:
        await fake.stop()


async def test_errcode_events_and_connection_takeover() -> None:
    """补齐 Task 11 要用的其余契约：错误响应包、事件帧、主动推送记录、新连接顶掉旧连接。"""
    fake = FakeWeComWs(accepted={"bot1": "sec"})
    url = await fake.start()
    errs: list = []
    try:
        client, frames, _ = await _client(url, fake)

        async def on_errcode(req_id: str, code: int, msg: str) -> None:
            errs.append((req_id, code, msg))

        client.on_errcode = on_errcode
        with pytest.raises(RuntimeError, match="WebSocket 未连接"):
            await client.send({"cmd": "aibot_respond_msg"})
        await client.start()
        await _wait(lambda: client.state == "subscribed")

        await fake.respond_errcode("bot1", "req-7", 846608, "stream expired")
        await _wait(lambda: bool(errs))
        assert errs == [("req-7", 846608, "stream expired")]
        await asyncio.sleep(0.3)
        assert frames == [] and len(errs) == 1  # errcode 0 的 ping 回包不分发

        await fake.send_event("bot1", "enter_chat")
        await _wait(lambda: len(frames) == 1)
        assert frames[0]["body"]["event"]["eventtype"] == "enter_chat"
        assert frames[0]["body"]["msgtype"] == "event" and "chatid" not in frames[0]["body"]

        await fake.send_message("bot1", text="群里说", chat_type="group", chatid="G1")
        await _wait(lambda: len(frames) == 2)
        assert frames[1]["body"]["chatid"] == "G1" and frames[1]["body"]["chattype"] == "group"

        body = {"chatid": "G1", "msgtype": "markdown", "markdown": {"content": "hi"}}
        await client.send({"cmd": "aibot_send_msg", "headers": {"req_id": "r"}, "body": body})
        await fake.wait_frame(lambda f: f["cmd"] == "aibot_send_msg")
        assert fake.sent_messages() == [body]

        other, _, _ = await _client(url, fake)
        await other.start()
        await _wait(lambda: other.state == "subscribed")
        await _wait(lambda: client.kick_times != [])  # 旧连接收到 disconnected_event
        assert not any(f["cmd"] == "aibot_event_callback" for f in frames[1:])
        await other.stop()
        await client.stop()
        await client.stop()  # 幂等
        assert client.state == "disconnected"
    finally:
        await fake.stop()
