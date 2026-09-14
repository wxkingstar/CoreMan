"""机器人相关的角色矩阵（spec §10.2）——纯函数，路由只做编排。"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from coreman.core.db.models import Bot, RelayServer, User

MANAGER_ROLES = ("ai_committee", "platform_admin")
BotRole = Literal["creator", "admin"]


def bot_role(user: User, bot: Bot, member_ids: Collection[uuid.UUID]) -> BotRole | None:
    """创建者 → creator，bot_members 里的管理员 → admin，其余无角色。"""
    if bot.created_by == user.id:
        return "creator"
    return "admin" if user.id in member_ids else None


def is_bot_admin(user: User, bot: Bot, member_ids: Collection[uuid.UUID]) -> bool:
    """creator 或 admin。"""
    return bot_role(user, bot, member_ids) is not None


def same_team(user: User, bot: Bot) -> bool:
    """用户有团队，且与 bot 同团队（两边都为空不算同团队）。"""
    return user.team_id is not None and bot.team_id == user.team_id


def is_manager(user: User) -> bool:
    """ai_committee / platform_admin。"""
    return user.role in MANAGER_ROLES


def can_create_bot(user: User) -> bool:
    """member 必须先有团队才能建机器人，其余角色不限。"""
    return user.role != "member" or user.team_id is not None


def can_view_sensitive(user: User, bot: Bot, member_ids: Collection[uuid.UUID]) -> bool:
    """敏感字段（凭证、env 等）：bot 管理员，或本团队 team_lead；manager 不旁路。"""
    return is_bot_admin(user, bot, member_ids) or (
        user.role == "team_lead" and same_team(user, bot)
    )


def can_view_env_full(user: User) -> bool:
    """env_vars_full 只给 ai_committee。"""
    return user.role == "ai_committee"


def can_edit_bot(user: User, bot: Bot, member_ids: Collection[uuid.UUID]) -> bool:
    """编辑机器人配置：只有 bot 管理员。"""
    return is_bot_admin(user, bot, member_ids)


def can_switch_relay(user: User, bot: Bot, member_ids: Collection[uuid.UUID]) -> bool:
    """切换 relay：bot 管理员或 manager。"""
    return is_bot_admin(user, bot, member_ids) or is_manager(user)


def can_toggle_bot(user: User, bot: Bot, member_ids: Collection[uuid.UUID]) -> bool:
    """启停机器人：bot 管理员、本团队 team_lead 或 manager。"""
    return (
        is_bot_admin(user, bot, member_ids)
        or (user.role == "team_lead" and same_team(user, bot))
        or is_manager(user)
    )


def can_delete_bot(user: User, bot: Bot) -> bool:
    """删除机器人：创建者或 platform_admin。"""
    return bot.created_by == user.id or user.role == "platform_admin"


def can_manage_members(user: User, bot: Bot) -> bool:
    """增删机器人管理员：同删除权限。"""
    return can_delete_bot(user, bot)


def can_reassign_team(user: User) -> bool:
    """改派机器人所属团队：manager。"""
    return is_manager(user)


def relay_allowed_for_bot(
    user: User,
    relay: RelayServer,
    *,
    bot_team_id: uuid.UUID | None,
    creator_team_id: uuid.UUID | None,
) -> bool:
    """relay 团队策略：公共池，或属于 bot / 创建者的团队；manager 不受限。"""
    if relay.team_id is None or is_manager(user):
        return True
    return relay.team_id in {t for t in (bot_team_id, creator_team_id) if t is not None}


@dataclass(frozen=True)
class BotPermissions:
    """前端按钮显隐用的一次性汇总。"""

    role: BotRole | None
    can_view_sensitive: bool
    can_view_env_full: bool
    can_edit: bool
    can_switch_relay: bool
    can_toggle: bool
    can_delete: bool
    can_manage_members: bool
    can_reassign_team: bool


def permissions_for(user: User, bot: Bot, member_ids: Collection[uuid.UUID]) -> BotPermissions:
    """把上面各条判定打包成一个对象，供详情接口序列化。"""
    return BotPermissions(
        role=bot_role(user, bot, member_ids),
        can_view_sensitive=can_view_sensitive(user, bot, member_ids),
        can_view_env_full=can_view_env_full(user),
        can_edit=can_edit_bot(user, bot, member_ids),
        can_switch_relay=can_switch_relay(user, bot, member_ids),
        can_toggle=can_toggle_bot(user, bot, member_ids),
        can_delete=can_delete_bot(user, bot),
        can_manage_members=can_manage_members(user, bot),
        can_reassign_team=can_reassign_team(user),
    )
