"""运行时节点上 Agent 操作的请求：经反向通道送达节点，响应有大小上限，不记录请求内容。"""

from __future__ import annotations

import json
from typing import Any

import httpx

from coreman.core.crypto import Cipher
from coreman.core.db.models import RelayServer
from coreman.core.runtime_nodes.transport import ReverseTransport

OPERATIONS = {
    "workspace-info",
    "workspace-init",
    "workspace-list",
    "workspace-read",
    "workspace-write",
    "workspace-export",
    "workspace-import",
    "workspace-finish",
    "workspace-git-status",
    "workspace-git-test",
    "workspace-git-backup",
    "workspace-git-restore",
    "ping",
    "status",
    "probe-rate-limits",
    "health-check",
    "pull",
    "pr",
    "init",
    "install-skill",
    "collect-memory",
    "read-memory",
    "deploy-memory",
}


class AgentError(Exception):
    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def make_http(relay: RelayServer) -> httpx.AsyncClient:
    """按实例所属节点构造反向通道客户端；未绑定节点的实例不可用。"""
    if relay.runtime_node_id is None:
        raise AgentError("实例未绑定运行时节点")
    return httpx.AsyncClient(
        base_url="http://runtime",
        transport=ReverseTransport(relay.runtime_node_id, relay.model_provider),
        trust_env=False,
        follow_redirects=False,
    )


async def call_agent(
    relay: RelayServer,
    cipher: Cipher | None,
    operation: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """向实例所属节点下发一次 Agent 操作。

    `cipher` 仅为调用方兼容保留：反向通道由节点凭证鉴权，不再向节点下发实例令牌。
    """
    del cipher
    if operation not in OPERATIONS or not relay.is_active or relay.runtime_node_id is None:
        raise AgentError("实例未启用、未绑定运行时节点或操作不支持")
    timeout = (
        950
        if operation
        in {"pull", "pr", "init", "install-skill", "workspace-git-backup", "workspace-git-restore"}
        else 130
    )
    try:
        async with make_http(relay) as client:
            async with client.stream(
                "POST",
                "/",
                json={**(payload or {}), "type": operation},
                timeout=timeout,
            ) as response:
                if response.status_code != 200:
                    raise AgentError(f"Agent 请求失败（HTTP {response.status_code}）")
                data = bytearray()
                async for part in response.aiter_bytes():
                    data.extend(part)
                    if len(data) > 4 * 1024 * 1024:
                        raise AgentError("Agent 响应超过上限")
                result = json.loads(data)
                if not isinstance(result, dict) or result.get("success") is not True:
                    raise AgentError(
                        "Agent 未完成操作，请查看该实例状态",
                        code=str(result["code"])
                        if isinstance(result, dict)
                        and result.get("code") in {"conflict", "instructions_conflict"}
                        else None,
                    )
                return result
    except (httpx.HTTPError, ValueError) as exc:
        raise AgentError(f"Agent 连接失败（{type(exc).__name__}）") from exc
