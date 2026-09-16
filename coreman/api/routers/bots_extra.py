"""机器人子资源：切换 relay、启停、协作者、白名单。

与 bots.py 的 CRUD 共用 `/api/admin/bots` 前缀，拆成第二个路由器只是为了让 bots.py 不再变长；
校验与视图函数（load_bot / member_ids_of / build_out）都从那边复用，换机的判定与副作用则在
`coreman.core.bots.switch_relay` 里——worker 的限流自动切换走的是同一条路。
"""

from __future__ import annotations

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
from coreman.core.audit import diff_dict, record_audit
from coreman.core.bots.switch_relay import SwitchError
from coreman.core.bots.switch_relay import switch_relay as domain_switch_relay
from coreman.core.crypto import Cipher
from coreman.core.db.models import BotAllowedUser, BotMember, RelayServer, User

router = APIRouter(prefix="/api/admin/bots", tags=["bots"], dependencies=[Depends(verify_csrf)])


class SwitchRelayIn(BaseModel):
    relay_server_id: uuid.UUID
    model: str | None = Field(default=None, min_length=1, max_length=100)
    workspace_mode: Literal["copy", "git", "existing"] = "copy"
    target_directory: str | None = Field(default=None, min_length=1, max_length=500)
    allow_stored_memory: bool = False


class MemberIn(BaseModel):
    user_id: uuid.UUID


class AllowedUsersIn(BaseModel):
    """整体替换：空数组 = 不限制。"""

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


@router.post("/{bot_id}/switch-relay")
async def switch_relay(
    bot_id: uuid.UUID,
    body: SwitchRelayIn,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """换机 / 换模型。判定与副作用都在领域服务里，worker 的限流自动切换走的是同一条路。"""
    bot = await load_bot(session, bot_id)
    member_ids = await member_ids_of(session, bot.id)
    # 权限判定必须先于 If-Match：无权的人漏带版本号时该看到 403，而不是 428/409 那种
    # 「像是自己版本号写错了」的提示。领域服务里的同一道检查保留，worker 走的是那条路。
    if not can_switch_relay(user, bot, member_ids):
        raise forbidden()
    require_if_match(request, bot.version)
    target = await session.get(RelayServer, body.relay_server_id)
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
    from coreman.core.bots.workspace_transfer import require_workspace, target_path
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
    source_online = False
    if bot.relay_server_id:
        source = await session.get(RelayServer, bot.relay_server_id)
        if source:
            try:
                await require_workspace(source, request.app.state.cipher)
                source_online = True
            except (AgentError, ApiError):
                pass
    return {
        "code": 0,
        "data": {
            "directory": directory,
            "exists": bool(info.get("exists")),
            "empty": bool(info.get("empty")),
            "owned": bool(info.get("owned")),
            "source_online": source_online,
            "git_configured": bool(bot.git_url),
            "memory_snapshot_at": bot.memory_snapshot_at,
        },
    }
