"""子任务工具：主会话派出、追问、查看、取消子任务；子任务轮次报告自己卡住了。

前四个登记在助理工具注册表的 `jobs` 组（子任务轮次没有这一组），最后一个登记在 `subtask` 组
（只有子任务轮次有）。数据在平台自己的库里、不调飞书。分派时只认这一轮核验过的助理记录
（`scope.bot`），够不着别人的子任务。

登记在注册表自己的文件末尾（`coreman/core/assistants/tools.py`），这里不导入它。
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import jobs
from coreman.core.assistants.approvals import service as approvals_service
from coreman.core.assistants.kinds import SUBTASK_KIND
from coreman.core.assistants.records import is_assistant
from coreman.core.crypto import Cipher
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolbase import Arguments, input_schema

_INVALID = "invalid_tool_or_arguments"
JobId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[0-9a-fA-F-]{32,36}$"),
    Field(description="Job id returned by assistant_spawn"),
]


class Spawn(Arguments):
    title: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=jobs.MAX_TITLE),
        Field(description="A few words the owner would recognise"),
    ]
    brief: Annotated[str, StringConstraints(min_length=1, max_length=jobs.MAX_BRIEF)] = Field(
        description=(
            "Everything the sub-task needs: what to find out, where to look (chat ids, document"
            " links, names), what the answer should contain. It starts with no context."
        )
    )


class Message(Arguments):
    id: JobId
    text: Annotated[str, StringConstraints(min_length=1, max_length=jobs.MAX_MESSAGE)] = Field(
        description="The follow-up question; the sub-task still has its earlier context"
    )


class Listing(Arguments):
    id: JobId | None = Field(
        default=None, description="Omit to list recent jobs; give one id for its brief and result"
    )


class Cancel(Arguments):
    id: JobId


class Blocked(Arguments):
    reason: Annotated[str, StringConstraints(min_length=1, max_length=jobs.MAX_REASON)] = Field(
        description="What is needed and from whom, in one or two sentences"
    )


OWN: dict[str, tuple[type[Arguments], str]] = {
    "assistant_spawn": (
        Spawn,
        "Start a sub-task for an investigation that takes a while, so this conversation is not"
        " held up. It runs in its own session with read-only personal tools plus the knowledge"
        " base and follow-ups; it cannot send, edit or submit approvals. Its conclusion comes"
        " back to this conversation later as a platform turn. Do not wait for it.",
    ),
    "assistant_job_message": (
        Message,
        "Ask a finished or blocked sub-task a follow-up question in its own session.",
    ),
    "assistant_job_list": (
        Listing,
        "List your sub-tasks with their status, or read one in full (brief, result, why it is"
        " blocked).",
    ),
    "assistant_job_cancel": (Cancel, "Stop a sub-task that is still queued or running."),
}
SUBTASK: dict[str, tuple[type[Arguments], str]] = {
    "assistant_job_blocked": (
        Blocked,
        "Call this when you cannot finish without the owner's decision, an authorization, or an"
        " action you are not allowed to take. Then end your answer with what you found so far"
        " and what you suggest.",
    ),
}

HINTS = {
    "job_not_found": "没有这个子任务；用 assistant_job_list 看现有的。",
    "job_busy": "这个子任务还在跑，等它的结论投回来再追问；不想等就 assistant_job_cancel。",
    "job_closed": "这个子任务已经失败或取消，不能追问也不能再取消；需要的话重新派一个。",
    "job_round_limit": "这个子任务的轮数已到上限；还要查就重新派一个，把已有的结论写进交代。",
    "too_many_jobs": "进行中的子任务已到上限，等一个结束或取消一个再派。",
    "daily_job_limit": "今天派出的子任务已到上限；剩下的自己在这一轮里查，或记待跟进明天再派。",
    "daily_budget_exhausted": "今天的轮次预算已用完，所有者不在场时不能再派子任务；记待跟进。",
    "content_locked": "所有者的数据密钥不可用，现在存不了内容；不要重试，如实告诉所有者。",
    "assistant_unavailable": "助理现在跑不了平台发起的轮次；如实告诉所有者。",
}


def _definitions(source: dict[str, tuple[type[Arguments], str]]) -> list[dict[str, Any]]:
    return [
        {"name": name, "description": text, "inputSchema": input_schema(args)}
        for name, (args, text) in source.items()
    ]


def definitions() -> list[dict[str, Any]]:
    return _definitions(OWN)


def subtask_definitions() -> list[dict[str, Any]]:
    return _definitions(SUBTASK)


async def _run(session: AsyncSession, scope: Scope, name: str, args: Any) -> dict[str, Any]:
    bot = scope.bot
    if name == "assistant_job_blocked":
        await jobs.block(session, bot.id, scope.task, args.reason)
        return {"ok": True}
    if name == "assistant_spawn":
        row = await jobs.spawn(
            session, bot, title=args.title, brief=args.brief, attended=not scope.unattended
        )
        return {"job": jobs.view(row)}
    if name == "assistant_job_list":
        if args.id is not None:
            found = await jobs.get(session, bot.id, jobs.parse_id(args.id))
            return {"job": jobs.view(found, full=True)}
        rows = await jobs.list_(session, bot.id, scope="recent")
        return {
            "jobs": [jobs.view(row) for row in rows],
            "limits": {
                "active": jobs.MAX_ACTIVE,
                "per_day": jobs.MAX_DAILY,
                "rounds": jobs.MAX_ROUNDS,
            },
        }
    job_id = jobs.parse_id(args.id)
    if name == "assistant_job_message":
        row = await jobs.message(session, bot, job_id, args.text, attended=not scope.unattended)
    else:
        # 主会话自己取消的：不必再投回一轮告诉它。
        row = await jobs.cancel(session, bot.id, job_id, code="cancelled_by_assistant")
    return {"job": jobs.view(row)}


async def dispatch(
    session: AsyncSession, cipher: Cipher, scope: Scope, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """执行一个子任务工具；调用方（MCP 入口）负责提交。`cipher` 用不上：内容由数据密钥加解密。

    注册表按组把过关了，这里再按任务类型核一次：子任务轮次不能派子任务，别的轮次不能报卡住。
    """
    found = OWN.get(name) or SUBTASK.get(name)
    in_subtask = scope.task.kind == SUBTASK_KIND
    if found is None or not is_assistant(scope.bot) or (name in SUBTASK) != in_subtask:
        return {"error": _INVALID}
    try:
        args: Any = found[0].model_validate(arguments)
    except ValidationError as exc:
        return {"error": _INVALID, "invalid": approvals_service._problems(exc)}
    try:
        return await _run(session, scope, name, args)
    except jobs.JobError as exc:
        out: dict[str, Any] = {"error": exc.code}
        if exc.code in HINTS:
            out["hint"] = HINTS[exc.code]
        if exc.code == "too_many_jobs":
            out["limit"] = jobs.MAX_ACTIVE
        if exc.code == "daily_job_limit":
            out["limit"] = jobs.MAX_DAILY
        return out
