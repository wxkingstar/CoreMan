"""测试用运行时节点：运行时实例都必须属于某个已注册节点。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import RelayServer, RuntimeNode


async def add_node(
    session: AsyncSession,
    *,
    name: str = "test-node",
    workspace_root: str = "/data/skills",
    **values: Any,
) -> RuntimeNode:
    values.setdefault(
        "capabilities", {p: {"installed": True, "login": "ready"} for p in ("claude", "codex")}
    )
    node = RuntimeNode(
        id=uuid.uuid4(),
        name=name,
        token_hash="0" * 64,
        workspace_root=workspace_root,
        hostname="test-host",
        username="ai",
        platform="linux",
        architecture="amd64",
        environment="host",
        version="test",
        **values,
    )
    session.add(node)
    await session.flush()
    return node


async def add_node_relay(
    session: AsyncSession,
    *,
    name: str = "test-node",
    provider: str = "claude",
    workspace_root: str = "/data/skills",
    node: RuntimeNode | None = None,
    **values: Any,
) -> RelayServer:
    """建一个节点（或复用给定节点）并挂上该 AI 类型的实例；展示名是「节点名 / 类型」。"""
    node = node or await add_node(session, name=name, workspace_root=workspace_root)
    relay = RelayServer(
        runtime_node_id=node.id,
        name=f"{node.id}/{provider}",
        model_provider=provider,
        **values,
    )
    session.add(relay)
    await session.flush()
    return relay


async def attach_node(
    session: AsyncSession, relay: RelayServer, *, workspace_root: str = "/data/skills"
) -> RuntimeNode:
    """把已有实例挂到一个新节点上（给 seed_bot 之类的公共夹具补节点用）。"""
    node = await add_node(session, name=relay.name, workspace_root=workspace_root)
    relay.runtime_node_id = node.id
    await session.flush()
    return node
