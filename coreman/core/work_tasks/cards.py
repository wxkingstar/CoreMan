"""任务平台的飞书卡片：待选择事项（派给你、待你验收）与完成、取消通知。

待选择事项在网页和卡片上是同一份（spec §3.4）：问题变了（新问题、被回答、被撤掉）就把旧卡片
改成结果，再给新问题发一张卡。卡片只发到被问的人的私聊；按钮回调走 `personal:<任务 id>`、
事件 `work_task_ask`，由 worker 的 `work_task_cards.handle` 处理。

用哪个飞书员工发：跟这件事有关的 AI 员工（执行员工、发起的员工、调度员），都没有时用系统设置
里的「任务通知员工」（`work_task_notifier_bot_id`）。对方不在这个应用的可用范围内时发不出去，
出站箱记失败，网页上照样能处理。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.api_tasks.service import feishu_user_id
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import outbox
from coreman.core.db.models import (
    Bot,
    OutboxItem,
    Setting,
    User,
    WorkTask,
    WorkTaskEvent,
    WorkTaskWatcher,
)
from coreman.core.work_tasks.service import NEEDS_TEXT, OTHER, clip

EVENT_KEY = "work_task_ask"
TEXT_FIELD = "text"
NOTIFIER_SETTING = "work_task_notifier_bot_id"
TEXT_MAX = 500


def task_url(base_url: str, code: str) -> str:
    return f"{base_url.rstrip('/')}/work-tasks/{code}"


def card_task_id(row: WorkTask) -> str:
    return f"personal:{row.id}"


async def sender(session: AsyncSession, row: WorkTask) -> Bot | None:
    """发卡片的飞书员工：和这件事有关的优先，否则用系统设置里的任务通知员工。"""
    candidates = [row.assignee_bot_id, row.requester_bot_id, row.dispatcher_bot_id]
    setting = await session.get(Setting, NOTIFIER_SETTING)
    if setting is not None and setting.value:
        try:
            candidates.append(uuid.UUID(str(setting.value)))
        except ValueError:
            pass
    for bot_id in candidates:
        bot = await session.get(Bot, bot_id) if bot_id else None
        if bot is not None and bot.enabled and bot.platform == "feishu" and not is_assistant(bot):
            return bot
    return None


def _button(row: WorkTask, ask_id: str, key: str, label: str, primary: bool) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if primary else "default",
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "task_id": card_task_id(row),
                    "event_key": EVENT_KEY,
                    "level": f"{ask_id}:{key}",
                },
            }
        ],
    }


def ask_card(row: WorkTask, ask: dict[str, Any], base_url: str) -> dict[str, Any]:
    """一个问题、几个选项按钮；要写理由的选项和「其他」放进表单，和输入框一起提交。"""
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": ask["question"]}}
    ]
    if ask.get("why"):
        elements.append({"tag": "markdown", "content": f"<font color='grey'>{ask['why']}</font>"})
    plain = [o for o in ask["options"] if o["key"] not in NEEDS_TEXT]
    texted = [o for o in ask["options"] if o["key"] in NEEDS_TEXT]
    for i, option in enumerate(plain):
        elements.append(_button(row, ask["id"], option["key"], option["label"], i == 0))
    submits = [*texted, *([{"key": OTHER, "label": "其他"}] if ask.get("other") else [])]
    if submits:
        elements.append(
            {
                "tag": "form",
                "name": f"work_task_{row.id}",
                "elements": [
                    {
                        "tag": "input",
                        "name": TEXT_FIELD,
                        "max_length": TEXT_MAX,
                        "label": {"tag": "plain_text", "content": "写一句话（退回、拒绝要写理由）"},
                        "label_position": "top",
                        "placeholder": {"tag": "plain_text", "content": "理由，或你自己的答案"},
                    },
                    *(
                        {
                            **_button(row, ask["id"], o["key"], o["label"], False),
                            "name": o["key"],
                            "form_action_type": "submit",
                        }
                        for o in submits
                    ),
                ],
            }
        )
    elements.append(
        {"tag": "markdown", "content": f"[在任务页查看 {row.code}]({task_url(base_url, row.code)})"}
    )
    return {
        "schema": "2.0",
        "task_id": card_task_id(row),
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": clip(f"{row.code} {row.title}", 60)},
        },
        "body": {"elements": elements},
    }


def result_card(row: WorkTask, line: str, base_url: str) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "template": "grey",
            "title": {"tag": "plain_text", "content": clip(f"{row.code} {row.title}", 60)},
        },
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": line}},
                {
                    "tag": "markdown",
                    "content": f"[在任务页查看 {row.code}]({task_url(base_url, row.code)})",
                },
            ]
        },
    }


def _ask_key(row: WorkTask, ask_id: str) -> str:
    return f"work-task:{row.id}:ask:{ask_id}"


async def _send_ask(session: AsyncSession, row: WorkTask, base_url: str) -> None:
    ask = row.ask or {}
    to = ask.get("to_user_id")
    bot = await sender(session, row)
    pid = await feishu_user_id(session, uuid.UUID(to)) if to else None
    if bot is None or not pid:
        return
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=_ask_key(row, ask["id"]),
        target={"user_id": pid},
        payload={"card": ask_card(row, ask, base_url)},
    )


async def _close_ask(
    session: AsyncSession, row: WorkTask, ask: dict[str, Any], line: str, base_url: str
) -> None:
    """把旧问题的卡片改成结果；卡片还没发出去（或没发成）就不改。"""
    sent = await session.scalar(
        select(OutboxItem).where(
            OutboxItem.dedupe_key == _ask_key(row, ask["id"]), OutboxItem.status == "sent"
        )
    )
    message_id = (sent.payload or {}).get("_feishu_message_id") if sent else None
    if sent is None or not message_id:
        return
    await outbox.add(
        session,
        bot_id=sent.bot_id,
        platform="feishu",
        kind="card_update",
        dedupe_key=f"{_ask_key(row, ask['id'])}:closed",
        target={"chat_id": "", "message_id": message_id},
        payload={"card": result_card(row, line, base_url)},
    )


async def _notify(
    session: AsyncSession, row: WorkTask, line: str, actor_id: uuid.UUID | None, base_url: str
) -> None:
    """完成、取消：告诉发起人、代表人和关注人（做这件事的人自己不用再收）。"""
    bot = await sender(session, row)
    if bot is None:
        return
    people = {row.requester_user_id, row.principal_user_id} | set(
        await session.scalars(
            select(WorkTaskWatcher.user_id).where(WorkTaskWatcher.work_task_id == row.id)
        )
    )
    for user_id in people - {None, actor_id}:
        assert user_id is not None
        user = await session.get(User, user_id)
        pid = await feishu_user_id(session, user_id) if user and user.status == "active" else None
        if not pid:
            continue
        await outbox.add(
            session,
            bot_id=bot.id,
            platform="feishu",
            kind="send",
            dedupe_key=f"work-task:{row.id}:{row.status}:{row.updated_at.timestamp()}:{user_id}",
            target={"user_id": pid},
            payload={"card": result_card(row, line, base_url)},
        )


async def sync(
    session: AsyncSession,
    row: WorkTask,
    *,
    before_ask: dict[str, Any] | None,
    before_status: str,
    actor_id: uuid.UUID | None,
    base_url: str,
) -> None:
    """一次操作之后：问题变了就收旧卡、发新卡；刚完成或取消的通知相关的人。"""
    if not base_url:
        return
    after_id = (row.ask or {}).get("id")
    if before_ask and before_ask.get("id") != after_id:
        await _close_ask(session, row, before_ask, await outcome_line(session, row), base_url)
    if after_id and (before_ask or {}).get("id") != after_id:
        await _send_ask(session, row, base_url)
    if row.status != before_status and row.status in ("done", "cancelled"):
        await _notify(session, row, await outcome_line(session, row), actor_id, base_url)


async def outcome_line(session: AsyncSession, row: WorkTask) -> str:
    """旧卡片上写什么：最近一条给人看的事件，没有时写现状。"""
    text = await session.scalar(
        select(WorkTaskEvent.human_text)
        .where(WorkTaskEvent.work_task_id == row.id, WorkTaskEvent.human_text != "")
        .order_by(WorkTaskEvent.id.desc())
        .limit(1)
    )
    if row.status == "done":
        return f"已完成：{row.summary}" if row.summary else "已完成"
    if row.status == "cancelled":
        return text or "已取消"
    return text or "已处理"
