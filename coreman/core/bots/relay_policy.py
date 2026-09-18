"""Relay 可见性与模型校验。API 路由与 worker 共用。

原先住在 `coreman/api/routers/relay_servers.py` / `bots.py` 里，被限流自动切换
拉了进来：worker 不该为了两个判定去 import FastAPI 路由模块。原位置改成再导出，既有导入
路径与测试一字不改。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import RelayServer, RuntimeNode, User
from coreman.core.errors import ApiError
from coreman.core.relay.models import effective_models, load_catalog, supports_xhigh

MANAGER_ROLES = ("ai_committee", "platform_admin")


def relay_visible(user: User, relay: RelayServer) -> bool:
    """visibility='admins' 的实例只对 managers 可见（bots 路由复用）。"""
    return relay.visibility == "all" or user.role in MANAGER_ROLES


def relay_available(relay: RelayServer) -> bool:
    """实例能承接机器人：已启用且属于某个运行时节点（独立中继实例已不再支持）。"""
    return bool(relay.is_active) and relay.runtime_node_id is not None


def backend_unavailable_reason(relay: RelayServer, node: RuntimeNode | None) -> str | None:
    """Only admission is gated; existing bots continue to run unchanged."""
    if not relay_available(relay) or node is None or not node.is_active:
        return "disabled"
    if (node.root_change or {}).get("status") == "pending":
        return "root_pending"
    cap = (node.capabilities or {}).get(relay.model_provider, {})
    if cap.get("installed") is False:
        return "not_installed"
    if cap.get("installed") is not True:
        return "unknown"
    if cap.get("login") == "required":
        return "login_required"
    if cap.get("login") != "ready":
        return "unknown"
    return None


async def validate_backend_ready(session: AsyncSession, relay: RelayServer) -> None:
    node = await session.get(RuntimeNode, relay.runtime_node_id) if relay.runtime_node_id else None
    reason = backend_unavailable_reason(relay, node)
    if reason:
        message = {
            "disabled": "目标运行时未启用",
            "root_pending": "项目主目录修改等待 Runtime 确认",
            "not_installed": "AI 未安装",
            "login_required": "AI 未登录",
            "unknown": "AI 状态未知，请升级 Runtime 并确认登录状态",
        }[reason]
        raise ApiError(422, 422, message)


async def validate_model_for_relay(
    session: AsyncSession, relay: RelayServer | None, model: str, effort: str | None
) -> None:
    """模型必须落在目标 relay 的有效集内；未绑 relay 时只要求在目录里且未退役。"""
    catalog = await load_catalog(session)
    allowed = (
        effective_models(relay, catalog) if relay else [r.model for r in catalog if not r.retired]
    )
    if model not in allowed:
        raise ApiError(422, 422, "模型不在目标运行时的有效模型集内" if relay else "模型不在目录中")
    if effort == "xhigh" and not supports_xhigh(model, catalog):
        raise ApiError(422, 422, "该模型不支持 xhigh")
