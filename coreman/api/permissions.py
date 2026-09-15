"""角色矩阵中的通用判定；资源级规则写在各路由模块。"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends

from coreman.api.deps import current_user
from coreman.api.errors import forbidden
from coreman.core.db.models import User

ROLE_ORDER = {"member": 0, "team_lead": 1, "ai_committee": 2, "platform_admin": 3}
MANUAL_TRACKED = ("team_id", "locale", "bot_accessible", "position", "skills", "status")


def role_at_least(user: User, role: str) -> bool:
    return ROLE_ORDER[user.role] >= ROLE_ORDER[role]


def require_roles(*roles: str) -> Callable[..., Coroutine[Any, Any, User]]:
    allowed = set(roles)

    async def _dep(user: User = Depends(current_user)) -> User:
        if user.role not in allowed:
            raise forbidden()
        return user

    return _dep


def can_edit_user(actor: User, target: User, changes: dict[str, Any]) -> bool:
    """能否改派用户的团队、角色与状态。"""
    if target.source == "bootstrap":
        return False
    if actor.id == target.id and ("role" in changes or "status" in changes):
        return False
    new_role = changes.get("role")
    if actor.role == "platform_admin":
        return True
    if actor.role == "ai_committee":
        return target.role != "platform_admin" and new_role != "platform_admin"
    if actor.role == "team_lead":
        # 停用/启用改的是能不能登录，只有 ai_committee 与 platform_admin 能做
        if "status" in changes:
            return False
        if "team_id" in changes and changes["team_id"] not in (actor.team_id, None):
            return False
        same_team = actor.team_id is not None and target.team_id == actor.team_id
        return (
            same_team
            and target.role in ("member", "team_lead")
            and new_role in (None, "member", "team_lead")
        )
    return False
