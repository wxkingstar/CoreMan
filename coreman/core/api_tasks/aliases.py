"""群别名：群成员在飞书群里 @ AI 员工发 `/api 绑定 <别名>` 绑定，API 用别名指定在哪个群承接。"""

from __future__ import annotations

import re
import uuid
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.chat.commands import normalize
from coreman.core.db.models import BotConversationAlias

ALIAS_RE = re.compile(r"^[\w\-一-鿿]{1,32}$")
_COMMAND = re.compile(r"^/api(?:\s+(.*))?$", re.DOTALL)
_BIND = ("绑定", "bind")
_UNBIND = ("解绑", "解除绑定", "unbind")

Action = Literal["show", "bind", "unbind", "invalid"]


def parse(text: str) -> tuple[Action, str] | None:
    """`/api`、`/api 绑定 名称`、`/api 解绑`；不是这条命令返回 None。

    必须带斜杠：随口说一句「api」是在跟 AI 员工说话，不能被当成命令吞掉。
    别名不合法时返回 ("invalid", 原文)。
    """
    match = _COMMAND.match(normalize(text))
    if match is None:
        return None
    rest = (match.group(1) or "").strip()
    if not rest:
        return "show", ""
    word, _, arg = rest.partition(" ")
    if word in _UNBIND and not arg.strip():
        return "unbind", ""
    if word in _BIND:
        # normalize 会转成小写：别名要保留原样，从原文里取。
        original = re.sub(r"^\s*/api\s+\S+\s*", "", text.strip(), flags=re.IGNORECASE)
        alias = original.strip()
        return ("bind", alias) if ALIAS_RE.match(alias) else ("invalid", alias)
    return None


async def for_chat(
    session: AsyncSession, bot_id: uuid.UUID, chat_id: str
) -> BotConversationAlias | None:
    row: BotConversationAlias | None = await session.scalar(
        select(BotConversationAlias).where(
            BotConversationAlias.bot_id == bot_id, BotConversationAlias.chat_id == chat_id
        )
    )
    return row


async def by_alias(
    session: AsyncSession, bot_id: uuid.UUID, alias: str
) -> BotConversationAlias | None:
    row: BotConversationAlias | None = await session.scalar(
        select(BotConversationAlias).where(
            BotConversationAlias.bot_id == bot_id, BotConversationAlias.alias == alias
        )
    )
    return row


async def bind(
    session: AsyncSession, bot_id: uuid.UUID, chat_id: str, alias: str, user_id: uuid.UUID | None
) -> str | None:
    """绑定（同群已有别名就替换）；别名被别的群占用时返回提示，成功返回 None。"""
    taken = await by_alias(session, bot_id, alias)
    if taken is not None and taken.chat_id != chat_id:
        return f"别名「{alias}」已被另一个群使用，请换一个"
    current = await for_chat(session, bot_id, chat_id)
    try:
        async with session.begin_nested():
            if current is not None:
                current.alias, current.created_by = alias, user_id
            else:
                session.add(
                    BotConversationAlias(
                        bot_id=bot_id, alias=alias, chat_id=chat_id, created_by=user_id
                    )
                )
    except IntegrityError:
        # 另一个群同时绑了同一个别名：唯一键挡住了，按「已被占用」回复。
        return f"别名「{alias}」已被另一个群使用，请换一个"
    return None


async def unbind(session: AsyncSession, bot_id: uuid.UUID, chat_id: str) -> str | None:
    """解绑本群；返回被解除的别名，本来没绑返回 None。"""
    current = await for_chat(session, bot_id, chat_id)
    if current is None:
        return None
    alias = current.alias
    await session.execute(delete(BotConversationAlias).where(BotConversationAlias.id == current.id))
    return alias
