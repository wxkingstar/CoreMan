"""调度公共 AI 员工：助理代所有者把一个问题交给他有权使用的普通员工。

一次求助是一行 `assistant_jobs`（`kind = 'bot_help'`，`bot_id` 是助理，`target_bot_id` 是目标
员工）；目标员工那边的一轮是一个 `assistant_bot_help` 任务，会话键 `assistant-help:<编号>`，
只在本模块入队。助理这边的问题与答复在 `assistant_jobs` 的密文列里；目标员工名下的那一轮照
普通私聊的方式存放，标为私有、只有所有者本人看得到内容（分层总览裁定 17）。

资格与员工之间求助同一个口径（`bot_collaboration.authorized_peer`）：目标员工启用、是飞书的
普通员工、开启了接受求助，所有者在它的可用人范围内。派出时、目标那一轮开轮前、审批执行时
各查一次。全部函数只 flush，调用方提交。
"""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.records import is_assistant
from coreman.core.chat import bot_collaboration
from coreman.core.db.models import AssistantSettings, Bot, BotAllowedUser, User
from coreman.core.prompting import Speaker

TOOL = "assistant_ask_bot"
# 问题的字数上限：够讲清一件事，贴不下整段聊天记录。
MAX_QUESTION = 4000
MAX_REF = 100
LIST_LIMIT = 100
DESCRIPTION_LIMIT = 200
SESSION_PREFIX = "assistant-help:"


class DispatchError(Exception):
    """调用方能照着改的拒绝；`code` 原样作为工具的 error 返回，不带任何内容。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def session_key_for(job_id: uuid.UUID) -> str:
    """这次求助在目标员工那边的会话键：与任何真实会话都不相撞，也发不出消息。"""
    return f"{SESSION_PREFIX}{job_id}"


def clean_question(value: str) -> str:
    """去掉首尾空白并卡住长度。

    Raises:
        DispatchError: invalid_question / question_too_long
    """
    out = value.strip()
    if not out:
        raise DispatchError("invalid_question")
    if len(out) > MAX_QUESTION:
        raise DispatchError("question_too_long")
    return out


async def owner_of(session: AsyncSession, bot: Bot) -> tuple[AssistantSettings, User]:
    """这位助理开通时记下的身份与它的所有者；助理或所有者现在不可用时拒绝。

    Raises:
        DispatchError: assistant_unavailable
    """
    if not (
        bot.enabled
        and is_assistant(bot)
        and bot.assistant_state == "active"
        and bot.owner_id is not None
    ):
        raise DispatchError("assistant_unavailable")
    prefs = await session.get(AssistantSettings, bot.id)
    owner = await session.get(User, bot.owner_id, populate_existing=True)
    if (
        prefs is None
        or not assistant_settings.identified(prefs)
        or owner is None
        or owner.status != "active"
        or owner.source == "bootstrap"
    ):
        raise DispatchError("assistant_unavailable")
    return prefs, owner


async def authorize(session: AsyncSession, bot: Bot, target_id: uuid.UUID) -> tuple[Bot, Speaker]:
    """所有者现在能不能经这位助理调度 `target_id`；返回目标员工与所有者的发言者身份。

    找不到与调不动分开报：前者是没有这位员工（或它是助理、已停用、不是飞书员工），后者是它
    没开接受求助、所有者不在它的可用人范围内，或所有者的飞书身份对不上了。

    Raises:
        DispatchError: assistant_unavailable / bot_not_found / bot_not_available
    """
    prefs, owner = await owner_of(session, bot)
    target = await session.get(Bot, target_id, populate_existing=True)
    if (
        target is None
        or target.id == bot.id
        or target.kind != "standard"
        or target.platform != "feishu"
        or not target.enabled
    ):
        raise DispatchError("bot_not_found")
    try:
        speaker = await bot_collaboration.authorized_peer(
            session, bot.id, target.id, prefs.platform_user_id, owner.id
        )
    except ValueError:
        raise DispatchError("bot_not_available") from None
    return target, speaker


def _dispatchable(bot: Bot) -> tuple[ColumnElement[bool], ...]:
    return (
        Bot.kind == "standard",
        Bot.platform == "feishu",
        Bot.enabled.is_(True),
        Bot.id != bot.id,
    )


async def candidates(session: AsyncSession, bot: Bot) -> list[Bot]:
    """所有者经这位助理可以调度的员工，按名称排序，至多 `LIST_LIMIT` 位。"""
    try:
        _, owner = await owner_of(session, bot)
    except DispatchError:
        return []
    limited = exists().where(BotAllowedUser.bot_id == Bot.id)
    named = exists().where(BotAllowedUser.bot_id == Bot.id, BotAllowedUser.user_id == owner.id)
    return list(
        await session.scalars(
            select(Bot)
            .where(*_dispatchable(bot), Bot.accept_bot_help.is_(True), or_(~limited, named))
            .order_by(Bot.name, Bot.bot_key)
            .limit(LIST_LIMIT)
        )
    )


def listing(rows: list[Bot]) -> list[dict[str, str]]:
    """给助理看的名单：标识、名称、一句话简介。"""
    return [
        {
            "bot": row.bot_key,
            "name": " ".join(row.name.split()),
            "description": " ".join((row.description or "").split())[:DESCRIPTION_LIMIT],
        }
        for row in rows
    ]


async def resolve(session: AsyncSession, bot: Bot, ref: str) -> Bot:
    """按标识（`bot_key`）或名称找到目标员工并核对资格。

    Raises:
        DispatchError: invalid_bot / bot_not_found / bot_ambiguous / bot_not_available /
            assistant_unavailable
    """
    ref = " ".join(ref.split())
    if not ref or len(ref) > MAX_REF:
        raise DispatchError("invalid_bot")
    await owner_of(session, bot)
    base = _dispatchable(bot)
    found = list(await session.scalars(select(Bot).where(*base, Bot.bot_key == ref).limit(1)))
    if not found:
        found = list(
            await session.scalars(
                select(Bot)
                .where(*base, func.lower(Bot.name) == ref.lower())
                .order_by(Bot.bot_key)
                .limit(2)
            )
        )
    if not found:
        raise DispatchError("bot_not_found")
    if len(found) > 1:
        raise DispatchError("bot_ambiguous")
    target, _ = await authorize(session, bot, found[0].id)
    return target
