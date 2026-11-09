"""群里需要确认的写入：平台暂存参数，提问人点确认后原样执行，不经过模型。"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox
from coreman.core.db.models import InteractionState, Task
from coreman.core.feishu_docs import resume
from coreman.core.feishu_docs.policy import DocsScope
from coreman.core.timeutils import utcnow

CARD_EVENT = "feishu_group_write"
KIND = "feishu_group_write"
TTL = timedelta(minutes=10)
RESULT_KEY = resume.CONFIRM_RESULT_KEY
MAX_VALUE = 500
TITLES = {
    "feishu_calendar_update_event": "修改日程",
    "feishu_calendar_add_attendees": "邀请参会人",
    "feishu_calendar_delete_event": "删除日程",
    "feishu_calendar_reply": "回复日程邀请",
    "feishu_calendar_create_event": "创建日程",
    "feishu_task_create": "创建任务",
    "feishu_task_add_subtask": "创建子任务",
    "feishu_create_document": "在指定位置新建文档",
    "feishu_create_sheet": "在指定位置新建表格",
    "feishu_create_folder": "在指定位置新建文件夹",
    "feishu_save_to_drive": "保存文件到指定位置",
    "feishu_wiki_create_node": "在指定知识库新建页面",
    "feishu_copy_file": "复制文件",
    "feishu_task_update": "修改任务",
    "feishu_task_delete": "删除任务",
    "feishu_share_document": "修改文档权限",
    "feishu_move_file": "移动文件",
    "feishu_base_delete_record": "删除多维表记录",
}
PENDING = {
    "status": "pending_confirmation",
    "hint": "已发确认卡片，提问人点确认后才执行。说一句请他确认，然后结束本轮，不要说已经完成。",
}


def _lines(arguments: dict[str, Any]) -> str:
    """卡片内容来自实际参数，不用模型写的描述。JSON 转义换行，一个值伪造不出别的行。"""
    rows = []
    for key, value in arguments.items():
        text = json.dumps(value, ensure_ascii=False, default=str)
        rows.append(f"- {key}：{text[:MAX_VALUE]}{'…' if len(text) > MAX_VALUE else ''}")
    return "\n".join(rows)


def card(
    origin_task_id: int, state_id: uuid.UUID, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    def button(label: str, verb: str, kind: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": kind,
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "task_id": f"personal:{origin_task_id}",
                        "event_key": CARD_EVENT,
                        "level": f"{verb}:{state_id}",
                    },
                }
            ],
        }

    title = TITLES.get(name, "修改飞书内容")
    return {
        "schema": "2.0",
        "task_id": f"personal:{origin_task_id}",
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"确认：{title}"},
        },
        "body": {
            "elements": [
                # 参数可能受文档内容影响，用纯文本展示，不让其中的标记（@所有人、链接）生效。
                {"tag": "div", "text": {"tag": "plain_text", "content": _lines(arguments)}},
                button("确认执行", "confirm", "primary"),
                button("取消", "cancel", "default"),
                {
                    "tag": "markdown",
                    "content": (
                        "<font color='grey'>只有提问人能操作，将以提问人身份执行，"
                        "10 分钟内有效。</font>"
                    ),
                },
            ]
        },
    }


async def propose(
    session: AsyncSession,
    scope: DocsScope,
    name: str,
    arguments: dict[str, Any],
    target: str | None,
    digest: str,
) -> dict[str, Any]:
    from coreman.core.feishu_docs import write_tools

    raw = scope.event.payload.get("raw") or {}
    if (
        scope.event.kind != "message"
        or (raw.get("header") or {}).get("event_type") == "card.action.trigger"
    ):
        # 点卡片开的回合过不了 group_card_scope 的来源核验，发出去的确认卡永远点不动。
        return {
            "error": "confirmation_unavailable",
            "hint": "这一轮由卡片按钮触发，无法发确认卡。请提问人直接发一条消息说明要做的事。",
        }
    # 按任务串行，同参数并发提议时只出一张卡。
    await session.scalar(select(Task.id).where(Task.id == scope.task.id).with_for_update())
    prefix = f"{scope.task.id}:{digest}:"
    now = utcnow()
    existing = await session.scalar(
        select(InteractionState.id)
        .where(
            InteractionState.kind == KIND,
            InteractionState.bot_id == scope.bot.id,
            InteractionState.scope_key.startswith(prefix, autoescape=True),
            InteractionState.status == "open",
            InteractionState.expires_at > now,
        )
        .limit(1)
    )
    if existing is not None:
        return dict(PENDING)
    state = InteractionState(
        bot_id=scope.bot.id,
        kind=KIND,
        # 带 uuid：上一张卡结束后同参数还能再提，不撞 (kind, scope_key) 唯一约束。
        scope_key=f"{prefix}{uuid.uuid4()}",
        state={
            "tool": name,
            "arguments": arguments,
            "target": target,
            "digest": digest,
            "user_id": str(scope.speaker_user_id),
            "chat_id": scope.chat_id,
            "origin_task_id": scope.task.id,
        },
        status="open",
        expires_at=now + TTL,
    )
    session.add(state)
    await session.flush()
    await outbox.add(
        session,
        bot_id=scope.bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=f"feishu-group-write:{state.id}",
        target={"chat_id": scope.chat_id},
        payload={"card": card(scope.task.id, state.id, name, arguments)},
    )
    write_tools.record(session, scope, name, "confirm", target, "pending_confirmation", digest)
    return dict(PENDING)
