"""已登记 Relay agent 的认证请求；响应有大小上限，不记录请求内容或令牌。"""

from __future__ import annotations

from typing import Any

import httpx

from coreman.core.crypto import Cipher
from coreman.core.db.models import RelayServer
from coreman.core.relay.safe_transport import RegisteredTransport

OPERATIONS = {
    "ping",
    "check-active-tasks",
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
    "mail-probe",
}


class AgentError(Exception):
    pass


def make_http(relay: RelayServer) -> httpx.AsyncClient:
    if relay.runtime_node_id:
        from coreman.core.runtime_nodes.transport import ReverseTransport

        return httpx.AsyncClient(
            base_url=relay.relay_url,
            transport=ReverseTransport(relay.runtime_node_id, relay.model_provider),
            trust_env=False, follow_redirects=False,
        )
    if not relay.agent_port:
        raise AgentError("实例尚未配置 Agent 端口")
    return httpx.AsyncClient(
        base_url=f"http://{relay.host}:{relay.agent_port}",
        transport=RegisteredTransport(relay.host, relay.agent_port),
        trust_env=False,
        follow_redirects=False,
    )


async def call_agent(
    relay: RelayServer, cipher: Cipher, operation: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    if operation not in OPERATIONS or not relay.is_active or not relay.agent_token_enc:
        raise AgentError("实例未启用、缺少令牌或操作不支持")
    token = cipher.decrypt(relay.agent_token_enc, "relay_servers.agent_token_enc")
    timeout = 950 if operation in {"pull", "pr", "init", "install-skill"} else 130
    try:
        async with make_http(relay) as client:
            async with client.stream(
                "POST",
                "/",
                json={**(payload or {}), "type": operation},
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            ) as response:
                if response.status_code != 200:
                    raise AgentError(f"Agent 请求失败（HTTP {response.status_code}）")
                data = bytearray()
                async for part in response.aiter_bytes():
                    data.extend(part)
                    if len(data) > 4 * 1024 * 1024:
                        raise AgentError("Agent 响应超过上限")
                import json

                result = json.loads(data)
                if not isinstance(result, dict) or result.get("success") is not True:
                    raise AgentError("Agent 未完成操作，请查看该实例状态")
                return result
    except (httpx.HTTPError, ValueError) as exc:
        raise AgentError(f"Agent 连接失败（{type(exc).__name__}）") from exc
