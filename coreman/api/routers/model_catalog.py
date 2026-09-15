"""模型目录（model_catalog 表，替代代码常量 MODEL_PROVIDERS）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.core.audit import diff_dict, record_audit
from coreman.core.db.models import Bot, ModelCatalog, RelayServer, User
from coreman.core.relay.models import backend_of, default_model
from coreman.core.relay.models import load_catalog as load_catalog  # 再导出

router = APIRouter(
    prefix="/api/admin/model-catalog",
    tags=["model-catalog"],
    dependencies=[Depends(verify_csrf)],
)
# B008：同 platform_apps，require_roles(...) 不能写进参数默认值里，挪成模块级单例。
_MANAGERS = require_roles("ai_committee", "platform_admin")


class CatalogIn(BaseModel):
    provider: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9_-]+$")
    model: str = Field(min_length=1, max_length=100)
    display_name: str | None = Field(default=None, max_length=100)
    is_default: bool = False
    retired: bool = False
    supports_xhigh: bool = False
    sort_order: int = 0


class CatalogPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    is_default: bool | None = None
    retired: bool | None = None
    supports_xhigh: bool | None = None
    sort_order: int | None = None

    def changes(self) -> dict[str, Any]:
        """只取请求体里真正出现过的字段：None 是合法取值（display_name 可以清空），
        不能用「值为 None」来判断「没传」。"""
        return self.model_dump(include=self.model_fields_set)


def catalog_out(row: ModelCatalog) -> dict[str, Any]:
    return {
        "provider": row.provider,
        "model": row.model,
        "display_name": row.display_name,
        "is_default": row.is_default,
        "retired": row.retired,
        "supports_xhigh": row.supports_xhigh,
        "sort_order": row.sort_order,
        "backend": backend_of(row.model),
    }


async def _current_default(session: AsyncSession, provider: str) -> str | None:
    """该 provider 当前对外生效的默认模型（与 default_model() 同一套判定）。"""
    return default_model(provider, await load_catalog(session, provider))


def _note_default_change(diff: dict[str, list[Any]], old: str | None, new: str | None) -> None:
    """默认模型易主时补进审计 diff：is_default 的连带改动不在 changes 里，不记就看不见。"""
    if old != new:
        diff["default_model"] = [old, new]


def _reject_retired_default(retired: bool, is_default: bool) -> None:
    """退役行不能同时是默认：default_model() 会跳过退役行，落库就成了自相矛盾的状态。"""
    if retired and is_default:
        raise ApiError(422, 422, "已退役的模型不能设为默认")


def _post_patch_default(row: ModelCatalog, changes: dict[str, Any]) -> bool:
    """patch 落库后这一行还是不是默认：显式给了就按给的；否则只有「当前默认行被退役」
    这一种会变——那时 _reassign_default 把默认权交给别人。"""
    if "is_default" in changes:
        return bool(changes["is_default"])
    return row.is_default and not changes.get("retired")


async def _set_default(session: AsyncSession, row: ModelCatalog) -> None:
    """同一 provider 下至多一个默认：把 row 置为默认，其余全部清掉。"""
    for other in await load_catalog(session, row.provider):
        other.is_default = other is row
    row.is_default = True


async def _reassign_default(session: AsyncSession, provider: str, exclude: str) -> None:
    """默认模型退役/删除后改派：给下一个未退役、排序最靠前的行（都退役了就没有默认）。

    exclude 那行（正在退役的那个）也会被清掉 is_default——它不能既退役又是默认。
    """
    rows = await load_catalog(session, provider)
    candidates = [r for r in rows if r.model != exclude and not r.retired]
    winner = candidates[0] if candidates else None
    for r in rows:
        r.is_default = r is winner


@router.get("")
async def list_catalog(
    provider: str | None = None,
    _: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rows = await load_catalog(session, provider)
    rows.sort(key=lambda r: (r.provider, -r.sort_order, r.model))
    return {"code": 0, "data": [catalog_out(r) for r in rows]}


@router.post("", status_code=201)
async def create_entry(
    body: CatalogIn,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _reject_retired_default(body.retired, body.is_default)
    if await session.get(ModelCatalog, {"provider": body.provider, "model": body.model}):
        raise ApiError(409, 409, "模型已存在")
    old_default = await _current_default(session, body.provider)
    row = ModelCatalog(**body.model_dump())
    session.add(row)
    await session.flush()
    if body.is_default:
        await _set_default(session, row)
    diff = diff_dict({}, body.model_dump())
    _note_default_change(diff, old_default, await _current_default(session, body.provider))
    await record_audit(
        session,
        action="catalog.create",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="model_catalog",
        target_id=f"{row.provider}/{row.model}",
        diff=diff,
        ip=client_ip(request),
    )
    await session.commit()
    await session.refresh(row)
    return {"code": 0, "data": catalog_out(row)}


@router.patch("/{provider}/{model:path}")
async def patch_entry(
    provider: str,
    model: str,
    body: CatalogPatch,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await session.get(ModelCatalog, {"provider": provider, "model": model})
    if row is None:
        raise not_found("模型不存在")
    changes = body.changes()
    if not changes:
        raise ApiError(422, 422, "至少修改一个字段")
    # 用 patch 落库后的取值判矛盾组合（本次没带的字段沿用现值），且赶在任何写入之前。
    _reject_retired_default(
        bool(changes.get("retired", row.retired)), _post_patch_default(row, changes)
    )
    old_default = await _current_default(session, provider)
    before = {k: getattr(row, k) for k in changes}
    for key, value in changes.items():
        setattr(row, key, value)
    if changes.get("is_default"):
        await _set_default(session, row)
    if changes.get("retired") and row.is_default:
        await _reassign_default(session, provider, exclude=model)
    diff = diff_dict(before, {k: getattr(row, k) for k in changes})
    _note_default_change(diff, old_default, await _current_default(session, provider))
    await record_audit(
        session,
        action="catalog.update",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="model_catalog",
        target_id=f"{provider}/{model}",
        diff=diff,
        ip=client_ip(request),
    )
    await session.commit()
    await session.refresh(row)
    return {"code": 0, "data": catalog_out(row)}


@router.delete("/{provider}/{model:path}")
async def delete_entry(
    provider: str,
    model: str,
    request: Request,
    actor: User = Depends(_MANAGERS),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    row = await session.get(ModelCatalog, {"provider": provider, "model": model})
    if row is None:
        raise not_found("模型不存在")
    # 「在用」要连 provider 一起看：同名 model 可以挂在多个 provider 下（种子里
    # minimax/MiniMax-M2.7 就同时属于 claude 和 minimax）。bots 没有 provider 列，
    # 由 relay 的 model_provider 决定；未绑 relay 的 bot 无从判断，保守视为在用。
    in_use = (
        select(Bot.id)
        .outerjoin(RelayServer, RelayServer.id == Bot.relay_server_id)
        .where(
            Bot.model == model,
            or_(Bot.relay_server_id.is_(None), RelayServer.model_provider == provider),
        )
        .limit(1)
    )
    if (await session.execute(in_use)).first():
        raise ApiError(409, 409, "仍有机器人使用该模型")
    old_default = await _current_default(session, provider)
    snapshot, was_default = catalog_out(row), row.is_default
    await session.delete(row)
    await session.flush()
    if was_default:
        await _reassign_default(session, provider, exclude=model)
    diff = diff_dict(snapshot, {})
    _note_default_change(diff, old_default, await _current_default(session, provider))
    await record_audit(
        session,
        action="catalog.delete",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="model_catalog",
        target_id=f"{provider}/{model}",
        diff=diff,
        ip=client_ip(request),
    )
    await session.commit()
    return {"code": 0, "data": None}
