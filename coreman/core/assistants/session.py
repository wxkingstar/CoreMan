"""会话类：按主键取行、取单个实体、刷新实体时，自动把助理的内容列还原成明文。

读这几张表的代码绝大多数是直接 `session.get(...)`，不经过 bus 函数；在这里兜住，调用点不用改。
`scalars()` / `execute()` 的结果是惰性的，这里管不到，由调用点显式 `sealed.open_rows`。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed


class SealedSession(AsyncSession):
    async def get(self, entity: Any, ident: Any, **kw: Any) -> Any:
        row = await super().get(entity, ident, **kw)
        await sealed.open_row(self, row)
        return row

    async def scalar(self, statement: Any, params: Any = None, **kw: Any) -> Any:
        value = await super().scalar(statement, params, **kw)
        if type(value) in sealed.COLUMNS:
            await sealed.open_row(self, value)
        return value

    async def refresh(
        self, instance: object, attribute_names: Any = None, with_for_update: Any = None
    ) -> None:
        await super().refresh(instance, attribute_names, with_for_update)
        await sealed.open_row(self, instance)
