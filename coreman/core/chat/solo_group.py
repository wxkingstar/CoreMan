"""飞书群里真人只有发话人自己：这一轮按私聊处理。

worker 每一轮开头实时问飞书（不缓存），是单人群就在任务上记 `PAYLOAD_KEY`；之后私聊才有的
能力（个人工具、确认卡、对话记录只给本人看）都认这个标记。个人工具的服务端核验
（`feishu_personal.policy.verified_origin_scope`）每次调用仍按来源事件逐项复核，标记只替换
「这是私聊」那一项。

单人群的条件全部满足才算：群信息里明确不是外部群、真人数为 1、成员列表里只有发话人。
机器人不计入真人（飞书的 user_count 与成员列表都不含机器人）。任何一项查不到、超时或报错，
都按普通群处理。
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol
from urllib.parse import quote

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, Task
from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuClient

log = get_logger(__name__)

# 任务 payload 上的标记：只有 worker 写，严格为 True 才算。
PAYLOAD_KEY = "solo_group"
# 两次查询合计的上限：查不到就按普通群，不能拖住这一轮。
TIMEOUT_SECONDS = 5.0


class _Client(Protocol):
    async def call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]: ...


def marked(task: Task) -> bool:
    """这一轮开头判断过是单人群。"""
    return (task.payload or {}).get(PAYLOAD_KEY) is True


async def solo(cipher: Cipher, bot: Bot, chat_id: str, platform_user_id: str) -> bool:
    """用机器人自己的飞书应用实时查一次；凭据不可用也按普通群。"""
    try:
        config = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
        client = FeishuClient(config["app_id"], config["app_secret"])
    except Exception as exc:  # noqa: BLE001 查不到一律按普通群
        log.warning("solo_group_check_failed", error=type(exc).__name__)
        return False
    try:
        return await solo_with(client, chat_id, platform_user_id)
    finally:
        await client.aclose()


async def solo_with(client: _Client, chat_id: str, platform_user_id: str) -> bool:
    """同 `solo`，用调用方给的客户端。只有三项都明确满足才返回 True。"""
    if not chat_id or not platform_user_id:
        return False
    path = f"/open-apis/im/v1/chats/{quote(chat_id, safe='')}"
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            info = (await client.call("GET", path)).get("data") or {}
            if info.get("external") is not False or str(info.get("user_count")) != "1":
                return False
            members = (
                await client.call(
                    "GET",
                    path + "/members",
                    params={"member_id_type": "user_id", "page_size": 10},
                )
            ).get("data") or {}
    except Exception as exc:  # noqa: BLE001 查不到一律按普通群
        log.warning("solo_group_check_failed", error=type(exc).__name__)
        return False
    items = [item for item in members.get("items") or [] if isinstance(item, dict)]
    return (
        len(items) == 1
        and items[0].get("member_id") == platform_user_id
        and not members.get("has_more")
        and str(members.get("member_total", 1)) == "1"
    )
