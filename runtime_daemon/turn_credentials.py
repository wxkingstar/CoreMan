"""助理沙箱里的模型登录凭证：每轮从节点的登录文件收窄出一份只读副本。

节点上的 CLI 登录由所有员工共用。OAuth 的刷新令牌可能是一次性的：沙箱里的 CLI 若用它刷新，
新令牌写不回只读文件，节点原来那枚又已作废，整台节点的登录就坏了。所以副本里**没有刷新令牌**：
- Claude：`.credentials.json` 只留 `claudeAiOauth` 里除 `refreshToken` 以外的字段，其它顶层键
  （如各 MCP 服务的 OAuth 令牌）一概不带；
- Codex：`auth.json` 去掉 `tokens.refresh_token`，`last_refresh` 改成现在，免得 CLI 以为该刷新了；
  用 API Key 登录的原样带上（没有刷新这回事）。
访问令牌剩余有效期不足 `MARGIN_SECONDS` 时不起这一轮（`credential_expiring`）：刷新留给沙箱外
的普通轮次与健康检查，由它们照常写回节点的登录文件。节点用环境变量登录（API Key、长效令牌）
时只把那一个变量带进去。

沙箱里被注入的助理读得到这份副本：拿到的只是一枚到期即失效的访问令牌，而沙箱出网只通平台与
模型接口，带不出去；刷新地址不在放行名单里。
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# 访问令牌至少还要有这么久才起一轮（秒）。长于一轮的常见时长，短于令牌的常见寿命。
MARGIN_SECONDS = 30 * 60
# 节点用环境变量登录时，带进沙箱的那一个变量（按顺序取第一个有值的）。
ENV_LOGINS = {
    "claude": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "codex": ("OPENAI_API_KEY",),
}


class Unavailable(Exception):
    """这一轮拿不到可用的模型凭证；`code` 是给平台的原因代码。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Credential:
    """收窄后的凭证：一个要只读挂进配置目录的文件内容，或几个环境变量。"""

    content: bytes | None = None
    env: Mapping[str, str] = field(default_factory=dict)


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise Unavailable("credential_missing") from None
    except (OSError, ValueError):
        raise Unavailable("credential_unreadable") from None
    if not isinstance(data, dict):
        raise Unavailable("credential_unreadable")
    return data


def _from_env(provider: str, env: Mapping[str, str]) -> Credential | None:
    for key in ENV_LOGINS[provider]:
        if env.get(key):
            return Credential(env={key: env[key]})
    return None


def claude(config_dir: Path, env: Mapping[str, str], now: float | None = None) -> Credential:
    """Claude 的凭证副本。`config_dir` 是当前席位的 `CLAUDE_CONFIG_DIR`（或 `~/.claude`）。"""
    found = _from_env("claude", env)
    if found is not None:
        return found
    data = _load(config_dir / ".credentials.json")
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict) or not isinstance(oauth.get("accessToken"), str):
        raise Unavailable("credential_unreadable")
    expires = oauth.get("expiresAt")
    if not isinstance(expires, int | float):
        raise Unavailable("credential_unreadable")
    if expires / 1000 - (time.time() if now is None else now) < MARGIN_SECONDS:
        raise Unavailable("credential_expiring")
    narrowed = {k: v for k, v in oauth.items() if k != "refreshToken"}
    return Credential(content=json.dumps({"claudeAiOauth": narrowed}).encode())


def _jwt_expiry(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return float(exp) if isinstance(exp, int | float) else None


def codex(codex_home: Path, env: Mapping[str, str], now: float | None = None) -> Credential:
    """Codex 的凭证副本。`codex_home` 是当前席位的 `CODEX_HOME`（或 `~/.codex`）。"""
    found = _from_env("codex", env)
    if found is not None:
        return found
    data = _load(codex_home / "auth.json")
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        if isinstance(data.get("OPENAI_API_KEY"), str) and data["OPENAI_API_KEY"]:
            key = {"OPENAI_API_KEY": data["OPENAI_API_KEY"]}
            return Credential(content=json.dumps(key).encode())
        raise Unavailable("credential_unreadable")
    access = tokens.get("access_token")
    expiry = _jwt_expiry(access) if isinstance(access, str) else None
    if expiry is None:
        raise Unavailable("credential_unreadable")
    moment = time.time() if now is None else now
    if expiry - moment < MARGIN_SECONDS:
        raise Unavailable("credential_expiring")
    narrowed = dict(data)
    narrowed["tokens"] = {k: v for k, v in tokens.items() if k != "refresh_token"}
    narrowed["last_refresh"] = (
        datetime.fromtimestamp(moment, tz=timezone.utc).isoformat().replace("+00:00", "Z")  # noqa: UP017 3.10
    )
    return Credential(content=json.dumps(narrowed).encode())


def for_provider(provider: str, seat_env: Mapping[str, str], home: Path) -> Credential:
    """按当前席位的环境找到登录文件并收窄。

    Raises:
        Unavailable: 没登录、文件读不懂、访问令牌快到期
    """
    if provider == "claude":
        directory = seat_env.get("CLAUDE_CONFIG_DIR")
        return claude(Path(directory) if directory else home / ".claude", seat_env)
    if provider == "codex":
        directory = seat_env.get("CODEX_HOME")
        return codex(Path(directory) if directory else home / ".codex", seat_env)
    raise Unavailable("provider_unsupported")


def write(directory: Path, credential: Credential) -> Path | None:
    """把文件型凭证写进本轮的 secrets 目录（0600），返回路径；环境变量型返回 None。"""
    if credential.content is None:
        return None
    path = directory / "credential"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(credential.content)
    return path
