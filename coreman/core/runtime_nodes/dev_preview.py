"""开发环境预览：员工所在节点是否开着（节点心跳在各后端的能力里声明 dev_preview_v1）。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import RelayServer, RuntimeNode

RUNTIME_CAPABILITY = "dev_preview_v1"


async def supported(session: AsyncSession, relay: RelayServer | None, backend: str) -> bool:
    if relay is None or relay.runtime_node_id is None:
        return False
    node = await session.get(RuntimeNode, relay.runtime_node_id)
    capability = ((node.capabilities if node else None) or {}).get(backend) or {}
    return isinstance(capability, dict) and capability.get(RUNTIME_CAPABILITY) is True
