"""模型目录的读取、有效模型集与默认模型（spec §5.3 末段；本计划裁决 2、3）。

`load_catalog` 原先住在 `coreman/api/routers/model_catalog.py`，限流自动切换（spec §8.8）
把它拉了进来：worker 不该为了读一张目录表去 import FastAPI 路由模块。原位置再导出。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import ModelCatalog, RelayServer


def backend_of(model: str, provider: str | None = None) -> Literal["claude", "codex"]:
    """按模型名前缀判断后端：codex/ 开头走 codex，其余走 claude。"""
    if provider in {"claude", "codex"}:
        return "codex" if provider == "codex" else "claude"
    return "codex" if model.startswith("codex/") else "claude"


def catalog_sorted(rows: Sequence[ModelCatalog]) -> list[ModelCatalog]:
    """目录展示顺序：sort_order 降序、model 升序。"""
    return sorted(rows, key=lambda r: (-r.sort_order, r.model))


def effective_models(relay: RelayServer, catalog: Sequence[ModelCatalog]) -> list[str]:
    """relay 的有效模型集：inherit 取 provider 下未退役的全部，restricted 取白名单与目录的交集。"""
    mine = [r for r in catalog_sorted(catalog) if r.provider == relay.model_provider]
    if relay.supported_models_mode == "restricted":
        known = {r.model for r in mine}  # 含已退役
        return [m for m in (relay.supported_models or []) if m in known]
    return [r.model for r in mine if not r.retired]


def default_model(provider: str, catalog: Sequence[ModelCatalog]) -> str | None:
    """provider 的默认模型：优先未退役且 is_default，否则排序后的第一个。"""
    mine = [r for r in catalog_sorted(catalog) if r.provider == provider and not r.retired]
    for r in mine:
        if r.is_default:
            return r.model
    return mine[0].model if mine else None


def supports_xhigh(model: str, catalog: Sequence[ModelCatalog]) -> bool:
    """该模型是否支持 xhigh 思考档位。"""
    return any(r.model == model and r.supports_xhigh for r in catalog)


async def load_catalog(session: AsyncSession, provider: str | None = None) -> list[ModelCatalog]:
    """目录行（可按 provider 过滤），按 sort_order 降序、model 升序。relay/bots 路由复用。"""
    stmt = select(ModelCatalog)
    if provider:
        stmt = stmt.where(ModelCatalog.provider == provider)
    return catalog_sorted((await session.execute(stmt)).scalars().all())
