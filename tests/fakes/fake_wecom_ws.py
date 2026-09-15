"""假的企微智能机器人 WS 服务：真起一个本地 websockets server，供网关测试端到端跑。

只实现被测代码真正依赖的那部分协议（平台协议 §3.1/§3.2）：订阅校验、ping 应答、
把消息/事件推给客户端、错误响应包，以及企微那个关键脾气——同一 `bot_id` 的新连接会把旧连接
踢下线（先发 `disconnected_event` 再断）。收到的每一帧都记在 `frames` 里供断言。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import WebSocketException

_POLL = 0.05
# 订阅帧里有明文 secret，而 websockets 的 DEBUG 日志会把每一帧原样打出来。
# 给服务端一个钉死在 INFO 的 logger：LOG_LEVEL=DEBUG 跑测试也不会把密钥喷进 CI 日志。
_WS_LIB_LOGGER = logging.getLogger("tests.fake_wecom_ws")
_WS_LIB_LOGGER.setLevel(logging.INFO)


def _is_finish_frame(frame: dict[str, Any]) -> bool:
    if frame.get("cmd") != "aibot_respond_msg":
        return False
    stream = (frame.get("body") or {}).get("stream")
    return isinstance(stream, dict) and bool(stream.get("finish"))


class FakeWeComWs:
    """内存版企微 WS 服务端。

    Attributes:
        accepted: `bot_id → secret`，订阅时逐字比对
        connections: 当前活动连接（按 bot_id，同 bot 只留最新一条）
        frames: 收到的全部帧（已解析）
        subscribe_attempts: 收到过的订阅帧数量（含被拒的）
    """

    def __init__(self, *, accepted: dict[str, str]) -> None:
        self.accepted = dict(accepted)
        self.connections: dict[str, ServerConnection] = {}
        self.frames: list[dict[str, Any]] = []
        self.subscribe_attempts = 0
        self._silenced: set[str] = set()
        self.send_rejections: list[tuple[int, str]] = []
        self._finish_rejection: tuple[int, str, float] | None = None
        self._server: Server | None = None

    # ---- 生命周期 ----

    async def start(self) -> str:
        self._server = await serve(self._handler, "127.0.0.1", 0, logger=_WS_LIB_LOGGER)
        port = self._server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}"

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        self.connections.clear()

    # ---- 测试用的主动动作 ----

    async def send_message(
        self,
        bot_id: str,
        *,
        text: str,
        from_user: str = "zs",
        chat_type: str = "single",
        chatid: str | None = None,
        msgid: str | None = None,
        msgtype: str = "text",
    ) -> str:
        """推一条用户消息回调，返回 `req_id`（被动回复必须原样透传它）。"""
        body: dict[str, Any] = {
            "msgid": msgid or f"msg-{uuid.uuid4().hex[:12]}",
            "aibotid": bot_id,
            "chattype": chat_type,
            "from": {"userid": from_user},
            "msgtype": msgtype,
            msgtype: {"content": text},
        }
        if chat_type == "group":
            # chatid 只有群聊才有（§3.2）；单聊即便传了也不放，跟企微保持一致。
            body["chatid"] = chatid or "group1"
        return await self.send_frame(bot_id, "aibot_msg_callback", body)

    async def send_event(self, bot_id: str, eventtype: str, **fields: Any) -> str:
        """推一条事件回调；`**fields` 直接并进 `body.event`（如 `template_card_event={...}`）。

        `msgid` 例外：它是消息体自己的字段（去重键），显式给了就用它，不进 `event`。
        """
        msgid = fields.pop("msgid", None)
        body = self._event_body(bot_id, eventtype, fields)
        if msgid is not None:
            body["msgid"] = str(msgid)
        return await self.send_frame(bot_id, "aibot_event_callback", body)

    async def send_media(
        self,
        bot_id: str,
        *,
        msgtype: str,
        url: str = "https://media.test/x",
        aeskey: str = "k" * 32,
        filename: str | None = None,
        items: list[dict[str, Any]] | None = None,
        quote: dict[str, Any] | None = None,
        from_user: str = "zs",
        chat_type: str = "single",
        chatid: str | None = None,
        msgid: str | None = None,
    ) -> str:
        """推一条媒体消息回调（image/file/video/mixed），形状与企微长连接文档一致。"""
        payload: dict[str, Any]
        if msgtype == "mixed":
            payload = {"msg_item": list(items or [])}
        else:
            payload = {"url": url, "aeskey": aeskey}
            if filename:
                payload["filename"] = filename
        body: dict[str, Any] = {
            "msgid": msgid or f"msg-{uuid.uuid4().hex[:12]}",
            "aibotid": bot_id,
            "chattype": chat_type,
            "from": {"userid": from_user},
            "msgtype": msgtype,
            msgtype: payload,
        }
        if quote is not None:
            body["quote"] = quote
        if chat_type == "group":
            body["chatid"] = chatid or "group1"
        return await self.send_frame(bot_id, "aibot_msg_callback", body)

    async def send_card_event(
        self,
        bot_id: str,
        *,
        task_id: str,
        selected: dict[str, list[str]] | None = None,
        card_type: str = "vote_interaction",
        event_key: str = "submit_choice",
        from_user: str = "zs",
        chat_type: str = "single",
        chatid: str | None = None,
        msgid: str | None = None,
    ) -> str:
        """推一条模板卡片事件回调；返回 req_id（5 秒内的 aibot_respond_update_msg 要透传它）。"""
        event: dict[str, Any] = {
            "card_type": card_type,
            "event_key": event_key,
            "task_id": task_id,
            "selected_items": {
                "selected_item": [
                    {"question_key": k, "option_ids": {"option_id": list(v)}}
                    for k, v in (selected or {}).items()
                ]
            },
        }
        body = self._event_body(bot_id, "template_card_event", {"template_card_event": event})
        body["from"] = {"userid": from_user}
        body["chattype"] = chat_type
        if chat_type == "group":
            body["chatid"] = chatid or "group1"
        if msgid is not None:
            body["msgid"] = str(msgid)
        return await self.send_frame(bot_id, "aibot_event_callback", body)

    async def send_frame(self, bot_id: str, cmd: str, body: dict[str, Any]) -> str:
        req_id = str(uuid.uuid4())
        await self._send(
            self._require(bot_id), {"cmd": cmd, "headers": {"req_id": req_id}, "body": body}
        )
        return req_id

    async def kick(self, bot_id: str) -> None:
        """把这个 bot 当前连接踢下线（发 `disconnected_event` 后立刻断开）。"""
        ws = self._require(bot_id)
        del self.connections[bot_id]
        await self._disconnect(ws, bot_id)

    async def respond_errcode(self, bot_id: str, req_id: str, errcode: int, errmsg: str) -> None:
        await self._send(
            self._require(bot_id),
            {"cmd": "", "headers": {"req_id": req_id}, "errcode": errcode, "errmsg": errmsg},
        )

    def reject_finish_frames(
        self, *, errcode: int, errmsg: str = "stream not exist", delay: float = 0.0
    ) -> None:
        """此后每收到一帧 `finish=true` 的流推送，都在 `delay` 秒后异步回这个错误码。

        `respond_errcode` 是用例自己掐着点发的，模拟不了「连接都要关了它还在路上」——而
        企微的错误码恰恰是几百毫秒到几秒后才回来的。
        """
        self._finish_rejection = (errcode, errmsg, delay)

    def silence(self, bot_id: str, on: bool) -> None:
        """开了之后不再回 ping（模拟链路假死，逼客户端看门狗动手）。"""
        self._silenced.add(bot_id) if on else self._silenced.discard(bot_id)

    # ---- 断言辅助 ----

    async def wait_frame(
        self,
        pred: Callable[[dict[str, Any]], bool],
        timeout: float = 5.0,  # noqa: ASYNC109 轮询式等待是测试约定，不换 asyncio.timeout
    ) -> dict[str, Any]:
        for _ in range(max(1, int(timeout / _POLL))):
            for frame in list(self.frames):
                if pred(frame):
                    return frame
            await asyncio.sleep(_POLL)
        raise AssertionError("等待的帧没有出现")

    def stream_contents(self, req_id: str) -> list[tuple[str, bool]]:
        """该 req_id 上收到的流式推送序列 `(content, finish)`（content 是全量累积文本）。"""
        out: list[tuple[str, bool]] = []
        for frame in self.frames:
            if frame.get("cmd") != "aibot_respond_msg":
                continue
            if (frame.get("headers") or {}).get("req_id") != req_id:
                continue
            stream = (frame.get("body") or {}).get("stream")
            if isinstance(stream, dict):
                out.append((str(stream.get("content", "")), bool(stream.get("finish"))))
        return out

    def sent_messages(self) -> list[dict[str, Any]]:
        """主动推送（`aibot_send_msg`）的 body 列表。"""
        return [f.get("body") or {} for f in self.frames if f.get("cmd") == "aibot_send_msg"]

    def card_updates(self) -> list[dict[str, Any]]:
        """卡片更新（`aibot_respond_update_msg`）的整帧列表——断言要看 headers 里的 req_id。"""
        return [f for f in self.frames if f.get("cmd") == "aibot_respond_update_msg"]

    def sent_cards(self) -> list[dict[str, Any]]:
        """主动推送里 `msgtype == template_card` 的那些 body。"""
        return [b for b in self.sent_messages() if b.get("msgtype") == "template_card"]

    # ---- 服务端内部 ----

    async def _handler(self, ws: ServerConnection) -> None:
        bot_id: str | None = None
        try:
            frame = self._record(await ws.recv())
            req_id = str((frame.get("headers") or {}).get("req_id", ""))
            if frame.get("cmd") != "aibot_subscribe":
                await self._reject(ws, req_id, "expect aibot_subscribe")
                return
            self.subscribe_attempts += 1
            body = frame.get("body") or {}
            candidate = str(body.get("bot_id", ""))
            if candidate not in self.accepted or self.accepted[candidate] != body.get("secret"):
                await self._reject(ws, req_id, "invalid bot_id or secret")
                return
            bot_id = candidate
            await self._evict(bot_id)
            self.connections[bot_id] = ws
            await self._send(
                ws, {"cmd": "", "headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"}
            )
            async for raw in ws:
                frame = self._record(raw)
                if frame.get("cmd") == "ping" and bot_id not in self._silenced:
                    await self._send(
                        ws,
                        {
                            "cmd": "",
                            "headers": frame.get("headers") or {},
                            "errcode": 0,
                            "errmsg": "ok",
                        },
                    )
                elif frame.get("cmd") == "aibot_send_msg" and self.send_rejections:
                    code, message = self.send_rejections.pop(0)
                    await self._send(
                        ws,
                        {
                            "cmd": "",
                            "headers": frame.get("headers") or {},
                            "errcode": code,
                            "errmsg": message,
                        },
                    )
                elif self._finish_rejection is not None and _is_finish_frame(frame):
                    await self._reject_late(ws, frame, self._finish_rejection)
                elif frame.get("cmd", "").startswith("aibot_"):
                    await self._send(
                        ws,
                        {
                            "cmd": "",
                            "headers": frame.get("headers") or {},
                            "errcode": 0,
                            "errmsg": "ok",
                        },
                    )
        except (WebSocketException, OSError):
            pass
        finally:
            if bot_id is not None and self.connections.get(bot_id) is ws:
                del self.connections[bot_id]

    def _record(self, raw: str | bytes) -> dict[str, Any]:
        frame = json.loads(raw)
        if not isinstance(frame, dict):
            raise AssertionError(f"收到非对象帧：{frame!r}")
        self.frames.append(frame)
        return frame

    def _require(self, bot_id: str) -> ServerConnection:
        ws = self.connections.get(bot_id)
        if ws is None:
            raise AssertionError(f"{bot_id} 当前没有活动连接")
        return ws

    def _event_body(self, bot_id: str, eventtype: str, fields: dict[str, Any]) -> dict[str, Any]:
        return {
            "msgid": f"evt-{uuid.uuid4().hex[:12]}",
            "aibotid": bot_id,
            "chattype": "single",
            "from": {"userid": "zs"},
            "msgtype": "event",
            "event": {"eventtype": eventtype, **fields},
        }

    async def _evict(self, bot_id: str) -> None:
        """新连接顶掉旧连接：企微就是这么干的，客户端的踢线熔断全靠它触发。"""
        old = self.connections.pop(bot_id, None)
        if old is not None:
            await self._disconnect(old, bot_id)

    async def _disconnect(self, ws: ServerConnection, bot_id: str) -> None:
        with contextlib.suppress(WebSocketException, OSError):
            await self._send(
                ws,
                {
                    "cmd": "aibot_event_callback",
                    "headers": {"req_id": str(uuid.uuid4())},
                    "body": self._event_body(bot_id, "disconnected_event", {}),
                },
            )
            await ws.close()

    async def _reject_late(
        self, ws: ServerConnection, frame: dict[str, Any], rejection: tuple[int, str, float]
    ) -> None:
        errcode, errmsg, delay = rejection
        await asyncio.sleep(delay)
        await self._send(
            ws,
            {
                "cmd": "",
                "headers": {"req_id": str((frame.get("headers") or {}).get("req_id", ""))},
                "errcode": errcode,
                "errmsg": errmsg,
            },
        )

    async def _reject(self, ws: ServerConnection, req_id: str, errmsg: str) -> None:
        await self._send(
            ws,
            {"cmd": "", "headers": {"req_id": req_id}, "errcode": 853000, "errmsg": errmsg},
        )

    async def _send(self, ws: ServerConnection, frame: dict[str, Any]) -> None:
        await ws.send(json.dumps(frame, ensure_ascii=False))
