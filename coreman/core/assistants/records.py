"""个人助理记录：类型判断、固定取值、锁定字段、可见性——纯函数，路由只做编排。"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from coreman.core.db.models import Bot, PlatformApp, User

# 助理记录上这些列的取值固定，对应「只接私聊、只属于本人」。
ASSISTANT_FIXED: dict[str, object] = {
    "group_observe": "off",
    "group_role": "member",
    "accept_bot_help": False,
    "api_access": False,
    "dev_mode": False,
}
# 所有者也不能改的字段：固定取值的列，加上由平台分配的归属、目录、凭证与开通状态。
LOCKED_FIELDS = frozenset(
    {
        *ASSISTANT_FIXED,
        "group_observe_scope",
        "team_id",
        "working_dir",
        "credentials",
        "assistant_state",
    }
)


ASSISTANT_TOGGLE_MESSAGE = (
    "个人助理不能直接启停：所有者在私聊里发「停用助理」，或由平台管理员在助理列表里停用"
)
ASSISTANT_DELETE_MESSAGE = (
    "个人助理要先停用（所有者在私聊里发「停用助理」，或由平台管理员在助理列表里停用），"
    "停用完成后才能删除"
)


def is_assistant(bot: Bot) -> bool:
    return bot.kind == "assistant"


def unsupported(feature: str) -> str:
    """助理暂未开放的功能在入口处拒绝时，给用户或模型看的原因。"""
    return f"个人助理暂不支持{feature}"


def locked_changes(bot: Bot, changes: Mapping[str, object]) -> list[str]:
    """这次修改里助理记录不允许动的字段名；普通记录恒为空。"""
    if not is_assistant(bot):
        return []
    return sorted(LOCKED_FIELDS & changes.keys())


def visible_to(bot: Bot, user: User) -> bool:
    """助理记录只对所有者可见，管理员也不例外；普通记录不在这里限制。"""
    return not is_assistant(bot) or bot.owner_id == user.id


def new_assistant_bot(
    owner: User,
    app: PlatformApp,
    *,
    relay_server_id: uuid.UUID | None,
    model: str,
    working_dir: str,
    credentials_enc: str,
) -> Bot:
    return Bot(
        bot_key=f"assistant-{owner.id.hex[:16]}",
        platform="feishu",
        name="个人助理",
        kind="assistant",
        owner_id=owner.id,
        created_by=owner.id,
        platform_app_id=app.id,
        team_id=None,
        relay_server_id=relay_server_id,
        model=model,
        working_dir=working_dir,
        credentials_enc=credentials_enc,
        # 开通流程走完（授权完成）才改成 active；在那之前跑不了任何一轮。
        assistant_state="provisioning",
        **ASSISTANT_FIXED,
    )
