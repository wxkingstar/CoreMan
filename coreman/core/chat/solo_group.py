"""飞书群里真人只有发话人自己：这一轮按私聊处理。

worker 每一轮开头判断一次，是单人群就在任务上记 `PAYLOAD_KEY`；之后私聊才有的能力（个人工具、
确认卡、对话记录只给本人看）都认这个标记。个人工具的服务端核验
（`feishu_personal.policy.verified_origin_scope`）每次调用仍按来源事件逐项复核，标记只替换
「这是私聊」那一项。

单人群的条件全部满足才算：群信息里明确不是外部群、真人数为 1、成员列表里只有发话人。
机器人不计入真人（飞书的 user_count 与成员列表都不含机器人）。任何一项查不到、超时或报错，
都按普通群处理。

这是个小场景，不能拖慢普通群：每个 worker 进程按（机器人，群）缓存结果。确认是普通群的记
7 天，期间不再调接口；单人群记一小时；查不到的记几分钟。单人群缓存期间群里有别人说话，就说明
不止一个人，立即改记为普通群；拉人进群但新来的人还没说话时，最多要等一小时才回到群规则；普通群
有人退出变成单人群，最多 7 天（或 worker 重启）后才按私聊处理（产品上接受这些延迟）。
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
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
# 单人群记多久（进了人最晚这么久回到群规则）；确认是普通群记多久（期间不再调接口）；
# 查不到记多久（免得飞书出问题时每轮都等超时）。
CACHE_SECONDS = 3600.0
GROUP_CACHE_SECONDS = 7 * 24 * 3600.0
FAILURE_CACHE_SECONDS = 300.0
_CACHE_LIMIT = 4096
# （机器人, 群）→（到期时刻, 群里唯一真人的 user_id；不是单人群或查不到为空串）
_CACHE: OrderedDict[tuple[str, str], tuple[float, str]] = OrderedDict()


class LookupFailed(Exception):
    """飞书没给出可信的答案（报错、超时、字段缺失）。"""


class _Client(Protocol):
    async def call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]: ...


def marked(task: Task) -> bool:
    """这一轮开头判断过是单人群。"""
    return (task.payload or {}).get(PAYLOAD_KEY) is True


async def solo(cipher: Cipher, bot: Bot, chat_id: str, platform_user_id: str) -> bool:
    """发话人是不是这个群里唯一的真人。先看缓存，没有或过期才用机器人自己的飞书应用查。"""
    if not chat_id or not platform_user_id:
        return False
    key = (str(bot.id), chat_id)
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached is not None and cached[0] > now:
        if cached[1] and cached[1] != platform_user_id:
            # 别人在这个群里说话：不止一个人了，不等缓存过期，改记为普通群。
            _remember(key, "", now + GROUP_CACHE_SECONDS)
            return False
        return cached[1] == platform_user_id
    try:
        config = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
        client = FeishuClient(config["app_id"], config["app_secret"])
    except Exception as exc:  # noqa: BLE001 查不到一律按普通群
        log.warning("solo_group_check_failed", error=type(exc).__name__)
        _remember(key, "", now + FAILURE_CACHE_SECONDS)
        return False
    try:
        human = await sole_human(client, chat_id)
    except LookupFailed:
        _remember(key, "", now + FAILURE_CACHE_SECONDS)
        return False
    finally:
        await client.aclose()
    _remember(key, human, now + (CACHE_SECONDS if human else GROUP_CACHE_SECONDS))
    return human == platform_user_id


def forget() -> None:
    """清空缓存（测试用）。"""
    _CACHE.clear()


def _remember(key: tuple[str, str], human: str, until: float) -> None:
    _CACHE[key] = (until, human)
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_LIMIT:
        _CACHE.popitem(last=False)


async def solo_with(client: _Client, chat_id: str, platform_user_id: str) -> bool:
    """不走缓存，用调用方给的客户端查一次。只有三项都明确满足才返回 True。"""
    if not chat_id or not platform_user_id:
        return False
    try:
        return await sole_human(client, chat_id) == platform_user_id
    except LookupFailed:
        return False


async def sole_human(client: _Client, chat_id: str) -> str:
    """群里唯一真人的 user_id；不是只有一个内部真人时返回空串，查不到抛 `LookupFailed`。"""
    path = f"/open-apis/im/v1/chats/{quote(chat_id, safe='')}"
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            info = (await client.call("GET", path)).get("data") or {}
            external, count = info.get("external"), info.get("user_count")
            if external is None or count is None:
                raise LookupFailed
            if external is not False or str(count) != "1":
                return ""
            members = (
                await client.call(
                    "GET",
                    path + "/members",
                    params={"member_id_type": "user_id", "page_size": 10},
                )
            ).get("data") or {}
    except LookupFailed:
        raise
    except Exception as exc:  # noqa: BLE001 查不到一律按普通群
        log.warning("solo_group_check_failed", error=type(exc).__name__)
        raise LookupFailed from exc
    items = [item for item in members.get("items") or [] if isinstance(item, dict)]
    human = items[0].get("member_id") if len(items) == 1 else None
    if (
        not isinstance(human, str)
        or members.get("has_more")
        or str(members.get("member_total", 1)) != "1"
    ):
        return ""
    return human
