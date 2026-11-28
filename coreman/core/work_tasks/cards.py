"""任务平台的飞书卡片：待选择事项（派给你、待你验收）与完成、取消通知。

待选择事项在网页和卡片上是同一份（spec §3.4）：问题变了（新问题、被回答、被撤掉）就把旧卡片
改成结果，再给新问题发一张卡。卡片发到被问的人的私聊；从群里发起的事（调度员交接出来的任务
和它下面的子任务、问题），被问的人在那个群里说过话时不另发卡：问题和按钮画在这件事在群里的
那张进展卡上（见 `progress`），卡片挪到群里最新的位置并 @ 他，别人看得到在等谁定什么，只有他
（和代表人）点了才算。按钮回调走 `personal:<任务 id>`，私聊的卡是事件 `work_task_ask`，
进展卡上的是 `work_task_progress_ask`（任务 id 是根任务的），都由 worker 的
`work_task_cards.handle` 处理。

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
    InboundEvent,
    OutboxItem,
    Setting,
    User,
    WorkTask,
    WorkTaskEvent,
    WorkTaskWatcher,
)
from coreman.core.work_tasks import progress
from coreman.core.work_tasks.service import NEEDS_TEXT, OTHER, clip, name_of

EVENT_KEY = "work_task_ask"
# 画在群里进展卡上的问题：按钮的任务 id 是根任务的，按问题编号找到被问的那一件。
PROGRESS_EVENT = "work_task_progress_ask"
TEXT_FIELD = "text"
NOTIFIER_SETTING = "work_task_notifier_bot_id"
TEXT_MAX = 500


def text_field(row: WorkTask) -> str:
    """这个问题的输入框在卡上的名字。"""
    return f"{TEXT_FIELD}_{row.id}"


def typed_text(selected: Any) -> str:
    """从卡片回调带回的表单值里取出填的那句话（输入框名字见 `text_field`，旧卡是 `text`）。"""
    if not isinstance(selected, dict):
        return ""
    for name, values in selected.items():
        if (name == TEXT_FIELD or str(name).startswith(f"{TEXT_FIELD}_")) and isinstance(
            values, list
        ):
            return str(values[0]) if values else ""
    return ""


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


def _button(
    row: WorkTask,
    ask_id: str,
    key: str,
    label: str,
    primary: bool,
    allowed: list[str] | None = None,
    on: WorkTask | None = None,
) -> dict[str, Any]:
    """`on`：按钮画在哪件事的进展卡上（根任务）；不给就是这个问题自己的卡。"""
    value: dict[str, Any] = {
        "task_id": card_task_id(on or row),
        "event_key": PROGRESS_EVENT if on is not None else EVENT_KEY,
        "level": f"{ask_id}:{key}",
    }
    if allowed:
        # 发在群里的卡：谁点了才算。网关据此当场提示别人「不是问你的」，worker 再核对一遍权限。
        value["allowed"] = allowed
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if primary else "default",
        "behaviors": [{"type": "callback", "value": value}],
    }


def ask_elements(
    row: WorkTask,
    ask: dict[str, Any],
    *,
    mention: str = "",
    number: int = 0,
    allowed: list[str] | None = None,
    on: WorkTask | None = None,
) -> list[dict[str, Any]]:
    """一个问题的那几块：点名、问题、为什么问他、选项按钮，以及带输入框的表单。

    要写理由的选项和「其他」放进表单，和输入框一起提交。`mention` 是开头点名的那一行，
    `number` 是一张卡上有几个问题时这是第几个，`allowed` 是能回答的人的飞书 user_id
    （发在群里时才有），`on` 见 `_button`。
    """
    elements: list[dict[str, Any]] = []
    if mention:
        elements.append({"tag": "markdown", "content": mention})
    question = f"{number}. {ask['question']}" if number else ask["question"]
    elements.append({"tag": "div", "text": {"tag": "plain_text", "content": question}})
    if ask.get("why"):
        elements.append({"tag": "markdown", "content": f"<font color='grey'>{ask['why']}</font>"})
    plain = [o for o in ask["options"] if o["key"] not in NEEDS_TEXT]
    texted = [o for o in ask["options"] if o["key"] in NEEDS_TEXT]
    buttons = [
        _button(row, ask["id"], option["key"], option["label"], i == 0, allowed, on)
        for i, option in enumerate(plain)
    ]
    elements += _option_rows(buttons)
    # 只有它一个时就是输入框旁边的「提交」；和「退回」「拒绝」并排时还叫「其他」，免得分不清。
    other = [{"key": OTHER, "label": "其他" if texted else "提交"}] if ask.get("other") else []
    # 输入框和提交按钮的名字在整张卡里不能重复（飞书会拒收整张卡）：一张进展卡上可能同时
    # 画着几个问题，名字带上问题所在任务的 id。
    submits = [
        {
            **_button(row, ask["id"], o["key"], o["label"], False, allowed, on),
            "name": f"{o['key']}_{row.id}",
            "form_action_type": "submit",
        }
        for o in [*texted, *other]
    ]
    if submits:
        entry: dict[str, Any] = {
            "tag": "input",
            "name": text_field(row),
            "max_length": TEXT_MAX,
            # 撑满所在的那一格：默认宽度在电脑上只占半行，和右边的按钮之间空一大截。
            "width": "fill",
            "placeholder": {
                "tag": "plain_text",
                # 和「提交」同一行时格子窄，提示写长了手机上会被截断。
                "content": "写一句理由，或你自己的答案" if texted else "其他答案",
            },
        }
        # 只有「其他」时输入框和提交按钮放同一行；还有要写理由的选项（退回、拒绝）时，
        # 输入框一行、几个按钮并排一行。
        body = (
            [_side_by_side([entry, submits[0]], weights=[3, 1])]
            if len(submits) == 1
            else [entry, _side_by_side(submits, narrow="bisect")]
        )
        elements.append({"tag": "form", "name": f"work_task_{row.id}", "elements": body})
    return elements


# 一行并排几个按钮时，每个按钮最多放几个字，放不下飞书会截成省略号。NARROW 按窄屏手机实测：
# 两个一行放得下 4 个字，三个一行只放得下 2 个字。WIDE 是电脑上的宽度。
NARROW_LABEL_MAX = {2: 4, 3: 2}
WIDE_LABEL_MAX = {2: 12, 3: 8, 4: 6}


def _label_width(button: dict[str, Any]) -> float:
    """按钮上的字有多宽：汉字算一个，字母数字算半个。"""
    return sum(1.0 if ord(c) > 0x2E7F else 0.5 for c in str(button["text"]["content"]))


def _option_rows(buttons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """选项按钮怎么排：每一行都撑满卡片宽度，字不能被截断。

    手机上也放得下的并排一行（四个短的排成两行两个）；只有电脑上放得下的，电脑上并排、
    手机上自动变成一个一行；电脑上也放不下的一个一行。
    """
    count = len(buttons)
    widest = max((_label_width(b) for b in buttons), default=0.0)
    if count in NARROW_LABEL_MAX and widest <= NARROW_LABEL_MAX[count]:
        return [_side_by_side(buttons, narrow="none")]
    if count == 4 and widest <= NARROW_LABEL_MAX[2]:
        return [_side_by_side(buttons, narrow="bisect")]
    if count in WIDE_LABEL_MAX and widest <= WIDE_LABEL_MAX[count]:
        return [_side_by_side(buttons, narrow="stretch")]
    for button in buttons:
        button["width"] = "fill"
    return buttons


def _side_by_side(
    cells: list[dict[str, Any]], weights: list[int] | None = None, *, narrow: str = "none"
) -> dict[str, Any]:
    """几块并排成一行，不给 `weights` 时等宽。

    `narrow` 是窄屏上怎么办（飞书的 flex_mode）：`none` 仍然一行，`bisect` 两个一行，
    `stretch` 一个一行。
    """
    for cell in cells:
        if cell.get("tag") == "button":
            cell["width"] = "fill"
    return {
        "tag": "column_set",
        "flex_mode": narrow,
        "horizontal_spacing": "8px",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": (weights or [1] * len(cells))[n],
                "vertical_align": "center",
                "elements": [cell],
            }
            for n, cell in enumerate(cells)
        ],
    }


def ask_card(row: WorkTask, ask: dict[str, Any], base_url: str) -> dict[str, Any]:
    """发到被问的人私聊的那张卡：一个问题、几个选项按钮。"""
    elements = ask_elements(row, ask)
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


async def group_mention(
    session: AsyncSession, root: WorkTask, bot_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[str, list[str]] | None:
    """在这件事的来源群里点名一个人：返回点名用的那一段和他的飞书 user_id；他不在群里返回 None。

    `bot_id` 是发卡的员工（它收得到群里所有人的消息）：被问的人在它这里的 open_id 从入站记录里
    取。他没在这个群说过话（可能不在群里）就不在群里问他。只认带 open_id 的记录：任务开工、
    续做时平台写的合成事件也记在这个群和这个人名下，但没有 open_id，不能拿最新的一条当准。
    """
    pid = await feishu_user_id(session, user_id)
    if not pid:
        return None
    spoke = (
        InboundEvent.bot_id == bot_id,
        InboundEvent.chat_id == root.chat_id,
        InboundEvent.sender_platform_user_id == pid,
    )
    open_id = await session.scalar(
        select(InboundEvent.sender_open_id)
        .where(*spoke, InboundEvent.sender_open_id.is_not(None))
        .order_by(InboundEvent.id.desc())
        .limit(1)
    )
    if open_id is None and (
        await session.scalar(select(InboundEvent.id).where(*spoke).limit(1)) is None
    ):
        return None
    person = await session.get(User, user_id)
    return (f"<at id={open_id}></at>" if open_id else f"**{name_of(person)}**"), [pid]


async def _send_ask(session: AsyncSession, row: WorkTask, base_url: str) -> None:
    ask = row.ask or {}
    to = ask.get("to_user_id")
    pid = await feishu_user_id(session, uuid.UUID(to)) if to else None
    if to and await progress.shows_ask(session, row, uuid.UUID(to)):
        # 从群里发起的事：问题画在群里那张进展卡上，卡片挪到最新的位置并 @ 他，不另发一张。
        await progress.refresh(session, row, base_url=base_url)
        return
    bot = await sender(session, row)
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
    """完成、取消：告诉发起人、代表人和关注人（做这件事的人自己不用再收）。

    AI 员工拆出来的子任务和它问的问题，结果交回上级任务，不再逐件私信代表人；从群里发起的事
    在群里有进展卡。这两类只通知自己点了关注的人。
    """
    bot = await sender(session, row)
    if bot is None:
        return
    watchers = set(
        await session.scalars(
            select(WorkTaskWatcher.user_id).where(WorkTaskWatcher.work_task_id == row.id)
        )
    )
    quiet = row.requester_bot_id is not None or (row.parent_id is None and progress.group_root(row))
    people = watchers if quiet else {row.requester_user_id, row.principal_user_id} | watchers
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
