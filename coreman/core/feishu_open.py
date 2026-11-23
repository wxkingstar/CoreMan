"""从消息流卡片跳进飞书会话：卡片链接指向平台这个地址，再 302 到飞书 AppLink。

飞书消息流卡片的跳转链接只收 https 网页地址，`applink.feishu.cn/client/...` 一律报
230001 param is invalid（2026-11 实测，打开会话、打开机器人都不行）；点开网页地址时客户端
会跟着 302 打开 AppLink，进到对应会话。打开会话的 AppLink 带 `position`（消息接口返回的
`message_position`）时定位到那条消息，这是飞书「消息链接」用的同一个参数。

链接带签名的 `d/u/b/s` 时，点开顺带删掉这张卡片（看过就不再占消息列表）。只跳到飞书
AppLink 固定的两个协议，参数按格式校验，不是任意跳转。这个地址要能从公网访问（手机上点
卡片不一定在内网）。
"""

from __future__ import annotations

import hashlib
import hmac
import re
from urllib.parse import urlencode

PATH = "/api/feishu/open"
_CHAT = re.compile(r"oc_[A-Za-z0-9]{1,64}")
_APP = re.compile(r"cli_[A-Za-z0-9]{1,64}")
_USER = re.compile(r"[A-Za-z0-9_]{1,64}")
_POSITION = re.compile(r"[0-9]{1,12}")
_TASK = re.compile(r"[0-9]{1,18}")
_SIGNATURE = re.compile(r"[0-9a-f]{32}")


def chat_biz(chat_id: str) -> str:
    """一个群一张卡片：同一群里的下一轮复用它，不在消息列表里越积越多。"""
    return "coreman-chat-" + hashlib.sha1(chat_id.encode()).hexdigest()[:24]


def _key(master_key: bytes) -> bytes:
    return hmac.new(master_key, b"coreman:feishu-open", hashlib.sha256).digest()


def sign(master_key: bytes, bot_id: str, chat_id: str, task_id: int, user_id: str) -> str:
    message = f"{bot_id}:{chat_id}:{task_id}:{user_id}".encode()
    return hmac.new(_key(master_key), message, hashlib.sha256).hexdigest()[:32]


def verify(
    master_key: bytes, bot_id: str, chat_id: str, task_id: str, user_id: str, signature: str
) -> bool:
    if not (
        _TASK.fullmatch(task_id) and _USER.fullmatch(user_id) and _SIGNATURE.fullmatch(signature)
    ):
        return False
    expected = sign(master_key, bot_id, chat_id, int(task_id), user_id)
    return hmac.compare_digest(expected, signature)


def link(
    public_base_url: str,
    *,
    chat_id: str | None = None,
    app_id: str | None = None,
    position: str | None = None,
    clear: tuple[bytes, str, int, str] | None = None,
) -> str:
    """消息流卡片用的跳转链接：有会话就进会话（有位置就定位），否则进和这个应用的私聊。

    `clear` 是（主密钥, 机器人 ID, 任务号, 提问人）：点开时删掉这张卡片。
    """
    query: dict[str, str] = {"chat": chat_id} if chat_id else {"app": app_id or ""}
    if chat_id and position:
        query["pos"] = position
    if chat_id and clear is not None:
        master_key, bot_id, task_id, user_id = clear
        query.update(
            b=bot_id,
            d=str(task_id),
            u=user_id,
            s=sign(master_key, bot_id, chat_id, task_id, user_id),
        )
    return f"{public_base_url.rstrip('/')}{PATH}?{urlencode(query)}"


def applink(*, chat: str | None, app: str | None, position: str | None = None) -> str | None:
    """跳转目标；参数不合格式时返回 None。"""
    if chat and _CHAT.fullmatch(chat):
        query = {"openChatId": chat}
        if position and _POSITION.fullmatch(position):
            query["position"] = position
        return f"https://applink.feishu.cn/client/chat/open?{urlencode(query)}"
    if app and _APP.fullmatch(app):
        return f"https://applink.feishu.cn/client/bot/open?{urlencode({'appId': app})}"
    return None
