"""群里的进展卡：从群里发起、拆了子任务的事，在群里留一张卡，原地更新。

子任务不在群里出声（结果由平台交回上级），群里的人只看得到开头那一句，之后很久没有动静，
不知道事情还在不在做、卡在谁那里。这张卡是一张待办清单，按任务此刻的状态画：每件子任务一行，
做完的打勾、写用了多久和一句话结果，在做的写几点开始和最近一次进展，在等人的写等谁、从几点
起；后面还没开始的步骤（牵头的员工用 `update_task` 的 `plan` 写的）列在最后。状态一变就改
这张卡，不另发消息。内容全部取自任务上给人看的那条线，不经过模型；时间用飞书的
`<local_datetime>`，按看的人的时区显示。

只管调度员交接出来的任务（来源 `dispatch`，有来源群）：根任务第一次有子任务时发卡，回复在
发起人的那条消息下面；之后根任务或它下面任何一件事有变化都重画。卡片由根任务的执行员工发
（它在这个群里），更新沿用发卡的那位。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.api_tasks import reply_to
from coreman.core.bus import outbox
from coreman.core.chat.liveness import clock
from coreman.core.db.models import Bot, InboundEvent, OutboxItem, User, WorkTask, WorkTaskEvent
from coreman.core.work_tasks.service import clip, name_of

ASK_PREFIX = "[问题]"
LINE_MAX = 120
# 做完的事最多列这么多件（最近的）：再早的只说还有几件，细的去任务页看。
MAX_DONE = 8
# 后面还没开始的步骤最多列这么多条。
MAX_PLAN = 8
PLAN_KIND = "plan"


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


def _took(child: WorkTask) -> str:
    """做完的一件用了多久。"""
    start = child.claimed_at or child.created_at
    if child.closed_at is None or start is None:
        return ""
    minutes = int((child.closed_at - start).total_seconds() // 60)
    if minutes < 1:
        return "不到 1 分钟"
    if minutes < 60:
        return f"{minutes} 分钟"
    return f"{minutes // 60} 小时 {minutes % 60} 分钟" if minutes % 60 else f"{minutes // 60} 小时"


async def _latest(session: AsyncSession, child: WorkTask, now: datetime) -> str:
    """在做的一件最近一次给人看的进展（员工用 `update_task` 写的那一句）和时间。"""
    event = await session.scalar(
        select(WorkTaskEvent)
        .where(
            WorkTaskEvent.work_task_id == child.id,
            WorkTaskEvent.kind == "progress",
            WorkTaskEvent.human_text != "",
        )
        .order_by(WorkTaskEvent.id.desc())
        .limit(1)
    )
    if event is None:
        return ""
    return f"，{clip(event.human_text, 40)}（{clock(event.created_at, now)}）"


async def _line(
    session: AsyncSession, child: WorkTask, asked: list[str], now: datetime
) -> tuple[str, str]:
    """一件子任务的那一行，以及它算哪一类（done / waiting_human / running / cancelled）。

    标题、结果这些人写的字先按长度截断，时间标签接在后面：标签不能被截断。
    """
    who = await _who(session, child)
    since = clock(child.claimed_at or child.created_at, now)
    if is_question(child):
        question = child.title.removeprefix("问：")
        if child.status == "done":
            # 回答写在后面：群里的人要知道定了什么，不只是「答过了」。
            answer = f" → {child.summary}" if child.summary else ""
            return clip(f"✅ {who} 定了：{question}{answer}", LINE_MAX), "done"
        if child.status == "cancelled":
            return clip(f"⏹ 不用{who}回答了：{question}", LINE_MAX), "cancelled"
        text = clip(f"🟡 等 **{who}** 回答：{question}", LINE_MAX)
        return f"{text} · {clock(child.created_at, now)} 问的", "waiting_human"
    head = f"{child.title} — {who}" if who else child.title
    if child.status == "done":
        text = clip(f"✅ {head}" + (f"：{child.summary}" if child.summary else ""), LINE_MAX)
        took = _took(child)
        return text + (f"（用时 {took}）" if took else ""), "done"
    if child.status == "cancelled":
        return clip(f"⏹ {head}（已取消）", LINE_MAX), "cancelled"
    waiting = child.waiting_on or {}
    if asked:
        text = clip(f"🟡 {head}，在等 **{'、'.join(asked)}** 回答", LINE_MAX)
        return f"{text} · {since} 开始", "waiting_human"
    if child.status == "waiting" and waiting.get("kind") == "human":
        text = clip(f"🟡 {head}：{waiting.get('human_text') or '等人处理'}", LINE_MAX)
        return f"{text} · {since} 开始", "waiting_human"
    if child.status == "review":
        return clip(f"🟡 {head}，等验收", LINE_MAX), "waiting_human"
    if child.status == "todo" and child.assignee_user_id:
        return clip(f"🟡 等 **{who}** 接手：{child.title}", LINE_MAX), "waiting_human"
    return (
        f"{clip(f'⏳ {head}', LINE_MAX)} · {since} 开始{await _latest(session, child, now)}",
        "running",
    )


async def plan_steps(session: AsyncSession, root: WorkTask, taken: set[str]) -> list[str]:
    """牵头的员工写下的、后面还没开始的步骤；已经拆成子任务的（标题相同）不再列。"""
    event = await session.scalar(
        select(WorkTaskEvent)
        .where(WorkTaskEvent.work_task_id == root.id, WorkTaskEvent.kind == PLAN_KIND)
        .order_by(WorkTaskEvent.id.desc())
        .limit(1)
    )
    steps = (event.agent_detail or {}).get("steps") if event is not None else None
    if not isinstance(steps, list):
        return []
    return [str(step) for step in steps if isinstance(step, str) and step not in taken][:MAX_PLAN]


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
    now = datetime.now(UTC)
    lines: list[str] = []
    kinds: list[str] = []
    for child in children:
        text, kind = await _line(session, child, asked.get(child.id, []), now)
        lines.append(text)
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
    # 按先后排成一张清单：做完的打勾在上，在做和等人的接着，后面的步骤在最后。
    # 做完的太多时只留最近几件，免得正在做的那几行被挤得很远。
    finished = [n for n, k in enumerate(kinds) if k in ("done", "cancelled")]
    hidden = set(finished[:-MAX_DONE]) if len(finished) > MAX_DONE else set()
    shown = [text for n, text in enumerate(lines) if n not in hidden]
    if hidden:
        shown.insert(0, f"✅ 更早做完的 {len(hidden)} 件见任务页")
    if root.status not in ("done", "cancelled"):
        taken = {child.title for child in children}
        shown += [f"☐ {clip(step, LINE_MAX)}" for step in await plan_steps(session, root, taken)]
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": "\n".join(shown)}]
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
