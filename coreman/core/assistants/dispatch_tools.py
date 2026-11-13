"""调度公共员工的两个工具：看可以问谁，把一个问题交给其中一位。

登记在助理工具注册表的 `jobs` 组（子任务轮次没有这一组）。数据在平台自己的库里、不调飞书。
分派时只认这一轮核验过的助理记录（`scope.bot`）与它的所有者。只读的求助直接派出；要对方
改东西（`allow_write`）一律先提交审批单，不看所有者在不在场。

登记在注册表自己的文件末尾（`coreman/core/assistants/tools.py`），这里不导入它。
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import dispatch as core
from coreman.core.assistants import jobs
from coreman.core.assistants.approvals import dispatching, tower
from coreman.core.assistants.approvals import service as approvals_service
from coreman.core.assistants.kinds import SUBTASK_KIND
from coreman.core.assistants.records import is_assistant
from coreman.core.config import get_settings
from coreman.core.crypto import Cipher
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolbase import Arguments, input_schema

_INVALID = "invalid_tool_or_arguments"
SENT_HINT = (
    "已派出。答复会由平台作为单独的一轮投回本会话，不要等它；"
    "告诉所有者一句已请这位员工查这件事即可。"
)


class AskBot(Arguments):
    bot: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=core.MAX_REF),
        Field(description="The `bot` value (or exact name) from assistant_bot_list"),
    ]
    # 长度不写进 schema：超长要返回 question_too_long 与上限，而不是一句笼统的参数无效。
    question: str = Field(
        description=(
            f"One self-contained question, at most {core.MAX_QUESTION} characters: what to find"
            " out, the scope, what the answer should contain. Only what this needs; no raw chat"
            " logs, no other people's private details."
        )
    )
    allow_write: bool = Field(
        default=False,
        description=(
            "true only when the owner explicitly asked for the employee to change something;"
            " it goes to the owner for approval first and is dispatched after approval"
        ),
    )


class BotList(Arguments):
    """没有入参。"""


OWN: dict[str, tuple[type[Arguments], str]] = {
    "assistant_bot_list": (
        BotList,
        "List the AI employees you may ask on the owner's behalf: key, name, what each does.",
    ),
    "assistant_ask_bot": (
        AskBot,
        "Ask another AI employee one question on the owner's behalf. It works in its own session"
        " with the owner's business permissions and sends nothing to anyone; its answer comes"
        " back to this conversation later as a platform turn. Do not wait for it.",
    ),
}

HINTS = {
    "invalid_question": "问题不能为空。",
    "question_too_long": "问题超过字数上限：只写完成这件事必需的信息，不要贴聊天记录或整份文档。",
    "invalid_bot": "bot 填 assistant_bot_list 返回的标识或名称。",
    "bot_not_found": "没有这位员工，或它不能被调度；用 assistant_bot_list 看可以问谁。",
    "bot_ambiguous": "不止一位员工叫这个名字：改用 assistant_bot_list 返回的 bot 标识。",
    "bot_not_available": (
        "这位员工没有开启接受求助，或所有者不在它的可用人范围内："
        "如实告诉所有者，由他找这位员工的管理者。"
    ),
    "assistant_unavailable": "助理现在不能调度公共员工；如实告诉所有者。",
    "already_dispatched": "这次求助已经派出过了，等它的答复。",
    "too_many_jobs": "进行中的调度与子任务合计已到上限，等一个结束或取消一个再派。",
    "daily_job_limit": "今天派出的调度与子任务已到上限；记待跟进，明天再问。",
    "daily_budget_exhausted": "今天的轮次预算已用完，所有者不在场时不能再调度；记待跟进。",
    "content_locked": "所有者的数据密钥不可用，现在存不了内容；不要重试，如实告诉所有者。",
}


def definitions() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": text, "inputSchema": input_schema(args)}
        for name, (args, text) in OWN.items()
    ]


async def _run(session: AsyncSession, scope: Scope, name: str, args: Any) -> dict[str, Any]:
    bot = scope.bot
    if name == "assistant_bot_list":
        return {
            "bots": core.listing(await core.candidates(session, bot)),
            "limits": {
                "question_chars": core.MAX_QUESTION,
                "active": jobs.MAX_ACTIVE,
                "per_day": jobs.MAX_DAILY,
            },
        }
    question = core.clean_question(args.question)
    target = await core.resolve(session, bot, args.bot)
    if args.allow_write:
        # 要对方改东西是对外操作：不论所有者在不在场，都先由他在审批系统里批准。
        out = await dispatching.submit(
            session,
            scope,
            target=target,
            question=question,
            cfg=tower.config(get_settings(), tower.DISPATCH_TARGET),
        )
        return out if "error" in out else {**out, "bot": target.bot_key}
    row = await core.send(
        session, bot, target, question, allow_write=False, attended=not scope.unattended
    )
    return {
        "job": jobs.view(row),
        "bot": target.bot_key,
        "name": " ".join(target.name.split()),
        "hint": SENT_HINT,
    }


async def dispatch(
    session: AsyncSession, cipher: Cipher, scope: Scope, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """执行一个调度工具；调用方（MCP 入口）负责提交。`cipher` 用不上：内容由数据密钥加解密。

    注册表按组把过关了，这里再按任务类型核一次：子任务轮次不能调度公共员工。
    """
    found = OWN.get(name)
    if found is None or not is_assistant(scope.bot) or scope.task.kind == SUBTASK_KIND:
        return {"error": _INVALID}
    try:
        args: Any = found[0].model_validate(arguments)
    except ValidationError as exc:
        return {"error": _INVALID, "invalid": approvals_service._problems(exc)}
    try:
        return await _run(session, scope, name, args)
    except (core.DispatchError, jobs.JobError) as exc:
        out: dict[str, Any] = {"error": exc.code}
        if exc.code in HINTS:
            out["hint"] = HINTS[exc.code]
        if exc.code == "question_too_long":
            out["limit"] = core.MAX_QUESTION
        if exc.code == "too_many_jobs":
            out["limit"] = jobs.MAX_ACTIVE
        if exc.code == "daily_job_limit":
            out["limit"] = jobs.MAX_DAILY
        return out
