"""技能输入校验与预设选择；不执行仓库内容。"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from coreman.core.db.models import Skill
from coreman.core.prompting.env_vars import is_reserved_key
from coreman.core.relay.safe_transport import validate_host

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def name(value: str) -> str:
    if not NAME.fullmatch(value):
        raise ValueError("名称仅允许字母、数字、下划线和连字符，最多 100 字符")
    return value


def git_url(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > 1000 or any(c in value for c in ("\n", "\r", "\x00")):
        raise ValueError("Git 地址无效")
    if value.startswith("git@"):
        match = re.fullmatch(r"git@([A-Za-z0-9.-]+):([A-Za-z0-9_./-]+)\.git", value)
        if match is None or ".." in match.group(2).split("/"):
            raise ValueError("SSH Git 地址无效")
        validate_host(match.group(1))
    else:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.port not in (None, 443)
            or parts.path in ("", "/")
        ):
            raise ValueError("Git 来源须使用 HTTPS 或 git@host:path.git，不能含凭证")
        validate_host(parts.hostname)
    return value


def env_vars(values: dict[str, str]) -> dict[str, str]:
    if len(values) > 100:
        raise ValueError("环境变量超过 100 项")
    for key, value in values.items():
        if not ENV_KEY.fullmatch(key) or is_reserved_key(key):
            raise ValueError("环境变量名无效，或试图覆盖运行时身份与授权")
        if len(value) > 4000 or "\x00" in value:
            raise ValueError("环境变量值无效或过长")
    return values


def selected_groups(skill: Skill, selected: list[str], data_source: str | None) -> list[str]:
    if len(selected) != len(set(selected)) or not set(selected) <= set(skill.selectable_env_groups):
        raise ValueError("选择了目录之外或重复的环境组")
    source = data_source or skill.default_data_source
    if source is not None and source not in (skill.data_sources or {}):
        raise ValueError("选择的数据源不存在")
    if source not in (None, "mysql", "doris"):
        raise ValueError("当前仅支持 MySQL/Doris 数据源")
    keys = list(skill.env_groups)
    for group in selected:
        actual = (
            f"{group}_doris" if source == "doris" and group in skill.doris_enabled_groups else group
        )
        keys.append(actual)
    return list(dict.fromkeys(keys))


def user_inputs(skill: Skill, values: dict[str, str]) -> dict[str, str]:
    env_vars(values)
    if not set(values) <= set(skill.user_env_vars):
        raise ValueError("包含技能目录未声明的用户配置")
    missing = [
        key
        for key, options in skill.user_env_vars.items()
        if options.get("required") and not values.get(key)
    ]
    if missing:
        raise ValueError("缺少技能要求的用户配置")
    return values
