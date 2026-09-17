"""CoreMan 飞书应用权限清单：扫码创建时一次申请，之后由 CoreMan 在内部收窄。

平台底座模板（智能体应用配置清单）已含收发消息、卡片、表情回复、群信息、文档评论与
应用自管理等权限；这里只列 CoreMan 在底座之上还需要的部分。清单只能增量叠加，平台
不校验名称，不存在的权限点会被确认页忽略。

用户身份权限按「连接飞书」全部档位的上限申请：应用侧一次开通，个人授权时由
`feishu_personal.permissions.select_scopes` 按本人选择的档位收窄，发送等高风险操作
还要经过服务端独立校验。新增个人工具需要的权限时加在这里，已创建的应用在后台
「补齐权限」重新扫码即可生效。
"""

from __future__ import annotations

import base64
import gzip
import json
from typing import Any

from coreman.core.feishu_personal import service as personal_service

# 应用身份：员工身份识别（user_id 是卡片归属与身份校验的依据）、通讯录姓名，
# 以及后台管理应用基础信息、能力、可用范围与发布版本。
TENANT_SCOPES: tuple[str, ...] = (
    "contact:user.employee_id:readonly",
    "contact:user.base:readonly",
    "im:message:send_as_bot",
    "im:message:readonly",
    "im:message.p2p_msg:readonly",
    "im:message.group_at_msg:readonly",
    "im:message.group_at_msg.include_bot:readonly",
    "im:message.reactions:write_only",
    "im:resource",
    "im:chat:read",
    "cardkit:card:write",
    "application:bot.basic_info:read",
    "application:application:self_manage",
    "application:application:patch",
    "application:bot.menu:write",
    "application:app_slash_command:read",
    "application:app_slash_command:write",
)

# 用户身份：连接飞书全部档位（含第一档的以用户身份发送）。
USER_SCOPES: tuple[str, ...] = tuple(
    sorted(set(personal_service.SCOPES.split()) | {"im:message", "im:message.send_as_user"})
)

TENANT_EVENTS: tuple[str, ...] = (
    "im.message.receive_v1",
    "im.chat.member.bot.added_v1",
    "im.chat.access_event.bot_p2p_chat_entered_v1",
)

CALLBACKS: tuple[str, ...] = ("card.action.trigger",)


def addons() -> dict[str, Any]:
    return {
        "preset": True,
        "scopes": {"tenant": list(TENANT_SCOPES), "user": list(USER_SCOPES)},
        "events": {"items": {"tenant": list(TENANT_EVENTS)}},
        "callbacks": {"items": list(CALLBACKS)},
    }


def encode_addons(value: dict[str, Any]) -> str:
    """与官方 SDK 相同的编码：紧凑 JSON → gzip(mtime=0) → 无填充 base64url。"""
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    compressed = gzip.compress(payload.encode("utf-8"), mtime=0)
    return base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")


def missing_scopes(granted: list[str], *, kind: str) -> list[str]:
    """清单里有、应用当前没开通的权限；用于提示「补齐权限」。"""
    wanted = TENANT_SCOPES if kind == "tenant" else USER_SCOPES
    return sorted(set(wanted) - set(granted) - {"offline_access"})
