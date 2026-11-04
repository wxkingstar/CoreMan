"""扫码创建 / 补齐飞书应用，以及机器人所属飞书应用的管理。"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Request, UploadFile
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.bot_permissions import can_create_bot
from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.routers.platform_apps import decrypt_secret
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.bots.events import notify_bot_changed
from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    Department,
    FeishuAppRegistration,
    FeishuPersonalGrant,
    PlatformApp,
    User,
    UserIdentity,
)
from coreman.core.feishu_apps import management, manifest
from coreman.core.feishu_apps import service as registrations
from coreman.core.knowledge import installation
from coreman.core.platforms.feishu import FeishuClient

router = APIRouter(prefix="/api/admin", tags=["feishu-apps"], dependencies=[Depends(verify_csrf)])


def _cipher(request: Request) -> Cipher:
    return request.app.state.cipher  # type: ignore[no-any-return]


async def admin_feishu_bot(
    session: AsyncSession, bot_id: uuid.UUID, actor: User, *, lock: bool = False
) -> Bot:
    query = select(Bot).where(Bot.id == bot_id)
    bot = await session.scalar(query.with_for_update() if lock else query)
    if bot is None:
        raise not_found("机器人不存在")
    if not await installation.bot_admin(session, bot, actor):
        raise forbidden()
    if bot.platform != "feishu":
        raise ApiError(422, 422, "只有飞书机器人可以管理飞书应用")
    return bot


def bot_app_id(cipher: Cipher, bot: Bot) -> str:
    app_id = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD).get("app_id")
    if not app_id:
        raise ApiError(422, 422, "机器人还没有关联飞书应用")
    return app_id


async def _audit(
    session: AsyncSession,
    request: Request,
    actor: User,
    action: str,
    target: str,
    diff: dict[str, Any],
) -> None:
    await record_audit(
        session,
        action=action,
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="bot" if action.startswith("bot.") else "feishu_app_registration",
        target_id=target,
        diff=diff,
        ip=client_ip(request),
    )


class RegistrationIn(BaseModel):
    purpose: Literal["create", "update"]
    bot_id: uuid.UUID | None = None
    name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    avatar_url: str | None = Field(default=None, max_length=500)
    # 新建时优先复用本人扫码创建、还没被员工使用的应用。
    reuse: bool = True
    # 补齐权限时连同群聊旁听的权限一起申请（旁听还没打开时由开启入口传入）。
    observe: bool = False
    # 补齐权限时连同管理可用范围的权限一起申请（由可用范围面板传入）。
    visibility: bool = False


@router.post("/feishu-app-registrations", status_code=201)
async def start_registration(
    body: RegistrationIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cipher = _cipher(request)
    if body.purpose == "create":
        if not can_create_bot(user):
            raise forbidden()
        if body.reuse and (row := await registrations.reusable(session, user)):
            return {"code": 0, "data": {**registrations.out(cipher, row), "reused": True}}
        row = await registrations.start(
            session,
            cipher,
            user,
            purpose="create",
            name=body.name,
            description=body.description,
            avatar_url=body.avatar_url,
        )
    else:
        if body.bot_id is None:
            raise ApiError(422, 422, "补齐权限需要指定机器人")
        bot = await admin_feishu_bot(session, body.bot_id, user)
        row = await registrations.start(
            session,
            cipher,
            user,
            purpose="update",
            bot=bot,
            app_id=bot_app_id(cipher, bot),
            observe=body.observe,
            visibility=body.visibility,
        )
    await _audit(
        session,
        request,
        user,
        "feishu_app.registration_start",
        str(row.id),
        {
            "purpose": [None, row.purpose],
            "bot_id": [None, str(body.bot_id) if body.bot_id else None],
        },
    )
    await session.commit()
    return {"code": 0, "data": {**registrations.out(cipher, row), "reused": False}}


@router.get("/feishu-app-registrations/{registration_id}")
async def get_registration(
    registration_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cipher = _cipher(request)
    row = await registrations.load(session, user, registration_id)
    retry_after = await registrations.refresh(cipher, row)
    if row.purpose == "update" and row.status == "succeeded" and row.bot_id:
        bot = await admin_feishu_bot(session, row.bot_id, user, lock=True)
        changed = registrations.apply_update(cipher, row, bot)
        if row.status == "consumed":
            detail: dict[str, Any] = {"app_id": [row.app_id, row.app_id]}
            if changed:
                detail["credentials"] = ["***", "***"]
            await _audit(session, request, user, "bot.feishu_app_update", str(bot.id), detail)
        if changed:
            await notify_bot_changed(session, bot.id)
    await session.commit()
    return {"code": 0, "data": registrations.out(cipher, row, retry_after)}


@router.delete("/feishu-app-registrations/{registration_id}")
async def cancel_registration(
    registration_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cipher = _cipher(request)
    row = await registrations.load(session, user, registration_id)
    registrations.cancel(row)
    await session.commit()
    return {"code": 0, "data": registrations.out(cipher, row)}


# ---- 飞书应用管理 ---------------------------------------------------------------

MAX_AVATAR_BYTES = 2 * 1024 * 1024
_HTTPS = r"^https://[^\s]{1,500}$"


def client_for(cipher: Cipher, bot: Bot) -> FeishuClient:
    """以机器人自己的应用凭证建客户端；测试会替换这个工厂。"""
    creds = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
    if not creds.get("app_id") or not creds.get("app_secret"):
        raise ApiError(422, 422, "机器人还没有关联飞书应用")
    return FeishuClient(creds["app_id"], creds["app_secret"])


async def _audit_app(
    session: AsyncSession, request: Request, actor: User, bot: Bot, op: str, detail: dict[str, Any]
) -> None:
    await _audit(session, request, actor, f"bot.feishu_app_{op}", str(bot.id), detail)
    await session.commit()


@router.get("/bots/{bot_id}/feishu-app")
async def feishu_app_overview(
    bot_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cipher = _cipher(request)
    bot = await admin_feishu_bot(session, bot_id, user)
    app_id = bot_app_id(cipher, bot)
    client = client_for(cipher, bot)
    try:
        data = await management.overview(client, observe=bot.group_observe != "off")
    finally:
        await client.aclose()
    origin = (
        await session.execute(
            select(FeishuAppRegistration, User.display_name)
            .join(User, User.id == FeishuAppRegistration.user_id)
            .where(
                FeishuAppRegistration.bot_id == bot.id,
                FeishuAppRegistration.purpose == "create",
                FeishuAppRegistration.status == "consumed",
                FeishuAppRegistration.app_id == app_id,
            )
            .order_by(FeishuAppRegistration.created_at.desc())
            .limit(1)
        )
    ).first()
    connections = await session.scalar(
        select(func.count())
        .select_from(FeishuPersonalGrant)
        .where(FeishuPersonalGrant.bot_id == bot.id, FeishuPersonalGrant.status == "connected")
    )
    online = data["versions"]["online"]
    return {
        "code": 0,
        "data": {
            **data,
            "app_id": app_id,
            "origin": {
                "one_click": True,
                "created_by_name": origin[1],
                "created_at": origin[0].created_at.isoformat(),
            }
            if origin
            else {"one_click": False},
            "employee": {"name": bot.name, "description": bot.description},
            "personal_connections": connections or 0,
            "next_version": management.next_version(online["version"] if online else None),
            "console_url": f"https://open.feishu.cn/app/{app_id}",
        },
    }


class BaseInfoIn(BaseModel):
    language: Literal["zh_cn", "en_us", "ja_jp"] = "zh_cn"
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=200)
    help_use: str | None = Field(default=None, pattern=_HTTPS)


OBSERVE_PERMISSION_ERRORS = {
    "missing": "飞书应用还没有「获取群组中所有消息」权限，请先在群聊旁听设置里扫码补齐",
    "pending": "「获取群组中所有消息」权限还在等企业管理员审核，审核通过后才能开启群聊旁听",
}


async def observe_permission(cipher: Cipher, bot: Bot) -> str:
    """机器人的飞书应用有没有群聊旁听要的权限（见 management.observe_permission）。"""
    client = client_for(cipher, bot)
    try:
        return await management.observe_permission(client)
    finally:
        await client.aclose()


async def collaboration_permission(cipher: Cipher, bot: Bot) -> str:
    """机器人之间求助要的权限，口径同 observe_permission。"""
    client = client_for(cipher, bot)
    try:
        return await management.scope_permission(client, manifest.COLLABORATION_SCOPES)
    finally:
        await client.aclose()


@router.get("/bots/{bot_id}/feishu-app/observe-permission")
async def feishu_app_observe_permission(
    bot_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await admin_feishu_bot(session, bot_id, user)
    return {"code": 0, "data": {"status": await observe_permission(_cipher(request), bot)}}


@router.patch("/bots/{bot_id}/feishu-app/base")
async def update_feishu_app_base(
    bot_id: uuid.UUID,
    body: BaseInfoIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cipher = _cipher(request)
    bot = await admin_feishu_bot(session, bot_id, user)
    client = client_for(cipher, bot)
    try:
        await management.update_base(
            client,
            bot_app_id(cipher, bot),
            language=body.language,
            name=body.name,
            description=body.description,
            help_use=body.help_use,
        )
    finally:
        await client.aclose()
    await _audit_app(session, request, user, bot, "base", {"name": [None, body.name]})
    return {"code": 0, "data": {"ok": True, "publish_required": True}}


@router.post("/bots/{bot_id}/feishu-app/avatar")
async def upload_feishu_app_avatar(
    bot_id: uuid.UUID,
    avatar: UploadFile,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cipher = _cipher(request)
    bot = await admin_feishu_bot(session, bot_id, user)
    if avatar.content_type not in ("image/png", "image/jpeg"):
        raise ApiError(422, 422, "图标只支持 PNG 或 JPEG")
    content = await avatar.read(MAX_AVATAR_BYTES + 1)
    if not content or len(content) > MAX_AVATAR_BYTES:
        raise ApiError(422, 422, "图标不能超过 2 MB")
    client = client_for(cipher, bot)
    try:
        url = await management.upload_avatar(
            client, content, avatar.filename or "avatar", avatar.content_type
        )
        overview = await management.overview(client)
        app = overview["app"] or {}
        await management.update_base(
            client,
            bot_app_id(cipher, bot),
            language=app.get("primary_language") or "zh_cn",
            name=app.get("name") or bot.name,
            description=app.get("description") or bot.description or bot.name,
            avatar_url=url,
        )
    finally:
        await client.aclose()
    await _audit_app(session, request, user, bot, "avatar", {"avatar_url": [None, url]})
    return {"code": 0, "data": {"avatar_url": url, "publish_required": True}}


class MenuIn(BaseModel):
    menu_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    parent_menu_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=60)
    kind: Literal["link", "event", "submenu", "message"]
    pc_url: str | None = Field(default=None, pattern=_HTTPS)
    mobile_url: str | None = Field(default=None, pattern=_HTTPS)
    event_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,30}$")
    sort: int = Field(default=0, ge=0, le=999999)

    @model_validator(mode="after")
    def _content(self) -> MenuIn:
        if self.kind == "link" and not (self.pc_url or self.mobile_url):
            raise ValueError("跳转链接菜单需要填写链接")
        if self.kind == "event" and not self.event_key:
            raise ValueError("事件菜单需要填写事件标识")
        return self


class BotAbilityIn(BaseModel):
    language: Literal["zh_cn", "en_us", "ja_jp"] = "zh_cn"
    get_started_desc: str | None = Field(default=None, max_length=64)
    menu_enabled: bool | None = None
    menu_display_strategy: Literal[1, 2, 3] | None = None
    menus: list[MenuIn] = Field(default_factory=list, max_length=60)

    @model_validator(mode="after")
    def _tree(self) -> BotAbilityIn:
        by_id = {m.menu_id: m for m in self.menus}
        if len(by_id) != len(self.menus):
            raise ValueError("菜单标识不能重复")
        for menu in self.menus:
            if menu.parent_menu_id is None:
                continue
            parent = by_id.get(menu.parent_menu_id)
            if parent is None or parent.kind != "submenu" or parent.parent_menu_id is not None:
                raise ValueError("子菜单只能挂在一级「展开子菜单」下")
            if menu.kind == "submenu":
                raise ValueError("菜单最多两级")
        if self.menu_enabled and not self.menus:
            raise ValueError("开启菜单时至少需要一个菜单项")
        return self


@router.patch("/bots/{bot_id}/feishu-app/bot")
async def update_feishu_app_bot(
    bot_id: uuid.UUID,
    body: BotAbilityIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cipher = _cipher(request)
    bot = await admin_feishu_bot(session, bot_id, user)
    client = client_for(cipher, bot)
    try:
        await management.update_bot(
            client,
            bot_app_id(cipher, bot),
            language=body.language,
            get_started_desc=body.get_started_desc,
            menu_enabled=body.menu_enabled,
            menu_display_strategy=body.menu_display_strategy,
            menus=[m.model_dump() for m in body.menus],
        )
    finally:
        await client.aclose()
    await _audit_app(
        session,
        request,
        user,
        bot,
        "bot",
        {"menu_enabled": [None, body.menu_enabled], "menus": [None, len(body.menus)]},
    )
    return {"code": 0, "data": {"ok": True, "publish_required": True}}


VISIBILITY_PERMISSION_ERRORS = {
    "missing": (
        "飞书登录应用和这个机器人的应用都还没有管理可用范围的权限：给登录应用开通一次即可"
        "管理所有机器人，或在「可用范围」里为这个机器人扫码补齐"
    ),
    "pending": "管理可用范围的权限还在等企业管理员审核，审核通过后才能在 CoreMan 管理可用范围",
}


def platform_client_for(cipher: Cipher, app: PlatformApp) -> FeishuClient:
    """以平台应用（飞书登录应用）的凭证建客户端；测试会替换这个工厂。"""
    return FeishuClient(app.app_id or "", decrypt_secret(cipher, app))


async def _login_app(session: AsyncSession) -> PlatformApp | None:
    """启用且具备登录能力的飞书平台应用（同扫码登录取的那个）。"""
    stmt = select(PlatformApp).where(
        PlatformApp.platform == "feishu",
        PlatformApp.enabled.is_(True),
        PlatformApp.capabilities.any("login"),  # type: ignore[arg-type]
    )
    return (await session.execute(stmt.order_by(PlatformApp.created_at))).scalars().first()


async def _visibility_access(
    session: AsyncSession, request: Request, bot: Bot, app_id: str
) -> tuple[FeishuClient | None, dict[str, Any], dict[str, Any] | None]:
    """找能读写这个机器人可用范围的应用，顺带读出当前名单。

    可用范围接口能读写企业内任意应用，所以优先用飞书登录应用：它开通一次两个权限就能管理
    所有机器人。登录应用没有权限（或没配置）时退回机器人自己的应用。返回（客户端、权限说明、
    当前名单）；客户端为 None 表示两边都没有权限，否则由调用方关闭。权限说明含 permission、
    via（platform / self）与 platform_app（登录应用名称）。
    """
    cipher = _cipher(request)
    login = await _login_app(session)
    info: dict[str, Any] = {"platform_app": login.name if login else None}
    if login is not None and login.app_id:
        client = platform_client_for(cipher, login)
        try:
            raw = await management.get_visibility(client, app_id)
        except ApiError as exc:
            await client.aclose()
            # 只在「没权限」时退回机器人自己的应用；其他失败照常报出来，不能说成缺权限。
            if exc.status_code != 409:
                raise
        except BaseException:
            await client.aclose()
            raise
        else:
            return client, {**info, "permission": "granted", "via": "platform"}, raw
    client = client_for(cipher, bot)
    try:
        permission = await management.scope_permission(client, manifest.VISIBILITY_SCOPES)
        if permission != "granted":
            await client.aclose()
            return None, {**info, "permission": permission, "via": None}, None
        raw = await management.get_visibility(client, app_id)
    except BaseException:
        await client.aclose()
        raise
    return client, {**info, "permission": "granted", "via": "self"}, raw


async def _people(session: AsyncSession, feishu_ids: list[str]) -> list[dict[str, Any]]:
    """飞书 user_id → CoreMan 成员；没同步进来的只给飞书 ID。"""
    rows = await session.execute(
        select(UserIdentity.platform_user_id, User.id, User.display_name, User.login_name)
        .join(User, User.id == UserIdentity.user_id)
        .where(UserIdentity.platform == "feishu", UserIdentity.platform_user_id.in_(feishu_ids))
    )
    known = {row[0]: row for row in rows}
    return [
        {
            "feishu_id": fid,
            "id": str(known[fid][1]) if fid in known else None,
            "display_name": known[fid][2] if fid in known else None,
            "login_name": known[fid][3] if fid in known else None,
        }
        for fid in feishu_ids
    ]


async def _departments(session: AsyncSession, feishu_ids: list[str]) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(Department.platform_dept_id, Department.id, Department.name, Department.path).where(
            Department.platform == "feishu", Department.platform_dept_id.in_(feishu_ids)
        )
    )
    known = {row[0]: row for row in rows}
    return [
        {
            "feishu_id": fid,
            "id": str(known[fid][1]) if fid in known else None,
            "name": known[fid][2] if fid in known else None,
            "path": known[fid][3] if fid in known else None,
        }
        for fid in feishu_ids
    ]


async def _visibility_out(session: AsyncSession, raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "permission": "granted",
        "visible_to_all": raw["is_all"],
        "users": await _people(session, raw["users"]),
        "departments": await _departments(session, raw["departments"]),
        "groups": raw["groups"],
        "invisible": {
            "users": await _people(session, raw["invisible_users"]),
            "departments": await _departments(session, raw["invisible_departments"]),
            "groups": raw["invisible_groups"],
        },
    }


@router.get("/bots/{bot_id}/feishu-app/visibility")
async def feishu_app_visibility(
    bot_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """线上实际生效的可用范围；没有可用权限时只返回权限说明（见 _visibility_access）。"""
    bot = await admin_feishu_bot(session, bot_id, user)
    app_id = bot_app_id(_cipher(request), bot)
    client, info, raw = await _visibility_access(session, request, bot, app_id)
    if client is None or raw is None:
        return {"code": 0, "data": info}
    await client.aclose()
    return {"code": 0, "data": {**info, **await _visibility_out(session, raw)}}


class VisibilityIn(BaseModel):
    """增量修改：visible_to_all 不给表示不切换全员 / 部分成员。"""

    visible_to_all: bool | None = None
    add_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=2000)
    remove_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=2000)
    add_department_ids: list[uuid.UUID] = Field(default_factory=list, max_length=2000)
    remove_department_ids: list[uuid.UUID] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def _has_change(self) -> VisibilityIn:
        lists = (
            self.add_user_ids,
            self.remove_user_ids,
            self.add_department_ids,
            self.remove_department_ids,
        )
        if self.visible_to_all is None and not any(lists):
            raise ValueError("没有要修改的内容")
        return self


async def _feishu_user_ids(session: AsyncSession, ids: list[uuid.UUID]) -> list[str]:
    if not ids:
        return []
    rows = await session.execute(
        select(UserIdentity.user_id, UserIdentity.platform_user_id).where(
            UserIdentity.platform == "feishu", UserIdentity.user_id.in_(ids)
        )
    )
    found: dict[uuid.UUID, str] = {row[0]: row[1] for row in rows}
    missing = len(set(ids) - set(found))
    if missing:
        raise ApiError(422, 422, f"有 {missing} 位成员没有飞书账号，无法加入或移出可用范围")
    return list(dict.fromkeys(found[i] for i in ids))


async def _feishu_department_ids(session: AsyncSession, ids: list[uuid.UUID]) -> list[str]:
    if not ids:
        return []
    found = list(
        await session.scalars(
            select(Department.platform_dept_id).where(
                Department.platform == "feishu", Department.id.in_(ids)
            )
        )
    )
    if len(found) != len(set(ids)):
        raise ApiError(422, 422, "所选部门不是飞书通讯录部门")
    return found


@router.patch("/bots/{bot_id}/feishu-app/visibility")
async def update_feishu_app_visibility(
    bot_id: uuid.UUID,
    body: VisibilityIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """增量增删可用名单或切换全员可见，线上立即生效，返回修改后的名单。"""
    cipher = _cipher(request)
    bot = await admin_feishu_bot(session, bot_id, user)
    app_id = bot_app_id(cipher, bot)
    add_users = await _feishu_user_ids(session, body.add_user_ids)
    remove_users = await _feishu_user_ids(session, body.remove_user_ids)
    add_departments = await _feishu_department_ids(session, body.add_department_ids)
    remove_departments = await _feishu_department_ids(session, body.remove_department_ids)
    client, info, before = await _visibility_access(session, request, bot, app_id)
    if client is None or before is None:
        raise ApiError(409, 409, VISIBILITY_PERMISSION_ERRORS[info["permission"]])
    try:
        if body.visible_to_all is False and before["is_all"] and not (add_users or add_departments):
            raise ApiError(422, 422, "改为部分成员可见时，请至少添加一位成员或一个部门")
        await management.update_visibility(
            client,
            app_id,
            visible_to_all=body.visible_to_all,
            add_users=add_users,
            remove_users=remove_users,
            add_departments=add_departments,
            remove_departments=remove_departments,
        )
        after = await management.get_visibility(client, app_id)
    finally:
        await client.aclose()
    await _audit_app(
        session,
        request,
        user,
        bot,
        "visibility",
        {
            "visible_to_all": [before["is_all"], after["is_all"]],
            "users_added": [None, add_users or None],
            "users_removed": [remove_users or None, None],
            "departments_added": [None, add_departments or None],
            "departments_removed": [remove_departments or None, None],
            "via": [None, info["via"]],
        },
    )
    return {"code": 0, "data": {**info, **await _visibility_out(session, after)}}


class PublishIn(BaseModel):
    version: str = Field(pattern=r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
    changelog: str = Field(min_length=1, max_length=500)
    remark: str = Field(min_length=1, max_length=500)


@router.post("/bots/{bot_id}/feishu-app/publish")
async def publish_feishu_app(
    bot_id: uuid.UUID,
    body: PublishIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cipher = _cipher(request)
    bot = await admin_feishu_bot(session, bot_id, user)
    client = client_for(cipher, bot)
    try:
        result = await management.publish(
            client,
            bot_app_id(cipher, bot),
            version=body.version,
            changelog=body.changelog,
            remark=body.remark,
        )
    finally:
        await client.aclose()
    await _audit_app(session, request, user, bot, "publish", {"version": [None, result["version"]]})
    return {"code": 0, "data": result}


@router.post("/bots/{bot_id}/feishu-app/scopes/apply")
async def apply_feishu_app_scopes(
    bot_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cipher = _cipher(request)
    bot = await admin_feishu_bot(session, bot_id, user)
    client = client_for(cipher, bot)
    try:
        await management.apply_scopes(client)
    finally:
        await client.aclose()
    await _audit_app(session, request, user, bot, "scopes_apply", {})
    return {"code": 0, "data": {"ok": True}}


class CommandIn(BaseModel):
    command: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    description: str = Field(min_length=1, max_length=100)
    icon_key: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,48}_outlined$")


class CommandPatch(BaseModel):
    description: str = Field(min_length=1, max_length=100)
    icon_key: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,48}_outlined$")


CommandId = Path(pattern=r"^[0-9]{1,32}$")


@router.post("/bots/{bot_id}/feishu-app/slash-commands/defaults")
async def add_default_feishu_slash_commands(
    bot_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """补齐 CoreMan 内置斜杠指令（/new /stop /sessions /connect /help），已有的同名指令不改。"""
    cipher = _cipher(request)
    bot = await admin_feishu_bot(session, bot_id, user)
    client = client_for(cipher, bot)
    try:
        created = await management.ensure_default_commands(client)
    finally:
        await client.aclose()
    await _audit_app(session, request, user, bot, "command_defaults", {"created": [None, created]})
    return {"code": 0, "data": {"created": created}}


@router.post("/bots/{bot_id}/feishu-app/slash-commands", status_code=201)
async def create_feishu_slash_command(
    bot_id: uuid.UUID,
    body: CommandIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cipher = _cipher(request)
    bot = await admin_feishu_bot(session, bot_id, user)
    client = client_for(cipher, bot)
    try:
        command_id = await management.create_command(
            client, command=body.command, description=body.description, icon_key=body.icon_key
        )
    finally:
        await client.aclose()
    await _audit_app(
        session, request, user, bot, "command_create", {"command": [None, body.command]}
    )
    return {"code": 0, "data": {"command_id": command_id}}


@router.patch("/bots/{bot_id}/feishu-app/slash-commands/{command_id}")
async def update_feishu_slash_command(
    bot_id: uuid.UUID,
    body: CommandPatch,
    request: Request,
    command_id: str = CommandId,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cipher = _cipher(request)
    bot = await admin_feishu_bot(session, bot_id, user)
    client = client_for(cipher, bot)
    try:
        await management.update_command(
            client, command_id, description=body.description, icon_key=body.icon_key
        )
    finally:
        await client.aclose()
    await _audit_app(
        session, request, user, bot, "command_update", {"command_id": [None, command_id]}
    )
    return {"code": 0, "data": {"ok": True}}


@router.delete("/bots/{bot_id}/feishu-app/slash-commands/{command_id}")
async def delete_feishu_slash_command(
    bot_id: uuid.UUID,
    request: Request,
    command_id: str = CommandId,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cipher = _cipher(request)
    bot = await admin_feishu_bot(session, bot_id, user)
    client = client_for(cipher, bot)
    try:
        await management.delete_command(client, command_id)
    finally:
        await client.aclose()
    await _audit_app(
        session, request, user, bot, "command_delete", {"command_id": [command_id, None]}
    )
    return {"code": 0, "data": {"ok": True}}
