"""运行时控制类环境变量黑名单：API 与 runtime daemon 两份规则必须逐项一致。"""

from __future__ import annotations

import uuid

import pytest

import runtime_daemon.daemon as daemon
from coreman.core.bots import env_policy
from coreman.core.prompting.env_vars import build_env
from coreman.core.prompting.system_prompt import Speaker

BLOCKED = [
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_BASE_URL",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_USE_BEDROCK",
    "CODEX_HOME",
    "NODE_OPTIONS",
    "LD_PRELOAD",
    "DYLD_INSERT_LIBRARIES",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "GIT_SSH_COMMAND",
    "GIT_CONFIG_COUNT",
    "SSH_AUTH_SOCK",
    "BASH_ENV",
    "ENV",
    "SHELL",
    "HOME",
    "PATH",
    "HTTPS_PROXY",
    "SSL_CERT_FILE",
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "XDG_CONFIG_HOME",
    "COREMAN_NODE_TOKEN",
    "http_proxy",
    "ld_preload",
]
ALLOWED = [
    "DB_PASSWORD",
    "ERP_BASE_URL",
    "API_BASE",
    "NODE_ENV",
    "OA_TOKEN",
    "GIT_AUTHOR_NAME",
    "GIT_COMMITTER_EMAIL",
    "COREMAN_BOT_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
]


def test_daemon_copy_matches_api_policy() -> None:
    assert daemon.BLOCKED_ENV_KEYS == env_policy.BLOCKED_ENV_KEYS
    assert daemon.BLOCKED_ENV_PREFIXES == env_policy.BLOCKED_ENV_PREFIXES
    assert daemon.ALLOWED_ENV_KEYS == env_policy.ALLOWED_ENV_KEYS


@pytest.mark.parametrize("key", BLOCKED)
def test_control_variables_are_blocked_on_both_sides(key: str) -> None:
    assert env_policy.is_blocked_env_key(key)
    assert env_policy.blocked_env_keys([key]) == [key] == daemon.blocked_env_keys({key: "x"})


@pytest.mark.parametrize("key", ALLOWED)
def test_business_configuration_is_not_blocked(key: str) -> None:
    assert not env_policy.is_blocked_env_key(key)
    assert env_policy.blocked_env_keys([key]) == [] == daemon.blocked_env_keys({key: "x"})


def test_build_env_drops_stored_control_variables() -> None:
    """保存规则收紧前落库的键在下发前丢弃，其余配置不受影响。"""
    env = build_env(
        bot_key="b",
        platform="wecom",
        chat_id="g",
        chat_type="group",
        platform_user_id="u",
        session_id="s",
        speaker=Speaker("u", uuid.uuid4(), "u", "U"),
        bot_env={
            "ANTHROPIC_BASE_URL": "https://evil.example",
            "LD_PRELOAD": "/tmp/x.so",
            "DB": "1",
        },
    )
    assert "ANTHROPIC_BASE_URL" not in env and "LD_PRELOAD" not in env
    assert env["DB"] == "1"
