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
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.bus import tasks as bus
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    BotAllowedUser,
    GroupObserveDecision,
    WorkTask,
    WorkTaskEvent,
)
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
ACTIONS = ("create", "lead", "append", "clarify", "reply", "none")
# 调度员记录在 group_observe_decisions.outcome 里的结果。
OUTCOMES = {
    "create": "dispatched",
    "lead": "dispatch_lead",
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


# 运行时额度用完时调度员在群里说明一次；这么久之内同一个群不再说（每条消息都失败，不刷屏）。
QUOTA_NOTICE_EVERY = timedelta(hours=1)


async def quota_notice_sent(session: AsyncSession, bot_id: uuid.UUID, chat_id: str) -> bool:
    """这个群里，调度员最近是不是已经说过「运行时额度用完了」。"""
    row = await session.scalar(
        select(GroupObserveDecision.id)
        .where(
            GroupObserveDecision.bot_id == bot_id,
            GroupObserveDecision.chat_id == chat_id,
            GroupObserveDecision.created_at >= datetime.now(UTC) - QUOTA_NOTICE_EVERY,
            GroupObserveDecision.dispatch["error"].astext == "rate_limited",
            GroupObserveDecision.dispatch["said"].astext.is_not(None),
        )
        .limit(1)
    )
    return row is not None


def session_key(chat_id: str) -> str:
    """调度回合的会话键：每个群一条，调度员记得自己在这个群分派过什么，也不和 @ 它的对话混。"""
    return f"dispatch:{chat_id}"


def work_session_key(code: str) -> str:
    """执行员工做任务的会话键：每个任务一条，补充和追问续上同一个会话，不和群里的 @ 互相打断。"""
    return f"work:{code}"


def is_lead(row: WorkTask) -> bool:
    """调度员自己牵头的任务：调度员建的，执行人也是它自己。"""
    return (
        row.source == "dispatch"
        and row.dispatcher_bot_id is not None
        and row.assignee_bot_id == row.dispatcher_bot_id
    )


# 牵头任务的本轮块：调度员在任务会话里按目标拆子任务、等结果、排下一步。
LEAD_NOTE = (
    "这件事由你牵头。你仍然不自己查询、不动手改东西：做任务的规则里「能自己做就自己做」"
    "对你不适用，你的工作是把目标拆开、交给合适的 AI 员工、对照要求看结果、排下一步。\n"
    "- 先用 find_assignees 找人，再用 create_task 拆子任务。子任务的 brief 是员工之间传递要求的"
    "唯一载体，要写全：目标、背景、已有的约定和文档位置、需要对方配合的接口或数据、验收标准、"
    "截止。一位员工交回的结论，下一件子任务要用到的，原文带进 brief，不要只写「见上一个任务」。\n"
    "- 能并行的一起派，有先后的分批派：派完调用 wait_for 结束这一轮，结果回来再派下一批。\n"
    "- 结果回来时对照验收标准看证据（产出链接、检查结果、员工的原话）；不达标的写清差在哪，"
    "再派一件子任务，不要自己补。\n"
    "- 目标明确时由你判断着往前推：测试环境里怎么协调、先做哪一步、用哪条测试数据，你来定，"
    "不问人；业务上的取舍能从已有文档和员工交回的结论判断的，你定，并在群里说明依据。"
    "只有这些才问人：合并 PR 和发布、动正式环境的数据或配置、花钱或对外发出东西、"
    "删除等恢复不了的操作、超出目标范围的事，以及确实判断不了的业务取舍。\n"
    "- 派子任务时在 brief 里写明这件事属于哪个目标、在哪个环境做；"
    "测试环境里的操作员工不用再问人。\n"
    "- 要人定的事用 ask_human 问具体的人，一次一个决定，给出选项和你的推荐；"
    "同一批要问的一次问完，不要做一步问一步，也不要把几个问题攒成一段话发到群里。\n"
    "- 派出第一批子任务时，用 update_task 的 plan 写出后面还没开始的步骤（每步一句人话），"
    "群里的清单会列出来；派出其中一步或计划变了，就把剩下的步骤重写一遍。\n"
    "- 群里的回复只写人需要知道的：到哪一步了、谁在做什么、等谁定什么，三五行以内；"
    "细节留在任务里。\n"
    "- 群里这件事只有一张进展卡：步骤、在等谁、最新进展都画在上面。子任务有结果后接着做的那几轮"
    "不往群里发消息，用 update_task 的 human_text 写一句现在到哪了。\n"
    "- 还有子任务或问题没结果时，这一轮结束前调用 wait_for；目标全部达成后用 submit_result 交付，"
    "report 写给发起人看的总结：目标达成了没有、做了哪几件事、还剩什么要人处理。"
)


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
    mentioned: bool = False,
) -> str:
    """调度回合的本轮块：员工名录、这个群最近的任务和输出格式。

    单人群和有人 @ 它的回合多一个 reply：对方在直接对它说话，每条都要回应。
    """
    roster = "\n".join(f"- `{e.key}`（{e.name}）：{e.role or '没有写职责说明'}" for e in staff)
    direct = solo or mentioned
    if mentioned:
        opening = (
            "这一轮有人 @ 了你：他在直接对你说话，你必须回应。"
            "你是调度员，只负责安排，自己不查询、不动手；要办的事交给员工，"
            "要几位员工分步配合的目标由你牵头；问你进展、闲聊、致谢这类不需要安排人的话，"
            "用 reply 简短回一句。"
        )
    elif solo:
        opening = (
            "这一轮没有人 @ 你：这个群里只有一位真人，他说的每句话都由你回应。"
            "你是调度员，只负责安排，自己不查询、不动手；要办的事交给员工，"
            "闲聊、致谢这类不需要安排人的话，用 reply 简短回一句。"
        )
    else:
        opening = (
            "这一轮没有人 @ 你：判断模型认为新来的消息里有在请人办事的，需要你调度。"
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
            if is_lead(t):
                who = "你牵头"
            lines.append(
                f"- {t.code}（{t.created_at:%m-%d}，{who}，{t.status}）{t.title}：{t.summary}"
            )
    lines.append("@ 了某位 AI 员工（不是你）的消息，那位员工会自己回答：不要为它新建，也不要追加。")
    lines.append(
        "怎么决定：\n"
        "- create：交给最合适的一位员工。能一个人做完的不拆；确实分属不同员工、能各自交付时才拆，"
        f"最多 {MAX_TASKS} 件。\n"
        "  related：和上面某个任务有关、但要另起一件事时，把那个任务的编号写进来，"
        "员工会看到它当时的结果。\n"
        "  track：要不要在群里跟进。一问一答、查个数、说明办法这类当场能答完的，填 false："
        "你不发言，员工直接回复，群里不出任务编号。要多步处理、要等确认或别人配合、"
        "可能做很久、之后还要追问进展的，才填 true。\n"
        "- lead：这件事是一个目标，要几位员工先后配合、分几步才能做完（例如两个系统对接："
        "先对齐接口，再各自开发，最后联调），一次分派说不清楚。这时由你牵头：只建一件任务，"
        "执行人是你自己；之后你在这件任务的会话里拆子任务给员工、等结果、排下一步，做完再汇总。"
        "能一次分派清楚的用 create，不要牵头。\n"
        "- append：新消息是在补充或追问上面某个任务（例如提到了任务编号，或明显在接着说那件事），"
        "追加给原来的员工，不新建。你牵头的任务也一样：补充、追问，或者在回答它等人定的事，"
        "都追加给它，会交回你在那件任务的会话里处理。\n"
        "- clarify：需求不清、选错代价高时，问一句。\n"
        + (
            "- reply：不需要安排人时（闲聊、致谢、确认），像同事一样简短回一句，"
            f"不超过 {REPLY_LIMIT} 字；不要回答需要查询或动手才能答的问题，那些交给员工。"
            "没有合适的员工时，也用 reply 说明这件事暂时没人能接。"
            "除了上面 @ 员工的情况，不要选 none。\n"
            if direct
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
        ' "lead": {"title": "lead 时填：≤30 字的标题", "summary": "给人看的一句话：目标是什么",\n'
        '          "brief": "目标、背景、怎么算完成、已知的约束和要先定的事",\n'
        '          "related": ["相关的任务编号"]},\n'
        ' "task_code": "append 时填已有任务编号", "note": "append 时补充的内容",\n'
        ' "question": "clarify 时问的一句话"'
        + (', "text": "reply 时回的话"' if direct else "")
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
    elif plan.action == "lead":
        item = data.get("lead")
        if not isinstance(item, dict):
            raise ValueError("bad_lead")
        title = str(item.get("title") or "").strip()
        if not title:
            raise ValueError("missing_title")
        related = [c for c in item.get("related") or [] if isinstance(c, str)]
        plan.tasks.append(
            {
                "title": title[:TITLE_LIMIT],
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
                status="waiting" if live else "shadow",
                waiting_on={"kind": "queue", "human_text": "排队中"} if live else None,
                platform="feishu",
                chat_id=chat_id,
                origin_event_id=event_id,
                requester_user_id=requester_id,
                requester_name=requester_name,
                principal_user_id=requester_id,
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
            row.root_id = row.id
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
    elif plan.action == "lead":
        item = plan.tasks[0]
        row = WorkTask(
            title=item["title"],
            summary=item["summary"],
            status="waiting" if live else "shadow",
            waiting_on={"kind": "queue", "human_text": "排队中"} if live else None,
            platform="feishu",
            chat_id=chat_id,
            origin_event_id=event_id,
            requester_user_id=requester_id,
            requester_name=requester_name,
            principal_user_id=requester_id,
            # 执行人就是调度员自己：它在任务会话里拆子任务、等结果、排下一步。
            assignee_bot_id=dispatcher.id,
            dispatcher_bot_id=dispatcher.id,
            decision_id=decision_id,
            related_codes=item["related"],
            brief=item["brief"],
            tracked=True,
        )
        session.add(row)
        await session.flush()
        row.root_id = row.id
        session.add(
            WorkTaskEvent(
                work_task_id=row.id,
                kind="created",
                human_text=f"{trial}调度员{'自己牵头' if live else '会自己牵头'}",
                agent_detail=detail,
                **actor,
            )
        )
        codes.append(row.code)
        if live:
            await hand_off(session, row, event_id=event_id, requester_pid=requester_pid)
            line = f"这件事我来牵头：{item['title']}（{task_link(row.code, base_url)}）"
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
                if is_lead(target):
                    line = f"收到，记进 {task_link(target.code, base_url)} 了"
                else:
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
    row.closed_at, row.updated_at = None, datetime.now(UTC)
    # 在等子任务时来了补充：照常排一轮，但任务仍在等子任务，结果回来时平台才交得回去。
    if not (row.status == "waiting" and (row.waiting_on or {}).get("kind") == "task"):
        row.status, row.ask = "waiting", None
        row.waiting_on = {"kind": "queue", "human_text": f"排队等{bot.name}开始"}
    if row.claimed_at is None:
        row.claimed_at = row.updated_at
    await bus.enqueue(
        session,
        bus.NewTask(
            bot_id=bot.id,
            kind="chat",
            user_id=row.requester_user_id,
            inbound_event_id=event_id,
            session_key=work_session_key(row.code),
            dedupe_key=f"work:{row.id}:{event_id}",
            payload={
                "work_task_id": row.id,
                "work_note": note,
                "work_trigger": "dispatch",
                "bot_key": bot.bot_key,
                "platform_user_id": requester_pid,
                # 同一个任务的补充排在上一轮之后，不替代正在做的那一轮。
                "serialize_session": True,
            },
        ),
    )
