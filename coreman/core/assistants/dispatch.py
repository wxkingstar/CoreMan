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
from datetime import datetime

from sqlalchemy import ColumnElement, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import jobs
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants.kinds import BOT_HELP_KIND
from coreman.core.assistants.records import is_assistant
from coreman.core.audit import record_audit
from coreman.core.bus import tasks
from coreman.core.bus.tasks import NewTask
from coreman.core.chat import bot_collaboration
from coreman.core.db.models import AssistantJob, AssistantSettings, Bot, BotAllowedUser, User
from coreman.core.prompting import Speaker
from coreman.core.timeutils import aware_utc, utcnow

TOOL = "assistant_ask_bot"
# 问题的字数上限：够讲清一件事，贴不下整段聊天记录。
MAX_QUESTION = 4000
MAX_REF = 100
LIST_LIMIT = 100
DESCRIPTION_LIMIT = 200
SESSION_PREFIX = "assistant-help:"
AUDIT_ACTION = "assistant.dispatch"
# 行的标题：给所有者在「我的助理」页与列表里认的，不含问题内容。
TITLE = "请「{name}」协助"


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


async def send(
    session: AsyncSession,
    bot: Bot,
    target: Bot,
    question: str,
    *,
    allow_write: bool,
    attended: bool,
    approved: bool = False,
    job_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> AssistantJob:
    """记下一次求助并给目标员工排一轮。不查资格：调用方先 `resolve` 或 `authorize`。

    名额与每天的个数和子任务共用（`jobs._admit` 在同一把锁里数，不分种类）。问题只进这一行的
    密文列，目标员工那边的任务载荷只有编号；它开轮时才把问题解出来交给模型。

    Args:
        allow_write: 所有者是否批准了让对方改东西；只读求助为假
        attended: 发起它的那一轮所有者在不在场；不在场的要看每日轮次预算
        approved: 这是所有者刚在审批系统里批准的一次派出，不看预算
        job_id: 指定行的编号（审批执行用审批单的编号，重跑时不会派第二次）

    Raises:
        DispatchError: invalid_question / question_too_long / assistant_unavailable /
            already_dispatched
        jobs.JobError: content_locked / too_many_jobs / daily_job_limit /
            daily_budget_exhausted
    """
    now = aware_utc(now or utcnow())
    body = clean_question(question)
    prefs, owner = await owner_of(session, bot)
    await jobs._admit(session, bot, prefs, new=True, budget=not (attended or approved), now=now)
    row_id = job_id or uuid.uuid4()
    key = session_key_for(row_id)
    task = await tasks.enqueue(
        session,
        NewTask(
            bot_id=target.id,
            kind=BOT_HELP_KIND,
            user_id=owner.id,
            session_key=key,
            dedupe_key=f"{key}:round:1",
            payload={
                "job_id": str(row_id),
                "round": 1,
                "allow_write": allow_write,
                "bot_key": target.bot_key,
                "platform_user_id": prefs.platform_user_id,
            },
        ),
    )
    if task is None:
        raise DispatchError("already_dispatched")
    row = AssistantJob(
        id=row_id,
        bot_id=bot.id,
        kind="bot_help",
        origin="assistant",
        target_bot_id=target.id,
        status="queued",
        title_enc=TITLE.format(name=" ".join(target.name.split()))[: jobs.MAX_TITLE],
        brief_enc=body,
        session_key=key,
        origin_session_key=prefs.chat_id,
        rounds=1,
        last_task_id=task.id,
        report=True,
        report_seq=0,
        report_failures=0,
        deadline_at=now + jobs.LIFETIME,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    # 审计只记谁的助理、问了哪位员工、所有者在不在场、有没有放开写；问题正文不进审计。
    await record_audit(
        session,
        action=AUDIT_ACTION,
        actor_id=owner.id,
        actor_login=owner.login_name,
        target_type="bot",
        target_id=str(target.id),
        diff={
            "assistant_bot_id": str(bot.id),
            "job_id": str(row_id),
            "unattended": not attended,
            "allow_write": allow_write,
            "approved": approved,
            "question_chars": len(body),
        },
    )
    return row
