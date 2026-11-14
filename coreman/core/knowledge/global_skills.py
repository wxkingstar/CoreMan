"""全局技能：管理员在目录里勾选一次，所有普通 AI 员工（含之后新建的）都自动安装。

不另起一条下发链路，而是替每个员工排一次普通安装：代码同步、环境预设注入（effective_env）、
换机重装、一键升级都沿用逐员工安装的那一套，未升级的运行时节点也不受影响。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, BotSkill, GlobalSkill, Skill, User
from coreman.core.errors import ApiError
from coreman.core.knowledge import installation as installs
from coreman.core.knowledge.skill_policy import selected_groups, user_inputs
from coreman.core.timeutils import utcnow

MANAGER_ROLES = ("ai_committee", "platform_admin")
# 自动安装失败后隔多久再试：新员工的工作目录可能还没建好，过一阵通常就能装上。
RETRY_AFTER = timedelta(hours=1)
# 每个全局技能每轮最多排多少个员工，免得一轮里堆出成百上千个安装任务。
SWEEP_BATCH = 50
ACTIVE = ("installed", "installing", "pending_approval")


def problem(skill: Skill | None) -> str | None:
    """能否设为全局技能：要能不经审批、不填参数、不逐个授权就给每个员工装上。"""
    if skill is None or skill.deleted_at is not None:
        return "技能不存在"
    if not skill.enabled:
        return "技能已停用，请先启用"
    if skill.security_level == "internal":
        return "内部技能需要按员工审批，不能设为全局技能"
    if skill.install_type == "mcp" and skill.mcp_oauth:
        return "需要逐个员工 OAuth 授权的 MCP 技能不能设为全局技能"
    try:
        user_inputs(skill, {})
        selected_groups(skill, [], None)
    except ValueError:
        return "技能需要按员工填写安装参数，不能设为全局技能"
    return None


async def is_global(session: AsyncSession, skill_id: uuid.UUID) -> bool:
    return await session.get(GlobalSkill, skill_id) is not None


async def global_ids(session: AsyncSession) -> set[uuid.UUID]:
    return set(await session.scalars(select(GlobalSkill.skill_id)))


def eligible_bots() -> tuple[ColumnElement[bool], ...]:
    """会自动装上全局技能的员工：启用的普通员工，已分配运行时，工作目录就绪。个人助理排除。"""
    return (
        Bot.enabled.is_(True),
        Bot.kind == "standard",
        Bot.relay_server_id.is_not(None),
        Bot.workspace_state == "ready",
    )


async def install_one(
    session: AsyncSession,
    cipher: Cipher,
    *,
    bot: Bot,
    skill: Skill,
    actor: User,
    retry_failed: bool,
    now: datetime,
) -> bool:
    """给一个员工排全局技能的安装，返回是否排了。

    已装、正在装或待审的不动（手动装的保留员工自己的配置，也不改成全局来源）；
    失败的行只在管理员重新勾选时、或自动安装失败满 RETRY_AFTER 后重试。
    """
    row = await session.get(BotSkill, (bot.id, skill.id))
    if row is not None and row.status in ACTIVE:
        return False
    if (
        row is not None
        and row.status == "failed"
        and not retry_failed
        and not (row.installed_globally and row.updated_at < now - RETRY_AFTER)
    ):
        return False
    inputs = await installs.input_snapshot(
        session, cipher, bot, skill, selected=[], source=None, values=None, requester=actor
    )
    inputs.update(
        reinstall_code=True, approved_databases=[], security_prompt=None, global_skill=True
    )
    await installs.queue(session, cipher, bot=bot, skill=skill, actor=actor, inputs=inputs)
    row = await session.get(BotSkill, (bot.id, skill.id))
    assert row is not None
    # updated_at 没有自动更新，这里记下排队时间，失败后据此决定何时重试。
    row.installed_globally, row.updated_at = True, now
    return True


def pending_condition(now: datetime, *, retry_failed: bool) -> ColumnElement[bool]:
    """还没装上的员工：没有安装行、已卸载，或（按需）安装失败。"""
    failed: ColumnElement[bool] = BotSkill.status == "failed"
    if not retry_failed:
        failed = and_(
            failed, BotSkill.installed_globally.is_(True), BotSkill.updated_at < now - RETRY_AFTER
        )
    return or_(BotSkill.bot_id.is_(None), BotSkill.status == "uninstalled", failed)


async def pending_bots(
    session: AsyncSession, skill_id: uuid.UUID, now: datetime, *, retry_failed: bool, limit: int
) -> list[uuid.UUID]:
    return list(
        await session.scalars(
            select(Bot.id)
            .outerjoin(BotSkill, and_(BotSkill.bot_id == Bot.id, BotSkill.skill_id == skill_id))
            .where(
                *eligible_bots(),
                pending_condition(now, retry_failed=retry_failed),
            )
            .order_by(Bot.id)
            .limit(limit)
        )
    )


async def apply(
    session: AsyncSession,
    cipher: Cipher,
    skill: Skill,
    actor: User,
    *,
    retry_failed: bool,
    limit: int | None = None,
    skip_locked: bool = False,
) -> tuple[list[uuid.UUID], list[tuple[uuid.UUID, str]]]:
    """给还没装上的员工排安装。返回（已排队的员工，跳过的员工与原因）。"""
    now = utcnow()
    queued: list[uuid.UUID] = []
    skipped: list[tuple[uuid.UUID, str]] = []
    for bot_id in await pending_bots(
        session, skill.id, now, retry_failed=retry_failed, limit=limit or 100000
    ):
        query = select(Bot).where(Bot.id == bot_id)
        bot = await session.scalar(
            query.with_for_update(skip_locked=True) if skip_locked else query.with_for_update()
        )
        if bot is None:
            continue
        try:
            async with session.begin_nested():
                if await install_one(
                    session,
                    cipher,
                    bot=bot,
                    skill=skill,
                    actor=actor,
                    retry_failed=retry_failed,
                    now=now,
                ):
                    queued.append(bot_id)
        except ApiError as exc:
            skipped.append((bot_id, exc.message))
    return queued, skipped


async def release(
    session: AsyncSession, skill_id: uuid.UUID, actor: User, *, keep_installed: bool
) -> list[uuid.UUID]:
    """取消全局：随全局装上的行默认卸载；keep_installed 时改按手动安装保留。

    同时是业务系统配套技能的行只去掉全局来源，交给系统授权管。返回被卸载的员工。
    """
    rows = list(
        await session.scalars(
            select(BotSkill).where(
                BotSkill.skill_id == skill_id,
                BotSkill.installed_globally,
                BotSkill.status != "uninstalled",
            )
        )
    )
    removed: list[uuid.UUID] = []
    for row in rows:
        row.installed_globally = False
        if keep_installed or row.installed_by_system:
            continue
        bot = await session.scalar(select(Bot).where(Bot.id == row.bot_id).with_for_update())
        if bot is None:
            continue
        await installs.uninstall(session, bot, row, actor)
        removed.append(bot.id)
    # 已卸载的历史行也去掉标记，免得以后再设全局时被当成自动安装的失败行。
    for row in await session.scalars(
        select(BotSkill).where(BotSkill.skill_id == skill_id, BotSkill.installed_globally)
    ):
        row.installed_globally = False
    return removed


async def sweep(session: AsyncSession, cipher: Cipher) -> int:
    """调度器定期补装：新建的员工、刚分到运行时或刚启用的员工、自动安装失败满一小时的员工。

    以设置人的名义排队；设置人停用或失去委员会身份时跳过，需有人重新勾选。
    """
    count = 0
    pairs = (
        await session.execute(
            select(GlobalSkill, Skill)
            .join(Skill, Skill.id == GlobalSkill.skill_id)
            .order_by(Skill.name)
        )
    ).all()
    for entry, skill in pairs:
        if problem(skill):
            continue
        actor = await session.get(User, entry.set_by)
        if actor is None or actor.status != "active" or actor.role not in MANAGER_ROLES:
            continue
        queued, _ = await apply(
            session, cipher, skill, actor, retry_failed=False, limit=SWEEP_BATCH, skip_locked=True
        )
        count += len(queued)
    return count
