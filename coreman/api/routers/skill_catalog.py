"""技能目录、来源与加密环境预设。"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.bot_names import bot_names
from coreman.api.deps import client_ip, current_user, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.pagination import PageParams, paginate
from coreman.api.security import verify_csrf
from coreman.api.versioning import require_if_match
from coreman.core.audit import record_audit
from coreman.core.bots.events import notify_bot_changed
from coreman.core.bots.secrets import decrypt_json, encrypt_json, mask_dict, merge_secret_dict
from coreman.core.db.models import (
    BotSkill,
    EnvPreset,
    GlobalSkill,
    Skill,
    SkillApproval,
    SkillSource,
    User,
)
from coreman.core.knowledge import global_skills
from coreman.core.knowledge import skill_policy as policy
from coreman.core.knowledge.catalog_sync import CatalogAccessError, fetch_catalog, sync_catalog
from coreman.core.knowledge.git_auth import SOURCE_TOKEN_AAD, git_username, https_repository
from coreman.core.masking import mask_secret
from coreman.core.timeutils import utcnow

router = APIRouter(prefix="/api/admin", tags=["skills"], dependencies=[Depends(verify_csrf)])
PRESET_AAD = "env_presets.vars_enc"
MCP_AAD = "skills.mcp_config_enc"


def manager(actor: User) -> bool:
    return actor.role in ("ai_committee", "platform_admin")


def require_manager(actor: User) -> None:
    if not manager(actor):
        raise forbidden()


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    git_url: str | None = Field(default=None, max_length=1000)
    categories: dict[str, str] = Field(default_factory=dict, max_length=100)
    sort_order: int = Field(default=0, ge=0, le=10000)
    access_token: str | None = Field(default=None, max_length=2000, repr=False)
    remove_access_token: bool = False
    git_username: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def token_valid(self) -> SourceIn:
        if self.access_token:
            if self.remove_access_token or not self.git_url:
                raise ValueError("请填写仓库地址，且不能同时设置和移除 token")
            if (
                any(c.isspace() or ord(c) < 32 for c in self.access_token)
                or "•" in self.access_token
            ):
                raise ValueError("Project Access Token 格式无效，请填写完整值")
        return self

    @field_validator("key")
    @classmethod
    def key_valid(cls, value: str) -> str:
        return policy.name(value)

    @field_validator("git_url")
    @classmethod
    def url_valid(cls, value: str | None) -> str | None:
        return policy.git_url(value)

    @field_validator("git_username")
    @classmethod
    def username_valid(cls, value: str | None) -> str | None:
        return git_username(value.strip() if value else value)


class EnvField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=200)
    placeholder: str = Field(default="", max_length=500)
    required: bool = False


class SkillIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    source_id: uuid.UUID
    description: str = Field(default="", max_length=8000)
    category: str | None = Field(default=None, max_length=100)
    security_level: Literal["public", "internal"] = "public"
    version: str | None = Field(default=None, max_length=100)
    env_groups: list[str] = Field(default_factory=list, max_length=100)
    selectable_env_groups: dict[str, str] = Field(default_factory=dict, max_length=100)
    data_sources: dict[str, str] | None = None
    default_data_source: str | None = None
    doris_enabled_groups: list[str] = Field(default_factory=list, max_length=100)
    user_env_vars: dict[str, EnvField] = Field(default_factory=dict, max_length=100)
    install_type: Literal["git", "mcp"] = "git"
    external_repo_url: str | None = Field(default=None, max_length=1000)
    mcp_config: dict[str, Any] | None = None
    # 缺省（旧页面、管理命令）保留原值；只有明确传入才改。
    mcp_oauth: bool | None = None
    security_prompt_template: str | None = Field(default=None, max_length=16000)
    enabled: bool = False

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return policy.name(value)

    @field_validator("external_repo_url")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        return policy.git_url(value)

    @model_validator(mode="after")
    def valid_config(self) -> SkillIn:
        for key in self.env_groups + list(self.selectable_env_groups):
            policy.name(key)
        policy.env_vars({key: "" for key in self.user_env_vars})
        if len(self.env_groups) != len(set(self.env_groups)):
            raise ValueError("固定环境组不能重复")
        if set(self.doris_enabled_groups) - set(self.selectable_env_groups):
            raise ValueError("Doris 环境组不在可选数据库中")
        if self.data_sources and not set(self.data_sources) <= {"mysql", "doris"}:
            raise ValueError("数据源仅支持 MySQL/Doris")
        if self.default_data_source and self.default_data_source not in (self.data_sources or {}):
            raise ValueError("默认数据源不在选项中")
        if self.mcp_config is not None:
            policy.mcp_config(self.mcp_config)
        return self


class SkillStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class GlobalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_global: bool
    # 取消全局时：false 卸载随全局装上的（默认），true 改按手动安装保留在各员工处。
    keep_installed: bool = False


class PresetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_key: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    vars: dict[str, str] = Field(max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("group_key")
    @classmethod
    def valid_key(cls, value: str) -> str:
        return policy.name(value)

    @field_validator("vars")
    @classmethod
    def valid_vars(cls, values: dict[str, str]) -> dict[str, str]:
        return policy.env_vars(values)


def source_out(row: SkillSource) -> dict[str, Any]:
    return {
        **{
            key: getattr(row, key)
            for key in (
                "id",
                "key",
                "label",
                "git_url",
                "git_username",
                "categories",
                "sort_order",
                "version",
            )
        },
        "has_access_token": bool(row.access_token_enc),
    }


def apply_source(row: SkillSource, body: SourceIn, request: Request) -> None:
    if row.access_token_enc and not body.access_token and not body.remove_access_token:
        if (
            not body.git_url
            or not row.git_url
            or https_repository(body.git_url) != https_repository(row.git_url)
        ):
            raise ApiError(422, 422, "仓库地址已变更，请为新仓库重新填写 token 或移除原 token")
    for key, value in body.model_dump(exclude={"access_token", "remove_access_token"}).items():
        setattr(row, key, value)
    if body.remove_access_token:
        row.access_token_enc = None
    elif body.access_token:
        row.access_token_enc = request.app.state.cipher.encrypt(body.access_token, SOURCE_TOKEN_AAD)


def skill_out(row: Skill, *, is_global: bool = False) -> dict[str, Any]:
    names = tuple(SkillIn.model_fields.keys())
    return {
        "id": row.id,
        "revision": row.revision,
        "has_mcp_config": bool(row.mcp_config_enc),
        "is_global": is_global,
        **{key: getattr(row, key) for key in names if key != "mcp_config"},
    }


async def audited(
    session: AsyncSession,
    request: Request,
    actor: User,
    action: str,
    identity: str,
    diff: dict[str, Any] | None = None,
) -> None:
    await record_audit(
        session,
        actor_id=actor.id,
        actor_login=actor.login_name,
        action=action,
        target_type="skill_config",
        target_id=identity,
        diff=diff,
        ip=client_ip(request),
    )
    await session.commit()


@router.get("/skill-sources")
async def sources(
    actor: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    return {
        "code": 0,
        "data": [
            source_out(row)
            for row in await session.scalars(
                select(SkillSource).order_by(SkillSource.sort_order, SkillSource.key)
            )
        ],
    }


@router.post("/skill-sources")
async def create_source(
    body: SourceIn,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_manager(actor)
    row = SkillSource()
    apply_source(row, body, request)
    session.add(row)
    await session.flush()
    await audited(session, request, actor, "skill_source.create", str(row.id))
    return {"code": 0, "data": source_out(row)}


@router.put("/skill-sources/{identity}")
async def update_source(
    identity: uuid.UUID,
    body: SourceIn,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_manager(actor)
    row = await session.get(SkillSource, identity)
    if row is None:
        raise not_found("技能来源不存在")
    require_if_match(request, row.version)
    apply_source(row, body, request)
    row.updated_at = utcnow()
    await audited(session, request, actor, "skill_source.update", str(row.id))
    return {"code": 0, "data": source_out(row)}


@router.post("/skill-sources/{identity}/sync")
async def sync_source(
    identity: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_manager(actor)
    row = await session.get(SkillSource, identity)
    if row is None:
        raise not_found("技能来源不存在")
    require_if_match(request, row.version)
    if not row.git_url:
        raise ApiError(422, 422, "请先为来源填写 Git 仓库地址")
    url, username = row.git_url, row.git_username
    token = (
        request.app.state.cipher.decrypt(row.access_token_enc, SOURCE_TOKEN_AAD)
        if row.access_token_enc
        else None
    )
    # Do not retain a database transaction while fetching the remote manifest.
    await session.commit()
    try:
        entries = (
            await asyncio.to_thread(fetch_catalog, url, token, username)
            if token
            else await asyncio.to_thread(fetch_catalog, url)
        )
    except CatalogAccessError as exc:
        raise ApiError(422, 422, str(exc)) from None
    except (ValueError, OSError):
        raise ApiError(
            422,
            422,
            "无法同步来源，请检查服务端 Git、仓库读取权限，以及 marketplace/plugin 清单格式。",
        ) from None
    await session.refresh(actor)
    if actor.status != "active":
        raise forbidden()
    require_manager(actor)
    await session.refresh(row, with_for_update=True)
    require_if_match(request, row.version)
    if row.git_url != url:
        raise ApiError(409, 409, "技能来源已变更，请刷新后重新同步")
    try:
        result = await sync_catalog(
            session,
            source_key=row.key,
            url=url,
            entries=entries,
            cipher=request.app.state.cipher,
            actor=actor,
        )
    except ValueError:
        raise ApiError(
            409, 409, "同步冲突：来源已变化或技能名称属于其他来源，请检查后重试"
        ) from None
    await session.commit()
    return {"code": 0, "data": result}


@router.get("/skills")
async def list_skills(
    actor: User = Depends(current_user),
    params: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    query = select(Skill).where(Skill.deleted_at.is_(None))
    if not manager(actor):
        query = query.where(Skill.enabled)
    page = await paginate(session, query.order_by(Skill.name), params)
    ids = await global_skills.global_ids(session)
    items = [skill_out(row, is_global=row.id in ids) for row in page["items"]]
    return {"code": 0, "data": {**page, "items": items}}


async def apply_skill(session: AsyncSession, row: Skill, body: SkillIn, request: Request) -> None:
    source = await session.get(SkillSource, body.source_id)
    if source is None:
        raise ApiError(422, 422, "技能来源不存在")
    if body.install_type == "git" and not (body.external_repo_url or source.git_url):
        raise ApiError(422, 422, "技能没有 Git 来源")
    if body.install_type == "mcp" and body.mcp_config is None and not row.mcp_config_enc:
        raise ApiError(422, 422, "MCP 技能需要配置")
    oauth = bool(row.mcp_oauth) if body.mcp_oauth is None else body.mcp_oauth
    if body.install_type == "mcp" and oauth:
        config = body.mcp_config
        if config is None:
            config = json.loads(request.app.state.cipher.decrypt(row.mcp_config_enc, MCP_AAD))
        if config.get("type") not in ("http", "sse"):
            raise ApiError(422, 422, "只有远程（http/sse）MCP 服务可以用 OAuth 授权")
    for key, value in body.model_dump(exclude={"mcp_config", "mcp_oauth"}).items():
        setattr(row, key, value)
    row.mcp_oauth = oauth
    if body.mcp_config is not None:
        row.mcp_config_enc = request.app.state.cipher.encrypt(
            json.dumps(body.mcp_config, ensure_ascii=False), MCP_AAD
        )
    if body.install_type != "mcp":
        row.mcp_config_enc, row.mcp_oauth = None, False
    row.updated_at = utcnow()


async def live_skill(session: AsyncSession, identity: uuid.UUID, *, lock: bool = False) -> Skill:
    row = await session.get(Skill, identity, with_for_update=lock)
    if row is None or row.deleted_at is not None:
        raise not_found("技能不存在")
    return row


@router.post("/skills")
async def create_skill(
    body: SkillIn,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """新建技能；与已删除技能同名时复用那一行，等于恢复并按本次内容重新配置。"""
    require_manager(actor)
    row = await session.scalar(
        select(Skill)
        .where(Skill.name == body.name, Skill.deleted_at.is_not(None))
        .with_for_update()
    )
    action = "skill.restore" if row else "skill.create"
    if row is None:
        row = Skill()
    else:
        # 恢复不继承删除前的 MCP 密文与标记：和新建一样，由本次提交的配置决定。
        row.deleted_at, row.mcp_config_enc, row.mcp_oauth = None, None, False
    await apply_skill(session, row, body, request)
    session.add(row)
    await session.flush()
    await audited(session, request, actor, action, str(row.id))
    return {"code": 0, "data": skill_out(row)}


@router.put("/skills/{identity}")
async def update_skill(
    identity: uuid.UUID,
    body: SkillIn,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_manager(actor)
    row = await live_skill(session, identity)
    require_if_match(request, row.revision)
    await apply_skill(session, row, body, request)
    is_global = await global_skills.is_global(session, row.id)
    # 停用照常允许（紧急止损）；其余会让员工没法自动装上的改动，须先取消全局。
    if is_global and row.enabled and (reason := global_skills.problem(row)):
        raise ApiError(409, 409, f"这是全局技能，{reason}；请先取消全局再修改")
    await audited(session, request, actor, "skill.update", str(row.id))
    return {"code": 0, "data": skill_out(row, is_global=is_global)}


@router.patch("/skills/{identity}")
async def set_skill_status(
    identity: uuid.UUID,
    body: SkillStatusIn,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """列表里的启用开关：只改 enabled，不必回传整份目录配置。

    和整份保存一样会递增 revision，待审申请与排队中的安装因此作废，须重新申请。
    """
    require_manager(actor)
    row = await live_skill(session, identity)
    require_if_match(request, row.revision)
    if row.enabled != body.enabled:
        diff = {"enabled": [row.enabled, body.enabled]}
        row.enabled, row.updated_at = body.enabled, utcnow()
        action = "skill.enable" if body.enabled else "skill.disable"
        await audited(session, request, actor, action, str(row.id), diff)
    return {
        "code": 0,
        "data": skill_out(row, is_global=await global_skills.is_global(session, row.id)),
    }


@router.delete("/skills/{identity}")
async def delete_skill(
    identity: uuid.UUID,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """从目录删除技能。只留墓碑不删行：来源再同步时跳过它，安装与审批历史照常可查。

    仍有 AI 员工安装着（含安装中、安装未完成）或有待审申请的技能不能删，须先在员工处停用。
    行锁与安装申请的共享锁互斥，删除和新的安装申请只有一个能成功。
    """
    require_manager(actor)
    row = await live_skill(session, identity, lock=True)
    require_if_match(request, row.revision)
    if await global_skills.is_global(session, row.id):
        raise ApiError(409, 409, "这是全局技能，请先取消全局再删除")
    in_use = set(
        await session.scalars(
            select(BotSkill.bot_id).where(
                BotSkill.skill_id == row.id, BotSkill.status != "uninstalled"
            )
        )
    ) | set(
        await session.scalars(
            select(SkillApproval.bot_id).where(
                SkillApproval.skill_id == row.id, SkillApproval.status == "pending"
            )
        )
    )
    if in_use:
        names = sorted((await bot_names(session, in_use)).values())
        shown = "、".join(names[:5]) + (" 等" if len(names) > 5 else "")
        raise ApiError(
            409,
            409,
            f"仍有 {len(in_use)} 个 AI 员工在用或正在申请该技能（{shown}），"
            "请先在这些员工的技能中停用 / 撤回后再删除",
        )
    diff = {"name": [row.name, None], "enabled": [row.enabled, False]}
    row.deleted_at = row.updated_at = utcnow()
    row.enabled = False
    await audited(session, request, actor, "skill.delete", str(row.id), diff)
    return {"code": 0, "data": None}


@router.get("/env-presets")
async def presets(
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_manager(actor)
    rows = await session.scalars(select(EnvPreset).order_by(EnvPreset.group_key))
    return {
        "code": 0,
        "data": [
            {
                "group_key": row.group_key,
                "label": row.label,
                "vars": mask_dict(decrypt_json(request.app.state.cipher, row.vars_enc, PRESET_AAD)),
                "tags": row.tags,
                "version": row.version,
            }
            for row in rows
        ],
    }


@router.put("/env-presets/{key}")
async def put_preset(
    key: str,
    body: PresetIn,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_manager(actor)
    if key != body.group_key:
        raise ApiError(422, 422, "环境组标识不一致")
    row = await session.get(EnvPreset, key)
    require_if_match(request, row.version if row else 0)
    cipher = request.app.state.cipher
    current = decrypt_json(cipher, row.vars_enc, PRESET_AAD) if row else {}
    try:
        # A mask is a keep-value marker, never an editable part of a secret.
        for name, value in body.vars.items():
            if "•" in value and (name not in current or value != mask_secret(current[name])):
                raise ValueError("modified_mask")
        values = merge_secret_dict(current, body.vars)
    except ValueError:
        raise ApiError(
            422, 422, "掩码已被修改或没有对应原值，请重新打开编辑保留原值，或填写完整新值"
        ) from None
    if row is None:
        row = EnvPreset(group_key=key)
        session.add(row)
    row.label, row.tags = body.label, body.tags
    row.vars_enc, row.updated_at = encrypt_json(cipher, values, PRESET_AAD), utcnow()
    await audited(session, request, actor, "env_preset.save", key)
    return {
        "code": 0,
        "data": {
            "group_key": key,
            "label": row.label,
            "vars": mask_dict(values),
            "tags": row.tags,
            "version": row.version,
        },
    }


@router.put("/skills/{identity}/global")
async def set_global(
    identity: uuid.UUID,
    body: GlobalIn,
    request: Request,
    actor: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """设为或取消全局技能。

    设为全局（含重复勾选）时立即给所有已就绪的普通员工排安装，并重试之前失败的；
    之后新建、新分到运行时的员工由调度器定期补装。手动装过的员工不动。
    取消全局只处理随全局装上的行：默认卸载，keep_installed 时改按手动安装保留。
    """
    require_manager(actor)
    # 共享锁与目录删除、整份保存互斥。
    row = await session.get(Skill, identity, with_for_update={"read": True})
    if row is None or row.deleted_at is not None:
        raise not_found("技能不存在")
    entry = await session.get(GlobalSkill, identity, with_for_update=True)
    cipher = request.app.state.cipher
    data: dict[str, Any]
    if body.is_global:
        if reason := global_skills.problem(row):
            raise ApiError(422, 422, reason)
        if entry is None:
            session.add(GlobalSkill(skill_id=identity, set_by=actor.id))
        else:
            # 重新勾选的人接替自动补装的署名：设置人离开后靠这个续上。
            entry.set_by, entry.set_at = actor.id, utcnow()
        await session.flush()
        queued, skipped = await global_skills.apply(session, cipher, row, actor, retry_failed=True)
        names = await bot_names(session, [*queued, *(bot for bot, _ in skipped)])
        data = {
            "is_global": True,
            "queued": [{"bot_id": bot, "bot_name": names.get(bot)} for bot in queued],
            "skipped": [
                {"bot_id": bot, "bot_name": names.get(bot), "reason": reason}
                for bot, reason in skipped
            ],
        }
        diff = {"is_global": [entry is not None, True], "queued": [None, len(queued)]}
        action = "skill.global"
    else:
        if entry is not None:
            await session.delete(entry)
        removed = await global_skills.release(
            session, identity, actor, keep_installed=body.keep_installed
        )
        for bot_id in removed:
            await notify_bot_changed(session, bot_id)
        names = await bot_names(session, removed)
        data = {
            "is_global": False,
            "uninstalled": [{"bot_id": bot, "bot_name": names.get(bot)} for bot in removed],
        }
        diff = {"is_global": [entry is not None, False], "uninstalled": [None, len(removed)]}
        action = "skill.unglobal"
    await audited(session, request, actor, action, str(identity), diff)
    return {"code": 0, "data": data}
