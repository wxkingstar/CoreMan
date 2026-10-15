"""技能本地安装：登录用户拿一条带时效的安装命令；平台管理员可导出技能的明文环境变量。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.deps import client_ip, get_session
from coreman.api.errors import ApiError, forbidden, not_found
from coreman.api.routers.feishu_personal import interactive_user
from coreman.api.routers.skill_catalog import PRESET_AAD, live_skill
from coreman.api.security import verify_csrf
from coreman.core.audit import record_audit
from coreman.core.bots.secrets import decrypt_json
from coreman.core.db.models import EnvPreset, Skill, SkillSource, User
from coreman.core.knowledge import local_install
from coreman.core.knowledge.git_auth import SOURCE_TOKEN_AAD, https_repository
from coreman.core.knowledge.skill_policy import selected_groups
from coreman.core.logging import get_logger
from coreman.core.timeutils import utcnow

router = APIRouter(prefix="/api/admin", tags=["skills"], dependencies=[Depends(verify_csrf)])
public_router = APIRouter(tags=["skills"])
log = get_logger(__name__)
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class RedactInstallAccess(logging.Filter):
    """访问日志不留安装令牌。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            if isinstance(args[2], str) and args[2].startswith(local_install.SCRIPT_PATH):
                args[2] = local_install.SCRIPT_PATH + "[REDACTED]"
                record.args = tuple(args)
        return True


logging.getLogger("uvicorn.access").addFilter(RedactInstallAccess())


class EnvExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_env_groups: list[str] = Field(default_factory=list, max_length=100)
    data_source: str | None = None


def installable(skill: Skill | None) -> Skill:
    if skill is None or skill.deleted_at is not None:
        raise not_found("技能不存在")
    if not skill.enabled:
        raise ApiError(409, 409, "技能已停用")
    if skill.install_type != "git":
        raise ApiError(422, 422, "MCP 技能不支持本地安装")
    return skill


@router.post("/skills/{identity}/local-install")
async def local_install_command(
    identity: uuid.UUID,
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """任何登录用户都可以拿；命令里的令牌绑定本人和技能，30 分钟内可重复执行。"""
    skill = installable(await session.get(Skill, identity))
    now = utcnow()
    token = local_install.seal_token(request.app.state.cipher, actor.id, skill.id, now)
    response.headers.update(NO_STORE)
    return {
        "code": 0,
        "data": {
            "command": local_install.install_command(
                request.app.state.settings.public_base_url, token
            ),
            "expires_at": now + local_install.TOKEN_TTL,
        },
    }


async def token_skill(session: AsyncSession, request: Request, token: str) -> tuple[User, Skill]:
    try:
        user_id, skill_id = local_install.open_token(request.app.state.cipher, token, utcnow())
    except ValueError as exc:
        if str(exc) == "expired":
            raise ApiError(410, 410, "安装命令已过期，请回到 CoreMan 重新复制") from None
        raise not_found("安装命令无效") from None
    user = await session.get(User, user_id)
    if user is None or user.status != "active":
        raise forbidden()
    return user, installable(await session.get(Skill, skill_id))


@public_router.get(local_install.SCRIPT_PATH + "{token}")
async def install_script(
    token: str, request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    _, skill = await token_skill(session, request, token)
    archive = (
        request.app.state.settings.public_base_url.rstrip("/")
        + local_install.SCRIPT_PATH
        + token
        + "/archive"
    )
    return Response(
        local_install.install_script(skill.name, archive),
        media_type="text/x-shellscript",
        headers=NO_STORE,
    )


@public_router.get(local_install.SCRIPT_PATH + "{token}/archive")
async def install_archive(
    token: str, request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    """服务端代为克隆技能仓库，只下发这个技能的目录。"""
    user, skill = await token_skill(session, request, token)
    source = await session.get(SkillSource, skill.source_id)
    url = skill.external_repo_url or (source.git_url if source else None)
    if source is None or not url:
        raise ApiError(409, 409, "技能没有 Git 来源")
    url = local_install.repository(url)
    access_token = username = None
    # 来源令牌只用于来源自己的仓库，与员工安装一致。
    if source.access_token_enc:
        if not source.git_url or https_repository(url) != https_repository(source.git_url):
            raise ApiError(409, 409, "技能仓库与来源不一致，无法使用来源的访问令牌")
        access_token = request.app.state.cipher.decrypt(source.access_token_enc, SOURCE_TOKEN_AAD)
        username = source.git_username
    name, skill_id, user_id, login = skill.name, skill.id, user.id, user.login_name
    # 克隆可能要几十秒，不占着数据库事务。
    await session.commit()
    try:
        data = await asyncio.to_thread(local_install.fetch_skill, url, name, access_token, username)
    except LookupError:
        raise ApiError(404, 404, f"仓库里没有找到技能 {name}（按 SKILL.md 的 name 匹配）") from None
    except OverflowError:
        raise ApiError(413, 413, "技能目录超过 50 MB，不支持本地安装") from None
    except (ValueError, OSError) as exc:
        log.warning("skill_local_fetch_failed", skill=name, error_type=type(exc).__name__)
        raise ApiError(
            502, 502, "服务端读取技能仓库失败，请联系技能管理员检查来源的仓库地址与访问凭证"
        ) from None
    await record_audit(
        session,
        action="skill.local_install",
        actor_id=user_id,
        actor_login=login,
        target_type="skill_config",
        target_id=str(skill_id),
        ip=client_ip(request),
    )
    await session.commit()
    return Response(
        data,
        media_type="application/gzip",
        headers={**NO_STORE, "Content-Disposition": f'attachment; filename="{name}.tar.gz"'},
    )


@router.post("/skills/{identity}/env-exports")
async def env_exports(
    identity: uuid.UUID,
    body: EnvExportIn,
    request: Request,
    response: Response,
    actor: User = Depends(interactive_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """技能的环境组合并成 export 语句，含明文，仅平台管理员；每次查看都记审计。

    组合方式与员工安装相同：固定环境组加所选数据库（Doris 数据源取 `_doris` 组）。
    """
    if actor.role != "platform_admin":
        raise forbidden()
    skill = await live_skill(session, identity)
    try:
        keys = selected_groups(skill, body.selected_env_groups, body.data_source)
    except ValueError as exc:
        raise ApiError(422, 422, str(exc)) from None
    groups: list[tuple[str, str, dict[str, str]]] = []
    merged: dict[str, str] = {}
    for key in keys:
        preset = await session.get(EnvPreset, key)
        if preset is None:
            raise ApiError(409, 409, f"环境组 {key} 不存在")
        values = decrypt_json(request.app.state.cipher, preset.vars_enc, PRESET_AAD)
        for name, value in values.items():
            if merged.setdefault(name, value) != value:
                raise ApiError(409, 409, f"环境组之间变量 {name} 的取值冲突")
        groups.append((key, preset.label, values))
    text = local_install.export_lines(skill.name, groups, skill.user_env_vars)
    await record_audit(
        session,
        action="skill.env_export",
        actor_id=actor.id,
        actor_login=actor.login_name,
        target_type="skill_config",
        target_id=str(skill.id),
        diff={"env_groups": [None, keys]},
        ip=client_ip(request),
    )
    await session.commit()
    response.headers.update(NO_STORE)
    return {"code": 0, "data": {"text": text}}
