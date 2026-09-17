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
    User,
    UserIdentity,
)
from coreman.core.feishu_apps import management
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
            session, cipher, user, purpose="update", bot=bot, app_id=bot_app_id(cipher, bot)
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
        data = await management.overview(client)
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


class VisibilityIn(BaseModel):
    visible_to_all: bool
    user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=2000)
    department_ids: list[uuid.UUID] = Field(default_factory=list, max_length=2000)


@router.patch("/bots/{bot_id}/feishu-app/visibility")
async def update_feishu_app_visibility(
    bot_id: uuid.UUID,
    body: VisibilityIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cipher = _cipher(request)
    bot = await admin_feishu_bot(session, bot_id, user)
    user_ids: list[str] = []
    department_ids: list[str] = []
    if not body.visible_to_all:
        if not body.user_ids and not body.department_ids:
            raise ApiError(422, 422, "请至少选择一位成员或一个部门")
        rows = await session.execute(
            select(UserIdentity.user_id, UserIdentity.platform_user_id).where(
                UserIdentity.platform == "feishu", UserIdentity.user_id.in_(body.user_ids)
            )
        )
        identities: dict[uuid.UUID, str] = {row[0]: row[1] for row in rows}
        missing = len(set(body.user_ids) - set(identities))
        if missing:
            raise ApiError(422, 422, f"有 {missing} 位成员没有飞书账号，无法加入可用范围")
        departments = list(
            await session.scalars(
                select(Department.platform_dept_id).where(
                    Department.platform == "feishu", Department.id.in_(body.department_ids)
                )
            )
        )
        if len(departments) != len(set(body.department_ids)):
            raise ApiError(422, 422, "所选部门不是飞书通讯录部门")
        user_ids, department_ids = list(identities.values()), departments
    client = client_for(cipher, bot)
    try:
        await management.update_visibility(
            client,
            bot_app_id(cipher, bot),
            visible_to_all=body.visible_to_all,
            user_ids=user_ids,
            department_ids=department_ids,
        )
    finally:
        await client.aclose()
    await _audit_app(
        session,
        request,
        user,
        bot,
        "visibility",
        {
            "visible_to_all": [None, body.visible_to_all],
            "users": [None, len(user_ids)],
            "departments": [None, len(department_ids)],
        },
    )
    return {"code": 0, "data": {"ok": True, "publish_required": True}}


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
    """补齐 CoreMan 内置斜杠指令（/new /stop /sessions /help），已有的同名指令不改。"""
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
