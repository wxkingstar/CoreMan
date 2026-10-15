"""受限清单读取：仅导入展示元数据，不执行插件代码、不改变授权策略。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.audit import record_audit
from coreman.core.crypto import Cipher
from coreman.core.db.models import Skill, SkillSource, User
from coreman.core.knowledge.git_auth import https_repository, token_git_env
from coreman.core.knowledge.installation import MCP_AAD
from coreman.core.knowledge.skill_policy import git_url, mcp_config, mcp_needs_oauth, name


class CatalogAccessError(ValueError):
    """Fixed, credential-free diagnostics for repository authentication failures."""


# 服务器不认令牌时，有的直接回 401，有的让 Git 再要用户名，而终端提示已关闭。
AUTH_FAILURE_MARKERS = (
    b"401",
    b"http basic: access denied",
    b"authentication failed",
    b"could not read username",
    b"could not read password",
    b"terminal prompts disabled",
)
AUTH_FAILURE_MESSAGE = (
    "Git 认证失败。请检查 token 是否有效、是否过期或已撤销、是否有读取仓库的权限，"
    "以及 Git 用户名：GitLab 留空（使用 oauth2），阿里云云效 Codeup 填令牌所属的云效账号名。"
)


def read_json(root: Path, path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError("清单路径越界或文件不存在")
    if resolved.stat().st_size > 256 * 1024:
        raise ValueError("清单超过 256 KiB")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("清单必须为对象")
    return value


def read_mcp(root: Path, plugin_dir: Path, plugin: dict[str, Any]) -> dict[str, Any] | None:
    """插件声明的 MCP 服务，沿用 Claude Code 插件格式：plugin.json 的 mcpServers
    （对象或插件内相对路径），缺省时读插件目录的 .mcp.json。一个插件只登记一个服务；
    插件里有 SKILL.md 时仍是 Git 技能，MCP 声明不导入。"""
    if any("SKILL.md" in files for _, _, files in os.walk(plugin_dir)):
        return None
    declared = plugin.get("mcpServers")
    if isinstance(declared, str):
        relative = Path(declared)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("MCP 配置路径不能越界")
        declared = read_json(root, plugin_dir / relative)
    elif declared is None:
        if not (plugin_dir / ".mcp.json").exists():
            return None
        declared = read_json(root, plugin_dir / ".mcp.json")
    if not isinstance(declared, dict):
        raise ValueError("mcpServers 必须为对象")
    servers = declared.get("mcpServers", declared)
    if not isinstance(servers, dict) or len(servers) != 1:
        raise ValueError("一个插件只能声明一个 MCP 服务")
    (config,) = servers.values()
    if not isinstance(config, dict):
        raise ValueError("MCP 配置必须为对象")
    return mcp_config(config)


def read_catalog(root: Path) -> list[dict[str, Any]]:
    manifest = read_json(root, root / ".claude-plugin/marketplace.json")
    plugins = manifest.get("plugins")
    if not isinstance(plugins, list) or len(plugins) > 500:
        raise ValueError("插件清单无效或超过 500 项")
    result = []
    seen: set[str] = set()
    for entry in plugins:
        if not isinstance(entry, dict) or not isinstance(entry.get("source"), str):
            raise ValueError("目前仅支持仓库内插件目录")
        relative = Path(entry["source"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("插件目录不能越界")
        plugin = read_json(root, root / relative / ".claude-plugin/plugin.json")
        key = name(plugin.get("name", ""))
        if key in seen or (entry.get("name") and entry["name"] != key):
            raise ValueError("插件名称重复或与清单不一致")
        seen.add(key)
        description, version = plugin.get("description", ""), plugin.get("version")
        if not isinstance(description, str) or len(description) > 4000:
            raise ValueError("插件描述无效或过长")
        if version is not None and (not isinstance(version, str) or len(version) > 100):
            raise ValueError("插件版本无效")
        result.append(
            {
                "name": key,
                "description": description,
                "version": version,
                "mcp_config": read_mcp(root, root / relative, plugin),
            }
        )
    return result


def clone_repository(
    url: str, target: Path, access_token: str | None = None, username: str | None = None
) -> None:
    """浅克隆到 target。失败只给固定提示，不回显 Git 输出。"""
    git_url(url)
    # 不继承全局 Git hook/credential helper、模板或代理，仓库对象不是代码指令。
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and "proxy" not in key.lower()
    }
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_LFS_SKIP_SMUDGE="1",
        # 报错按英文识别，中文环境下的 Git 会把 "Authentication failed" 等译掉。
        LANGUAGE="en",
    )
    if access_token:
        url = https_repository(url)
        env.update(token_git_env(url, access_token, username))
    with tempfile.TemporaryFile() as errors:
        try:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "protocol.file.allow=never",
                    "-c",
                    "protocol.ext.allow=never",
                    "-c",
                    "http.followRedirects=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "clone",
                    "--depth",
                    "1",
                    "--template=",
                    "--",
                    url,
                    str(target),
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errors,
                timeout=180,
                check=True,
            )
        except subprocess.CalledProcessError:
            errors.seek(0)
            diagnostic = errors.read(8192).lower()
            if any(marker in diagnostic for marker in (b"403", b"not allowed to download code")):
                raise CatalogAccessError(
                    "Git 仓库拒绝读取（403）。请检查 Project Access Token "
                    "的 read_repository 权限，"
                    "并确认项目角色至少为 Reporter。"
                ) from None
            if any(marker in diagnostic for marker in AUTH_FAILURE_MARKERS):
                raise CatalogAccessError(AUTH_FAILURE_MESSAGE) from None
            raise ValueError("无法读取技能仓库，请检查 Git、地址和访问权限") from None
        except (subprocess.SubprocessError, OSError):
            raise ValueError("无法读取技能仓库，请检查 Git、地址和访问权限") from None


def fetch_catalog(
    url: str, access_token: str | None = None, username: str | None = None
) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="coreman-catalog-") as directory:
        root = Path(directory) / "repo"
        clone_repository(url, root, access_token, username)
        return read_catalog(root)


async def sync_catalog(
    session: AsyncSession,
    *,
    source_key: str,
    url: str,
    entries: list[dict[str, Any]],
    cipher: Cipher,
    actor: User | None = None,
) -> dict[str, int]:
    source = await session.scalar(
        select(SkillSource).where(SkillSource.key == source_key).with_for_update()
    )
    if source is None or source.git_url != url:
        raise ValueError("来源未登记或地址不一致，请先在管理台登记来源")
    created = updated = skipped = 0
    for entry in entries:
        skill = await session.scalar(
            select(Skill).where(Skill.name == entry["name"]).with_for_update()
        )
        if skill is not None and skill.deleted_at is not None:
            # 管理员删除过的技能不再导入；要恢复就在目录里用同名新建。
            skipped += 1
            continue
        if skill is not None and skill.source_id != source.id:
            raise ValueError("插件名称已属于其他来源，同步已撤销")
        # 声明了 MCP 的插件登记为 MCP 技能，配置以仓库为准；其余仍按 Git 技能安装。
        mcp = entry.get("mcp_config")
        install_type = "mcp" if mcp else "git"
        oauth = mcp is not None and mcp_needs_oauth(mcp)
        if skill is None:
            skill = Skill(
                name=entry["name"],
                source_id=source.id,
                enabled=False,
                description=entry["description"],
                version=entry["version"],
                install_type=install_type,
                mcp_oauth=oauth,
                mcp_config_enc=(
                    cipher.encrypt(json.dumps(mcp, ensure_ascii=False), MCP_AAD) if mcp else None
                ),
            )
            session.add(skill)
            created += 1
        elif (
            skill.description != entry["description"]
            or skill.version != entry["version"]
            or skill.install_type != install_type
            or skill.mcp_oauth != oauth
            or (
                mcp
                and (
                    not skill.mcp_config_enc
                    or json.loads(cipher.decrypt(skill.mcp_config_enc, MCP_AAD)) != mcp
                )
            )
        ):
            skill.description, skill.version = entry["description"] or "", entry["version"]
            skill.install_type, skill.mcp_oauth = install_type, oauth
            skill.mcp_config_enc = (
                cipher.encrypt(json.dumps(mcp, ensure_ascii=False), MCP_AAD) if mcp else None
            )
            updated += 1
    await record_audit(
        session,
        action="skill.catalog_sync",
        actor_id=actor.id if actor else None,
        actor_login=actor.login_name if actor else "cli",
        target_type="skill_source",
        target_id=str(source.id),
        diff={
            "created": [None, created],
            "updated": [None, updated],
            "skipped": [None, skipped],
        },
    )
    await session.flush()
    return {
        "created": created,
        "updated": updated,
        "unchanged": len(entries) - created - updated - skipped,
        "skipped": skipped,
    }
