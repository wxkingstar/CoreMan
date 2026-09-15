"""机器人 CRUD（spec §5.2、§10.2、§10.4）。子资源（切换 relay、启停、成员、白名单）在 Task 8。

读：任何登录用户都能看基础字段，敏感字段（system_prompt / 凭证 / 环境变量）按 §10.2 裁剪；
写：只有 bot 管理员（创建者 + bot_members），改派团队与删除另有更严的判定。
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import PurePosixPath
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.bot_permissions import (
    can_create_bot,
    can_delete_bot,
    can_edit_bot,
    can_reassign_team,
    permissions_for,
    relay_allowed_for_bot,
)
from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.pagination import PageParams, paginate
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match, set_etag
from coreman.core.audit import diff_dict, record_audit
from coreman.core.bots.events import notify_bot_changed as notify_bot_changed  # 再导出
from coreman.core.bots.platform_account import reserve_feishu_app
from coreman.core.bots.relay_policy import relay_available
from coreman.core.bots.relay_policy import relay_visible as relay_visible  # 再导出
from coreman.core.bots.relay_policy import (  # 再导出
    validate_model_for_relay as validate_model_for_relay,
)
from coreman.core.bots.secrets import (
    CREDENTIALS_AAD,
    ENV_AAD,
    ENV_KEY_RE,
    decrypt_json,
    encrypt_json,
    has_masked,
    mask_dict,
    merge_secret_dict,
    validate_credentials,
)
from coreman.core.bots.workspace import reserve_workspace
from coreman.core.chat import sessions
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    BOT_KEY_RE,
    Bot,
    BotAllowedUser,
    BotMember,
    RelayServer,
    RuntimeNode,
    Team,
    User,
)
from coreman.core.masking import is_masked, mask_secret
from coreman.core.relay.models import backend_of

router = APIRouter(prefix="/api/admin/bots", tags=["bots"], dependencies=[Depends(verify_csrf)])
# 审计 diff 里只记 ***（明文永不落库）。Task 8 复用。
# system_prompt 也在内：它受 can_view_sensitive 管（§10.2，manager 不旁路），而审计日志对
# ai_committee / platform_admin 是可读的——落明文等于给 manager 开了一条读提示词的后门。
# notify_webhook_url 也在内：企微群机器人 webhook 的 key 就写在 URL 里，它本身就是凭证。
AUDIT_MASKED_KEYS = ("credentials", "env_vars", "system_prompt", "notify_webhook_url")
# PATCH 里显式传 null 会直接违反 NOT NULL（→500），先按 422 挡掉。
NON_NULLABLE = (
    "name",
    "description",
    "model",
    "working_dir",
    "system_prompt",
    "verbosity_level",
    "sse_timeout_seconds",
    "credentials",
    "env_vars",
)


def normalize_working_dir(value: str) -> str:
    """工作目录原样下发给运行时节点当运行根。

    是否位于所选节点的项目主目录之下由 reserve_workspace 校验。
    """
    v = value.strip()
    if not v.startswith("/"):
        raise ValueError("工作目录必须是绝对路径")
    # .. 能爬出项目主目录，不接。
    if any(seg == ".." for seg in v.split("/")):
        raise ValueError("工作目录不能包含 .. 路径段")
    return str(PurePosixPath(v))


def _check_env_vars(v: dict[str, str]) -> dict[str, str]:
    bad = [k for k in v if not ENV_KEY_RE.match(k)]
    if bad:
        raise ValueError(f"环境变量名不合法：{', '.join(bad)}")
    if any(len(val) > 4000 for val in v.values()):
        raise ValueError("环境变量值过长")
    return v


def _check_https(v: str | None) -> str | None:
    # 脱敏值（ht••••ey）放行：校验器跑在路由之前，PATCH 里它表示「这一项没改」，
    # 创建时则由 create_bot 按 has_masked 的口径统一拒掉。
    if v is not None and not is_masked(v) and not v.startswith("https://"):
        raise ValueError("webhook 必须是 https://")
    return v


class BotIn(BaseModel):
    bot_key: str = Field(pattern=BOT_KEY_RE)
    platform: Literal["wecom", "feishu"]
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    avatar_url: str | None = Field(default=None, max_length=500)
    team_id: uuid.UUID | None = None
    relay_server_id: uuid.UUID | None = None
    model: str = Field(min_length=1, max_length=100)
    working_dir: str = Field(min_length=1, max_length=500)
    system_prompt: str = Field(default="", max_length=20000)
    verbosity_level: int = Field(default=1, ge=1, le=4)
    effort_level: Literal["low", "medium", "high", "xhigh"] | None = None
    sse_timeout_seconds: int = Field(default=3600, ge=1800, le=43200)
    credentials: dict[str, str]
    env_vars: dict[str, str] = Field(default_factory=dict)
    welcome_message: str | None = Field(default=None, max_length=2000)
    notify_webhook_url: str | None = Field(default=None, max_length=500)
    enabled: bool = True

    @field_validator("env_vars")
    @classmethod
    def _env_keys(cls, v: dict[str, str]) -> dict[str, str]:
        return _check_env_vars(v)

    @field_validator("notify_webhook_url")
    @classmethod
    def _https(cls, v: str | None) -> str | None:
        return _check_https(v)

    @field_validator("working_dir")
    @classmethod
    def _wd(cls, v: str) -> str:
        return normalize_working_dir(v)


class BotPatch(BaseModel):
    """全部可选；relay_server_id 出现即 409（走切换入口）；team_id 需 can_reassign_team。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    avatar_url: str | None = Field(default=None, max_length=500)
    team_id: uuid.UUID | None = None
    relay_server_id: uuid.UUID | None = None
    model: str | None = Field(default=None, min_length=1, max_length=100)
    working_dir: str | None = Field(default=None, min_length=1, max_length=500)
    system_prompt: str | None = Field(default=None, max_length=20000)
    verbosity_level: int | None = Field(default=None, ge=1, le=4)
    effort_level: Literal["low", "medium", "high", "xhigh"] | None = None
    sse_timeout_seconds: int | None = Field(default=None, ge=1800, le=43200)
    credentials: dict[str, str] | None = None
    env_vars: dict[str, str] | None = None
    welcome_message: str | None = Field(default=None, max_length=2000)
    notify_webhook_url: str | None = Field(default=None, max_length=500)

    @field_validator("env_vars")
    @classmethod
    def _env_keys(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        return None if v is None else _check_env_vars(v)

    @field_validator("notify_webhook_url")
    @classmethod
    def _https(cls, v: str | None) -> str | None:
        return _check_https(v)

    @field_validator("working_dir")
    @classmethod
    def _wd(cls, v: str | None) -> str | None:
        return None if v is None else normalize_working_dir(v)

    def changes(self) -> dict[str, Any]:
        """只取请求体里真正出现过的字段：None 是合法取值（avatar_url 可以清空），
        不能用「值为 None」来判断「没传」。"""
        return self.model_dump(include=self.model_fields_set)


def _cipher(request: Request) -> Cipher:
    return request.app.state.cipher  # type: ignore[no-any-return]


def _public(bot: Bot) -> dict[str, Any]:
    """机器人配置快照（审计 diff 用，全部 JSON 可序列化，不含任何密文/明文密钥）。"""
    return {
        "bot_key": bot.bot_key,
        "platform": bot.platform,
        "name": bot.name,
        "description": bot.description,
        "avatar_url": bot.avatar_url,
        "enabled": bot.enabled,
        "team_id": str(bot.team_id) if bot.team_id else None,
        "relay_server_id": str(bot.relay_server_id) if bot.relay_server_id else None,
        "model": bot.model,
        "working_dir": bot.working_dir,
        "system_prompt": bot.system_prompt,
        "verbosity_level": bot.verbosity_level,
        "effort_level": bot.effort_level,
        "sse_timeout_seconds": bot.sse_timeout_seconds,
        "welcome_message": bot.welcome_message,
        "notify_webhook_url": bot.notify_webhook_url,
    }


async def load_bot(session: AsyncSession, bot_id: uuid.UUID) -> Bot:
    """按 id 取机器人，取不到就 404（Task 8 复用）。"""
    bot = await session.get(Bot, bot_id)
    if bot is None:
        raise not_found("机器人不存在")
    return bot


async def member_ids_of(session: AsyncSession, bot_id: uuid.UUID) -> set[uuid.UUID]:
    """bot_members 里的用户 id 集合（不含创建者，创建者不入表）。"""
    rows = await session.execute(select(BotMember.user_id).where(BotMember.bot_id == bot_id))
    return set(rows.scalars().all())


async def resolve_relay_for_create(
    session: AsyncSession, user: User, relay_id: uuid.UUID | None, bot_team_id: uuid.UUID | None
) -> RelayServer | None:
    """创建时解析 relay：不存在/停用/不可见/不合团队策略一律 422（不泄漏存在性差异）。"""
    if relay_id is None:
        return None
    relay = await session.get(RelayServer, relay_id)
    if relay is None or not relay_available(relay) or not relay_visible(user, relay):
        raise ApiError(422, 422, "目标运行时未注册或不可用")
    if not relay_allowed_for_bot(
        user, relay, bot_team_id=bot_team_id, creator_team_id=user.team_id
    ):
        raise ApiError(422, 422, "目标运行时不属于本团队或公共池")
    return relay


def _checked_credentials(platform: str, creds: dict[str, str]) -> dict[str, str]:
    """凭证落库前的统一校验；ValueError 一律转 422 并把原因带给前端。"""
    try:
        validate_credentials(platform, creds)
    except ValueError as exc:
        raise ApiError(422, 422, str(exc)) from exc
    return creds


def _merged(current: dict[str, str], incoming: dict[str, str]) -> dict[str, str]:
    try:
        return merge_secret_dict(current, incoming)
    except ValueError as exc:
        raise ApiError(422, 422, str(exc)) from exc


async def _relay_of(session: AsyncSession, bot: Bot) -> RelayServer | None:
    """机器人当前绑定的 relay（未绑定 → None）。"""
    return await session.get(RelayServer, bot.relay_server_id) if bot.relay_server_id else None


def _plain_secrets(cipher: Cipher, bot: Bot) -> tuple[dict[str, str], dict[str, str]]:
    """当前落库的凭证与环境变量明文，只在进程内用于合并与「有没有变」的比对。"""
    return (
        decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD),
        decrypt_json(cipher, bot.env_vars_enc, ENV_AAD),
    )


