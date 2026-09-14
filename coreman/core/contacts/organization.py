"""组织查询的旧字段兼容视图；停用和引导账号不出现在业务通讯录。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Department, User, UserDepartment, UserIdentity


async def organization_members(session: AsyncSession) -> list[dict[str, Any]]:
    users = list(
        (
            await session.execute(
                select(User)
                .where(User.status == "active", User.source != "bootstrap")
                .order_by(User.login_name, User.id)
            )
        ).scalars()
    )
    depts = (
        await session.execute(
            select(UserDepartment.user_id, Department.path)
            .join(Department, Department.id == UserDepartment.department_id)
            .order_by(UserDepartment.is_primary.desc(), Department.path)
        )
    ).all()
    paths: dict[str, list[str]] = {}
    for uid, path in depts:
        paths.setdefault(str(uid), []).append(path)
    ids = (
        await session.execute(select(UserIdentity).order_by(UserIdentity.platform_user_id))
    ).scalars()
    identities = {(str(i.user_id), i.platform): i.platform_user_id for i in ids}
    return [
        {
            "id": str(u.id),
            "username": u.login_name,
            "real_name": u.display_name,
            "department": next(iter(paths.get(str(u.id), [])), ""),
            "departments": paths.get(str(u.id), []),
            "position": u.position,
            "skills": u.skills,
            "bot_accessible": u.bot_accessible,
            "wework_user_id": identities.get((str(u.id), "wecom")),
            "feishu_user_id": identities.get((str(u.id), "feishu")),
            "language": u.locale,
        }
        for u in users
    ]


def organization_tree(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    root: dict[str, Any] = {}
    for member in members:
        parts = [p for p in str(member["department"]).split("/") if p] or ["未分配部门"]
        tree = root
        for part in parts:
            node = tree.setdefault(part, {"children": {}, "members": [], "member_count": 0})
            node["member_count"] += 1
            tree = node["children"]
        node["members"].append(member)

    def render(nodes: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "children": render(n["children"]),
                "members": n["members"],
                "member_count": n["member_count"],
            }
            for name, n in sorted(nodes.items())
        ]

    return render(root)
