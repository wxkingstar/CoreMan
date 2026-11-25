"""通过 API 驱动 AI 员工：个人令牌、群别名与 API 任务。"""

from __future__ import annotations

# API 发起的那一轮写的是合成入站事件，消息 id 形如 `api:<任务 id>`，飞书上没有这条消息。
SYNTHETIC_PREFIX = "api:"
# 任务平台让 AI 员工开工的那一轮同样是合成事件：`work:<任务 id>:<序号>:<触发方式>`。
WORK_SYNTHETIC_PREFIX = "work:"


def reply_to(platform_msg_id: str | None) -> str | None:
    """拿入站事件的消息 id 当回复目标：合成事件没有可回复的消息，直接发在会话里。"""
    if not platform_msg_id or platform_msg_id.startswith((SYNTHETIC_PREFIX, WORK_SYNTHETIC_PREFIX)):
        return None
    return platform_msg_id