async def _check_team(session: AsyncSession, team_id: uuid.UUID | None) -> None:
    if team_id is not None and await session.get(Team, team_id) is None:
        raise ApiError(422, 422, "团队不存在")


async def build_out(
    session: AsyncSession,
    cipher: Cipher,
    bot: Bot,
    user: User,
    *,
    include_sensitive: bool = True,
) -> dict[str, Any]:
    """机器人对外视图。列表用 include_sensitive=False：永不带敏感字段与 env 明文。"""
    member_ids = await member_ids_of(session, bot.id)
    perms = permissions_for(user, bot, member_ids)
    relay = await _relay_of(session, bot)
    node = (
        await session.get(RuntimeNode, relay.runtime_node_id)
        if relay and relay.runtime_node_id
        else None
    )
    team = await session.get(Team, bot.team_id) if bot.team_id else None
    creator = await session.get(User, bot.created_by)
    allowed_count = (
        await session.execute(
            select(func.count()).select_from(BotAllowedUser).where(BotAllowedUser.bot_id == bot.id)
        )
    ).scalar_one()
    out: dict[str, Any] = {
        "id": str(bot.id),
        "bot_key": bot.bot_key,
        "platform": bot.platform,
        "name": bot.name,
        "description": bot.description,
        "avatar_url": bot.avatar_url,
        "enabled": bot.enabled,
        "team_id": str(bot.team_id) if bot.team_id else None,
        "team_name": team.name_zh if team else None,
        "created_by": str(bot.created_by),
        "created_by_name": creator.display_name if creator else None,
        "relay_server_id": str(bot.relay_server_id) if bot.relay_server_id else None,
        # 名字无条件给（bot 管理员总得知道自己的机器人跑在哪台上），地址按 relay 可见性给：
        # visibility='admins' 的实例，relay_servers 对普通成员连存在性都不透，这里也不能透。
        "relay_name": (
            f"{node.name} / {relay.model_provider}"
            if node and relay
            else relay.name if relay else None
        ),
        "relay_url": relay.relay_url if relay and relay_visible(user, relay) else None,
        "model": bot.model,
        "backend": backend_of(bot.model, relay.model_provider if relay else None),
        "working_dir": bot.working_dir,
        "verbosity_level": bot.verbosity_level,
        "effort_level": bot.effort_level,
        "sse_timeout_seconds": bot.sse_timeout_seconds,
        "welcome_message": bot.welcome_message,
        "member_count": len(member_ids),
        "allowed_user_count": allowed_count,
        "permissions": dataclasses.asdict(perms),
        "version": bot.version,
        "created_at": bot.created_at,
        "updated_at": bot.updated_at,
    }
    if not include_sensitive:
        return out
    if perms.can_view_sensitive:
        out["system_prompt"] = bot.system_prompt
        # webhook 的 key 就在 URL 里，与凭证同级：只给脱敏值，谁都拿不到明文。
        out["notify_webhook_url"] = mask_secret(bot.notify_webhook_url)
        out["credentials"] = mask_dict(decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD))
        out["env_vars"] = mask_dict(decrypt_json(cipher, bot.env_vars_enc, ENV_AAD))
    if perms.can_view_env_full:
        out["env_vars_full"] = decrypt_json(cipher, bot.env_vars_enc, ENV_AAD)
    return out


