"""机器人技能申请与委员会审批。入队和批准不等于已安装。"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.bot_names import bot_names
from coreman.api.deps import current_user, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.pagination import PageParams, paginate
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match
from coreman.core.audit import record_audit
from coreman.core.bots.events import notify_bot_changed
from coreman.core.bots.secrets import decrypt_json, mask_dict
from coreman.core.bus import tasks
from coreman.core.db.models import Bot, BotSkill, Skill, SkillApproval, SkillSource, User
from coreman.core.knowledge import installation as installs
from coreman.core.timeutils import utcnow

router = APIRouter(prefix="/api/admin", tags=["skill-install"], dependencies=[Depends(verify_csrf)])


async def admin_bot(session: AsyncSession, identity: uuid.UUID, actor: User) -> Bot:
    bot = await session.scalar(select(Bot).where(Bot.id == identity).with_for_update())
    if bot is None:
        raise not_found("机器人不存在")
    if not await installs.bot_admin(session, bot, actor):
        raise forbidden()
    return bot


async def audit(
    session: AsyncSession, actor: User, bot: Bot, action: str, skill_id: uuid.UUID
) -> None:
    await record_audit(
        session,
        action=f"skill.{action}",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="bot",
        target_id=str(bot.id),
        diff={"skill_id": [None, str(skill_id)]},
    )


def approval_out(row: SkillApproval) -> dict[str, Any]:
    keys = (
        "id",
        "bot_id",
        "skill_id",
        "requested_databases",
        "requested_security_prompt",
        "reinstall_code",
        "skill_revision",
        "approved_databases",
        "approved_security_prompt",
        "status",
        "requested_by",
        "requested_at",
        "reviewed_by",
        "reviewed_at",
        "review_comment",
        "version",
    )
    return {key: getattr(row, key) for key in keys}


@router.get("/bots/{bot_id}/skills")
async def bot_skills(
    bot_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await admin_bot(session, bot_id, actor)
    rows = (
        await session.execute(
            select(BotSkill, Skill)
            .join(Skill, Skill.id == BotSkill.skill_id)
            .where(BotSkill.bot_id == bot_id)
            .order_by(Skill.name)
        )
    ).all()
    pending = list(
        await session.scalars(
            select(SkillApproval)
            .where(SkillApproval.bot_id == bot_id, SkillApproval.status == "pending")
            .order_by(SkillApproval.requested_at)
        )
    )
    return {
        "code": 0,
        "data": {
            "items": [
                {
                    "skill_id": row.skill_id,
                    "name": skill.name,
                    "status": row.status,
                    "version": row.version,
                    "revision": row.revision,
                    "installed_at": row.installed_at,
                    "selected_env_groups": row.selected_env_groups,
                    "user_env_vars": mask_dict(
                        decrypt_json(
                            request.app.state.cipher, row.user_env_vars_enc, installs.USER_ENV_AAD
                        )
                    )
                    if row.user_env_vars_enc
                    else {},
                    "security_prompt": row.security_prompt,
                    "approved_databases": row.approved_databases,
                    "install_task_id": row.install_task_id,
                    "error_message": row.error_message,
                }
                for row, skill in rows
            ],
            "pending_approvals": [approval_out(row) for row in pending],
        },
    }


class InstallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_env_groups: list[str] = Field(default_factory=list, max_length=100)
    data_source: str | None = Field(default=None, max_length=50)
    user_env_vars: dict[str, str] | None = Field(default=None, max_length=100)
    requested_security_prompt: str | None = Field(default=None, max_length=16000)
    reinstall_code: bool = True


@router.post("/bots/{bot_id}/skills/{skill_id}/install")
async def install(
    bot_id: uuid.UUID,
    skill_id: uuid.UUID,
    body: InstallIn,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await admin_bot(session, bot_id, actor)
    skill = await session.get(Skill, skill_id)
    if skill is None:
        raise not_found("技能不存在")
    if await session.scalar(
        select(SkillApproval.id).where(
            SkillApproval.bot_id == bot_id,
            SkillApproval.skill_id == skill_id,
            SkillApproval.status == "pending",
        )
    ):
        raise ApiError(409, 409, "技能已有待审申请，请先审核或撤回")
    cipher = request.app.state.cipher
    inputs = await installs.input_snapshot(
        session,
        cipher,
        bot,
        skill,
        selected=body.selected_env_groups,
        source=body.data_source,
        values=body.user_env_vars,
        requester=actor,
    )
    inputs["reinstall_code"] = body.reinstall_code
    if skill.security_level == "internal":
        policy = body.requested_security_prompt or skill.security_prompt_template or ""
        if not policy.strip():
            raise ApiError(422, 422, "内部技能必须填写申请安全约束")
        if skill.selectable_env_groups and not body.selected_env_groups:
            raise ApiError(422, 422, "请选择申请的数据库范围")
        row = SkillApproval(
            bot_id=bot_id,
            skill_id=skill_id,
            requested_databases=body.selected_env_groups,
            requested_security_prompt=policy,
            reinstall_code=body.reinstall_code,
            skill_revision=skill.revision,
            request_inputs_enc=cipher.encrypt(
                json.dumps(inputs, ensure_ascii=False), installs.INPUTS_AAD
            ),
            requested_by=actor.id,
        )
        session.add(row)
        installed = await session.get(BotSkill, (bot_id, skill_id))
        if installed is None:
            session.add(BotSkill(bot_id=bot_id, skill_id=skill_id, status="pending_approval"))
        elif installed.installed_at is None:
            installed.status = "pending_approval"
        await session.flush()
        notifications = await installs.notify_approvers(session, bot, skill, row)
        await audit(session, actor, bot, "approval_requested", skill_id)
        await session.commit()
        return {
            "code": 0,
            "data": {
                "status": "pending_approval",
                "approval": approval_out(row),
                "notifications_queued": notifications,
            },
        }
    inputs.update(approved_databases=[], security_prompt=None)
    task = await installs.queue(session, cipher, bot=bot, skill=skill, actor=actor, inputs=inputs)
    await audit(session, actor, bot, "install_requested", skill_id)
    await session.commit()
    return {"code": 0, "data": {"status": "installing", "task_id": task.id}}


@router.delete("/bots/{bot_id}/skills/{skill_id}")
async def uninstall(
    bot_id: uuid.UUID,
    skill_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await admin_bot(session, bot_id, actor)
    row = await session.get(BotSkill, (bot_id, skill_id))
    if row is None:
        raise not_found("机器人技能不存在")
    require_if_match(request, row.revision)
    if row.install_task_id:
        await tasks.request_cancel(session, row.install_task_id, "skill_uninstalled")
    pending = list(
        await session.scalars(
            select(SkillApproval)
            .where(
                SkillApproval.bot_id == bot_id,
                SkillApproval.skill_id == skill_id,
                SkillApproval.status == "pending",
            )
            .with_for_update()
        )
    )
    for approval in pending:
        approval.status, approval.reviewed_by, approval.reviewed_at = (
            "withdrawn",
            actor.id,
            utcnow(),
        )
    row.status, row.install_task_id, row.security_prompt = "uninstalled", None, None
    row.approved_databases, row.approved_by, row.approved_at = [], None, None
    await session.flush()
    await installs.rebuild_prompt(session, bot)
    await notify_bot_changed(session, bot.id)
    await audit(session, actor, bot, "uninstalled", skill_id)
    await session.commit()
    return {"code": 0, "data": {"status": row.status, "revision": row.revision}}


@router.get("/skill-approvals")
async def approvals(
    actor: User = Depends(current_user),
    params: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if actor.role not in ("ai_committee", "platform_admin"):
        raise forbidden()
    page = await paginate(
        session, select(SkillApproval).order_by(SkillApproval.requested_at.desc()), params
    )
    names = await bot_names(session, (row.bot_id for row in page["items"]))
    return {
        "code": 0,
        "data": {
            **page,
            "items": [
                {**approval_out(row), "bot_name": names.get(row.bot_id)} for row in page["items"]
            ],
        },
    }


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    approved_databases: list[str] = Field(default_factory=list, max_length=100)
    approved_security_prompt: str | None = Field(default=None, max_length=16000)
    comment: str = Field(default="", max_length=2000)


@router.post("/skill-approvals/{identity}/review")
async def review(
    identity: uuid.UUID,
    body: ReviewIn,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if actor.role not in ("ai_committee", "platform_admin"):
        raise forbidden()
    row = await session.get(SkillApproval, identity)
    if row is None:
        raise not_found("审批不存在")
    bot = await session.scalar(select(Bot).where(Bot.id == row.bot_id).with_for_update())
    await session.refresh(row, with_for_update=True)
    require_if_match(request, row.version)
    if bot is None or row.status != "pending":
        raise ApiError(409, 409, "审批已处理或机器人已移除")
    if not set(body.approved_databases) <= set(row.requested_databases):
        raise ApiError(422, 422, "批准范围不能超出申请范围")
    task_id = None
    if body.decision == "approve":
        skill = await session.get(Skill, row.skill_id)
        requester = await session.get(User, row.requested_by)
        if skill is None or not skill.enabled or skill.revision != row.skill_revision:
            raise ApiError(409, 409, "技能目录已变更，请撤回后重新申请")
        if requester is None or not await installs.bot_admin(session, bot, requester):
            raise ApiError(403, 403, "申请人已失去机器人管理权限")
        inputs = json.loads(
            request.app.state.cipher.decrypt(row.request_inputs_enc, installs.INPUTS_AAD)
        )
        source = await session.get(SkillSource, skill.source_id)
        if (
            source is None
            or source.version != inputs["source_version"]
            or str(source.id) != inputs["source_id"]
        ):
            raise ApiError(409, 409, "技能来源已变更，请重新申请")
        if (
            inputs["relay_id"] != str(bot.relay_server_id)
            or inputs["working_dir"] != bot.working_dir
        ):
            raise ApiError(409, 409, "机器人实例或工作目录已变更，请重新申请")
        policy = body.approved_security_prompt or row.requested_security_prompt
        if not policy.strip() or (row.requested_databases and not body.approved_databases):
            raise ApiError(422, 422, "批准时必须明确安全约束与数据库范围")
        row.approved_databases, row.approved_security_prompt = body.approved_databases, policy
        row.status = "approved"
        inputs.update(
            selected_env_groups=[
                group for group in inputs["selected_env_groups"] if group in body.approved_databases
            ],
            approved_databases=body.approved_databases,
            security_prompt=policy,
        )
        task = await installs.queue(
            session,
            request.app.state.cipher,
            bot=bot,
            skill=skill,
            actor=actor,
            inputs=inputs,
            approval=row,
        )
        task_id = task.id
    else:
        row.status = "rejected"
        installed = await session.get(BotSkill, (bot.id, row.skill_id))
        if installed and installed.installed_at is None:
            installed.status = "uninstalled"
    row.reviewed_by, row.reviewed_at, row.review_comment = actor.id, utcnow(), body.comment
    await audit(session, actor, bot, f"approval_{row.status}", row.skill_id)
    await session.commit()
    return {"code": 0, "data": {"approval": approval_out(row), "task_id": task_id}}
