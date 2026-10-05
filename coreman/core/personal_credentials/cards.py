"""个人凭证的卡片与文案。外框与安全说明由系统固定渲染；agent 只能提供用途与字段标签。"""

from __future__ import annotations

import re
import uuid
from typing import Any

from coreman.core.personal_credentials.policy import FEISHU_MAX_VALUE, card_task_id

SECURITY_NOTE = (
    "🔒 安全说明：此表单的内容直接提交给 CoreMan 加密保存，不经过聊天，不会发送给 AI 模型。"
    "只有你本人与「{bot}」对话、或运行你创建的定时任务时才会使用。"
    "请勿在此填写飞书、企业微信或邮箱的登录密码。"
)
HANDOFF_NOTE = (
    "🔑 安全说明：此表单的内容直接提交给 CoreMan 加密暂存，不经过聊天，不会发送给 AI 模型。"
    "只在「{bot}」继续这一次任务时使用，任务结束即删除，不会保存，也不会出现在「我的凭证」里。"
    "请勿在此填写飞书、企业微信或邮箱的登录密码。"
)
GROUP_NOTICE = "已私信你一张安全表单，请在私聊里填写。"
# 续接消息里引用的原始请求最多保留的字数。
ORIGINAL_MAX = 2000
# 企微链接文案是 Markdown：agent 写的用途与标签里的链接、强调符号一律压平，免得冒充入口。
# 同时移除裸 URL 并折叠空白，防止多行注入。
_MARKDOWN = re.compile(r"[\[\]()<>`*_#|]")
_BARE_URL = re.compile(r"https?://\S+")


def security_note(bot_name: str, *, save: bool = True) -> str:
    return (SECURITY_NOTE if save else HANDOFF_NOTE).format(bot=bot_name)


def title(save: bool) -> str:
    return "需要你的个人凭证" if save else "需要你提供一次性密钥"


def _flat(text: str) -> str:
    text = _MARKDOWN.sub(" ", text)
    text = _BARE_URL.sub(" ", text)
    text = " ".join(text.split())  # collapse all whitespace including newlines
    return text


def _plain(content: str, **style: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "plain_text", "content": content, **style}}


def _label(field: dict[str, Any]) -> str:
    return str(field.get("label") or field["key"])


def form_card(
    request_id: uuid.UUID,
    *,
    bot_name: str,
    purpose: str,
    fields: list[dict[str, Any]],
    web_url: str | None = None,
    mention_open_id: str | None = None,
    save: bool = True,
) -> dict[str, Any]:
    task_id = card_task_id(request_id)
    inputs: list[dict[str, Any]] = []
    for field in fields:
        element: dict[str, Any] = {
            "tag": "input",
            "name": str(field["key"]),
            "required": True,
            "max_length": FEISHU_MAX_VALUE,
            "label": {"tag": "plain_text", "content": _label(field)},
            "label_position": "top",
            "placeholder": {
                "tag": "plain_text",
                "content": str(field.get("placeholder") or f"请输入{_label(field)}"),
            },
        }
        if field.get("secret", True):
            element["input_type"] = "password"
        inputs.append(element)
    elements: list[dict[str, Any]] = []
    if mention_open_id:
        elements.append({"tag": "markdown", "content": f"<at id={mention_open_id}></at>"})
    elements.append(_plain("用途：" + purpose))
    elements.append(
        {
            "tag": "form",
            "name": "credential_form",
            "elements": [
                *inputs,
                {
                    "tag": "button",
                    "name": "submit",
                    "form_action_type": "submit",
                    "type": "primary",
                    "text": {"tag": "plain_text", "content": "加密提交"},
                    "behaviors": [{"type": "callback", "value": {"task_id": task_id}}],
                },
            ],
        }
    )
    if web_url:
        elements.append({"tag": "markdown", "content": f"内容较长？[用网页填写]({web_url})"})
    elements.append(
        _plain(
            security_note(bot_name, save=save) + "1 小时内有效。",
            text_size="notation",
            text_color="grey",
        )
    )
    return {
        "schema": "2.0",
        "task_id": task_id,
        "header": {
            "template": "blue" if save else "indigo",
            "title": {"tag": "plain_text", "content": ("🔒 " if save else "🔑 ") + title(save)},
            "subtitle": {"tag": "plain_text", "content": f"AI 员工「{bot_name}」"},
        },
        "body": {"elements": elements},
    }


def _result(request_id: uuid.UUID, title: str, lines: list[str], template: str) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "task_id": card_task_id(request_id),
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "body": {"elements": [_plain(line) for line in lines]},
    }


def saved_card(
    request_id: uuid.UUID, keys: list[str] | tuple[str, ...], tail: str
) -> dict[str, Any]:
    return _result(
        request_id,
        "✅ 已保存",
        [f"已保存 {'、'.join(keys)}（内容不显示）。", tail],
        "green",
    )


def handoff_card(
    request_id: uuid.UUID, keys: list[str] | tuple[str, ...], tail: str, *, continuing: bool
) -> dict[str, Any]:
    """一次性交付的结果：继续时说用完即删；续不上时值已经丢弃，请用户重新发起。"""
    names = "、".join(keys)
    if continuing:
        return _result(
            request_id,
            "✅ 已交付",
            [f"已交付 {names}（内容不显示），只用于这一次任务，结束后立即删除。", tail],
            "green",
        )
    return _result(request_id, "⚠️ 未使用", [f"{names} 没有使用。", tail], "orange")


def expired_card(request_id: uuid.UUID) -> dict[str, Any]:
    return _result(request_id, "⌛ 表单已过期", ["请让 AI 员工重新发起。"], "grey")


def failed_card(request_id: uuid.UUID, message: str) -> dict[str, Any]:
    return _result(request_id, "⚠️ 提交未成功", [message, "请让 AI 员工重新发起。"], "orange")


def wecom_link(
    *, bot_name: str, purpose: str, fields: list[dict[str, Any]], url: str, save: bool = True
) -> str:
    labels = "、".join(_flat(_label(f)) for f in fields)
    icon = "🔒" if save else "🔑"
    return "\n".join(
        (
            f"**{icon} AI 员工「{_flat(bot_name)}」{title(save)}**",
            f"用途：{_flat(purpose)}",
            f"需要填写：{labels}",
            f"👉 [点这里安全填写]({url})（1 小时内有效，只有你本人能打开）",
            security_note(_flat(bot_name), save=save),
        )
    )


def resume_text(keys: list[str] | tuple[str, ...], original: str = "", *, save: bool = True) -> str:
    """续接消息：只有键名和用户自己的原始请求（引用它，续接才不会接到会话里别的任务上）。"""
    names = "、".join(keys)
    if save:
        head = (
            f"[CoreMan] 用户已通过安全表单提交 {names}，已作为环境变量注入本轮，"
            "值不会出现在对话中。"
        )
    else:
        head = (
            f"[CoreMan] 用户已通过安全表单提交一次性密钥 {names}，只在本轮的环境变量里，"
            "本轮结束即删除，值不会出现在对话中。"
        )
    original = original.strip()
    if not original:
        return head + "请继续完成之前的任务。"
    if len(original) > ORIGINAL_MAX:
        original = original[:ORIGINAL_MAX] + "…"
    return head + f"请继续完成用户的这条原始请求：{original}"
