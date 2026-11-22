"""从消息流卡片跳进飞书会话：卡片链接指向平台这个地址，再 302 到飞书 AppLink。

飞书消息流卡片的跳转链接只收 https 网页地址，`applink.feishu.cn/client/...` 一律报
230001 param is invalid（2026-11 实测，打开会话、打开机器人都不行）；点开网页地址时客户端
会跟着 302 打开 AppLink，进到对应会话。AppLink 只能打开会话，不能定位到某条消息。

只跳到飞书 AppLink 固定的两个协议，参数按格式校验，不是任意跳转。这个地址要能从公网访问
（手机上点卡片不一定在内网）。
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

PATH = "/api/feishu/open"
_CHAT = re.compile(r"oc_[A-Za-z0-9]{1,64}")
_APP = re.compile(r"cli_[A-Za-z0-9]{1,64}")


def link(public_base_url: str, *, chat_id: str | None = None, app_id: str | None = None) -> str:
    """消息流卡片用的跳转链接：有会话就进会话，否则进和这个应用的私聊。"""
    query = {"chat": chat_id} if chat_id else {"app": app_id or ""}
    return f"{public_base_url.rstrip('/')}{PATH}?{urlencode(query)}"


def applink(*, chat: str | None, app: str | None) -> str | None:
    """跳转目标；参数不合格式时返回 None。"""
    if chat and _CHAT.fullmatch(chat):
        return f"https://applink.feishu.cn/client/chat/open?{urlencode({'openChatId': chat})}"
    if app and _APP.fullmatch(app):
        return f"https://applink.feishu.cn/client/bot/open?{urlencode({'appId': app})}"
    return None
