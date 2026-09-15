"""平台设置（settings 表）：platform_admin 读写全量，其余角色只读三个默认值键。

`default_model` 是派生键：每次读取都从模型目录算（claude → codex 的默认模型），
写入会被 `SettingsPatch` 以 422 拒绝；settings 表里遗留的同名行不再被读取。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, current_user, get_session, get_store
from coreman.api.errors import ApiError
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.core.audit import diff_dict, record_audit
from coreman.core.bus.notify import notify
from coreman.core.db.models import PlatformApp, User
from coreman.core.relay.models import load_catalog
from coreman.core.settings_schema import (
    DERIVED_SETTING_KEYS,
    PUBLIC_DEFAULT_KEYS,
    SETTING_DEFAULTS,
    SettingsPatch,
    platform_default_model,
)
from coreman.core.settings_store import SettingsStore

router = APIRouter(
    prefix="/api/admin/settings", tags=["settings"], dependencies=[Depends(verify_csrf)]
)
# B008：同 platform_apps，require_roles(...) 不能写进参数默认值里，挪成模块级单例。
_ADMINS = require_roles("platform_admin")


async def _merged(store: SettingsStore, keys: tuple[str, ...] | None = None) -> dict[str, Any]:
    """库里没有的键回落到 SETTING_DEFAULTS：前端永远拿到完整的一套值。只读存储键。"""
    names = keys if keys is not None else tuple(SETTING_DEFAULTS)
    return {k: await store.get(k, default=SETTING_DEFAULTS[k]) for k in names}


async def _derived(session: AsyncSession) -> dict[str, Any]:
    """派生键的当前值：不走 SettingsStore 缓存，模型目录一改立即反映。"""
    return {"default_model": platform_default_model(await load_catalog(session))}


async def _view(
    session: AsyncSession, store: SettingsStore, keys: tuple[str, ...] | None = None
) -> dict[str, Any]:
    """存储键与派生键合并后的设置视图；keys 为 None 时是全量。"""
    wanted = keys if keys is not None else (*DERIVED_SETTING_KEYS, *SETTING_DEFAULTS)
    stored = tuple(k for k in wanted if k not in DERIVED_SETTING_KEYS)
    values = {**await _derived(session), **await _merged(store, stored)}
    return {k: values[k] for k in wanted}


def _check_not_null(changes: dict[str, Any]) -> None:
    """只有 default_effort_level 的缺省值本来就是 null，别的键置空会被读取方当成「有值且为
    None」：bootstrap_admin_enabled=null 会绕过下面的登录守卫把所有人挡在门外。"""
    blank = [k for k, v in changes.items() if v is None and SETTING_DEFAULTS[k] is not None]
    if blank:
        raise ApiError(422, 422, "设置项不能置空：" + "、".join(blank))


async def _check_login_app(session: AsyncSession, changes: dict[str, Any]) -> None:
    """关引导登录前必须已有别的登录入口，否则谁都进不来。"""
    if changes.get("bootstrap_admin_enabled") is not False:
        return
    stmt = (
        select(PlatformApp.id)
        .where(PlatformApp.enabled.is_(True), PlatformApp.capabilities.contains(["login"]))
        .limit(1)
    )
    if (await session.execute(stmt)).first() is None:
        raise ApiError(422, 422, "请先配置并启用一个具备登录能力的平台应用，再关闭引导登录")


@router.get("")
async def get_settings_all(
    _: User = Depends(_ADMINS),
    session: AsyncSession = Depends(get_session),
    store: SettingsStore = Depends(get_store),
) -> dict[str, Any]:
    return {"code": 0, "data": await _view(session, store)}


@router.put("")
async def put_settings(
    body: SettingsPatch,
    request: Request,
    actor: User = Depends(_ADMINS),
    session: AsyncSession = Depends(get_session),
    store: SettingsStore = Depends(get_store),
) -> dict[str, Any]:
    changes = body.changes()
    if not changes:
        raise ApiError(422, 422, "至少修改一个设置")
    _check_not_null(changes)
    await _check_login_app(session, changes)
    before = await _merged(store, tuple(changes))
    for key, value in changes.items():
        await store.set(key, value, updated_by=actor.id)
    diff = diff_dict(before, changes)
    # 原值重写不算改动：不记审计，避免「谁都没改什么」的噪声条目。
    if diff:
        await record_audit(
            session,
            action="settings.update",
            actor_id=actor.id,
            actor_login=actor.login_name,
            target_type="settings",
            target_id="settings",
            diff=diff,
            ip=client_ip(request),
        )
        # 提示词段落、默认 effort 之类都在这里：各进程要重载才能生效（没变化就不吵醒它们）。
        await notify(session, "config_changed", {"table": "settings", "id": "settings"})
        # store.set 走的是自己的 session，审计行还在请求 session 里，得单独提交。
        await session.commit()
    return {"code": 0, "data": await _view(session, store)}


@router.get("/defaults")
async def get_defaults(
    _: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    store: SettingsStore = Depends(get_store),
) -> dict[str, Any]:
    return {"code": 0, "data": await _view(session, store, PUBLIC_DEFAULT_KEYS)}
