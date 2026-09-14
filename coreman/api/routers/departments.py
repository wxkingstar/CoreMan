"""部门树（只读，由通讯录同步维护）。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import current_user, get_session
from coreman.api.security import verify_csrf
from coreman.core.db.models import Department, User, UserDepartment

router = APIRouter(
    prefix="/api/admin/departments", tags=["departments"], dependencies=[Depends(verify_csrf)]
)


@router.get("")
async def department_tree(
    platform: str = "wecom",
    _: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    depts = (
        (await session.execute(select(Department).where(Department.platform == platform)))
        .scalars()
        .all()
    )
    count_rows = await session.execute(
        select(UserDepartment.department_id, func.count()).group_by(UserDepartment.department_id)
    )
    counts: dict[uuid.UUID, int] = {dept_id: n for dept_id, n in count_rows}
    nodes: dict[uuid.UUID, dict[str, Any]] = {
        d.id: {
            "id": str(d.id),
            "platform_dept_id": d.platform_dept_id,
            "name": d.name,
            "path": d.path,
            "sort_order": d.sort_order,
            "member_count": int(counts.get(d.id, 0)),
            "children": [],
        }
        for d in depts
    }
    roots: list[dict[str, Any]] = []
    for d in sorted(depts, key=lambda x: (-x.sort_order, x.name)):
        (nodes[d.parent_id]["children"] if d.parent_id in nodes else roots).append(nodes[d.id])
    return {"code": 0, "data": roots}
