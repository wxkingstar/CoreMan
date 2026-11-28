"""群里的进展卡：从群里发起、拆了子任务的事，在群里留一张卡，原地更新。

子任务不在群里出声（结果由平台交回上级），群里的人只看得到开头那一句，之后很久没有动静，
不知道事情还在不在做、卡在谁那里。这张卡按任务此刻的状态画：每件子任务一行，谁在做、做完了
没有、在等谁；状态一变就改这张卡，不另发消息。内容全部取自任务上给人看的那条线（标题、
一句话结果、在等什么），不经过模型。

只管调度员交接出来的任务（来源 `dispatch`，有来源群）：根任务第一次有子任务时发卡，回复在
发起人的那条消息下面；之后根任务或它下面任何一件事有变化都重画。卡片由根任务的执行员工发
（它在这个群里），更新沿用发卡的那位。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.api_tasks import reply_to
from coreman.core.bus import outbox
from coreman.core.db.models import Bot, InboundEvent, OutboxItem, User, WorkTask
from coreman.core.work_tasks.service import clip, name_of

ASK_PREFIX = "[问题]"
LINE_MAX = 120
# 一张卡上最多列这么多件：再多就只说还有几件，细的去任务页看。
MAX_LINES = 12


def _key(root: WorkTask) -> str:
    return f"work-task:{root.id}:progress"


def is_question(row: WorkTask) -> bool:
    """AI 员工问人的那一件（`ask_human` 建的）。"""
    return row.assignee_user_id is not None and row.brief.startswith(ASK_PREFIX)


async def _root(session: AsyncSession, row: WorkTask) -> WorkTask | None:
    if row.parent_id is None:
        return row
    return await session.get(WorkTask, row.root_id) if row.root_id else None


def group_root(root: WorkTask | None) -> bool:
    """这件事是不是从群里发起、由调度员交接出来的：进展卡和提问卡都发在那个群里。"""
    return (
        root is not None
        and root.source == "dispatch"
        and root.platform == "feishu"
        and bool(root.chat_id)
        and root.tracked
        and root.status != "shadow"
    )


async def _who(session: AsyncSession, row: WorkTask) -> str:
    if row.assignee_bot_id:
        bot = await session.get(Bot, row.assignee_bot_id)
        return bot.name if bot else ""
    if row.assignee_user_id:
        return name_of(await session.get(User, row.assignee_user_id))
    return ""


async def _line(session: AsyncSession, child: WorkTask, asked: list[str]) -> tuple[str, str]:
    """一件子任务的那一行，以及它算哪一类（done / waiting_human / running / cancelled）。"""
    who = await _who(session, child)
    if is_question(child):
        question = child.title.removeprefix("问：")
        if child.status == "done":
            return f"✅ {who} 已回答：{question}", "done"
        if child.status == "cancelled":
            return f"⏹ 不用{who}回答了：{question}", "cancelled"
        return f"🟡 等 **{who}** 回答：{question}", "waiting_human"
    head = f"{child.title} — {who}" if who else child.title
    if child.status == "done":
        return f"✅ {head}" + (f"：{child.summary}" if child.summary else ""), "done"
    if child.status == "cancelled":
        return f"⏹ {head}（已取消）", "cancelled"
    waiting = child.waiting_on or {}
    if asked:
        return f"🟡 {head}，在等 **{'、'.join(asked)}** 回答", "waiting_human"
    if child.status == "waiting" and waiting.get("kind") == "human":
        return f"🟡 {head}：{waiting.get('human_text') or '等人处理'}", "waiting_human"
    if child.status == "review":
        return f"🟡 {head}，等验收", "waiting_human"
    if child.status == "todo" and child.assignee_user_id:
        return f"🟡 等 **{who}** 接手：{child.title}", "waiting_human"
    return f"⏳ {head}", "running"


async def _asked(session: AsyncSession, children: list[WorkTask]) -> dict[int, list[str]]:
    """每件子任务自己还在问的人：它问了人、在等回答时，卡上要写出在等谁。"""
    ids = [c.id for c in children if not is_question(c)]
    if not ids:
        return {}
    pending = await session.scalars(
        select(WorkTask)
        .where(
            WorkTask.parent_id.in_(ids),
            WorkTask.assignee_user_id.is_not(None),
            WorkTask.ask.is_not(None),
            WorkTask.status.not_in(("done", "cancelled", "shadow")),
        )
        .order_by(WorkTask.id)
    )
    out: dict[int, list[str]] = {}
    for item in pending:
        name = name_of(await session.get(User, item.assignee_user_id))
        names = out.setdefault(int(item.parent_id or 0), [])
        if name not in names:
            names.append(name)
    return out


async def card(session: AsyncSession, root: WorkTask, base_url: str) -> dict[str, Any] | None:
    """按此刻的状态画卡；根任务没有子任务时返回 None（没有可列的，不发卡）。"""
    children = list(
        await session.scalars(
            select(WorkTask)
            .where(WorkTask.parent_id == root.id, WorkTask.status != "shadow")
            .order_by(WorkTask.id)
        )
    )
    if not children:
        return None
    asked = await _asked(session, children)
    lines: list[str] = []
    kinds: list[str] = []
    for child in children:
        text, kind = await _line(session, child, asked.get(child.id, []))
        lines.append(clip(text, LINE_MAX))
        kinds.append(kind)
    live = [k for k in kinds if k != "cancelled"]
    done = sum(k == "done" for k in live)
    if root.status == "done":
        state, template = "已完成", "green"
    elif root.status == "cancelled":
        state, template = "已取消", "grey"
    elif "waiting_human" in kinds or (
        root.status == "waiting" and (root.waiting_on or {}).get("kind") == "human"
    ):
        state, template = "等人处理", "orange"
    else:
        state, template = "进行中", "blue"
    shown = lines[-MAX_LINES:]
    body = "\n".join(shown)
    if len(lines) > len(shown):
        body = f"前面还有 {len(lines) - len(shown)} 件，见任务页\n" + body
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": body}]
    if root.status == "done" and root.summary:
        elements.append({"tag": "markdown", "content": f"**结果：**{root.summary}"})
    url = f"{base_url.rstrip('/')}/work-tasks/{root.code}"
    elements.append({"tag": "markdown", "content": f"[在任务页查看 {root.code}]({url})"})
    return {
        "schema": "2.0",
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": clip(f"{root.code} {root.title}", 60)},
            "subtitle": {
                "tag": "plain_text",
                "content": f"{state} · 已完成 {done}/{len(live)} 件" if live else state,
            },
        },
        "body": {"elements": elements},
    }


async def refresh(session: AsyncSession, row: WorkTask, *, base_url: str) -> None:
    """`row` 所在的那件事有变化：没有进展卡就发一张，有就原地更新。不符合条件什么也不做。"""
    if not base_url:
        return
    root = await _root(session, row)
    if root is None or not group_root(root):
        return
    await session.flush()
    content = await card(session, root, base_url)
    if content is None:
        return
    first = await session.scalar(select(OutboxItem).where(OutboxItem.dedupe_key == _key(root)))
    if first is None:
        bot_id = root.assignee_bot_id or root.dispatcher_bot_id
        if bot_id is None:
            return
        origin = (
            await session.get(InboundEvent, root.origin_event_id) if root.origin_event_id else None
        )
        message_id = reply_to(origin.platform_msg_id) if origin is not None else None
        target: dict[str, Any] = {"chat_id": root.chat_id}
        payload: dict[str, Any] = {"card": content}
        if message_id and not message_id.startswith("action:"):
            # 回复在发起人的那条消息下面：一眼看出是哪件事的进展。
            target["message_id"] = message_id
            payload["_reply"] = True
        await outbox.add(
            session,
            bot_id=bot_id,
            platform="feishu",
            kind="send",
            dedupe_key=_key(root),
            target=target,
            payload=payload,
        )
        return
    if first.status == "pending":
        # 卡片还没发出去：直接换成最新的内容，发出去就是此刻的状态。
        first.payload = {**first.payload, "card": content}
        return
    message_id = (first.payload or {}).get("_feishu_message_id")
    if first.status != "sent" or not message_id:
        return
    last = await session.scalar(
        select(OutboxItem)
        .where(OutboxItem.dedupe_key.like(f"{_key(root)}:%"))
        .order_by(OutboxItem.id.desc())
        .limit(1)
    )
    shown = (last.payload if last is not None else first.payload or {}).get("card")
    if shown == content:
        return
    # 编号跟着上一次更新走：同一个状态只排一次。
    await outbox.add(
        session,
        bot_id=first.bot_id,
        platform="feishu",
        kind="card_update",
        dedupe_key=f"{_key(root)}:{last.id if last is not None else 0}",
        target={"chat_id": "", "message_id": message_id},
        payload={"card": content},
    )
