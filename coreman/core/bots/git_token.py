"""工作目录 Git 备份的访问令牌：员工自己填的，或取自打了 git 标签的环境预设里的某个变量。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import decrypt_json
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, EnvPreset
from coreman.core.errors import ApiError

GIT_TOKEN_AAD = "bots.git_token_enc"
PRESET_AAD = "env_presets.vars_enc"
# 委员会给预设打上这个标签，员工管理员才能把它用作 Git 令牌：预设里的令牌往往能访问整个组织。
GIT_TAG = "git"


async def preset_token(session: AsyncSession, cipher: Cipher, key: str, var: str) -> str:
    preset = await session.get(EnvPreset, key)
    if preset is None or GIT_TAG not in preset.tags:
        raise ApiError(422, 422, "环境预设不存在或未开放给 Git 备份使用")
    value = decrypt_json(cipher, preset.vars_enc, PRESET_AAD).get(var)
    if not value:
        raise ApiError(422, 422, "环境预设中没有该变量")
    return str(value)


async def git_token(session: AsyncSession, cipher: Cipher, bot: Bot) -> str:
    if bot.git_token_preset:
        return await preset_token(session, cipher, bot.git_token_preset, bot.git_token_var or "")
    return cipher.decrypt(bot.git_token_enc, GIT_TOKEN_AAD) if bot.git_token_enc else ""
