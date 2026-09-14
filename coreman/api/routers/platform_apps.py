"""平台应用（spec §5.1 platform_apps；§10.2：仅 platform_admin）。"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, get_session
from coreman.api.errors import ApiError, not_found
from coreman.api.pagination import PageParams, paginate
from coreman.api.permissions import require_roles
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match, set_etag
from coreman.core.audit import diff_dict, record_audit
from coreman.core.contacts import runner
from coreman.core.crypto import Cipher
from coreman.core.db.models import CAPABILITIES, ContactSyncRun, PlatformApp, User
from coreman.core.masking import is_masked, mask_secret
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.core.platforms.wecom import WeComClient, WeComError

# B008：不能把 require_roles("platform_admin") 直接塞进函数参数默认值里调用，
# 挪成模块级单例，端点签名里复用同一个依赖对象（还顺带让 FastAPI 的依赖缓存去重）。
_require_platform_admin = require_roles("platform_admin")
router = APIRouter(
    prefix="/api/admin/platform-apps",
    tags=["platform-apps"],
    dependencies=[Depends(verify_csrf), Depends(_require_platform_admin)],
)
SECRET_FIELDS = ("secret", "callback_token", "callback_aes_key")


class PlatformAppIn(BaseModel):
    platform: Literal["wecom", "feishu"]
    name: str = Field(min_length=1, max_length=100)
    capabilities: list[str] = Field(min_length=1)
    corp_id: str | None = Field(default=None, max_length=100)
    app_id: str | None = Field(default=None, max_length=100)
    secret: str = Field(min_length=1, max_length=500)
    callback_token: str | None = Field(default=None, max_length=500)
    callback_aes_key: str | None = Field(default=None, max_length=500)
    extra: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("capabilities")
    @classmethod
    def _caps(cls, v: list[str]) -> list[str]:
        bad = [c for c in v if c not in CAPABILITIES]
        if bad:
            raise ValueError(f"未知能力：{','.join(bad)}")
        return sorted(set(v), key=CAPABILITIES.index)

    @model_validator(mode="after")
    def _platform_fields(self) -> PlatformAppIn:
        if self.platform == "wecom" and not self.corp_id:
            raise ValueError("企微应用必须填写 corp_id")
        if self.platform == "feishu" and not self.app_id:
            raise ValueError("飞书应用必须填写 app_id")
        return self


def _aad(field: str) -> str:
    return f"platform_apps.{field}_enc"


def decrypt_secret(cipher: Cipher, app: PlatformApp) -> str:
    return cipher.decrypt(app.secret_enc, _aad("secret"))


def _public(app: PlatformApp) -> dict[str, Any]:
    """非密钥字段视图（审计快照与 diff 用，不涉及任何解密）。"""
    return {
        "platform": app.platform,
        "name": app.name,
        "capabilities": list(app.capabilities),
        "corp_id": app.corp_id,
        "app_id": app.app_id,
        "extra": app.extra,
        "enabled": app.enabled,
    }


def _plain(cipher: Cipher, app: PlatformApp) -> dict[str, Any]:
    """解密后的可比较视图（只用于 diff 与「是否修改」判断，绝不返回给前端）。"""
    return {
        **_public(app),
        "secret": decrypt_secret(cipher, app),
        "callback_token": (
            cipher.decrypt(app.callback_token_enc, _aad("callback_token"))
            if app.callback_token_enc
            else None
        ),
        "callback_aes_key": (
            cipher.decrypt(app.callback_aes_key_enc, _aad("callback_aes_key"))
            if app.callback_aes_key_enc
            else None
        ),
    }


def to_out(cipher: Cipher, app: PlatformApp, base_url: str = "") -> dict[str, Any]:
    plain = _plain(cipher, app)
    return {
        "id": str(app.id),
        "callback_url": f"{base_url}/api/callbacks/wecom/{app.id}"
        if app.platform == "wecom" and "callback" in app.capabilities
        else None,
        **{k: v for k, v in plain.items() if k not in SECRET_FIELDS},
        **{k: mask_secret(plain[k]) for k in SECRET_FIELDS},
        "version": app.version,
        "created_at": app.created_at,
        "updated_at": app.updated_at,
    }


async def load_app(session: AsyncSession, app_id: uuid.UUID) -> PlatformApp:
    app = await session.get(PlatformApp, app_id)
    if app is None:
        raise not_found("平台应用不存在")
    return app


def run_to_dict(run: ContactSyncRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "stats": run.stats,
        "error": run.error,
        "triggered_by": str(run.triggered_by) if run.triggered_by else None,
    }


def _apply(
    cipher: Cipher, app: PlatformApp, body: PlatformAppIn, current: dict[str, Any] | None
) -> None:
    app.platform, app.name, app.capabilities = body.platform, body.name, body.capabilities
    app.corp_id, app.app_id, app.extra, app.enabled = (
        body.corp_id,
        body.app_id,
        body.extra,
        body.enabled,
    )
    # secret 必填（min_length=1）：更新时脱敏值代表「未修改」，回退到解密出的旧值；
    # 创建时没有旧密文可回退，脱敏值已被 create_app 的 422 守卫挡下——两条路径都非空。
    secret = current["secret"] if (is_masked(body.secret) and current) else body.secret
    app.secret_enc = cipher.encrypt(secret, _aad("secret"))
    for field in ("callback_token", "callback_aes_key"):
        value = getattr(body, field)
        if is_masked(value):
            value = current[field] if current else None  # 脱敏值 = 未修改
        setattr(app, f"{field}_enc", cipher.encrypt(value, _aad(field)) if value else None)


def _cipher(request: Request) -> Cipher:
    return request.app.state.cipher  # type: ignore[no-any-return]


@router.get("")
async def list_apps(
    request: Request,
    platform: str | None = None,
    params: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(PlatformApp).order_by(PlatformApp.platform, PlatformApp.created_at)
    if platform:
        stmt = stmt.where(PlatformApp.platform == platform)
    page = await paginate(session, stmt, params)
    cipher = _cipher(request)
    return {
        "code": 0,
        "data": {
            **page,
            "items": [
                to_out(cipher, a, request.app.state.settings.public_base_url) for a in page["items"]
            ],
        },
    }


@router.post("", status_code=201)
async def create_app(
    body: PlatformAppIn,
    request: Request,
    user: User = Depends(_require_platform_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if any(is_masked(getattr(body, f)) for f in SECRET_FIELDS):
        # 脱敏值只在更新时代表「未修改」；创建时没有旧密文可以回退，必须拒绝，
        # 否则 secret 会被写成占位串、callback_token/aes_key 会被静默置空。
        raise ApiError(422, 422, "密钥不能是脱敏值")
    cipher = _cipher(request)
    app = PlatformApp(secret_enc="")
    _apply(cipher, app, body, None)
    session.add(app)
    await session.flush()
    await record_audit(
        session,
        action="platform_app.create",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="platform_app",
        target_id=str(app.id),
        diff=diff_dict({}, _plain(cipher, app), SECRET_FIELDS),
        ip=client_ip(request),
    )
    await session.commit()
    await session.refresh(app)
    return {"code": 0, "data": to_out(cipher, app, request.app.state.settings.public_base_url)}


@router.get("/{app_id}")
async def get_app(
    app_id: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    return {
        "code": 0,
        "data": to_out(
            _cipher(request),
            await load_app(session, app_id),
            request.app.state.settings.public_base_url,
        ),
    }


@router.put("/{app_id}")
async def update_app(
    app_id: uuid.UUID,
    body: PlatformAppIn,
    request: Request,
    response: Response,
    user: User = Depends(_require_platform_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cipher = _cipher(request)
    app = await load_app(session, app_id)
    require_if_match(request, app.version)
    before = _plain(cipher, app)
    _apply(cipher, app, body, before)
    await record_audit(
        session,
        action="platform_app.update",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="platform_app",
        target_id=str(app.id),
        diff=diff_dict(before, _plain(cipher, app), SECRET_FIELDS),
        ip=client_ip(request),
    )
    await session.commit()
    await session.refresh(app)
    set_etag(response, app.version)
    return {"code": 0, "data": to_out(cipher, app, request.app.state.settings.public_base_url)}


@router.delete("/{app_id}")
async def delete_app(
    app_id: uuid.UUID,
    request: Request,
    user: User = Depends(_require_platform_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    app = await load_app(session, app_id)
    snapshot = _public(app)  # 密钥字段一个都不进审计
    await session.delete(app)
    await record_audit(
        session,
        action="platform_app.delete",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="platform_app",
        target_id=str(app_id),
        diff=diff_dict(snapshot, {}),
        ip=client_ip(request),
    )
    await session.commit()
    return {"code": 0, "data": None}


@router.post("/{app_id}/test")
async def test_app(
    app_id: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    app = await load_app(session, app_id)
    client = (
        FeishuClient(app.app_id or "", decrypt_secret(_cipher(request), app))
        if app.platform == "feishu"
        else WeComClient(app.corp_id or "", decrypt_secret(_cipher(request), app))
    )
    try:
        await client.get_token(force=True)
    except (WeComError, FeishuError) as exc:
        return {"code": 0, "data": {"ok": False, "message": str(exc)}}
    except Exception as exc:  # noqa: BLE001 网络错误也要给用户看
        return {"code": 0, "data": {"ok": False, "message": f"连接失败: {type(exc).__name__}"}}
    finally:
        await client.aclose()
    return {"code": 0, "data": {"ok": True, "message": "access_token 获取成功"}}


@router.post("/{app_id}/sync", status_code=202)
async def trigger_sync(
    app_id: uuid.UUID,
    request: Request,
    user: User = Depends(_require_platform_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """触发一次通讯录同步：建 running 记录后立即返回，实际抓取与归并在后台任务里跑。

    run 行与审计写在同一个请求会话里，一次提交：审计失败时 run 行不会留下孤儿。
    """
    factory = request.app.state.session_factory
    run = await runner.start_run(factory, app_id, user.id, session=session)
    await record_audit(
        session,
        action="platform_app.sync",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="platform_app",
        target_id=str(app_id),
        diff=diff_dict({}, {"run_id": run.id}),
        ip=client_ip(request),
    )
    await session.commit()
    await session.refresh(run)
    # 后台任务另开会话，必须等本事务提交后再起，否则它读不到这条 run 行
    task = asyncio.create_task(runner.execute_run(factory, _cipher(request), run.id))
    request.app.state.background_tasks.add(task)
    task.add_done_callback(request.app.state.background_tasks.discard)
    return {"code": 0, "data": {"run_id": run.id}}


@router.get("/{app_id}/sync-runs")
async def list_runs(
    app_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await load_app(session, app_id)
    rows = (
        (
            await session.execute(
                select(ContactSyncRun)
                .where(ContactSyncRun.platform_app_id == app_id)
                .order_by(ContactSyncRun.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {"code": 0, "data": [run_to_dict(r) for r in rows]}


runs_router = APIRouter(
    prefix="/api/admin/sync-runs",
    tags=["platform-apps"],
    dependencies=[Depends(verify_csrf), Depends(_require_platform_admin)],
)


@runs_router.get("/{run_id}")
async def get_run(run_id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    run = await session.get(ContactSyncRun, run_id)
    if run is None:
        raise not_found("同步记录不存在")
    return {"code": 0, "data": run_to_dict(run)}