def _escape_like(value: str) -> str:
    """ILIKE 通配符转义：用户搜 `_` 时不该匹配任意字符。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("")
async def list_bots(
    request: Request,
    user: User = Depends(current_user),
    scope: Literal["mine", "team", "all"] = "mine",
    keyword: str | None = None,
    platform: str | None = None,
    enabled: bool | None = None,
    relay_server_id: uuid.UUID | None = None,
    model: str | None = None,
    team_id: uuid.UUID | None = None,
    params: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    # created_at 有并列（同一批次创建），补 id 兜底，保证翻页顺序稳定。
    stmt = select(Bot).order_by(Bot.created_at.desc(), Bot.id)
    if scope == "mine":
        stmt = stmt.where(
            or_(
                Bot.created_by == user.id,
                Bot.id.in_(select(BotMember.bot_id).where(BotMember.user_id == user.id)),
            )
        )
    elif scope == "team":
        if user.team_id is None:
            empty: dict[str, Any] = {
                "items": [],
                "total": 0,
                "page": params.page,
                "per_page": params.per_page,
            }
            return {"code": 0, "data": empty}
        stmt = stmt.where(Bot.team_id == user.team_id)
    if keyword:
        like = f"%{_escape_like(keyword)}%"
        stmt = stmt.where(
            or_(
                Bot.name.ilike(like, escape="\\"),
                Bot.bot_key.ilike(like, escape="\\"),
                Bot.description.ilike(like, escape="\\"),
            )
        )
    if platform:
        stmt = stmt.where(Bot.platform == platform)
    if enabled is not None:
        stmt = stmt.where(Bot.enabled == enabled)
    if relay_server_id is not None:
        stmt = stmt.where(Bot.relay_server_id == relay_server_id)
    if model:
        stmt = stmt.where(Bot.model == model)
    if team_id is not None:
        stmt = stmt.where(Bot.team_id == team_id)
    page = await paginate(session, stmt, params)
    rows: list[Bot] = page["items"]
    cipher = _cipher(request)
    items = [await build_out(session, cipher, b, user, include_sensitive=False) for b in rows]
    return {"code": 0, "data": {**page, "items": items}}


@router.post("", status_code=201)
async def create_bot(
    body: BotIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if not can_create_bot(user):
        raise forbidden()
    # 只有 manager 能替别的团队建；其它角色一律落在自己团队（member 到这里必然有团队）。
    team_id = body.team_id if can_reassign_team(user) and body.team_id is not None else user.team_id
    await _check_team(session, team_id)
    relay = await resolve_relay_for_create(session, user, body.relay_server_id, team_id)
    await validate_model_for_relay(session, relay, body.model, body.effort_level)
    if has_masked(body.credentials):
        raise ApiError(422, 422, "新建时凭证不能填脱敏值")
    if has_masked(body.env_vars):
        raise ApiError(422, 422, "新建时环境变量不能填脱敏值")
    if is_masked(body.notify_webhook_url):
        raise ApiError(422, 422, "新建时通知 webhook 不能填脱敏值")
    creds = _checked_credentials(body.platform, body.credentials)
    # bot_key 有唯一约束：先查再 409，别让 IntegrityError 变 500。
    dup = select(Bot.id).where(Bot.bot_key == body.bot_key).limit(1)
    if (await session.execute(dup)).first():
        raise ApiError(409, 409, "机器人标识已存在")
    await reserve_workspace(session, relay.id if relay else None, body.working_dir)
    cipher = _cipher(request)
    if body.platform == "feishu":
        await reserve_feishu_app(session, cipher, creds)
    bot = Bot(
        bot_key=body.bot_key,
        platform=body.platform,
        name=body.name,
        description=body.description,
        avatar_url=body.avatar_url,
        enabled=body.enabled,
        team_id=team_id,
        created_by=user.id,
        relay_server_id=relay.id if relay else None,
        model=body.model,
        working_dir=body.working_dir,
        system_prompt=body.system_prompt,
        # M3 技能合并前，merged 就等于原始提示词。
        merged_system_prompt=body.system_prompt,
        verbosity_level=body.verbosity_level,
        effort_level=body.effort_level,
        sse_timeout_seconds=body.sse_timeout_seconds,
        credentials_enc=encrypt_json(cipher, creds, CREDENTIALS_AAD),
        env_vars_enc=encrypt_json(cipher, body.env_vars, ENV_AAD),
        welcome_message=body.welcome_message,
        notify_webhook_url=body.notify_webhook_url,
    )
    session.add(bot)
    await session.flush()
    await record_audit(
        session,
        action="bot.create",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="bot",
        target_id=str(bot.id),
        diff=diff_dict(
            {}, {**_public(bot), "credentials": "x", "env_vars": "x"}, AUDIT_MASKED_KEYS
        ),
        ip=client_ip(request),
    )
    await notify_bot_changed(session, bot.id)
    await session.commit()
    # enabled / version / 时间戳都是服务端默认值，不刷回来视图里就是 None。
    await session.refresh(bot)
    return {"code": 0, "data": await build_out(session, cipher, bot, user)}


@router.get("/{bot_id}")
async def get_bot(
    bot_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await load_bot(session, bot_id)
    return {"code": 0, "data": await build_out(session, _cipher(request), bot, user)}


@router.patch("/{bot_id}")
async def patch_bot(
    bot_id: uuid.UUID,
    body: BotPatch,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await load_bot(session, bot_id)
    if not can_edit_bot(user, bot, await member_ids_of(session, bot.id)):
        raise forbidden()
    require_if_match(request, bot.version)
    changes = body.changes()
    if not changes:
        raise ApiError(422, 422, "至少修改一个字段")
    # 脱敏值 = 这一项没改（同 credentials/env_vars 的合并口径）；显式 null 仍然是清空。
    if is_masked(changes.get("notify_webhook_url")):
        del changes["notify_webhook_url"]
    if "relay_server_id" in changes:
        raise ApiError(409, 409, "切换运行时必须使用切换入口")
    if "team_id" in changes and not can_reassign_team(user):
        raise forbidden()
    blank = [k for k in NON_NULLABLE if k in changes and changes[k] is None]
    if blank:
        raise ApiError(422, 422, f"字段不能置空：{', '.join(blank)}")
    if "team_id" in changes:
        await _check_team(session, changes["team_id"])
    if "model" in changes or "effort_level" in changes:
        await validate_model_for_relay(
            session,
            await _relay_of(session, bot),
            changes.get("model", bot.model),
            changes.get("effort_level", bot.effort_level),
        )
    if "working_dir" in changes and changes["working_dir"] != bot.working_dir:
        await reserve_workspace(session, bot.relay_server_id, changes["working_dir"], bot_id=bot.id)
    cipher = _cipher(request)
    before = _public(bot)
    before_creds, before_env = _plain_secrets(cipher, bot)
    after_creds, after_env = before_creds, before_env
    incoming_creds: dict[str, str] | None = changes.pop("credentials", None)
    incoming_env: dict[str, str] | None = changes.pop("env_vars", None)
    if incoming_creds is not None:
        after_creds = _checked_credentials(bot.platform, _merged(before_creds, incoming_creds))
        if bot.platform == "feishu":
            await reserve_feishu_app(session, cipher, after_creds, bot_id=bot.id)
        bot.credentials_enc = encrypt_json(cipher, after_creds, CREDENTIALS_AAD)
    if incoming_env is not None:
        after_env = _merged(before_env, incoming_env)
        bot.env_vars_enc = encrypt_json(cipher, after_env, ENV_AAD)
    for key, value in changes.items():
        setattr(bot, key, value)
    if "system_prompt" in changes:
        from coreman.core.knowledge.installation import rebuild_prompt

        with session.no_autoflush:
            await rebuild_prompt(session, bot)
    # 换模型（可能连后端都换了）或换工作目录 = 换上下文，旧 relay 会话必须作废（spec §8.5）。
    # 以「值真的变了」为准：把原值重新提交一遍不该把用户正在进行的对话清掉。
    if before["model"] != bot.model or before["working_dir"] != bot.working_dir:
        await sessions.clear_bot(session, bot.id)
    diff = diff_dict(before, _public(bot), AUDIT_MASKED_KEYS)
    if after_creds != before_creds:
        diff["credentials"] = ["***", "***"]
    if after_env != before_env:
        diff["env_vars"] = ["***", "***"]
    await record_audit(
        session,
        action="bot.update",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="bot",
        target_id=str(bot.id),
        diff=diff,
        ip=client_ip(request),
    )
    await notify_bot_changed(session, bot.id)
    await session.commit()
    await session.refresh(bot)
    set_etag(response, bot.version)
    return {"code": 0, "data": await build_out(session, cipher, bot, user)}


@router.delete("/{bot_id}")
async def delete_bot(
    bot_id: uuid.UUID,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    bot = await load_bot(session, bot_id)
    if not can_delete_bot(user, bot):
        raise forbidden()
    snapshot = _public(bot)
    await session.delete(bot)
    await record_audit(
        session,
        action="bot.delete",
        actor_id=user.id,
        actor_login=user.login_name,
        target_type="bot",
        target_id=str(bot_id),
        diff=diff_dict(snapshot, {}, AUDIT_MASKED_KEYS),
        ip=client_ip(request),
    )
    # chat_sessions / user_reached 由外键 ON DELETE CASCADE 一并清掉，不用手动删。
    await notify_bot_changed(session, bot_id)
    await session.commit()
    return {"code": 0, "data": {"id": str(bot_id)}}
