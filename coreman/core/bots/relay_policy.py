"""Relay 可见性与模型校验（spec §5.3、§10.2）。API 路由与 worker 共用。

原先住在 `coreman/api/routers/relay_servers.py` / `bots.py` 里，被限流自动切换（spec §8.8）
拉了进来：worker 不该为了两个判定去 import FastAPI 路由模块。原位置改成再导出，既有导入
路径与测试一字不改。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import RelayServer, User
from coreman.core.errors import ApiError
from coreman.core.relay.models import effective_models, load_catalog, supports_xhigh

MANAGER_ROLES = ("ai_committee", "platform_admin")


def relay_visible(user: User, relay: RelayServer) -> bool:
    """visibility='admins' 的实例只对 managers 可见（bots 路由复用）。"""
    return relay.visibility == "all" or user.role in MANAGER_ROLES


async def validate_model_for_relay(
    session: AsyncSession, relay: RelayServer | None, model: str, effort: str | None
) -> None:
    """模型必须落在目标 relay 的有效集内；未绑 relay 时只要求在目录里且未退役。"""
    catalog = await load_catalog(session)
    allowed = (
        effective_models(relay, catalog) if relay else [r.model for r in catalog if not r.retired]
    )
    if model not in allowed:
        raise ApiError(422, 422, "模型不在目标 relay 的有效模型集内" if relay else "模型不在目录中")
    if effort == "xhigh" and not supports_xhigh(model, catalog):
        raise ApiError(422, 422, "该模型不支持 xhigh")
