"""AI 员工在做任务的那一轮里用的工具（第二期）：找人、拆子任务、问人、写进展、交付、转交、等结果。

只挂在任务会话（`work:<编号>`）里、而且这件事此刻在这位员工手上时；操作的永远是当前这件事
和它的子任务。人话字段有长度上限，超了返回 stop=false 让员工改短再交，平台不替它截断。
见 docs/superpowers/specs/2026-11-25-company-task-platform.md §5.2、§5.3、§6。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.api_tasks.service import allowed_to_use
from coreman.core.assistants.records import is_assistant
from coreman.core.chat.dispatch import is_lead
from coreman.core.db.models import WORK_TASK_OPEN, Bot, User, WorkTask
from coreman.core.work_tasks import cards, execute, progress
from coreman.core.work_tasks.service import (
    SUMMARY_MAX,
    TITLE_MAX,
    WorkTaskError,
    add_event,
    ask_accept,
    clip,
    make_ask,
    name_of,
)

MAX_DEPTH = 3
MAX_OPEN_CHILDREN = 5
MAX_ASKS_PER_CHAIN = 3
# 调度员牵头的任务是分几步推进的目标，要人定的事比一件普通任务多。
MAX_ASKS_PER_LEAD_CHAIN = 30
FOLLOWUPS_MAX = 5
MAX_PENDING_PER_PERSON = 5
PLAN_MAX = 8
QUESTION_MAX = 60
OPTION_MAX = 20
OPTIONS_MAX = 4
WHY_MAX = 40
LINE_MAX = 80
LONG_MAX = 20000
ARTIFACTS_MAX = 20

TASK_POLICY = """
# Working on a task
This turn works on a task registered on the task platform; this turn's context has its code,
principal, requirements and history. You act for the principal: use only their access, and never
widen it by passing work to someone else.

Two tracks. Everything a person reads (title, summary, result, question, why, reason, human_text)
is short and leads with the point: conclusion first, concrete numbers, no process. Everything an
agent reads (brief, context, detail, output, digest, report, handoff) is complete: whoever picks
it up must not need to come back and ask. Never write one track by cutting down the other.

Decide in this order:
1. Do it yourself if you can finish it with your own tools and access.
2. If another AI employee has the data, system access or expertise you lack, give them a subtask.
3. Ask a person only for a judgment, an approval, or information no tool or employee can give.
   Never ask what you can look up.
4. Hand off the whole task only when it falls outside your role entirely.

Subtasks (create_task):
- Call find_assignees first; pick by role, not by name alone.
- One subtask, one deliverable. Independent subtasks can run in parallel.
- brief covers: goal, background, known data and where it lives, constraints, expected output
  format, acceptance criteria, deadline.
- AI employees start immediately and silently; their result comes back to you. A person gets a
  Feishu card and may accept, pass it to an AI employee, reassign it, or decline with a reason.
  Give people only work that needs a human: approvals, contacting someone, actions in systems you
  cannot reach.

Questions (ask_human):
- Ask the person who actually knows; it goes to the principal if you name no one.
- One decision per question. Give mutually exclusive options when you can; the platform adds
  "Other".
- why says in one line why this person; context has everything needed to answer without opening
  anything else.

Waiting (wait_for):
- After the subtasks and questions you need, call wait_for and end the turn. Do not poll, sleep,
  or keep working on parts that depend on the answers.
- A subtask that stopped to wait for a person counts as a result: continue without it if you can,
  otherwise report stuck and say what is missing.

Ending a turn: do exactly one of these.
- Done: submit_result with result, output, digest, and artifacts for anything people should open.
  result is shown to everyone on the progress card in the group: one plain sentence that says
  what is now true, in words a colleague outside engineering understands ("退货已在测试环境
  走通，货回到了仓库"). No run ids, document or order numbers, branch names, field names,
  status codes or other internals; those belong in output.
  If the task has subtasks, report is required and covers: overall conclusion; for each subtask
  who did it, the conclusion, key data, where the output is, and whether you accepted it (failed
  and cancelled ones included); what you did yourself; open issues and risks; suggested next
  steps. Then reply in a line or two.
- Needs the principal's confirmation before an action with consequences (changing data,
  releasing, creating scheduled jobs): do not do it and do not submit; first line
  "<code> 待确认：" plus exactly what you will do. This turn's context may say which actions
  need no confirmation (for example work inside a test environment that belongs to a goal a
  dispatcher is leading) and which always do; follow it exactly.
