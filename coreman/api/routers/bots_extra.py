"""机器人子资源：切换 relay、启停、协作者、白名单。

与 bots.py 的 CRUD 共用 `/api/admin/bots` 前缀，拆成第二个路由器只是为了让 bots.py 不再变长；
校验与视图函数（load_bot / member_ids_of / build_out）都从那边复用，换机的判定与副作用则在
`coreman.core.bots.switch_relay` 里——worker 的限流自动切换走的是同一条路。
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.bot_permissions import (
    can_manage_members,
    can_switch_relay,
    can_toggle_bot,
    is_bot_admin,
)
from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.routers.bots import build_out, load_bot, member_ids_of, notify_bot_changed
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match, set_etag
from coreman.core.assistants.records import is_assistant
from coreman.core.audit import diff_dict, record_audit
from coreman.core.bots.switch_relay import SwitchError, is_moving, validate_switch
from coreman.core.bots.switch_relay import switch_relay as domain_switch_relay
from coreman.core.bus import tasks
from coreman.core.bus.tasks import ACTIVE, OPEN, NewTask
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    BotAllowedUser,
    BotApprover,
    BotMember,
    RelayServer,
    Task,
    User,
)
from coreman.core.timeutils import utcnow

router = APIRouter(prefix="/api/admin/bots", tags=["bots"], dependencies=[Depends(verify_csrf)])


class SwitchRelayIn(BaseModel):
    relay_server_id: uuid.UUID
    model: str | None = Field(default=None, min_length=1, max_length=100)
    workspace_mode: Literal["copy", "existing"] = "copy"
    target_directory: str | None = Field(default=None, min_length=1, max_length=500)
    allow_stored_memory: bool = False


class MemberIn(BaseModel):
    user_id: uuid.UUID


class AllowedUsersIn(BaseModel):
    """整体替换：空数组 = 不限制。"""

    user_ids: list[uuid.UUID] = Field(max_length=500)


class ApproversIn(BaseModel):
    """整体替换：空数组 = 由发起人自己确认。"""

    user_ids: list[uuid.UUID] = Field(max_length=500)


def _cipher(request: Request) -> Cipher:
    return request.app.state.cipher  # type: ignore[no-any-return]


def _member_out(user: User, added_at: datetime, added_by: uuid.UUID | None) -> dict[str, Any]:
    return {
        "user_id": str(user.id),
        "login_name": user.login_name,
        "display_name": user.display_name,
        "added_at": added_at,
        "added_by": str(added_by) if added_by else None,
    }


def _user_out(user: User) -> dict[str, Any]:
    return {
        "user_id": str(user.id),
        "login_name": user.login_name,
        "display_name": user.display_name,
    }


async def _members_of(session: AsyncSession, bot_id: uuid.UUID) -> list[dict[str, Any]]:
    """协作者列表，按加入时间排序（同一批次插入会并列，补 user_id 兜底）。"""
    rows = (
        await session.execute(
            select(BotMember, User)
            .join(User, User.id == BotMember.user_id)
            .where(BotMember.bot_id == bot_id)
            .order_by(BotMember.added_at, BotMember.user_id)
        )
    ).all()
    return [_member_out(u, m.added_at, m.added_by) for m, u in rows]


async def _allowed_of(session: AsyncSession, bot_id: uuid.UUID) -> list[dict[str, Any]]:
    """白名单用户列表，按显示名排序（重名时补 user_id 兜底）。"""
    rows = (
        (
            await session.execute(
                select(User)
                .join(BotAllowedUser, BotAllowedUser.user_id == User.id)
                .where(BotAllowedUser.bot_id == bot_id)
                .order_by(User.display_name, User.id)
            )
        )
        .scalars()
        .all()
    )
    return [_user_out(u) for u in rows]


async def _approvers_of(session: AsyncSession, bot_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(User)
                .join(BotApprover, BotApprover.user_id == User.id)
                .where(BotApprover.bot_id == bot_id)
                .order_by(User.display_name, User.id)
            )
        )
        .scalars()
        .all()
    )
    return [_user_out(u) for u in rows]


@router.post("/{bot_id}/switch-relay")
async def switch_relay(
    bot_id: uuid.UUID,
    body: SwitchRelayIn,
    request: Request,
    response: Response,
    background: bool = False,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """换机 / 换模型。判定与副作用都在领域服务里，worker 的限流自动切换走的是同一条路。

    `background=true` 且要换实例或目录时不在请求里迁移：判定通过就排一个切换作业，返回 202 与
    task_id，由 worker 执行（不要求员工空闲，等手上这一轮结束），进度与结论用 switch-jobs 查。
    只换模型、或不带 background 的旧调用方，仍当场完成。
    """
    bot = await load_bot(session, bot_id)
    member_ids = await member_ids_of(session, bot.id)
    # 权限判定必须先于 If-Match：无权的人漏带版本号时该看到 403，而不是 428/409 那种
    # 「像是自己版本号写错了」的提示。领域服务里的同一道检查保留，worker 走的是那条路。
    if not can_switch_relay(user, bot, member_ids):
        raise forbidden()
    require_if_match(request, bot.version)
    target = await session.get(RelayServer, body.relay_server_id)
    if background and target is not None and is_moving(bot, target, body.target_directory):
        return await _start_switch_job(
            session, bot, target, body, user, member_ids, request, response
        )
    try:
        result = await domain_switch_relay(
            session,
            bot=bot,
            target=target,
            model=body.model,
            actor=user,
            member_ids=member_ids,
            ip=client_ip(request),
            cipher=request.app.state.cipher,
            workspace_mode=body.workspace_mode,
            target_directory=body.target_directory,
            allow_stored_memory=body.allow_stored_memory,
        )
    except SwitchError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc
    await session.commit()
    await session.refresh(bot)
    set_etag(response, bot.version)
    return {
        "code": 0,
        "data": {
            "old_relay_id": str(result.old_relay_id) if result.old_relay_id else None,
            "new_relay_id": str(result.new_relay_id),
            "memory_status": result.memory_status,
            "old_model": result.old_model,
            "new_model": result.new_model,
            "bot": await build_out(session, _cipher(request), bot, user),
        },
    }


async def _start_switch_job(
    session: AsyncSession,
    bot: Bot,
    target: RelayServer,
    body: SwitchRelayIn,
    user: User,
    member_ids: set[uuid.UUID],
    request: Request,
    response: Response,
) -> dict[str, Any]:
    try:
        await validate_switch(
            session, bot=bot, target=target, model=body.model, actor=user, member_ids=member_ids
        )
    except SwitchError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc
    await session.refresh(bot, with_for_update=True)
    running = await session.scalar(
        select(Task.id)
        .where(Task.bot_id == bot.id, Task.kind == "relay_switch", Task.status.in_(OPEN))
        .limit(1)
    )
    if running is not None:
        raise ApiError(409, 409, "这位 AI 员工已有一个切换在进行，请等它结束或先取消")
    if bot.workspace_state in {"migrating", "initializing", "busy"} and (
        bot.workspace_deadline is None or bot.workspace_deadline > utcnow()
    ):
        raise ApiError(409, 409, "工作目录操作正在进行，请等待完成")
    task = await tasks.enqueue(
        session,
        NewTask(
            bot_id=bot.id,
            kind="relay_switch",
            lane="normal",
            priority=5,
            user_id=user.id,
            payload={
                "origin": "admin",
                "user_id": str(user.id),
                "target_relay_server_id": str(target.id),
                "model": body.model,
                "workspace_mode": body.workspace_mode,
                "target_directory": body.target_directory,
                "allow_stored_memory": body.allow_stored_memory,
                "ip": client_ip(request),
            },
        ),
    )
    if task is None:  # 没有 dedupe_key，不会撞重
        raise ApiError(500, 500, "切换作业创建失败")
    await session.commit()
    response.status_code = 202
    return {"code": 0, "data": {"task_id": task.id}}


async def _switch_job(session: AsyncSession, bot_id: uuid.UUID, task_id: int) -> Task:
    task = await session.get(Task, task_id, populate_existing=True)
    if task is None or task.bot_id != bot_id or task.kind != "relay_switch":
        raise not_found("切换作业不存在")
    return task


@router.get("/{bot_id}/switch-jobs/{task_id}")
async def switch_job(
    bot_id: uuid.UUID,
    task_id: int,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """切换作业的进度与结论：进行中看工作目录的阶段；结束后 result.ok 区分成功与被规则挡下。"""
    bot = await load_bot(session, bot_id)
    if not can_switch_relay(user, bot, await member_ids_of(session, bot.id)):
        raise forbidden()
    task = await _switch_job(session, bot.id, task_id)
    data: dict[str, Any] = {
        "task_id": task.id,
        "status": task.status,
        "phase": bot.workspace_phase,
        "result": task.result,
        # 失败（进程中断等）与取消时给人看的原因；被规则挡下的原因在 result.message 里。
        "error": (task.error_message or task.cancel_reason) if task.status != "succeeded" else None,
    }
    if task.status == "succeeded" and (task.result or {}).get("ok"):
        await session.refresh(bot)
        set_etag(response, bot.version)
        data["bot"] = await build_out(session, _cipher(request), bot, user)
    return {"code": 0, "data": data}


@router.post("/{bot_id}/switch-jobs/{task_id}/cancel")
async def cancel_switch_job(
    bot_id: uuid.UUID,
    task_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """取消切换：还在排队的直接作废；已经在跑的只在等待员工手上这一轮时生效，开始迁移后跑完为止。"""
    bot = await load_bot(session, bot_id)
    if not can_switch_relay(user, bot, await member_ids_of(session, bot.id)):
        raise forbidden()
    task = await _switch_job(session, bot.id, task_id)
    if task.status == "queued":
        await tasks.finish(
            session, task.id, status="cancelled", error_message="已取消", bot_id=task.bot_id
        )
    elif task.status in ACTIVE:
        await tasks.request_cancel(session, task.id, "已取消")
    await session.commit()
    return {"code": 0, "data": {"task_id": task.id}}


@router.post("/{bot_id}/toggle")
async def toggle_bot(
    bot_id: uuid.UUID,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """启停：一次点击就是一次翻转，不需要 If-Match（并发只会多翻一次，不会写坏配置）。"""
    bot = await load_bot(session, bot_id)
    if not can_toggle_bot(user, bot, await member_ids_of(session, bot.id)):
        raise forbidden()
    await session.refresh(bot, with_for_update=True)
    if bot.workspace_state in {"migrating", "initializing", "busy"}:
        raise ApiError(409, 409, "工作目录操作正在进行，请完成后再启停")
    before = bot.enabled
    bot.enabled = not before
    await record_audit(
        session,
        action="bot.toggle",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="bot",
        target_id=str(bot.id),
        diff=diff_dict({"enabled": before}, {"enabled": bot.enabled}),
        ip=client_ip(request),
    )
    await notify_bot_changed(session, bot.id)
    await session.commit()
    await session.refresh(bot)
    set_etag(response, bot.version)
    return {"code": 0, "data": await build_out(session, _cipher(request), bot, user)}


@router.get("/{bot_id}/members")
async def list_members(
    bot_id: uuid.UUID,
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await load_bot(session, bot_id)
    return {"code": 0, "data": await _members_of(session, bot_id)}


@router.post("/{bot_id}/members", status_code=201)
async def add_member(
    bot_id: uuid.UUID,
    body: MemberIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await load_bot(session, bot_id)
    if not can_manage_members(user, bot):
        raise forbidden()
    if is_assistant(bot):
        raise ApiError(422, 422, "个人助理只属于本人，不能添加成员或可用人")
    target = await session.get(User, body.user_id)
    if target is None or target.status != "active":
        raise ApiError(422, 422, "用户不存在或已停用")
    if target.id == bot.created_by:
        raise ApiError(409, 409, "创建者无需加入协作者")
    if target.id in await member_ids_of(session, bot.id):
        raise ApiError(409, 409, "该用户已是协作者")
    row = BotMember(bot_id=bot.id, user_id=target.id, added_by=user.id)
    session.add(row)
    await record_audit(
        session,
        action="bot.member_add",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="bot",
        target_id=str(bot.id),
        diff=diff_dict({}, {"user_id": str(target.id)}),
        ip=client_ip(request),
    )
    await session.commit()
    # added_at 是库端默认值，不刷回来响应里就是 null。
    await session.refresh(row)
    return {"code": 0, "data": _member_out(target, row.added_at, row.added_by)}


@router.delete("/{bot_id}/members/{user_id}")
async def remove_member(
    bot_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await load_bot(session, bot_id)
    if not can_manage_members(user, bot):
        raise forbidden()
    row = await session.get(BotMember, {"bot_id": bot_id, "user_id": user_id})
    if row is None:
        raise not_found("协作者不存在")
    await session.delete(row)
    await record_audit(
        session,
        action="bot.member_remove",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="bot",
        target_id=str(bot.id),
        diff=diff_dict({"user_id": str(user_id)}, {}),
        ip=client_ip(request),
    )
    await session.commit()
    return {"code": 0, "data": None}


@router.get("/{bot_id}/allowed-users")
async def list_allowed_users(
    bot_id: uuid.UUID,
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await load_bot(session, bot_id)
    return {"code": 0, "data": await _allowed_of(session, bot_id)}


@router.put("/{bot_id}/allowed-users")
async def replace_allowed_users(
    bot_id: uuid.UUID,
    body: AllowedUsersIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await load_bot(session, bot_id)
    if not is_bot_admin(user, bot, await member_ids_of(session, bot.id)):
        raise forbidden()
    if is_assistant(bot):
        raise ApiError(422, 422, "个人助理只属于本人，不能添加成员或可用人")
    wanted = list(dict.fromkeys(body.user_ids))  # 前端重复勾选不该变成主键冲突
    if wanted:
        # 与协作者同口径：停用的账号不能进白名单（否则停用只挡登录，机器人照用）。
        found = set(
            (
                await session.execute(
                    select(User.id).where(User.id.in_(wanted), User.status == "active")
                )
            )
            .scalars()
            .all()
        )
        missing = [str(uid) for uid in wanted if uid not in found]
        if missing:
            raise ApiError(422, 422, f"用户不存在或已停用：{', '.join(missing)}")
    old = set(
        (
            await session.execute(
                select(BotAllowedUser.user_id).where(BotAllowedUser.bot_id == bot.id)
            )
        )
        .scalars()
        .all()
    )
    if old != set(wanted):
        await session.execute(delete(BotAllowedUser).where(BotAllowedUser.bot_id == bot.id))
        session.add_all([BotAllowedUser(bot_id=bot.id, user_id=uid) for uid in wanted])
        await record_audit(
            session,
            action="bot.allowed_users",
            actor_id=user.id,
            actor_login=user.login_name,
            target_type="bot",
            target_id=str(bot.id),
            diff={
                "user_ids": [
                    sorted(str(uid) for uid in old),
                    sorted(str(uid) for uid in wanted),
                ]
            },
            ip=client_ip(request),
        )
    await session.commit()
    return {"code": 0, "data": await _allowed_of(session, bot_id)}


@router.get("/{bot_id}/approvers")
async def list_approvers(
    bot_id: uuid.UUID,
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await load_bot(session, bot_id)
    return {"code": 0, "data": await _approvers_of(session, bot_id)}


@router.put("/{bot_id}/approvers")
async def replace_approvers(
    bot_id: uuid.UUID,
    body: ApproversIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await load_bot(session, bot_id)
    if not is_bot_admin(user, bot, await member_ids_of(session, bot.id)):
        raise forbidden()
    wanted = list(dict.fromkeys(body.user_ids))
    if wanted:
        # 与注入时的 approver_logins 同一口径：存下来的人必须真能被注入，
        # 否则审批人名单里有人却永远收不到审批。
        found = set(
            (
                await session.execute(
                    select(User.id).where(
                        User.id.in_(wanted),
                        User.status == "active",
                        User.source != "bootstrap",
                        User.login_name.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        missing = [str(uid) for uid in wanted if uid not in found]
        if missing:
            raise ApiError(422, 422, f"用户不存在、已停用或无登录名：{', '.join(missing)}")
    old = set(
        (await session.execute(select(BotApprover.user_id).where(BotApprover.bot_id == bot.id)))
        .scalars()
        .all()
    )
    if old != set(wanted):
        await session.execute(delete(BotApprover).where(BotApprover.bot_id == bot.id))
        session.add_all([BotApprover(bot_id=bot.id, user_id=uid) for uid in wanted])
        await record_audit(
            session,
            action="bot.approvers",
            actor_id=user.id,
            actor_login=user.login_name,
            target_type="bot",
            target_id=str(bot.id),
            diff={
                "user_ids": [
                    sorted(str(uid) for uid in old),
                    sorted(str(uid) for uid in wanted),
                ]
            },
            ip=client_ip(request),
        )
    await session.commit()
    return {"code": 0, "data": await _approvers_of(session, bot_id)}


class WorkspacePreviewIn(BaseModel):
    relay_server_id: uuid.UUID
    target_directory: str | None = Field(default=None, min_length=1, max_length=500)


@router.post("/{bot_id}/workspace/switch-preview")
async def workspace_switch_preview(
    bot_id: uuid.UUID,
    body: WorkspacePreviewIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    from coreman.core.bots.permissions import relay_allowed_for_bot
    from coreman.core.bots.relay_policy import relay_available, relay_visible
    from coreman.core.bots.workspace_transfer import (
        require_workspace,
        shared_directory,
        target_path,
    )
    from coreman.core.knowledge.memory_transfer import active_counts
    from coreman.core.relay.agent_client import AgentError, call_agent

    bot = await load_bot(session, bot_id)
    if not can_switch_relay(user, bot, await member_ids_of(session, bot_id)):
        raise forbidden()
    target = await session.get(RelayServer, body.relay_server_id)
    creator = await session.get(User, bot.created_by)
    if (
        target is None
        or not relay_available(target)
        or (target.id != bot.relay_server_id and not relay_visible(user, target))
        or (
            target.id != bot.relay_server_id
            and not relay_allowed_for_bot(
                user,
                target,
                bot_team_id=bot.team_id,
                creator_team_id=creator.team_id if creator else None,
            )
        )
    ):
        raise ApiError(422, 422, "目标运行时未注册或不可用")
    directory = await target_path(session, bot, target, body.target_directory)
    try:
        await require_workspace(target, request.app.state.cipher)
        info = await call_agent(
            target,
            request.app.state.cipher,
            "workspace-info",
            {
                "working_dir": directory,
                "bot_id": str(bot.id),
            },
        )
    except AgentError as exc:
        raise ApiError(502, 502, "无法检查目标目录，请检查运行时连接") from exc
    source_online = shared = False
    if bot.relay_server_id:
        source = await session.get(RelayServer, bot.relay_server_id)
        if source:
            try:
                await require_workspace(source, request.app.state.cipher)
                source_online = True
            except (AgentError, ApiError):
                pass
        # 与换机同一条件：复制会覆盖非空目录时才探测；同一节点同一目录本来就不复制。
        if (
            source_online
            and source
            and info.get("exists")
            and not info.get("empty")
            and not (
                source.runtime_node_id == target.runtime_node_id and directory == bot.working_dir
            )
        ):
            # 只用于展示；真正换机时还会再探测一次，探测失败或旧版本那边会拦下。
            with contextlib.suppress(AgentError, ApiError):
                shared = await shared_directory(
                    bot, source, target, directory, request.app.state.cipher
                )
    return {
        "code": 0,
        "data": {
            "directory": directory,
            "exists": bool(info.get("exists")),
            "empty": bool(info.get("empty")),
            "owned": bool(info.get("owned")),
            # 旧运行时不报告，按有标记处理：只能使用本员工自己的目录。
            "marked": info.get("marked", True) is not False,
            "source_online": source_online,
            # 与当前运行时共享同一个目录（共享盘）：换机不复制文件。
            "shared": shared,
            "memory_snapshot_at": bot.memory_snapshot_at,
            # 切换会先等这些进行中的任务结束（按类别计数），排队中的不等。
            "active_tasks": await active_counts(session, bot.id),
            # 目标实例的推理检查结果：登录失效、额度用完时切过去也回不了话，提前看到。
            "target_health": target.health_status,
            "target_health_detail": target.health_detail,
        },
    }
