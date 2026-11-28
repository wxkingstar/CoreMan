"""群里的进展卡：从群里发起、拆了子任务的事，在群里只用一张卡说清全貌。

子任务不在群里出声（结果由平台交回上级），群里的人只看得到开头那一句，之后很久没有动静，
不知道事情还在不在做、卡在谁那里。这张卡是一张待办清单，按任务此刻的状态画：每件子任务一行，
做完的打勾、写用了多久和一句话结果，在做的写几点开始和最近一次进展，在等人的写等谁、从几点
起；后面还没开始的步骤（牵头的员工用 `update_task` 的 `plan` 写的）列在最后。状态一变就改
这张卡，不另发消息。内容全部取自任务上给人看的那条线，不经过模型；时间用飞书的
`<local_datetime>`，按看的人的时区显示。

一件事一张卡，分几块：目标；现在要人定的事（问题和按钮直接画在卡上，见 `cards`）；正在做的；
接下来的步骤；已完成的；牵头员工最近说的一句。标题下面是状态和进度。

只管调度员交接出来的任务（来源 `dispatch`，有来源群）：根任务第一次有子任务时发卡，回复在
发起人的那条消息下面；之后根任务或它下面任何一件事有变化都重画。平时原地更新；两种情况把卡
挪到群里最新的位置（新发一张，旧的那张缩成一行「已更新，最新的在下面」，按钮随之失效）：
有新的事要人定（更新卡片飞书不提醒人，新发才会 @ 到），或者内容有实质变化而卡片后面已经
隔了 `BURIED_AFTER` 条以上消息。只是「最近说的一句」变了不算实质变化。卡片由这个群的调度员发
（它在群里，收得到所有人的消息，取得到被问的人的 open_id），更新沿用发卡的那位。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.api_tasks import SYNTHETIC_PREFIX, WORK_SYNTHETIC_PREFIX, reply_to
from coreman.core.api_tasks.service import feishu_user_id
from coreman.core.bus import outbox
from coreman.core.chat.liveness import clock
from coreman.core.db.models import (
    WORK_TASK_OPEN,
    Bot,
    InboundEvent,
    OutboxItem,
    User,
    WorkTask,
    WorkTaskEvent,
)
from coreman.core.work_tasks.service import clip, name_of

ASK_PREFIX = "[问题]"
LINE_MAX = 120
# 做完的事最多列这么多件（最近的）：再早的只说还有几件，细的去任务页看。
MAX_DONE = 8
# 后面还没开始的步骤最多列这么多条。
MAX_PLAN = 8
PLAN_KIND = "plan"
# 卡片后面隔了这么多条消息、内容又有实质变化，就挪到最新的位置。
BURIED_AFTER = 10
# 进度条的格数。
BAR_CELLS = 8


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
        # 做完的一步只写是哪一步、谁做的、用了多久：每条后面再跟一句结果，一张卡就挤满了字，
        # 结果在任务页上看。
        took = _took(child)
        return clip(f"✅ {head}", LINE_MAX) + (f"（用时 {took}）" if took else ""), "done"
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
        if not item.ask:
            continue  # JSON 的 null：没有待答的问题
        name = name_of(await session.get(User, item.assignee_user_id))
        names = out.setdefault(int(item.parent_id or 0), [])
        if name not in names:
            names.append(name)
    return out


@dataclass(frozen=True)
class Rendered:
    card: dict[str, Any]
    # 实质内容的指纹：每一步的状态、待定的问题、后面的步骤、整件事的状态。
    sig: dict[str, Any]
    # 需要人出手的事（待答的问题、卡住等人、整件事办完）：出现新的就把卡挪到最新位置并 @ 人。
    attn: list[str]


def sender_id(root: WorkTask) -> uuid.UUID | None:
    """谁发这张卡：这个群的调度员，没有就是根任务的执行员工。"""
    return root.dispatcher_bot_id or root.assignee_bot_id


async def _pending_asks(session: AsyncSession, root: WorkTask) -> list[WorkTask]:
    """这件事上上下下还没人答的问题（问人、派给同事要不要接、待验收）。"""
    rows = await session.scalars(
        select(WorkTask)
        .where(
            or_(WorkTask.id == root.id, WorkTask.root_id == root.id),
            WorkTask.status.in_(WORK_TASK_OPEN),
        )
        .order_by(WorkTask.id)
    )
    # `ask` 是 JSON 列：没有问题时存的是 JSON 的 null，SQL 里判不出来，取出来再看。
    return [row for row in rows if row.ask and row.ask.get("to_user_id")]


async def shows_ask(session: AsyncSession, row: WorkTask, to: uuid.UUID) -> bool:
    """这个问题是不是画在群里的进展卡上（而不是私聊发卡）：从群里发起的事，被问的人在那个群里。"""
    from coreman.core.work_tasks import cards

    root = await _root(session, row)
    bot_id = sender_id(root) if root is not None else None
    if root is None or bot_id is None or not group_root(root):
        return False
    return await cards.group_mention(session, root, bot_id, to) is not None


def _bar(done: int, total: int) -> str:
    filled = round(BAR_CELLS * done / total) if total else 0
    return "▓" * filled + "░" * (BAR_CELLS - filled)


async def render(session: AsyncSession, root: WorkTask, base_url: str) -> Rendered | None:
    """按此刻的状态画卡；根任务下面什么都没有时返回 None（没有可列的，不发卡）。"""
    from coreman.core.work_tasks import cards

    children = list(
        await session.scalars(
            select(WorkTask)
            .where(WorkTask.parent_id == root.id, WorkTask.status != "shadow")
            .order_by(WorkTask.id)
        )
    )
    if not children:
        return None
    bot_id = sender_id(root)
    now = datetime.now(UTC)
    asked = await _asked(session, children)
    doing: list[str] = []
    finished: list[str] = []
    step_states: list[list[Any]] = []
    steps_done = steps_total = 0
    for child in children:
        text, kind = await _line(session, child, asked.get(child.id, []), now)
        if is_question(child):
            # 待答的问题画在「现在要你定」里；答过的进「已完成」。
            if kind in ("done", "cancelled"):
                finished.append(text)
            continue
        step_states.append([child.id, kind])
        if kind == "cancelled":
            finished.append(text)
            continue
        steps_total += 1
        if kind == "done":
            steps_done += 1
            finished.append(text)
        else:
            doing.append(text)
    closed = root.status in ("done", "cancelled")
    plan = [] if closed else await plan_steps(session, root, {c.title for c in children})
    steps_total += len(plan)

    # ---- 现在要人定的 ----
    attn: list[str] = []
    need: list[dict[str, Any]] = []
    private: list[str] = []
    principal = (
        await feishu_user_id(session, root.principal_user_id) if root.principal_user_id else None
    )
    for item in [] if closed else await _pending_asks(session, root):
        ask = item.ask or {}
        to = ask.get("to_user_id")
        found = (
            await cards.group_mention(session, root, bot_id, uuid.UUID(to))
            if to and bot_id is not None
            else None
        )
        person = name_of(await session.get(User, uuid.UUID(to))) if to else ""
        if found is None:
            # 被问的人不在这个群：问题私聊发给他了，这里只写在等谁。
            private.append(
                clip(f"🟡 等 **{person}** 回答（已私信）：{ask.get('question')}", LINE_MAX)
            )
            continue
        mention, allowed = found
        if principal and principal not in allowed:
            allowed = [*allowed, principal]  # 代表人可以代答
        attn.append(f"ask:{ask.get('id')}")
        need += cards.ask_elements(
            item, ask, mention=f"{mention} 请你来定", allowed=allowed, on=root
        )
    waiting = root.waiting_on or {}
    if root.status == "waiting" and waiting.get("kind") == "human" and root.principal_user_id:
        found = (
            await cards.group_mention(session, root, bot_id, root.principal_user_id)
            if bot_id is not None
            else None
        )
        who = (
            found[0] if found else f"**{name_of(await session.get(User, root.principal_user_id))}**"
        )
        text = clip(str(waiting.get("human_text") or "等你处理"), LINE_MAX)
        attn.append(f"wait:{text}")
        need.append({"tag": "markdown", "content": f"{who} {text}（在群里回复就行）"})
    if root.status == "done":
        attn.append("done")

    # ---- 状态与进度 ----
    if root.status == "done":
        state, template = "已完成", "green"
    elif root.status == "cancelled":
        state, template = "已取消", "grey"
    elif need or private or any(kind == "waiting_human" for _, kind in step_states):
        state, template = "等人处理", "orange"
    else:
        state, template = "进行中", "blue"
    progress = f" {_bar(steps_done, steps_total)} {steps_done}/{steps_total}" if steps_total else ""

    elements: list[dict[str, Any]] = []
    if root.status == "done":
        requester = (
            await cards.group_mention(session, root, bot_id, root.principal_user_id)
            if bot_id is not None and root.principal_user_id
            else None
        )
        lead = f"{requester[0]} 这件事办完了。" if requester else ""
        elements.append(
            {"tag": "markdown", "content": f"{lead}**结果**：{root.summary or '已完成'}"}
        )
        # 交付时写下的、还要人处理的事。完整汇报是给 Agent 看的那条线，留在任务页，不贴到群里。
        submitted = await session.scalar(
            select(WorkTaskEvent)
            .where(WorkTaskEvent.work_task_id == root.id, WorkTaskEvent.kind == "submitted")
            .order_by(WorkTaskEvent.id.desc())
            .limit(1)
        )
        left = (submitted.agent_detail or {}).get("followups") if submitted is not None else None
        if isinstance(left, list) and left:
            todo = [f"- {clip(str(x), LINE_MAX)}" for x in left]
            elements.append({"tag": "markdown", "content": "\n".join(["**还要人处理**", *todo])})
    elif root.summary:
        elements.append({"tag": "markdown", "content": f"**目标**：{root.summary}"})
    if need:
        elements.append({"tag": "markdown", "content": "**现在要你定**"})
        elements += need
    if doing or private:
        elements.append({"tag": "markdown", "content": "\n".join(["**正在做**", *private, *doing])})
    if plan:
        elements.append(
            {
                "tag": "markdown",
                "content": "\n".join(["**接下来**", *(f"☐ {clip(x, LINE_MAX)}" for x in plan)]),
            }
        )
    if finished and root.status == "done":
        # 办完了：结果在上面，明细不再逐条列。
        elements.append(
            {"tag": "markdown", "content": f"共 {steps_done} 步，都做完了，明细见任务页"}
        )
    elif finished:
        shown = finished[-MAX_DONE:]
        earlier = len(finished) - len(shown)
        title = f"**已完成（{len(finished)}）**" + (
            f"，更早的 {earlier} 件见任务页" if earlier else ""
        )
        elements.append({"tag": "markdown", "content": "\n".join([title, *shown])})
    if not closed:
        latest = await session.scalar(
            select(WorkTaskEvent)
            .where(
                WorkTaskEvent.work_task_id == root.id,
                WorkTaskEvent.kind == "progress",
                WorkTaskEvent.human_text != "",
            )
            .order_by(WorkTaskEvent.id.desc())
            .limit(1)
        )
        if latest is not None:
            line = f"最新：{clip(latest.human_text, LINE_MAX)}（{clock(latest.created_at, now)}）"
            elements.append({"tag": "markdown", "content": f"<font color='grey'>{line}</font>"})
    url = f"{base_url.rstrip('/')}/work-tasks/{root.code}"
    elements.append({"tag": "markdown", "content": f"[在任务页查看 {root.code}]({url})"})
    content = {
        "schema": "2.0",
        # 卡上的按钮按这个任务 id 核对「这张卡是我们发的」，见 worker 的 card_actions。
        "task_id": f"personal:{root.id}",
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": clip(f"{root.code} {root.title}", 60)},
            "subtitle": {"tag": "plain_text", "content": f"{state}{progress}"},
        },
        "body": {"elements": elements},
    }
    sig = {"steps": step_states, "asks": sorted(attn), "plan": plan, "status": root.status}
    return Rendered(content, sig, attn)


def stub(root: WorkTask, base_url: str) -> dict[str, Any]:
    """卡挪走之后留在原处的那一行：往上翻到这里的人知道最新的在下面。"""
    url = f"{base_url.rstrip('/')}/work-tasks/{root.code}"
    return {
        "schema": "2.0",
        "header": {
            "template": "grey",
            "title": {"tag": "plain_text", "content": clip(f"{root.code} {root.title}", 60)},
            "subtitle": {"tag": "plain_text", "content": "这张清单已更新，最新的在下面"},
        },
        "body": {
            "elements": [{"tag": "markdown", "content": f"[在任务页查看 {root.code}]({url})"}]
        },
    }


async def _buried(session: AsyncSession, root: WorkTask, current: OutboxItem) -> bool:
    """卡片后面是不是已经隔了很多消息（按调度员收到的群消息数，平台自己写的合成事件不算）。"""
    since = current.sent_at or current.created_at
    count = await session.scalar(
        select(func.count())
        .select_from(InboundEvent)
        .where(
            InboundEvent.bot_id == (root.dispatcher_bot_id or current.bot_id),
            InboundEvent.chat_id == root.chat_id,
            InboundEvent.kind == "message",
            InboundEvent.received_at > since,
            InboundEvent.platform_msg_id.not_like(f"{WORK_SYNTHETIC_PREFIX}%"),
            InboundEvent.platform_msg_id.not_like(f"{SYNTHETIC_PREFIX}%"),
        )
    )
    return int(count or 0) >= BURIED_AFTER


async def current_card(session: AsyncSession, root: WorkTask) -> OutboxItem | None:
    """这件事在群里最新的那张卡（已经发出去的）。"""
    items = await session.scalars(
        select(OutboxItem)
        .where(
            or_(OutboxItem.dedupe_key == _key(root), OutboxItem.dedupe_key.like(f"{_key(root)}:%")),
            OutboxItem.kind == "send",
            OutboxItem.status == "sent",
        )
        .order_by(OutboxItem.id.desc())
        .limit(1)
    )
    return items.first()


async def refresh(session: AsyncSession, row: WorkTask, *, base_url: str) -> None:
    """`row` 所在的那件事有变化：没有进展卡就发一张，有就更新或挪到最新位置。"""
    if not base_url:
        return
    root = await _root(session, row)
    if root is None or not group_root(root):
        return
    await session.flush()
    rendered = await render(session, root, base_url)
    bot_id = sender_id(root)
    if rendered is None or bot_id is None:
        return
    meta = {"_progress": {"sig": rendered.sig, "attn": rendered.attn}}
    items = list(
        await session.scalars(
            select(OutboxItem)
            .where(
                or_(
                    OutboxItem.dedupe_key == _key(root),
                    OutboxItem.dedupe_key.like(f"{_key(root)}:%"),
                )
            )
            .order_by(OutboxItem.id)
        )
    )
    sends = [item for item in items if item.kind == "send"]
    current = sends[-1] if sends else None

    async def post(key: str) -> None:
        origin = (
            await session.get(InboundEvent, root.origin_event_id) if root.origin_event_id else None
        )
        message_id = reply_to(origin.platform_msg_id) if origin is not None else None
        target: dict[str, Any] = {"chat_id": root.chat_id}
        payload: dict[str, Any] = {"card": rendered.card, **meta}
        if message_id and not message_id.startswith("action:"):
            # 回复在发起人的那条消息下面：一眼看出是哪件事的进展。
            target["message_id"] = message_id
            payload["_reply"] = True
        await outbox.add(
            session,
            bot_id=current.bot_id if current is not None else bot_id,
            platform="feishu",
            kind="send",
            dedupe_key=key,
            target=target,
            payload=payload,
        )

    if current is None:
        await post(_key(root))
        return
    if current.status == "pending":
        # 卡片还没发出去：直接换成最新的内容，发出去就是此刻的状态。
        current.payload = {**current.payload, "card": rendered.card, **meta}
        return
    message_id = (current.payload or {}).get("_feishu_message_id")
    if current.status != "sent" or not message_id:
        return
    shown = [item for item in items if not (item.payload or {}).get("_progress_stub")][-1]
    before = (shown.payload or {}).get("_progress") or {}
    if (shown.payload or {}).get("card") == rendered.card:
        return
    fresh = [key for key in rendered.attn if key not in (before.get("attn") or [])]
    changed = rendered.sig != before.get("sig")
    if fresh or (changed and await _buried(session, root, current)):
        # 挪到最新的位置：新发一张，旧的缩成一行，旧卡上的按钮随之失效。
        await post(f"{_key(root)}:post:{items[-1].id}")
        await outbox.add(
            session,
            bot_id=current.bot_id,
            platform="feishu",
            kind="card_update",
            dedupe_key=f"{_key(root)}:stub:{current.id}",
            target={"chat_id": "", "message_id": message_id},
            payload={"card": stub(root, base_url), "_progress_stub": True},
        )
        return
    await outbox.add(
        session,
        bot_id=current.bot_id,
        platform="feishu",
        kind="card_update",
        dedupe_key=f"{_key(root)}:u:{items[-1].id}",
        target={"chat_id": "", "message_id": message_id},
        payload={"card": rendered.card, **meta},
    )
