"""飞书单人群（真人只有发话人自己）的这一轮按私聊处理：判断、记到任务上、交给后面各阶段。

每一轮都判断一次（见 `core.chat.solo_group`，按群缓存，多数轮次不发请求），结果写进任务
payload：个人工具等服务端核验凭它把这个群当私聊；不是单人群时清掉旧标记（同一任务被重新认领、
群里进了人）。后面的阶段只看
`Intake.private`，不再各自判断群和私聊。

只有本人在场、身份已核实的普通对话才判断：旁听插话、调度、告警、协作、任务平台、业务系统
发起的回合，以及个人助理和企业微信，都照原来的规则。
"""

from __future__ import annotations

import json
from dataclasses import replace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.api_tasks import groups as api_groups
from coreman.core.chat import alert_sources, solo_group
from coreman.core.feishu_personal.policy import PRIVATE_TURN_KINDS
from coreman.runtime.worker.chat.models import Intake
from coreman.runtime.worker.context import TaskContext

# 这些来源的一轮不是发话人本人当面要的，或者结果不只回给他。
_OTHER_ORIGINS = ("observe", "dispatch", alert_sources.ORIGIN)
_OTHER_KEYS = (
    "collaboration_id",
    "collaboration_phase",
    "human_collaboration_id",
    "work_task_id",
    "api_task_id",
)


def eligible(ctx: TaskContext, intake: Intake) -> bool:
    payload = ctx.task.payload or {}
    return (
        intake.bot.platform == "feishu"
        and intake.bot.kind == "standard"
        and intake.chat_type == "group"
        and ctx.task.kind in PRIVATE_TURN_KINDS
        and payload.get("origin") not in _OTHER_ORIGINS
        and not any(payload.get(key) for key in _OTHER_KEYS)
        and not api_groups.is_group_payload(payload)
        and not intake.session_key.startswith("work:")
        and intake.speaker.known
        and intake.speaker.user_id is not None
        and bool(intake.speaker.platform_user_id)
    )


async def check(ctx: TaskContext, intake: Intake) -> bool:
    """只问飞书，不碰数据库：调用方可以在不持有事务的时候查（最多几秒）。"""
    return eligible(ctx, intake) and await solo_group.solo(
        ctx.cipher, intake.bot, intake.chat_id, intake.speaker.platform_user_id
    )


async def apply(session: AsyncSession, ctx: TaskContext, intake: Intake) -> Intake:
    """判断这一轮是不是单人群，记到任务上，返回带结果的 `Intake`。"""
    return await record(session, ctx, intake, await check(ctx, intake))


async def record(session: AsyncSession, ctx: TaskContext, intake: Intake, is_solo: bool) -> Intake:
    """把判断结果写进任务（是就加标记，不是就清掉旧标记），返回带结果的 `Intake`。"""
    if is_solo:
        await session.execute(
            text("UPDATE tasks SET payload = payload || CAST(:extra AS jsonb) WHERE id = :id"),
            {"extra": json.dumps({solo_group.PAYLOAD_KEY: True}), "id": ctx.task.id},
        )
        ctx.task.payload = {**(ctx.task.payload or {}), solo_group.PAYLOAD_KEY: True}
    elif solo_group.PAYLOAD_KEY in (ctx.task.payload or {}):
        await session.execute(
            text("UPDATE tasks SET payload = payload - :key WHERE id = :id"),
            {"key": solo_group.PAYLOAD_KEY, "id": ctx.task.id},
        )
        ctx.task.payload = {
            k: v for k, v in (ctx.task.payload or {}).items() if k != solo_group.PAYLOAD_KEY
        }
    if is_solo:
        ctx.log.info("solo_group_private", chat_id=intake.chat_id)
    ctx.solo_checked = True
    return replace(intake, solo=is_solo)
