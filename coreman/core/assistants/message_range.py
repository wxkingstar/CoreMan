"""个人助理授权的消息范围：全部会话、只群聊、只单聊，由所有者自己选。

飞书以本人身份读消息时，除了基础的读消息权限，还要按会话类型各开一项补充权限；只群聊就不
申请单聊那一项，只单聊反之，另一类会话飞书自己就不给读（见 `feishu_personal.permissions`）。
平台再按会话类型拦一道（`chat_guard`）。

改范围要重新授权：旧令牌当场作废，新的授权链接发到所有者与助理的私聊（「我的助理」页上也
给出），盯消息替他查授权结果，完成之前助理读不了飞书。
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import settings as assistant_settings
from coreman.core.bus import outbox
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, FeishuPersonalGrant, Task
from coreman.core.feishu_personal import permissions
from coreman.core.feishu_personal import service as personal
from coreman.core.feishu_personal.errors import PersonalError
from coreman.core.feishu_personal.policy import Scope

RANGES = permissions.MESSAGE_RANGES
TITLES = {"all": "全部会话", "group": "只群聊", "p2p": "只单聊"}
HINTS = {
    "all": "单聊和群聊的消息都能读。",
    "group": "只读群聊；单聊的消息飞书不会给助理读，平台也不碰单聊（含给人发单聊消息）。",
    "p2p": "只读单聊；群聊的消息飞书不会给助理读，平台也不碰群聊。",
}


async def grant_of(session: AsyncSession, bot: Bot) -> FeishuPersonalGrant | None:
    if bot.owner_id is None:
        return None
    return await session.get(FeishuPersonalGrant, (bot.id, bot.owner_id), populate_existing=True)


def view(grant: FeishuPersonalGrant | None) -> dict[str, Any]:
    """给所有者看的：现在的范围、授权状态，以及飞书侧是否真的按范围收住了。

    `enforced` 为假：飞书发下来的权限里仍有另一类会话的读权限（本人以前同意过），只靠平台拦；
    要本人先在飞书「设置 → 隐私 → 授权管理」里撤掉对这个应用的授权，再改一次范围。
    """
    if grant is None:
        return {"message_range": "all", "status": "revoked", "enforced": None}
    connected = grant.status == "connected" and bool(grant.token_enc)
    return {
        "message_range": grant.message_range,
        "status": grant.status,
        "enforced": permissions.range_enforced(grant.message_range, list(grant.scopes or []))
        if connected
        else None,
    }


async def change(
    session: AsyncSession, cipher: Cipher, bot: Bot, message_range: str, *, send: bool = True
) -> str:
    """改范围并发起重新授权；返回授权链接。调用方提交。

    `send` 为真时把授权卡片发到所有者的私聊（从页面上改的）；私聊卡片上点的由调用方原地换卡片。

    Raises:
        PersonalError: `invalid_message_range`、`authorization_required`（从没授权过）、
            `assistant_inactive`，以及发起授权时飞书或应用权限的错误
    """
    if message_range not in RANGES:
        raise PersonalError("invalid_message_range")
    grant = await grant_of(session, bot)
    prefs = await assistant_settings.get(session, bot.id)
    if grant is None or not assistant_settings.identified(prefs):
        raise PersonalError("authorization_required")
    scope = Scope(
        cast(Task, None),
        bot,
        grant.user_id,
        prefs.tenant_key,
        grant.app_id,
        prefs.open_id,
        prefs.platform_user_id,
        prefs.chat_id,
        None,
    )
    begun = await personal.change_message_range(session, cipher, scope, message_range)
    url = str(begun["authorization_url"])
    if not send:
        return url
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=f"assistant-message-range:{bot.id}:{grant.context_epoch}",
        target={"chat_id": prefs.chat_id},
        payload={"card": card(message_range, url)},
    )
    return url


def card(message_range: str, url: str) -> dict[str, Any]:
    """重新授权的卡片：说明改成了什么、点按钮去飞书授权。"""
    return {
        "schema": "2.0",
        "header": {"template": "turquoise", "title": _plain("重新授权飞书")},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"消息范围改成了**{TITLES[message_range]}**：{HINTS[message_range]}\n"
                        "原来的授权已经作废，点下面的按钮重新授权；完成之前我读不了你的飞书。"
                    ),
                },
                {
                    "tag": "button",
                    "type": "primary",
                    "text": _plain("前往飞书授权"),
                    "behaviors": [{"type": "open_url", "default_url": url}],
                },
            ]
        },
    }


def _plain(text: str) -> dict[str, str]:
    return {"tag": "plain_text", "content": text}
