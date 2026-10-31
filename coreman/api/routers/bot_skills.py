"""机器人技能申请与委员会审批。入队和批准不等于已安装。"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.bot_names import bot_names
from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.pagination import PageParams, paginate
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match
from coreman.core.audit import record_audit
from coreman.core.bots.events import notify_bot_changed
from coreman.core.bots.secrets import decrypt_json, mask_dict
from coreman.core.db.models import (
    Bot,
    BotSkill,
    RelayServer,
    Skill,
    SkillApproval,
    SkillSource,
    Task,
    User,
)
from coreman.core.knowledge import installation as installs
from coreman.core.knowledge import skill_policy as policy
from coreman.core.relay.agent_client import AgentError, call_agent
from coreman.core.timeutils import utcnow

router = APIRouter(prefix="/api/admin", tags=["skill-install"], dependencies=[Depends(verify_csrf)])


async def admin_bot(
    session: AsyncSession, identity: uuid.UUID, actor: User, *, lock: bool = True
) -> Bot:
    # 写路径锁 bots 行串行化安装/卸载；只读查询不加锁，免得被并发写操作或换机阻塞。
    query = select(Bot).where(Bot.id == identity)
    bot = await session.scalar(query.with_for_update() if lock else query)
    if bot is None:
        raise not_found("机器人不存在")
    if not await installs.bot_admin(session, bot, actor):
        raise forbidden()
    return bot


async def audit(
    session: AsyncSession,
    request: Request,
    actor: User,
    bot: Bot,
    action: str,
    skill_id: uuid.UUID,
) -> None:
    await record_audit(
        session,
        action=f"skill.{action}",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="bot",
        target_id=str(bot.id),
        diff={"skill_id": [None, str(skill_id)]},
        ip=client_ip(request),
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
    bot = await admin_bot(session, bot_id, actor, lock=False)
    rows = (
        await session.execute(
            select(BotSkill, Skill)
            .join(Skill, Skill.id == BotSkill.skill_id)
            # 目录已删除的技能只剩历史行，不再列出；仍在生效的行照常显示，便于停用。
            .where(
                BotSkill.bot_id == bot_id,
                or_(Skill.deleted_at.is_(None), BotSkill.status != "uninstalled"),
            )
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
    authorizers = {row.mcp_authorized_by for row, _ in rows if row.mcp_authorized_by}
    names: dict[uuid.UUID, str] = {}
    if authorizers:
        for user_id, display_name in await session.execute(
            select(User.id, User.display_name).where(User.id.in_(authorizers))
        ):
            names[user_id] = display_name
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
                    "mcp_oauth": skill.install_type == "mcp" and skill.mcp_oauth,
                    # 令牌在实例的 CLI 里；换了实例（换节点或 AI 类型）就要重新授权。
                    "mcp_authorized": row.mcp_authorized_at is not None
                    and row.mcp_authorized_relay_id == bot.relay_server_id,
                    "mcp_authorized_at": row.mcp_authorized_at,
                    "mcp_authorized_by": names.get(row.mcp_authorized_by),
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
    # 共享锁与目录删除的行锁互斥：删除与新申请并发时只有一个成功。
    skill = await session.get(Skill, skill_id, with_for_update={"read": True})
    if skill is None or skill.deleted_at is not None:
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
    existing = await session.get(BotSkill, (bot_id, skill_id))
    if existing is not None:
        # 手动安装过的技能归员工自己管，撤销业务系统授权时不再连带卸载。
        existing.installed_by_system = None
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
        # 安全约束可选：技能自身已做约束时可留空；未传字段才回落到目录模板。
        policy = body.requested_security_prompt
        if policy is None:
            policy = skill.security_prompt_template or ""
        policy = policy.strip()
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
        await audit(session, request, actor, bot, "approval_requested", skill_id)
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
    await audit(session, request, actor, bot, "install_requested", skill_id)
    await session.commit()
    return {"code": 0, "data": {"status": "installing", "task_id": task.id}}


async def upgrade_one(
    session: AsyncSession, request: Request, actor: User, bot: Bot, skill: Skill, row: BotSkill
) -> Task:
    """按上次成功安装的配置重装代码并重算环境变量；内部技能沿用原批准范围与安全约束。"""
    if await session.scalar(
        select(SkillApproval.id).where(
            SkillApproval.bot_id == bot.id,
            SkillApproval.skill_id == skill.id,
            SkillApproval.status == "pending",
        )
    ):
        raise ApiError(409, 409, "有待审申请")
    cipher = request.app.state.cipher
    groups = list(row.selected_env_groups)
    inputs = await installs.input_snapshot(
        session,
        cipher,
        bot,
        skill,
        selected=groups,
        source=await installs.last_data_source(session, cipher, bot.id, skill.id),
        values=None,
        requester=actor,
    )
    inputs.update(reinstall_code=True, upgrade=True)
    approval = None
    if skill.security_level == "internal":
        policy = row.security_prompt
        inputs.update(approved_databases=groups, security_prompt=policy)
        # 由点击升级的委员会成员按原范围重新批准一次，安装任务仍走同一套审批校验。
        approval = SkillApproval(
            bot_id=bot.id,
            skill_id=skill.id,
            requested_databases=groups,
            requested_security_prompt=policy or "",
            reinstall_code=True,
            skill_revision=skill.revision,
            request_inputs_enc=cipher.encrypt(
                json.dumps(inputs, ensure_ascii=False), installs.INPUTS_AAD
            ),
            requested_by=actor.id,
            approved_databases=groups,
            approved_security_prompt=policy,
            status="approved",
            reviewed_by=actor.id,
            reviewed_at=utcnow(),
            review_comment="一键升级：沿用原批准范围",
        )
        session.add(approval)
        await session.flush()
    else:
        inputs.update(approved_databases=[], security_prompt=None)
    return await installs.queue(
        session, cipher, bot=bot, skill=skill, actor=actor, inputs=inputs, approval=approval
    )


@router.post("/skills/{skill_id}/upgrade")
async def upgrade(
    skill_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """为已安装该技能的所有 AI 员工重装技能并刷新环境变量；不满足条件的员工跳过并说明原因。"""
    if actor.role not in ("ai_committee", "platform_admin"):
        raise forbidden()
    skill = await session.get(Skill, skill_id, with_for_update={"read": True})
    if skill is None or skill.deleted_at is not None:
        raise not_found("技能不存在")
    if not skill.enabled:
        raise ApiError(409, 409, "技能已停用，请先启用")
    bot_ids = list(
        await session.scalars(
            select(BotSkill.bot_id)
            .where(
                BotSkill.skill_id == skill_id,
                BotSkill.status != "uninstalled",
                BotSkill.installed_at.is_not(None),
            )
            .order_by(BotSkill.bot_id)
        )
    )
    names = await bot_names(session, bot_ids)
    queued: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for bot_id in bot_ids:
        bot = await session.scalar(select(Bot).where(Bot.id == bot_id).with_for_update())
        row = await session.get(BotSkill, (bot_id, skill_id))
        if bot is None or row is None:
            continue
        item = {"bot_id": bot_id, "bot_name": names.get(bot_id)}
        try:
            async with session.begin_nested():
                task = await upgrade_one(session, request, actor, bot, skill, row)
        except ApiError as exc:
            skipped.append({**item, "reason": exc.message})
            continue
        queued.append({**item, "task_id": task.id})
    await record_audit(
        session,
        action="skill.upgrade",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="skill",
        target_id=str(skill_id),
        diff={"queued": [None, len(queued)], "skipped": [None, len(skipped)]},
        ip=client_ip(request),
    )
    await session.commit()
    return {"code": 0, "data": {"queued": queued, "skipped": skipped}}


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
    await installs.uninstall(session, bot, row, actor)
    await notify_bot_changed(session, bot.id)
    await audit(session, request, actor, bot, "uninstalled", skill_id)
    await session.commit()
    return {"code": 0, "data": {"status": row.status, "revision": row.revision}}


class MCPLoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=8, max_length=200)
    callback_url: str = Field(min_length=1, max_length=8000)


async def mcp_target(
    session: AsyncSession, bot_id: uuid.UUID, skill_id: uuid.UUID, actor: User
) -> tuple[Bot, Skill, RelayServer, dict[str, str]]:
    """授权的对象：已安装、需要 OAuth 的 MCP 技能，以及员工当前的运行时实例。"""
    bot = await admin_bot(session, bot_id, actor, lock=False)
    row = await session.get(BotSkill, (bot_id, skill_id))
    skill = await session.get(Skill, skill_id)
    if row is None or skill is None:
        raise not_found("机器人技能不存在")
    if skill.install_type != "mcp" or not skill.mcp_oauth:
        raise ApiError(422, 422, "这个技能不需要 OAuth 授权")
    if row.status != "installed":
        raise ApiError(409, 409, "技能安装完成后才能授权")
    relay = await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None
    if relay is None or not relay.is_active or relay.runtime_node_id is None:
        raise ApiError(409, 409, "运行时未分配或不可用")
    payload = {
        "project_dir": bot.working_dir,
        "server_name": policy.mcp_server_name(skill, bot.id),
    }
    # 下面要等节点和用户授权，不占着数据库事务。
    await session.commit()
    return bot, skill, relay, payload


async def mcp_call(
    relay: RelayServer, request: Request, operation: str, payload: dict[str, Any]
) -> dict[str, Any]:
    cipher = request.app.state.cipher
    try:
        if (await call_agent(relay, cipher, "ping")).get("mcp_oauth") != 1:
            raise ApiError(409, 409, "请先升级运行时节点，再授权 MCP 技能")
        return await call_agent(relay, cipher, operation, payload)
    except AgentError as exc:
        if exc.detail:
            raise ApiError(422, 422, exc.detail) from exc
        raise ApiError(502, 502, "授权操作未完成，请检查运行时在线状态") from exc


async def mcp_record(
    session: AsyncSession,
    request: Request,
    actor: User,
    bot: Bot,
    skill_id: uuid.UUID,
    relay: RelayServer | None,
) -> BotSkill:
    row = await session.get(BotSkill, (bot.id, skill_id), with_for_update=True)
    if row is None:
        raise not_found("机器人技能不存在")
    row.mcp_authorized_at = utcnow() if relay else None
    row.mcp_authorized_by = actor.id if relay else None
    row.mcp_authorized_relay_id = relay.id if relay else None
    await audit(
        session, request, actor, bot, "mcp_authorized" if relay else "mcp_revoked", skill_id
    )
    await session.commit()
    return row


@router.post("/bots/{bot_id}/skills/{skill_id}/mcp-login")
async def mcp_login_start(
    bot_id: uuid.UUID,
    skill_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """由员工所在实例的 CLI 发起登录，返回授权链接；用户授权后把回调地址交给 complete。"""
    _, _, relay, payload = await mcp_target(session, bot_id, skill_id, actor)
    data = await mcp_call(relay, request, "mcp-login-start", payload)
    return {
        "code": 0,
        "data": {key: data.get(key) for key in ("session_id", "authorization_url", "expires_in")},
    }


@router.post("/bots/{bot_id}/skills/{skill_id}/mcp-login/complete")
async def mcp_login_complete(
    bot_id: uuid.UUID,
    skill_id: uuid.UUID,
    body: MCPLoginIn,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot, _, relay, payload = await mcp_target(session, bot_id, skill_id, actor)
    await mcp_call(
        relay,
        request,
        "mcp-login-complete",
        {**payload, "session_id": body.session_id, "callback_url": body.callback_url},
    )
    row = await mcp_record(session, request, actor, bot, skill_id, relay)
    return {
        "code": 0,
        "data": {"mcp_authorized_at": row.mcp_authorized_at, "revision": row.revision},
    }


@router.delete("/bots/{bot_id}/skills/{skill_id}/mcp-login")
async def mcp_logout(
    bot_id: uuid.UUID,
    skill_id: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot, _, relay, payload = await mcp_target(session, bot_id, skill_id, actor)
    await mcp_call(relay, request, "mcp-logout", payload)
    row = await mcp_record(session, request, actor, bot, skill_id, None)
    return {"code": 0, "data": {"revision": row.revision}}


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
    # 带上技能名：目录里已删除的技能不在技能列表中，历史申请也要能看出是哪个。
    skill_names = dict(
        (
            await session.execute(
                select(Skill.id, Skill.name).where(
                    Skill.id.in_({row.skill_id for row in page["items"]})
                )
            )
        )
        .tuples()
        .all()
    )
    return {
        "code": 0,
        "data": {
            **page,
            "items": [
                {
                    **approval_out(row),
                    "bot_name": names.get(row.bot_id),
                    "skill_name": skill_names.get(row.skill_id),
                }
                for row in page["items"]
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
        if row.requested_databases and not body.approved_databases:
            raise ApiError(422, 422, "批准时必须选择数据库范围")
        policy = body.approved_security_prompt
        if policy is None:
            policy = row.requested_security_prompt
        policy = policy.strip() or None
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
    await audit(session, request, actor, bot, f"approval_{row.status}", row.skill_id)
    await session.commit()
    return {"code": 0, "data": {"approval": approval_out(row), "task_id": task_id}}
