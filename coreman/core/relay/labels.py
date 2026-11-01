"""运行时实例给人看的名字，以及「能不能拿来选」的判定。

实例行的 `name` 是注册时生成的 `{节点 id}/{provider}`，唯一但没人看得懂；凡是和人交互的地方
（管理台、IM 回复与卡片）一律用「节点名 / provider」，与运行时管理页上的节点名对得上。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.relay_policy import backend_unavailable_reason
from coreman.core.db.models import RelayServer, RuntimeNode

# 节点明确报告没装或没登录的 AI：这种实例不出现在可选列表里（同一台机器可能只登了一种 AI）。
UNSELECTABLE_REASONS = ("not_installed", "login_required")


def relay_label(relay: RelayServer, node: RuntimeNode | None) -> str:
    return f"{node.name} / {relay.model_provider}" if node else relay.name


def signed_in(relay: RelayServer, node: RuntimeNode | None) -> bool:
    return backend_unavailable_reason(relay, node) not in UNSELECTABLE_REASONS


async def load_nodes(
    session: AsyncSession, relays: Iterable[RelayServer]
) -> dict[uuid.UUID | None, RuntimeNode]:
    """按实例的 runtime_node_id 取节点；键允许 None，调用方直接 .get(relay.runtime_node_id)。"""
    ids = {r.runtime_node_id for r in relays if r.runtime_node_id}
    if not ids:
        return {}
    rows = await session.execute(select(RuntimeNode).where(RuntimeNode.id.in_(ids)))
    return {n.id: n for n in rows.scalars()}
