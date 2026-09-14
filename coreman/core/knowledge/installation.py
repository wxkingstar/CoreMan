"""技能安装的审批快照、任务入队与合并提示词。"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.permissions import is_bot_admin
from coreman.core.bots.secrets import decrypt_json, merge_secret_dict
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    BotMember,
    BotSkill,
    PlatformApp,
    Skill,
    SkillApproval,
    SkillSource,
    Task,
    User,
    UserIdentity,
)
from coreman.core.errors import ApiError
from coreman.core.knowledge.skill_policy import selected_groups, user_inputs
from coreman.runtime.bus import outbox, tasks

INPUTS_AAD = "skill_install.inputs"
USER_ENV_AAD = "bot_skills.user_env_vars_enc"
PRESET_AAD = "env_presets.vars_enc"
EFFECTIVE_AAD = "bot_skills.effective_env_enc"
MCP_AAD = "skills.mcp_config_enc"


async def bot_admin(session: AsyncSession, bot: Bot, actor: User) -> bool:
    members = list(
        await session.scalars(select(BotMember.user_id).where(BotMember.bot_id == bot.id))
    )
    return actor.status == "active" and is_bot_admin(actor, bot, members)


async def rebuild_prompt(session: AsyncSession, bot: Bot) -> None:
    rows = list(
        await session.scalars(
            select(BotSkill)
            .where(
                BotSkill.bot_id == bot.id,
                BotSkill.status != "uninstalled",
                BotSkill.installed_at.is_not(None),
            )
            .order_by(BotSkill.skill_id)
        )
    )
    policies = [row.security_prompt for row in rows if row.security_prompt]
    bot.merged_system_prompt = bot.system_prompt + (
        "\n\n# 已批准技能的安全约束\n\n" + "\n\n".join(policies) if policies else ""
    )


async def input_snapshot(
    session: AsyncSession,
    cipher: Cipher,
    bot: Bot,
    skill: Skill,
    *,
    selected: list[str],
    source: str | None,
    values: dict[str, str] | None,
    requester: User,
) -> dict[str, Any]:
    if not skill.enabled:
        raise ApiError(409, 409, "技能已停用")
    catalog_source = await session.get(SkillSource, skill.source_id)
    if catalog_source is None:
        raise ApiError(409, 409, "技能来源不存在")
    previous = await session.get(BotSkill, (bot.id, skill.id))
    if previous and previous.install_task_id:
        task = await session.get(Task, previous.install_task_id)
        if task and task.status in tasks.OPEN:
            raise ApiError(409, 409, "技能安装仍在进行，请先等待或取消")
    current = (
        decrypt_json(cipher, previous.user_env_vars_enc, USER_ENV_AAD)
        if previous and previous.user_env_vars_enc
        else {}
    )
    current = {key: value for key, value in current.items() if key in skill.user_env_vars}
    try:
        entered = user_inputs(
            skill, current if values is None else merge_secret_dict(current, values)
        )
        selected_groups(skill, selected, source)
    except ValueError as exc:
        raise ApiError(422, 422, str(exc)) from None
    return {
        "selected_env_groups": selected,
        "data_source": source or skill.default_data_source,
        "user_env_vars": entered,
        "requester_id": str(requester.id),
        "source_id": str(catalog_source.id),
        "source_version": catalog_source.version,
        "relay_id": str(bot.relay_server_id) if bot.relay_server_id else None,
        "working_dir": bot.working_dir,
    }


async def queue(
    session: AsyncSession,
    cipher: Cipher,
    *,
    bot: Bot,
    skill: Skill,
    actor: User,
    inputs: dict[str, Any],
    approval: SkillApproval | None = None,
) -> Task:
    if not bot.relay_server_id:
        raise ApiError(409, 409, "机器人尚未分配实例")
    row = await session.get(BotSkill, (bot.id, skill.id))
    if row is None:
        row = BotSkill(bot_id=bot.id, skill_id=skill.id, status="installing")
        session.add(row)
    if row.install_task_id:
        existing = await session.get(Task, row.install_task_id)
        if existing and existing.status in tasks.OPEN:
            raise ApiError(409, 409, "已有安装任务在进行")
    identity = str(approval.id) if approval else str(uuid.uuid4())
    task = await tasks.enqueue(
        session,
        tasks.NewTask(
            bot_id=bot.id,
            kind="skill_install",
            user_id=actor.id,
            dedupe_key=f"skill-install:{identity}",
            payload={
                "skill_id": str(skill.id),
                "skill_revision": skill.revision,
                "approval_id": str(approval.id) if approval else None,
                "inputs_enc": cipher.encrypt(json.dumps(inputs, ensure_ascii=False), INPUTS_AAD),
            },
        ),
    )
    if task is None:
        raise ApiError(409, 409, "此审批已提交安装")
    row.status, row.install_task_id, row.error_message = "installing", task.id, None
    await session.flush()
    return task


async def notify_approvers(
    session: AsyncSession, bot: Bot, skill: Skill, approval: SkillApproval
) -> int:
    apps = list(
        await session.scalars(
            select(PlatformApp).where(
                PlatformApp.platform == bot.platform,
                PlatformApp.enabled,
                PlatformApp.capabilities.contains(["notify"]),
            )
        )
    )
    if len(apps) != 1:
        return 0
    recipients = list(
        (
            await session.execute(
                select(User, UserIdentity)
                .join(UserIdentity, UserIdentity.user_id == User.id)
                .where(
                    User.status == "active",
                    User.role == "ai_committee",
                    UserIdentity.platform == bot.platform,
                )
            )
        ).all()
    )
    for actor, identity in recipients:
        await outbox.add(
            session,
            bot_id=None,
            platform=bot.platform,
            kind="notify",
            dedupe_key=f"skill-approval:{approval.id}:{actor.id}",
            target={
                "platform_app_id": str(apps[0].id),
                "user_id": str(actor.id),
                "platform_user_id": identity.platform_user_id,
                "skill_approval_id": str(approval.id),
            },
            payload={
                "msgtype": "text",
                "content": (
                    f"技能授权待审核\n机器人：{bot.name}\n技能：{skill.name}\n"
                    "请在 CoreMan 审批页查看申请。"
                ),
            },
        )
    return len(recipients)


async def effective_env(session: AsyncSession, cipher: Cipher, bot: Bot) -> dict[str, str]:
    """技能凭据仅随有效安装下发；用户手工配置优先，卸载无需猜测字典归属。"""
    from coreman.core.bots.secrets import ENV_AAD

    values: dict[str, str] = {}
    rows = await session.scalars(
        select(BotSkill)
        .where(
            BotSkill.bot_id == bot.id,
            BotSkill.status != "uninstalled",
            BotSkill.installed_at.is_not(None),
            BotSkill.effective_env_enc.is_not(None),
        )
        .order_by(BotSkill.skill_id)
    )
    for row in rows:
        incoming = decrypt_json(cipher, row.effective_env_enc or "", EFFECTIVE_AAD)
        for key, value in incoming.items():
            if key in values and values[key] != value:
                raise ValueError("skill_environment_conflict")
            values[key] = value
    values.update(decrypt_json(cipher, bot.env_vars_enc, ENV_AAD))
    return values


async def recover_installations(session: AsyncSession, now: Any) -> int:
    """任务已终结但安装行未收尾时，仅标记未知失败，不重放外部安装。"""
    candidates = list(
        await session.scalars(
            select(BotSkill.bot_id)
            .outerjoin(Task, Task.id == BotSkill.install_task_id)
            .where(
                BotSkill.status == "installing",
                (Task.id.is_(None) | Task.status.not_in(tasks.OPEN)),
            )
            .distinct()
            .order_by(BotSkill.bot_id)
            .limit(500)
        )
    )
    count = 0
    for identity in candidates:
        bot = await session.scalar(
            select(Bot).where(Bot.id == identity).with_for_update(skip_locked=True)
        )
        if bot is None:
            continue
        rows = await session.scalars(
            select(BotSkill).where(BotSkill.bot_id == identity, BotSkill.status == "installing")
        )
        for row in rows:
            task = await session.get(Task, row.install_task_id) if row.install_task_id else None
            if task is not None and task.status in tasks.OPEN:
                continue
            row.status, row.install_task_id = "failed", None
            row.error_message = "安装任务已中断，请检查实例状态后重新提交；不会自动重试。"
            count += 1
    return count
