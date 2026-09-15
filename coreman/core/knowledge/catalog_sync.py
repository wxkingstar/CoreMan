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
from coreman.core.db.models import Skill, SkillSource, User
from coreman.core.knowledge.git_auth import https_repository, token_git_env
from coreman.core.knowledge.skill_policy import git_url, name


class CatalogAccessError(ValueError):
    """Fixed, credential-free diagnostics for repository authentication failures."""


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


def read_catalog(root: Path) -> list[dict[str, str | None]]:
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
        result.append({"name": key, "description": description, "version": version})
    return result


def fetch_catalog(url: str, access_token: str | None = None) -> list[dict[str, str | None]]:
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
    )
    if access_token:
        url = https_repository(url)
        env.update(token_git_env(url, access_token))
    with tempfile.TemporaryDirectory(prefix="coreman-catalog-") as directory:
        root = Path(directory) / "repo"
        with (Path(directory) / "git-error").open("w+b") as errors:
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
                        str(root),
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
                if any(
                    marker in diagnostic for marker in (b"403", b"not allowed to download code")
                ):
                    raise CatalogAccessError(
                        "Git 仓库拒绝读取（403）。请检查 Project Access Token "
                        "的 read_repository 权限，"
                        "并确认项目角色至少为 Reporter。"
                    ) from None
                if any(
                    marker in diagnostic
                    for marker in (b"401", b"http basic: access denied", b"authentication failed")
                ):
                    raise CatalogAccessError(
                        "Git 认证失败。请检查 token 是否有效、是否过期或已撤销，"
                        "以及 read_repository 权限。"
                    ) from None
                raise ValueError("无法读取技能仓库，请检查 Git、地址和访问权限") from None
            except (subprocess.SubprocessError, OSError):
                raise ValueError("无法读取技能仓库，请检查 Git、地址和访问权限") from None
        return read_catalog(root)


async def sync_catalog(
    session: AsyncSession,
    *,
    source_key: str,
    url: str,
    entries: list[dict[str, str | None]],
    actor: User | None = None,
) -> dict[str, int]:
    source = await session.scalar(
        select(SkillSource).where(SkillSource.key == source_key).with_for_update()
    )
    if source is None or source.git_url != url:
        raise ValueError("来源未登记或地址不一致，请先在管理台登记来源")
    created = updated = 0
    for entry in entries:
        skill = await session.scalar(
            select(Skill).where(Skill.name == entry["name"]).with_for_update()
        )
        if skill is not None and skill.source_id != source.id:
            raise ValueError("插件名称已属于其他来源，同步已撤销")
        if skill is None:
            skill = Skill(
                name=entry["name"],
                source_id=source.id,
                enabled=False,
                description=entry["description"],
                version=entry["version"],
            )
            session.add(skill)
            created += 1
        elif skill.description != entry["description"] or skill.version != entry["version"]:
            skill.description, skill.version = entry["description"] or "", entry["version"]
            updated += 1
    await record_audit(
        session,
        action="skill.catalog_sync",
        actor_id=actor.id if actor else None,
        actor_login=actor.login_name if actor else "cli",
        target_type="skill_source",
        target_id=str(source.id),
        diff={"created": [None, created], "updated": [None, updated]},
    )
    await session.flush()
    return {"created": created, "updated": updated, "unchanged": len(entries) - created - updated}