- Stuck: first line "<code> 卡住了：" plus where it is stuck and who must do what. Do not submit.
- Handed off: handoff_task, then end the turn.
- Waiting: wait_for, then end the turn.

Other rules:
- Use update_task for milestones in long work or when your plan changes; write human_text only
  when a person needs to know.
- A reworked task after a failed review: fix exactly what the reason names and resubmit the full
  output, not a diff.
- digest is what a later agent reads when this task comes up again: background, conclusion, key
  data or where outputs live, decisions, open issues.
- Every member can see every task: keep personal data out of every field. Put large data in files
  and link them in artifacts.
- A tool result with stop=false: fix what it says and call again. stop=true is a refusal: do not
  get around it another way; adjust the plan (do it yourself, finish with what you have, or
  report stuck).
"""

DESCRIPTIONS = {
    "find_assignees": (
        "按名字或能力关键词找能接子任务的 AI 员工和同事，返回 type、id、名字与职责。"
        "AI 员工只列出代表人能用、接受协作的。"
        "同事只在给了 query 时才列出。"
    ),
    "create_task": (
        "给当前任务拆一个子任务：assignee={type: bot|human, id}。"
        "title ≤30 字、summary ≤60 字给人看（title 会出现在群里的进展卡上，写成人能看懂的一件事），"
        "brief 给接手的人看（背景、数据、约束、验收标准）。"
        "AI 员工接手立即开工（不出声，结果交回你）；"
        "同事会收到飞书卡片。之后调用 wait_for 等结果。"
        f"限制：任务链最多 {MAX_DEPTH} 层，"
        f"每个任务同时进行中的子任务和问题最多 {MAX_OPEN_CHILDREN} 个；"
        "不能派给自己或链上已有的 AI 员工；"
        f"每人同时最多 {MAX_PENDING_PER_PERSON} 件待回答的事。"
    ),
    "ask_human": (
        "问人一个问题：question ≤60 字，options 最多 4 个（每个 ≤20 字，平台自动加「其他」），"
        "why ≤40 字，context 写全背景。to_user_id 不填时问代表人。对方在飞书卡片或网页上回答，"
        "答复交回你。之后调用 wait_for。"
        f"限制：同一条任务链最多问人 {MAX_ASKS_PER_CHAIN} 次"
        f"（调度员牵头的任务 {MAX_ASKS_PER_LEAD_CHAIN} 次）；"
        f"每人同时最多 {MAX_PENDING_PER_PERSON} 件待回答的事。"
    ),
    "update_task": (
        "写进展：detail 是给 Agent 看的细节；human_text ≤80 字，只在人需要知道时写"
        "（会显示在群里清单上这件事的那一行，写成一句人话，如「正在等测试环境发布」）。"
        f"plan 可选：后面还没开始的步骤，每步一句人话（≤{TITLE_MAX} 字，最多 {PLAN_MAX} 步），"
        "显示在群里清单的最后；每次给出的是完整的剩余步骤，会替换上一次的。"
    ),
    "submit_result": (
        "交付当前任务：result ≤60 字给人看，一句人话说清现在怎样了，"
        "不写运行编号、单号、分支、字段名、状态码这些细节（放进 output）；output 完整输出；"
        "digest 记要（背景、结论、关键数据或产出位置、决定、遗留问题）；"
        "artifacts=[{title,url}]；有子任务时 report（完整汇报）必填。"
        f"followups 可选：交付后交给人自己去办的事，每条一句人话、写明要谁做什么（≤{LINE_MAX} 字，"
        f"最多 {FOLLOWUPS_MAX} 条），会显示在群里这件事的卡上。要人点头后你还得接着做的事"
        "（例如同意合并）不写在这里：先用 ask_human 问，等答复、办完再交付。"
        "只在做完时用；卡住或等确认时不要交付。"
    ),
    "handoff_task": (
        "把当前任务整件转交给另一位 AI 员工：to_bot_id，reason ≤80 字给人看，handoff 交接包"
        "（做到哪了、结论、还剩什么、产出在哪、踩过的坑、建议下一步）。之后结束这一轮。"
        "只能转给 AI 员工，而且要等所有子任务和问题都有结果。"
    ),
    "wait_for": (
        "等子任务和问题的结果：调用后结束这一轮，都有结果时平台把结果交回你接着做。"
        "没有在等的子任务或问题时会失败，这时直接交付。"
    ),
}
_STR = {"type": "string"}
SCHEMAS: dict[str, dict[str, Any]] = {
    "find_assignees": {
        "type": "object",
        "properties": {"query": _STR},
        "additionalProperties": False,
    },
    "create_task": {
        "type": "object",
        "properties": {
            "title": _STR,
            "summary": _STR,
            "brief": _STR,
            "assignee": {
                "type": "object",
                "properties": {"type": {"type": "string", "enum": ["bot", "human"]}, "id": _STR},
                "required": ["type", "id"],
            },
            "due_at": {"type": "string", "description": "ISO 8601，可选"},
        },
        "required": ["title", "brief", "assignee"],
        "additionalProperties": False,
    },
    "ask_human": {
        "type": "object",
        "properties": {
            "question": _STR,
            "options": {"type": "array", "items": _STR, "maxItems": OPTIONS_MAX},
            "why": _STR,
            "context": _STR,
            "to_user_id": _STR,
        },
        "required": ["question", "context"],
        "additionalProperties": False,
    },
    "update_task": {
        "type": "object",
        "properties": {
            "detail": _STR,
            "human_text": _STR,
            "plan": {"type": "array", "items": _STR},
        },
        "required": ["detail"],
        "additionalProperties": False,
    },
    "submit_result": {
        "type": "object",
        "properties": {
            "result": _STR,
            "output": _STR,
            "digest": _STR,
            "report": _STR,
            "followups": {"type": "array", "items": _STR},
            "artifacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"title": _STR, "url": _STR},
                    "required": ["title", "url"],
                },
                "maxItems": ARTIFACTS_MAX,
            },
        },
        "required": ["result", "output", "digest"],
        "additionalProperties": False,
    },
    "handoff_task": {
        "type": "object",
        "properties": {"to_bot_id": _STR, "reason": _STR, "handoff": _STR},
        "required": ["to_bot_id", "reason", "handoff"],
        "additionalProperties": False,
    },
    "wait_for": {"type": "object", "properties": {}, "additionalProperties": False},
}


class Invalid(Exception):
    """参数不合规：员工改了能重交（stop=false）。"""


def definitions() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": DESCRIPTIONS[name], "inputSchema": SCHEMAS[name]}
        for name in DESCRIPTIONS
    ]


def _text(args: dict[str, Any], key: str, limit: int, label: str, *, required: bool = True) -> str:
    value = args.get(key)
    value = " ".join(str(value).split()) if isinstance(value, str) else ""
    if required and not value:
        raise Invalid(f"{label}（{key}）不能为空")
    if len(value) > limit:
        raise Invalid(f"{label}（{key}）最多 {limit} 字，现在 {len(value)} 字：请写得更精炼")
    return value


def _long(args: dict[str, Any], key: str, label: str, *, required: bool = True) -> str:
    value = args.get(key)
    value = value.strip() if isinstance(value, str) else ""
    if required and not value:
        raise Invalid(f"{label}（{key}）不能为空，要写全")
    return value[:LONG_MAX]


def _uuid(value: Any, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise Invalid(f"{label}不是有效的编号，请先用 find_assignees 查") from None


async def _chain_bots(session: AsyncSession, row: WorkTask) -> set[uuid.UUID]:
    """这条任务链上已经出现过的 AI 员工：不能再派给它们（防止转圈）。"""
    bots: set[uuid.UUID] = set()
    current: WorkTask | None = row
    while current is not None:
        for bot_id in (current.assignee_bot_id, current.requester_bot_id):
            if bot_id:
                bots.add(bot_id)
        current = await session.get(WorkTask, current.parent_id) if current.parent_id else None
    return bots


async def _target_bot(
    session: AsyncSession, row: WorkTask, me: Bot, raw: Any, *, label: str
) -> Bot:
    bot = await session.get(Bot, _uuid(raw, label))
    if bot is None or not bot.enabled or bot.platform != "feishu" or is_assistant(bot):
        raise Invalid(f"{label}不存在、已停用或不能接任务")
    if bot.id in await _chain_bots(session, row) or bot.id == me.id:
        raise Invalid(f"「{bot.name}」已经在这条任务链上，不能再派给它（会转圈）")
    if not bot.accept_bot_help:
        raise Invalid(f"「{bot.name}」没有开启接受其他 AI 员工的协作")
    if row.principal_user_id is None or not await allowed_to_use(
        session, bot, row.principal_user_id
    ):
        raise Invalid(f"代表人不在「{bot.name}」的可用名单里")
    return bot


async def _check_room(session: AsyncSession, row: WorkTask) -> None:
    if row.depth + 1 > MAX_DEPTH:
        raise WorkTaskError(409, f"任务链最多 {MAX_DEPTH} 层，这里不能再往下拆")
    open_children = await session.scalar(
        select(func.count())
        .select_from(WorkTask)
        .where(WorkTask.parent_id == row.id, WorkTask.status.in_(WORK_TASK_OPEN))
    )
    if int(open_children or 0) >= MAX_OPEN_CHILDREN:
        raise WorkTaskError(409, f"同时进行中的子任务最多 {MAX_OPEN_CHILDREN} 个")


def _child(row: WorkTask, me: Bot, *, title: str, summary: str, brief: str) -> WorkTask:
    return WorkTask(
        title=title,
        summary=summary,
        brief=brief,
        status="todo",
        source="agent",
        requester_bot_id=me.id,
        requester_name=me.name,
        principal_user_id=row.principal_user_id,
        parent_id=row.id,
        root_id=row.root_id or row.id,
        depth=row.depth + 1,
        review_required=False,
        priority=row.priority,
    )


async def find_assignees(
    session: AsyncSession, row: WorkTask, me: Bot, args: dict[str, Any]
) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    like = f"%{query}%"
    stmt = select(Bot).where(
        Bot.enabled.is_(True), Bot.platform == "feishu", Bot.accept_bot_help.is_(True)
    )
    if query:
        stmt = stmt.where(
            or_(Bot.name.ilike(like), Bot.bot_key.ilike(like), Bot.description.ilike(like))
        )
    chain = await _chain_bots(session, row)
    bots = []
    for bot in await session.scalars(stmt.order_by(Bot.name).limit(30)):
        if bot.id in chain or bot.id == me.id or is_assistant(bot):
            continue
        if row.principal_user_id and not await allowed_to_use(session, bot, row.principal_user_id):
            continue
        bots.append(
            {
                "type": "bot",
                "id": str(bot.id),
                "name": bot.name,
                "about": clip(bot.description or "", 120),
            }
        )
    people = []
    if query:
        users = await session.scalars(
            select(User)
            .where(
                User.status == "active",
                or_(User.display_name.ilike(like), User.login_name.ilike(like)),
            )
            .limit(10)
        )
        people = [{"type": "human", "id": str(u.id), "name": name_of(u)} for u in users]
    return {"ai_employees": bots[:15], "people": people}


async def create_task(
    session: AsyncSession, row: WorkTask, me: Bot, args: dict[str, Any], base_url: str
) -> dict[str, Any]:
    title = _text(args, "title", TITLE_MAX, "标题")
    summary = _text(args, "summary", SUMMARY_MAX, "一句话", required=False)
    brief = _long(args, "brief", "简报")
    raw = args.get("assignee")
    target: dict[str, Any] = raw if isinstance(raw, dict) else {}
    kind = target.get("type")
    due_at = None
    if args.get("due_at"):
        try:
            due_at = datetime.fromisoformat(str(args["due_at"]))
        except ValueError:
            raise Invalid("due_at 要是 ISO 8601 时间") from None
    await _check_room(session, row)
    child = _child(row, me, title=title, summary=summary, brief=brief)
    child.due_at = due_at
    if kind == "bot":
        bot = await _target_bot(session, row, me, target.get("id"), label="assignee.id")
        child.assignee_bot_id, child.start_mode = bot.id, "auto"
        session.add(child)
        await session.flush()
        add_event(
            session,
            child,
            kind="created",
            actor_type="bot",
            actor_id=str(me.id),
            human_text=f"{me.name}派给{bot.name}",
            detail={"brief": brief, "parent": row.code},
        )
        await execute.start(
            session,
            child,
            bot=bot,
            trigger="subtask",
            triggered_by={"bot": me.name, "parent": row.code},
            silent=True,
        )
        who = bot.name
    elif kind == "human":
        person = await session.get(User, _uuid(target.get("id"), "assignee.id"))
        if person is None or person.status != "active":
            raise Invalid("assignee.id 对应的同事不存在或未启用")
        await _check_pending(session, person)
        child.assignee_user_id, child.start_mode = person.id, "manual"
        session.add(child)
        await session.flush()
        child.ask = ask_accept(child, person.id, me.name)
        add_event(
            session,
            child,
            kind="created",
            actor_type="bot",
            actor_id=str(me.id),
            human_text=f"{me.name}派给{name_of(person)}",
            detail={"brief": brief, "parent": row.code},
        )
        await cards.sync(
            session, child, before_ask=None, before_status="", actor_id=None, base_url=base_url
        )
        who = name_of(person)
    else:
        raise Invalid("assignee.type 只能是 bot 或 human")
    add_event(
        session,
        row,
        kind="subtask",
        actor_type="bot",
        actor_id=str(me.id),
        human_text=f"拆出 {child.code}：{title}（{who}）",
        detail={"child": child.code, "brief": brief},
    )
    return {"code": child.code, "assignee": who, "next": "派完要等的，调用 wait_for 后结束这一轮"}


async def _check_pending(session: AsyncSession, person: User) -> None:
    pending = await session.scalar(
        select(func.count())
        .select_from(WorkTask)
        .where(WorkTask.ask["to_user_id"].astext == str(person.id))
    )
    if int(pending or 0) >= MAX_PENDING_PER_PERSON:
        raise WorkTaskError(
            409, f"{name_of(person)}手上已经有 {MAX_PENDING_PER_PERSON} 件待回答的事，先别再打扰"
        )


async def ask_human(
    session: AsyncSession, row: WorkTask, me: Bot, args: dict[str, Any], base_url: str
) -> dict[str, Any]:
    question = _text(args, "question", QUESTION_MAX, "问题")
    why = _text(args, "why", WHY_MAX, "为什么问他", required=False)
    context = _long(args, "context", "背景")
    raw_options = args.get("options") or []
    if not isinstance(raw_options, list) or len(raw_options) > OPTIONS_MAX:
        raise Invalid(f"options 最多 {OPTIONS_MAX} 个")
    options = []
    for i, item in enumerate(raw_options):
        label = " ".join(str(item).split())
        if not label or len(label) > OPTION_MAX:
            raise Invalid(f"第 {i + 1} 个选项要 1～{OPTION_MAX} 字")
        options.append({"key": f"o{i + 1}", "label": label})
    to_raw = args.get("to_user_id") or row.principal_user_id
    person = await session.get(User, _uuid(to_raw, "to_user_id")) if to_raw else None
    if person is None or person.status != "active":
        raise Invalid("to_user_id 对应的同事不存在或未启用")
    root = row.root_id or row.id
    asked = await session.scalar(
        select(func.count())
        .select_from(WorkTask)
        .where(
            WorkTask.root_id == root,
            WorkTask.source == "agent",
            WorkTask.assignee_user_id.is_not(None),
            WorkTask.brief.like("[问题]%"),
        )
    )
    top = row if root == row.id else await session.get(WorkTask, root)
    limit = MAX_ASKS_PER_LEAD_CHAIN if top is not None and is_lead(top) else MAX_ASKS_PER_CHAIN
    if int(asked or 0) >= limit:
        raise WorkTaskError(409, f"这件事已经问过人 {limit} 次，先自己判断或交付说明")
    await _check_pending(session, person)
    await _check_room(session, row)
    child = _child(
        row, me, title=clip(f"问：{question}", TITLE_MAX), summary="", brief=f"[问题]\n{context}"
    )
    child.assignee_user_id, child.start_mode = person.id, "manual"
    # 问题是所属那件事上的一个决策点，不是一件新任务：编号挂在那件事下面（T-12-Q1），
    # 不从任务编号的序列里取号，任务列表里也不单列（见任务列表接口）。
    number = await session.scalar(
        select(func.count())
        .select_from(WorkTask)
        .where(WorkTask.parent_id == row.id, WorkTask.brief.like("[问题]%"))
    )
    child.code = f"{row.code}-Q{int(number or 0) + 1}"
    session.add(child)
    await session.flush()
    child.ask = make_ask(
        kind="question", to_user_id=person.id, question=question, options=options, why=why
    )
    if not options:
        # 没有选项：只有「其他」（输入框）。
        child.ask["options"] = []
    add_event(
        session,
        child,
        kind="asked",
        actor_type="bot",
        actor_id=str(me.id),
        human_text=f"{me.name}问{name_of(person)}：{question}",
        detail={"question": question, "options": options, "why": why, "context": context},
    )
    add_event(
        session,
        row,
        kind="subtask",
        actor_type="bot",
        actor_id=str(me.id),
        human_text=f"问{name_of(person)}（{child.code}）：{question}",
        detail={"child": child.code, "context": context},
    )
    await cards.sync(
        session, child, before_ask=None, before_status="", actor_id=None, base_url=base_url
    )
    return {"code": child.code, "next": "调用 wait_for 后结束这一轮，答复会交回你"}


async def update_task(
    session: AsyncSession, row: WorkTask, me: Bot, args: dict[str, Any]
) -> dict[str, Any]:
    detail = _long(args, "detail", "细节")
    human = _text(args, "human_text", LINE_MAX, "给人看的一句", required=False)
    raw = args.get("plan")
    steps: list[str] | None = None
    if raw is not None:
        if not isinstance(raw, list) or len(raw) > PLAN_MAX:
            raise Invalid(f"plan 是后面还没开始的步骤，最多 {PLAN_MAX} 步")
        steps = []
        for i, item in enumerate(raw):
            step = " ".join(str(item).split())
            if not step or len(step) > TITLE_MAX:
                raise Invalid(f"plan 第 {i + 1} 步要 1～{TITLE_MAX} 字")
            steps.append(step)
    add_event(
        session,
        row,
        kind="progress",
        actor_type="bot",
        actor_id=str(me.id),
        human_text=human,
        detail={"detail": detail},
    )
    if steps is not None:
        # 群里清单最后那几行「还没开始的步骤」：每次给完整的剩余步骤，以最新一次为准。
        add_event(
            session,
            row,
            kind=progress.PLAN_KIND,
            actor_type="bot",
            actor_id=str(me.id),
            detail={"steps": steps},
        )
    return {"ok": True}


async def submit_result(
    session: AsyncSession, row: WorkTask, me: Bot, args: dict[str, Any], chat_task_id: int
) -> dict[str, Any]:
    result = _text(args, "result", SUMMARY_MAX, "结果")
    output = _long(args, "output", "完整输出")
    digest = _long(args, "digest", "记要")
    report = _long(args, "report", "完整汇报", required=False)
    has_children = await session.scalar(
        select(WorkTask.id).where(WorkTask.parent_id == row.id).limit(1)
    )
    if has_children and not report:
        raise Invalid("这件事有子任务：report（完整汇报）必填，失败或取消的子任务也要写明")
    artifacts = []
    for item in args.get("artifacts") or []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())[:LINE_MAX]
        url = str(item.get("url") or "").strip()
        if title and url.startswith(("https://", "http://")):
            artifacts.append({"title": title, "url": url[:2000]})
    if progress.group_root(row) and is_lead(row):
        # 牵头的事还有问题挂在群里的卡上没人答：这时交付，卡片变成「已完成」，问题和按钮就没了。
        open_asks = [q for q in await progress.pending_asks(session, row) if q.id != row.id]
        if open_asks:
            codes = "、".join(q.code for q in open_asks[:5])
            raise Invalid(
                f"还有 {len(open_asks)} 个问题在等人回答（{codes}）："
                "这件事还不能交付。调用 wait_for 结束这一轮，答复回来、把后面的事办完再交付"
            )
    raw = args.get("followups") or []
    if not isinstance(raw, list) or len(raw) > FOLLOWUPS_MAX:
        raise Invalid(f"followups 是交付后还要人处理的事，最多 {FOLLOWUPS_MAX} 条")
    followups = []
    for i, item in enumerate(raw):
        line = " ".join(str(item).split())
        if not line or len(line) > LINE_MAX:
            raise Invalid(f"followups 第 {i + 1} 条要 1～{LINE_MAX} 字")
        followups.append(line)
    row.digest, row.report, row.artifacts = digest, report, artifacts[:ARTIFACTS_MAX]
    add_event(
        session,
        row,
        kind="submitted",
        actor_type="bot",
        actor_id=str(me.id),
        detail={
            "result": result,
            "output": output,
            "digest": digest,
            "report": report,
            "followups": followups,
        },
        chat_task_id=chat_task_id,
    )
    return {"ok": True, "next": "交付已记下，这一轮的回复简短说明结果即可"}


async def handoff_task(
    session: AsyncSession, row: WorkTask, me: Bot, args: dict[str, Any]
) -> dict[str, Any]:
    reason = _text(args, "reason", LINE_MAX, "转交原因")
    handoff = _long(args, "handoff", "交接包")
    from coreman.core.work_tasks.flow import settled

    pending = await session.scalar(
        select(WorkTask.id).where(WorkTask.parent_id == row.id, WorkTask.status != "shadow")
    )
    if pending and any(
        not settled(c)
        for c in await session.scalars(select(WorkTask).where(WorkTask.parent_id == row.id))
    ):
        raise WorkTaskError(409, "还有子任务或问题没结果，等它们有结果、汇总之后再转交")
    bot = await _target_bot(session, row, me, args.get("to_bot_id"), label="to_bot_id")
    row.assignee_bot_id, row.start_mode, row.claimed_at = bot.id, "auto", None
    row.status, row.ask, row.waiting_on = "todo", None, None
    add_event(
        session,
        row,
        kind="handoff",
        actor_type="bot",
        actor_id=str(me.id),
        human_text=f"{me.name} → {bot.name}：{reason}",
        detail={"from": me.name, "to": bot.name, "reason": reason, "note": handoff},
    )
    await execute.start(
        session,
        row,
        bot=bot,
        trigger="handoff",
        triggered_by={"bot": me.name},
        note=f"{me.name}转交给你，交接包：\n{handoff}",
        silent=row.requester_bot_id is not None,
    )
    return {"ok": True, "next": f"已交给{bot.name}，结束这一轮，简短说明转交给谁"}


async def wait_for(session: AsyncSession, row: WorkTask, me: Bot) -> dict[str, Any]:
    children = list(
        await session.scalars(
            select(WorkTask).where(WorkTask.parent_id == row.id, WorkTask.status != "shadow")
        )
    )
    from coreman.core.work_tasks.flow import settled

    pending = [c for c in children if not settled(c)]
    if not pending:
        return {"error": "nothing_to_wait", "stop": False, "message": "没有在等结果的子任务或问题"}
    row.status, row.ask = "waiting", None
    row.waiting_on = {
        "kind": "task",
        "codes": [c.code for c in pending],
        "human_text": clip(f"等 {'、'.join(c.code for c in pending)} 的结果", SUMMARY_MAX),
    }
    add_event(
        session,
        row,
        kind="waiting",
        actor_type="bot",
        actor_id=str(me.id),
        detail={"codes": [c.code for c in pending]},
    )
    return {"ok": True, "next": "结束这一轮；都有结果时平台把结果交回你"}


async def invoke(
    session: AsyncSession,
    row: WorkTask,
    me: Bot,
    name: str,
    args: dict[str, Any],
    *,
    chat_task_id: int,
    base_url: str,
) -> dict[str, Any]:
    try:
        async with session.begin_nested():
            if name == "find_assignees":
                return await find_assignees(session, row, me, args)
            if name in ("create_task", "ask_human"):
                made = await (create_task if name == "create_task" else ask_human)(
                    session, row, me, args, base_url
                )
                # 群里发起的事：多了一件子任务或一个问题，进展卡跟着变。
                await progress.refresh(session, row, base_url=base_url)
                return made
            if name == "update_task":
                updated = await update_task(session, row, me, args)
                # 这一行的最近进展、后面的步骤都画在群里的清单上。
                await progress.refresh(session, row, base_url=base_url)
                return updated
            if name == "submit_result":
                return await submit_result(session, row, me, args, chat_task_id)
            if name == "handoff_task":
                return await handoff_task(session, row, me, args)
            if name == "wait_for":
                waited = await wait_for(session, row, me)
                await progress.refresh(session, row, base_url=base_url)
                return waited
    except Invalid as exc:
        return {"error": "invalid_arguments", "stop": False, "message": str(exc)}
    except WorkTaskError as exc:
        return {"error": "refused", "stop": True, "message": exc.message}
    return {"error": "unknown_tool"}


NAMES = frozenset(DESCRIPTIONS)
