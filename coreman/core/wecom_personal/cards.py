"""企业微信个人工具在私聊里用的档位卡片。私聊指令与绑定完成的通知都会发它。"""

from __future__ import annotations

import secrets
from typing import Any

CARD_PREFIX = "wecom_personal"
QUESTION_KEY = "wecom_personal_level"
# 卡片选项最多 11 个字，完整说明放在卡片前那条消息里。
OPTIONS = (
    ("readonly", "仅读取"),
    ("all_except_send", "读写（不发邮件）"),
    ("all", "全部（含发邮件）"),
)


def card_task_id(origin_task_id: int) -> str:
    return f"{CARD_PREFIX}@{origin_task_id}@{secrets.token_hex(4)}"


def selection_card(task_id: str, level: str = "readonly", icon_url: str = "") -> dict[str, Any]:
    source: dict[str, Any] = {"desc": "企业微信个人工具"}
    if icon_url:
        source["icon_url"] = icon_url
    return {
        "card_type": "vote_interaction",
        "source": source,
        "main_title": {"title": "连接企业微信", "desc": "选择允许 AI 员工使用的范围"},
        "checkbox": {
            "question_key": QUESTION_KEY,
            "option_list": [
                {"id": option, "text": title, "is_checked": option == level}
                for option, title in OPTIONS
            ],
            "mode": 0,
            "disable": False,
        },
        "submit_button": {"text": "连接", "key": "wecom_personal_connect"},
        "task_id": task_id,
    }
