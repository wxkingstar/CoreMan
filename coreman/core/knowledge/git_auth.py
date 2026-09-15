"""Ephemeral, repository-scoped Git HTTP authentication; no token in URLs or argv."""

from __future__ import annotations

import base64
import re

from coreman.core.knowledge.skill_policy import git_url

SOURCE_TOKEN_AAD = "skill_sources.access_token_enc"


def https_repository(url: str) -> str:
    git_url(url)
    if url.startswith("git@"):
        match = re.fullmatch(r"git@([A-Za-z0-9.-]+):(.+)", url)
        assert match is not None
        url = f"https://{match[1]}/{match[2]}"
    return url.rstrip("/")


def token_git_env(url: str, token: str) -> dict[str, str]:
    if not token or len(token) > 2000 or any(c.isspace() or ord(c) < 32 for c in token):
        raise ValueError("Project Access Token 格式无效")
    url = https_repository(url)
    credential = base64.b64encode(f"oauth2:{token}".encode()).decode()
    config = {
        "credential.helper": "",
        "http.followRedirects": "false",
        "http.sslVerify": "true",
        f"http.{url}.extraHeader": f"Authorization: Basic {credential}",
    }
    env = {"GIT_CONFIG_COUNT": str(len(config))}
    for i, (key, value) in enumerate(config.items()):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env
