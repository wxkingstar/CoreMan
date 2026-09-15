import uuid

import pytest

from coreman.core.knowledge import skill_policy
from coreman.core.prompting.env_vars import build_env, env_keys_for_log, is_reserved_key
from coreman.core.prompting.system_prompt import Speaker


def test_aliases_override_and_identity() -> None:
    known = Speaker("zs", uuid.uuid4(), "zhangsan", "张三")
    env = build_env(
        bot_key="sales",
        platform="wecom",
        chat_id="zs",
        chat_type="single",
        platform_user_id="zs",
        session_id="sid-1",
        speaker=known,
        bot_env={"DB_PASSWORD": "x", "BOT_KEY": "wrong"},
    )
    assert env["BOT_KEY"] == "sales" == env["COREMAN_BOT_KEY"] and env["DB_PASSWORD"] == "x"
    assert env["AGENT_CHAT_ID"] == "zs" == env["COREMAN_CHAT_ID"]
    assert env["COREMAN_CHAT_TYPE"] == "single" == env["AGENT_CHAT_TYPE"]
    assert env["AGENT_WEWORK_USER_ID"] == "zs" == env["COREMAN_PLATFORM_USER_ID"]
    assert env["COREMAN_PLATFORM"] == "wecom"
    assert env["AGENT_BROWSER_SESSION"] == "sid-1" == env["COREMAN_SESSION_ID"]
    assert env["BOT_USER_LOGIN"] == "zhangsan" == env["COREMAN_USER_LOGIN"]
    assert env["AGENT_REAL_NAME"] == "张三" == env["COREMAN_USER_NAME"]
    assert "DB_PASSWORD" in env_keys_for_log(env) and "x" not in " ".join(env_keys_for_log(env))


def test_unknown_speaker_has_no_identity_env() -> None:
    env = build_env(
        bot_key="b",
        platform="wecom",
        chat_id="g1",
        chat_type="group",
        platform_user_id="wo123",
        session_id="s",
        speaker=Speaker("wo123", None, None, None),
        bot_env={},
    )
    assert not {
        "BOT_USER_LOGIN",
        "COREMAN_USER_LOGIN",
        "AGENT_REAL_NAME",
        "COREMAN_USER_NAME",
    } & set(env)
    assert env["COREMAN_CHAT_TYPE"] == "group" and env["COREMAN_CHAT_ID"] == "g1"


@pytest.mark.parametrize("known", [False, True])
def test_static_credentials_never_survive_request_identity_rebinding(known: bool) -> None:
    speaker = Speaker("current", uuid.uuid4() if known else None, None, None)
    forged = {
        "COREMAN_USER_LOGIN": "someone-else",
        "BOT_USER_LOGIN": "someone-else",
        "COREMAN_USER_NAME": "someone-else",
        "AGENT_REAL_NAME": "someone-else",
        "BOT_TOKEN_ERP": "old-token",
        "COREMAN_SYSTEMS": "old-systems",
        "BOT_SYSTEMS_CONFIG": "old-systems",
    }
    env = build_env(
        bot_key="b",
        platform="wecom",
        chat_id="g",
        chat_type="group",
        platform_user_id="current",
        session_id="s",
        speaker=speaker,
        # 平台只按 BOT_TOKEN_ 签发发言者令牌；其他前缀（如某业务系统自己的 *_TOKEN）
        # 是机器人静态配置，照常下发。
        bot_env={**forged, "DB_HOST": "shared", "BOT_KEY": "forged", "OA_TOKEN": "static"},
    )
    assert not set(forged) & set(env)
    assert env["BOT_KEY"] == "b" and env["DB_HOST"] == "shared" and env["OA_TOKEN"] == "static"


def test_reserved_keys_are_shared_with_skill_policy() -> None:
    assert is_reserved_key("BOT_TOKEN_ANY") and is_reserved_key("COREMAN_USER_LOGIN")
    assert not is_reserved_key("OA_TOKEN")
    with pytest.raises(ValueError):
        skill_policy.env_vars({"BOT_TOKEN_ERP": "x"})
    assert skill_policy.env_vars({"OA_TOKEN": "x"}) == {"OA_TOKEN": "x"}
