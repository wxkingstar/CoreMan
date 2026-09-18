"""用公开的长连接订阅接口校验企业微信智能机器人的 Bot ID 与 Secret。

连上 `wss://openws.work.weixin.qq.com` 发一帧 `aibot_subscribe`，返回 errcode=0 即凭证有效，
随后立刻断开交给网关接管。同一个机器人同时只能有一条长连接，新连接会把旧连接踢掉，所以只在
新建员工时校验：编辑已有员工的凭证不走这里，免得把正在服务的连接踢下线。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

WS_URL = "wss://openws.work.weixin.qq.com"
TIMEOUT = 10.0

# 订阅帧的尾巴就是 secret：库日志钉在 INFO，进程开 DEBUG 时也不会顺着帧日志漏出去。
_WS_LOGGER = logging.getLogger("coreman.wecom.verify")
_WS_LOGGER.setLevel(logging.INFO)


class VerifyError(Exception):
    """rejected：企业微信拒绝了这组凭证；unavailable：连不上或没有按时应答。"""

    def __init__(self, code: str, errcode: int | None = None) -> None:
        self.code = code
        self.errcode = errcode
        super().__init__(code if errcode is None else f"{code}:{errcode}")


async def verify_credentials(
    bot_id: str,
    secret: str,
    *,
    url: str = WS_URL,
    timeout: float = TIMEOUT,  # noqa: ASYNC109 one bounded handshake
) -> None:
    frame = {
        "cmd": "aibot_subscribe",
        "headers": {"req_id": str(uuid.uuid4())},
        "body": {"bot_id": bot_id, "secret": secret},
    }
    try:
        async with (
            asyncio.timeout(timeout),
            connect(
                url,
                ping_interval=None,
                close_timeout=3,
                proxy=None,
                logger=_WS_LOGGER,
            ) as ws,
        ):
            await ws.send(json.dumps(frame, ensure_ascii=False))
            raw = await ws.recv()
    except (OSError, WebSocketException, TimeoutError) as exc:
        raise VerifyError("unavailable") from exc
    try:
        reply = json.loads(raw)
    except ValueError as exc:
        raise VerifyError("unavailable") from exc
    if not isinstance(reply, dict):
        raise VerifyError("unavailable")
    try:
        errcode = int(reply.get("errcode", -1))
    except (TypeError, ValueError):
        errcode = -1
    if errcode != 0:
        raise VerifyError("rejected", errcode)
