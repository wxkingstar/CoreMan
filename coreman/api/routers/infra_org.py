"""带 scope 保护的组织 API 与旧路径别名。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import get_session
from coreman.api.infra_auth import require_scope
from coreman.core.contacts.organization import organization_members, organization_tree

router = APIRouter(tags=["infra-org"], dependencies=[Depends(require_scope("org"))])


@router.get("/api/infra/org/members")
@router.get("/api/robot/organization/members", include_in_schema=False)
async def members(
    keyword: str = Query(default="", max_length=200),
    department: str = Query(default="", max_length=500),
    language: str = Query(default="", max_length=10),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rows = await organization_members(session)
    selected = [
        r
        for r in rows
        if (
            not keyword
            or keyword.casefold()
            in " ".join(str(r[k] or "") for k in ("username", "real_name", "skills")).casefold()
        )
        and (not department or any(department in p for p in r["departments"]))
        and (not language or r["language"] == language)
    ]
    return {"code": 0, "data": {"total": len(selected), "list": selected}}


@router.get("/api/infra/org/tree")
async def tree(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return {"code": 0, "data": organization_tree(await organization_members(session))}


@router.get("/api/infra/org/full")
async def full(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = await organization_members(session)
    return {
        "code": 0,
        "data": {
            "total": len(rows),
            "members": rows,
            "tree": organization_tree(rows),
            "departments": sorted({p for r in rows for p in r["departments"]}),
        },
    }
