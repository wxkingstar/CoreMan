"""群聊调度员：调度回合的本轮块、决定的校验，以及把决定记成任务。

第一段（判断要不要调度）沿用旁听的判断任务，只换判断题；判断为是时排一个调度回合
（`origin = dispatch`）。调度员在这一轮读群聊、员工名录和这个群最近的任务，按固定格式输出
一个 JSON 决定；平台在这里校验并落库。试运行（旁听开关 `shadow`）只记录「会这样分派」：
任务状态是 `shadow`，不交接、不往群里发任何消息。开启（`on`）时群里回一行简述，任务在内部
直接交给执行员工：排一轮它的对话（会话键 `work:<编号>`），以发起人的身份去做。

群里只有一位真人时（单人群），跳过第一段判断，每批消息都排调度回合；调度员每条都回应，
不需要安排人的话（闲聊、致谢）用 `reply` 直接回一句。
见 docs/superpowers/specs/2026-11-02-group-dispatcher-and-work-tasks.md。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.bus import tasks as bus
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, BotAllowedUser, WorkTask, WorkTaskEvent
from coreman.core.platforms.feishu import FeishuClient, FeishuError

ORIGIN = "dispatch"
LIMIT_SECONDS = 60
MAX_TASKS = 3
TITLE_LIMIT = 30
SUMMARY_LIMIT = 120
BRIEF_LIMIT = 4000
QUESTION_LIMIT = 100
REPLY_LIMIT = 300
RECENT_TASKS = 10
ACTIONS = ("create", "append", "clarify", "reply", "none")
# 调度员记录在 group_observe_decisions.outcome 里的结果。
OUTCOMES = {
    "create": "dispatched",
    "append": "dispatch_append",
    "clarify": "dispatch_clarify",
    "reply": "dispatch_reply",
    "none": "dispatch_none",
}
# 调度员等群安静的时长（秒）：比旁听的默认值短，单人群更短（只有一个人在说，不用等别人接话）。
# 只会缩短，管理台把旁听安静时长调得更短时以那个为准。
QUIET_SECONDS = 8
SOLO_QUIET_SECONDS = 3
# 群里真人数：按群短时缓存，人员进出群时最多这么久后才反映出来。
HUMANS_TTL_SECONDS = 300
_HUMANS: OrderedDict[tuple[str, str], tuple[float, int | None]] = OrderedDict()


def session_key(chat_id: str) -> str:
    """调度回合的会话键：每个群一条，调度员记得自己在这个群分派过什么，也不和 @ 它的对话混。"""
    return f"dispatch:{chat_id}"


def work_session_key(code: str) -> str:
    """执行员工做任务的会话键：每个任务一条，补充和追问续上同一个会话，不和群里的 @ 互相打断。"""
    return f"work:{code}"


def task_link(code: str, base_url: str) -> str:
    """群里出现的任务编号：带上管理台任务页的链接，点开能看任务内容。"""
    return f"[{code}]({base_url.rstrip('/')}/work-tasks/{code})" if base_url else code


@dataclass(frozen=True)
class Employee:
    key: str
    name: str
    role: str
    bot_id: uuid.UUID


async def employees(
    session: AsyncSession,
    dispatcher: Bot,
    chat_id: str,
    requester_id: uuid.UUID | None,
    cipher: Cipher,
) -> tuple[list[Employee], list[str]]:
    """这个群里能接任务的 AI 员工，以及查不清在不在群的原因。

    条件同机器人协作的候选：本平台的飞书 AI 员工、已启用、不是调度员、发起人在它的白名单里
    （白名单为空表示人人可用），并且确实在这个群（用它自己的凭证问飞书，结果短时缓存）。
    """
    from coreman.core.chat.collaboration_tools import present

    bots = list(
        await session.scalars(
            select(Bot)
            .where(
                Bot.platform == "feishu",
                Bot.enabled.is_(True),
                Bot.id != dispatcher.id,
                Bot.group_role == "member",
                # 个人助理只属于所有者，不接调度员派的活。
                Bot.kind == "standard",
            )
            .order_by(Bot.name)
        )
    )
    allowed: dict[uuid.UUID, set[uuid.UUID]] = {}
    rows = await session.execute(
        select(BotAllowedUser.bot_id, BotAllowedUser.user_id).where(
            BotAllowedUser.bot_id.in_([b.id for b in bots])
        )
    )
    for bot_id, user_id in rows.all():
        allowed.setdefault(bot_id, set()).add(user_id)
    usable = [b for b in bots if not allowed.get(b.id) or requester_id in allowed[b.id]]
    here, unverified = await present(usable, chat_id, cipher)
    return [
        Employee(
            key=b.bot_key,
            name=b.name,
            role=(b.group_observe_scope or b.description or "").strip()[:600],
            bot_id=b.id,
        )
        for b in here
    ], unverified


async def human_count(bot: Bot, chat_id: str, cipher: Cipher) -> int | None:
    """群里的真人数（飞书群信息里的 user_count，不含机器人）；查不到时返回 None。"""
    hit, cached = _cached_humans(str(bot.id), chat_id)
    if hit:
        return cached
    try:
        config = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
    except (ValueError, KeyError, TypeError):
        return _remember_humans(str(bot.id), chat_id, None)
    client = FeishuClient(config["app_id"], config["app_secret"])
    try:
        return await human_count_with(client, str(bot.id), chat_id)
    finally:
        await client.aclose()


async def human_count_with(client: Any, bot_id: str, chat_id: str) -> int | None:
    """同 `human_count`，用调用方已有的飞书客户端（网关进程里用它自己的那个）。"""
    hit, cached = _cached_humans(bot_id, chat_id)
    if hit:
        return cached
    count: int | None = None
    try:
        async with asyncio.timeout(5):
            body = await client.call("GET", f"/open-apis/im/v1/chats/{quote(chat_id, safe='')}")
        value = (body.get("data") or {}).get("user_count")
        count = int(value) if value is not None else None
    except (FeishuError, ValueError, KeyError, TypeError, TimeoutError):
        count = None
    return _remember_humans(bot_id, chat_id, count)


def _cached_humans(bot_id: str, chat_id: str) -> tuple[bool, int | None]:
    cached = _HUMANS.get((bot_id, chat_id))
    if cached is not None and cached[0] > time.monotonic():
        return True, cached[1]
    return False, None


def _remember_humans(bot_id: str, chat_id: str, count: int | None) -> int | None:
    key = (bot_id, chat_id)
    _HUMANS[key] = (time.monotonic() + HUMANS_TTL_SECONDS, count)
    _HUMANS.move_to_end(key)
    while len(_HUMANS) > 2048:
        _HUMANS.popitem(last=False)
    return count


async def recent_tasks(session: AsyncSession, chat_id: str) -> list[WorkTask]:
    return list(
        await session.scalars(
            select(WorkTask)
            .where(WorkTask.chat_id == chat_id)
            .order_by(WorkTask.id.desc())
            .limit(RECENT_TASKS)
        )
    )


def note(
    staff: list[Employee],
    unverified: list[str],
    tasks: list[WorkTask],
    names: dict[uuid.UUID, str],
    *,
    solo: bool = False,
) -> str:
    """调度回合的本轮块：员工名录、这个群最近的任务和输出格式。单人群多一个 reply。"""
    roster = "\n".join(f"- `{e.key}`（{e.name}）：{e.role or '没有写职责说明'}" for e in staff)
    opening = (
        "这一轮没有人 @ 你：这个群里只有一位真人，他说的每句话都由你回应。"
        "你是调度员，只负责安排，自己不查询、不动手；要办的事交给员工，"
        "闲聊、致谢这类不需要安排人的话，用 reply 简短回一句。"
        if solo
        else "这一轮没有人 @ 你：判断模型认为新来的消息里有在请人办事的，需要你调度。"
        "你是调度员，只负责安排，自己不回答、不查询、不动手。"
    )
    lines = [
        opening,
        "这个群里能接任务的 AI 员工（标识：职责）：",
        roster or "- （没有）",
    ]
    if unverified:
        lines.append("没能确认是否在群里的员工不在上面：" + "；".join(unverified[:5]))
    if tasks:
        lines.append("这个群最近的任务（从新到旧）：")
        for t in tasks:
            who = names.get(t.assignee_bot_id, "未分派") if t.assignee_bot_id else "未分派"
            lines.append(
                f"- {t.code}（{t.created_at:%m-%d}，{who}，{t.status}）{t.title}：{t.summary}"
            )
    lines.append("@ 了某位 AI 员工的消息，那位员工会自己回答：不要为它新建，也不要追加。")
    lines.append(
        "怎么决定：\n"
        "- create：交给最合适的一位员工。能一个人做完的不拆；确实分属不同员工、能各自交付时才拆，"
        f"最多 {MAX_TASKS} 件。\n"
        "  related：和上面某个任务有关、但要另起一件事时，把那个任务的编号写进来，"
        "员工会看到它当时的结果。\n"
        "  track：要不要在群里跟进。一问一答、查个数、说明办法这类当场能答完的，填 false："
        "你不发言，员工直接回复，群里不出任务编号。要多步处理、要等确认或别人配合、"
        "可能做很久、之后还要追问进展的，才填 true。\n"
        "- append：新消息是在补充或追问上面某个任务（例如提到了任务编号，或明显在接着说那件事），"
        "追加给原来的员工，不新建。\n"
        "- clarify：需求不清、选错代价高时，问一句。\n"
        + (
            "- reply：不需要安排人时（闲聊、致谢、确认），像同事一样简短回一句，"
            f"不超过 {REPLY_LIMIT} 字；不要回答需要查询或动手才能答的问题，那些交给员工。"
            "没有合适的员工时，也用 reply 说明这件事暂时没人能接。"
            "除了上面 @ 员工的情况，不要选 none。\n"
            if solo
            else "- none：其实不需要安排人（闲聊、已经有人在处理、点名问了具体的人、"
            "没有合适的员工）。\n"
        )
        + "只输出一个 JSON 代码块，不要输出其他内容：\n"
        "```json\n"
        '{"action": "create", "reason": "一句话依据",\n'
        ' "tasks": [{"title": "≤30 字的标题", "assignee": "员工标识", "track": false,\n'
        '            "summary": "给人看的一句话：要做什么",\n'
        '            "brief": "给执行员工的要求：背景、要做什么、怎么算完成、注意事项",\n'
        '            "related": ["相关的任务编号"]}],\n'
        ' "task_code": "append 时填已有任务编号", "note": "append 时补充的内容",\n'
        ' "question": "clarify 时问的一句话"'
        + (', "text": "reply 时回的话"' if solo else "")
        + "}\n```"
    )
    return "\n".join(lines)


@dataclass
class Plan:
    action: str
    reason: str = ""
    tasks: list[dict[str, Any]] = field(default_factory=list)
    task_code: str = ""
    note: str = ""
    question: str = ""
    text: str = ""


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse(text: str, staff: list[Employee], known_codes: set[str], *, solo: bool = False) -> Plan:
    """校验调度员的输出；不合格抛 ValueError（原因写进判断记录）。reply 只在单人群可用。"""
    match = _FENCE.search(text or "")
    raw = match.group(1) if match else (text or "").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError("not_json") from None
    if not isinstance(data, dict) or data.get("action") not in ACTIONS:
        raise ValueError("bad_action")
    if data["action"] == "reply" and not solo:
        raise ValueError("bad_action")
    plan = Plan(action=str(data["action"]), reason=str(data.get("reason") or "")[:300])
    keys = {e.key for e in staff}
    if plan.action == "create":
        items = data.get("tasks")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_TASKS:
            raise ValueError("bad_tasks")
        for item in items:
            if not isinstance(item, dict) or item.get("assignee") not in keys:
                raise ValueError("unknown_assignee")
            title = str(item.get("title") or "").strip()
            if not title:
                raise ValueError("missing_title")
            related = [c for c in item.get("related") or [] if isinstance(c, str)]
            plan.tasks.append(
                {
                    "title": title[:TITLE_LIMIT],
                    "assignee": item["assignee"],
                    # 没写当作不跟进：宁可少一个编号，也不在群里多刷一行。
                    "track": item.get("track") is True,
                    "summary": str(item.get("summary") or "").strip()[:SUMMARY_LIMIT],
                    "brief": str(item.get("brief") or "").strip()[:BRIEF_LIMIT],
                    "related": [c for c in related if c in known_codes][:5],
                }
            )
    elif plan.action == "append":
        plan.task_code = str(data.get("task_code") or "").strip()
        plan.note = str(data.get("note") or "").strip()[:BRIEF_LIMIT]
        if plan.task_code not in known_codes or not plan.note:
            raise ValueError("bad_append")
    elif plan.action == "clarify":
        plan.question = str(data.get("question") or "").strip()[:QUESTION_LIMIT]
        if not plan.question:
            raise ValueError("missing_question")
    elif plan.action == "reply":
        plan.text = str(data.get("text") or "").strip()[:REPLY_LIMIT]
        if not plan.text:
            raise ValueError("missing_text")
    return plan


async def record(
    session: AsyncSession,
    *,
    plan: Plan,
    dispatcher: Bot,
    chat_id: str,
    event_id: int | None,
    requester_id: uuid.UUID | None,
    requester_name: str,
    staff: list[Employee],
    decision_id: int | None,
    chat_task_id: int,
    live: bool = False,
    requester_pid: str = "",
    base_url: str = "",
) -> tuple[list[str], str | None]:
    """把决定记成任务和事件；返回涉及的任务编号，以及要回在群里的一行（不发言时为 None）。

    试运行（`live = False`）：任务状态 shadow，不交接、不发消息。开启时任务直接排进执行员工
    的队列，追加的补充也排一轮给原执行员工。每次交接都在群里说一行交给了谁：跟进的任务带编号，
    不跟进的不带编号。
    """
    by_key = {e.key: e for e in staff}
    actor = {"actor_type": "bot", "actor_id": str(dispatcher.id), "chat_task_id": chat_task_id}
    detail = {"reason": plan.reason, "decision_id": decision_id}
    trial = "" if live else "试运行："
    codes: list[str] = []
    line: str | None = None
    if plan.action == "create":
        created: list[WorkTask] = []
        for item in plan.tasks:
            who = by_key[item["assignee"]]
            row = WorkTask(
                title=item["title"],
                summary=item["summary"],
                status="queued" if live else "shadow",
                platform="feishu",
                chat_id=chat_id,
                origin_event_id=event_id,
                requester_user_id=requester_id,
                requester_name=requester_name,
                assignee_bot_id=who.bot_id,
                dispatcher_bot_id=dispatcher.id,
                decision_id=decision_id,
                related_codes=item["related"],
                brief=item["brief"],
                tracked=item["track"],
            )
            session.add(row)
            created.append(row)
        await session.flush()
        siblings = [r.code for r in created]
        for row, item in zip(created, plan.tasks, strict=True):
            if len(created) > 1:
                row.related_codes = [*row.related_codes, *(c for c in siblings if c != row.code)]
            name = by_key[item["assignee"]].name
            session.add(
                WorkTaskEvent(
                    work_task_id=row.id,
                    kind="created",
                    human_text=f"{trial}调度员{'交给' if live else '会交给'}{name}",
                    agent_detail=detail,
                    **actor,
                )
            )
            codes.append(row.code)
        if live:
            for row in created:
                await hand_off(session, row, event_id=event_id, requester_pid=requester_pid)
            line = "\n".join(
                f"@{by_key[item['assignee']].name} {item['title']}"
                f"（{task_link(row.code, base_url)}）"
                if row.tracked
                else f"交给 @{by_key[item['assignee']].name} 了：{item['title']}"
                for row, item in zip(created, plan.tasks, strict=True)
            )
    elif plan.action == "append":
        target = await session.scalar(
            select(WorkTask).where(WorkTask.code == plan.task_code, WorkTask.chat_id == chat_id)
        )
        if target is not None:
            session.add(
                WorkTaskEvent(
                    work_task_id=target.id,
                    kind="comment",
                    human_text=(
                        f"{trial}调度员{'补充' if live else '会补充'}给原执行员工：{plan.note[:60]}"
                    ),
                    agent_detail={**detail, "note": plan.note},
                    **actor,
                )
            )
            target.updated_at = datetime.now(UTC)
            codes.append(target.code)
            assignee = (
                await session.get(Bot, target.assignee_bot_id) if target.assignee_bot_id else None
            )
            # 试运行期间记下的任务没有真的交出去，开启后也不续它：那件事从没人做过。
            if live and assignee is not None and target.status != "shadow":
                await hand_off(
                    session, target, event_id=event_id, requester_pid=requester_pid, note=plan.note
                )
                line = (
                    f"补充给 @{assignee.name} 了（{task_link(target.code, base_url)}）"
                    if target.tracked
                    else f"补充给 @{assignee.name} 了"
                )
    elif plan.action == "clarify" and live:
        line = plan.question
    elif plan.action == "reply" and live:
        line = plan.text
    await session.flush()
    return codes, line


async def hand_off(
    session: AsyncSession,
    row: WorkTask,
    *,
    event_id: int | None,
    requester_pid: str,
    note: str = "",
    dedupe_key: str | None = None,
) -> None:
    """把任务交给执行员工：排一轮它的对话，回复挂在 `event_id` 那条群消息上。

    不靠飞书把调度员那条 @ 推给执行员工（机器人之间的推送要额外权限）。这一轮以发起人的
    身份做，执行员工的回合里会再核对一遍发起人和白名单。
    """
    if row.assignee_bot_id is None or event_id is None:
        return
    bot = await session.get(Bot, row.assignee_bot_id)
    if bot is None:
        return
    row.status, row.closed_at, row.updated_at = "queued", None, datetime.now(UTC)
    await bus.enqueue(
        session,
        bus.NewTask(
            bot_id=bot.id,
            kind="chat",
            user_id=row.requester_user_id,
            inbound_event_id=event_id,
            session_key=work_session_key(row.code),
            dedupe_key=dedupe_key or f"work:{row.id}:{event_id}",
            payload={
                "work_task_id": row.id,
                "work_note": note,
                "bot_key": bot.bot_key,
                "platform_user_id": requester_pid,
                # 同一个任务的补充排在上一轮之后，不替代正在做的那一轮。
                "serialize_session": True,
            },
        ),
    )
