"""列表分页（默认每页 50 条）。"""

from __future__ import annotations

from typing import Any, TypedDict

from fastapi import Query
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession


class PageParams:
    def __init__(
        self,
        page: int = Query(1, ge=1),
        per_page: int = Query(50, ge=1, le=200),
    ) -> None:
        self.page, self.per_page = page, per_page

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page


class Page(TypedDict):
    items: list[Any]
    total: int
    page: int
    per_page: int


async def paginate(session: AsyncSession, stmt: Select[Any], params: PageParams) -> Page:
    total = (
        await session.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))
    ).scalar_one()
    rows = (
        (await session.execute(stmt.offset(params.offset).limit(params.per_page))).scalars().all()
    )
    return {"items": list(rows), "total": total, "page": params.page, "per_page": params.per_page}
