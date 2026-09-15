"""请求级环境变量（spec §8.3 的表）。

每轮对话都按当前发言者重新算一遍，覆盖机器人自己配的同名变量——机器人级 env 是
静态配置，身份类的值只能由这里说了算，否则群里换个人说话，skill 拿到的还是上一个
人的 token。

新名一律 `COREMAN_*`；旧 skill 里到处写死的 `$BOT_KEY` / `$AGENT_*` /
`$BOT_USER_LOGIN` 作为别名同值下发，等存量 skill 迁完再摘。
"""

from __future__ import annotations

from coreman.core.bots.env_policy import blocked_env_keys, is_blocked_env_key
from coreman.core.logging import get_logger
from coreman.core.prompting.system_prompt import Speaker

log = get_logger(__name__)

# 新名 → 兼容别名（同值）。COREMAN_PLATFORM 没有兼容别名。
_ALIASES = {
    "COREMAN_BOT_KEY": "BOT_KEY",
    "COREMAN_CHAT_ID": "AGENT_CHAT_ID",
    "COREMAN_CHAT_TYPE": "AGENT_CHAT_TYPE",
    "COREMAN_PLATFORM_USER_ID": "AGENT_WEWORK_USER_ID",
    "COREMAN_USER_LOGIN": "BOT_USER_LOGIN",
    "COREMAN_USER_NAME": "AGENT_REAL_NAME",
    "COREMAN_SESSION_ID": "AGENT_BROWSER_SESSION",
}

# 请求级保留键：身份别名与业务系统配置只能由本轮已验证的发言者重建，机器人 env、
# 技能预设都不得提供同名键。
RESERVED_KEYS = frozenset(
    set(_ALIASES)
    | set(_ALIASES.values())
    | {"COREMAN_PLATFORM", "COREMAN_SYSTEMS", "BOT_SYSTEMS_CONFIG"}
)
# 平台按发言者签发的业务系统令牌前缀（见 core/auth/system_access.py）。静态配置里的
# 同前缀键必然是上一轮或他人的令牌，一律丢弃。
SPEAKER_TOKEN_PREFIX = "BOT_TOKEN_"


def is_reserved_key(key: str) -> bool:
    return key in RESERVED_KEYS or key.startswith(SPEAKER_TOKEN_PREFIX)


def build_env(
    *,
    bot_key: str,
    platform: str,
    chat_id: str,
    chat_type: str,
    platform_user_id: str,
    session_id: str,
    speaker: Speaker,
    bot_env: dict[str, str],
) -> dict[str, str]:
    """机器人 env 打底，请求级变量覆盖同名键。

    身份未知时**不下发**身份类变量：宁可让 skill 报「没有 $BOT_USER_LOGIN」
    而失败，也不能给它一个空串，让它当成某个真实账号去调后台接口。
    """
    # 先丢弃静态配置中的请求身份，再按本轮已验证的主体重建。否则未知发言者或
    # 通讯录缺少 login/name 时，会继承机器人创建者手填的身份与上一轮令牌。
    # 控制类变量（模型端点、解释器启动项等）保存时就会 422；这里兜住保存规则收紧前的
    # 存量数据：丢弃并记下键名，管理员下次编辑该机器人 env 时会被要求删掉。
    blocked = blocked_env_keys(bot_env)
    if blocked:
        log.warning("bot_env_blocked_keys_dropped", bot_key=bot_key, keys=blocked)
    env: dict[str, str] = {
        k: str(v)
        for k, v in bot_env.items()
        if not is_reserved_key(k) and not is_blocked_env_key(k)
    }
    request_level = {
        "COREMAN_BOT_KEY": bot_key,
        "COREMAN_PLATFORM": platform,
        "COREMAN_CHAT_ID": chat_id,
        "COREMAN_CHAT_TYPE": chat_type,
        "COREMAN_PLATFORM_USER_ID": platform_user_id,
        "COREMAN_SESSION_ID": session_id,
    }
    if speaker.known:
        if speaker.login_name:
            request_level["COREMAN_USER_LOGIN"] = speaker.login_name
        if speaker.display_name:
            request_level["COREMAN_USER_NAME"] = speaker.display_name
    for key, value in request_level.items():
        env[key] = value
        alias = _ALIASES.get(key)
        if alias:
            env[alias] = value
    return env


def env_keys_for_log(env: dict[str, str]) -> list[str]:
    """只给键名：env 的值里混着机器人配的密钥，日志里一个都不能出现。"""
    return sorted(env)
