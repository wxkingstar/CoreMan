"""个人助理自有工具的总注册表：个人工具 MCP 对助理记录在这里取清单、在这里分派。

普通轮次三组，子任务轮次另有一组：
- `knowledge`：知识库、待跟进与助理设置。数据在平台自己的库里、不调飞书，所以飞书授权换代之后
  照常可用（MCP 入口把它排在授权代数判断之前），子任务也可用。
- `approval`：审批单管理与「处理中」表情，连同写类个人工具的暂存（提交审批）。
- `jobs`：派子任务、追问、查看、取消，以及调度公共员工（看可以问谁、把问题交给其中一位）。
  子任务自己不可用。
- `subtask`：子任务报告自己卡住了。只有子任务轮次可用。

一轮能用哪几组由任务类型决定（`groups_for`）。没有 `approval` 组的轮次拿到的个人工具只有读类：
写类工具不列出，调用也不受理——子任务要对外做事，只能把建议写进结论，由主会话提交。
`knowledge`、`jobs`、`subtask` 与下面的 `voice` 的数据都在平台自己的库里（`LOCAL_GROUPS`），MCP
入口把它们排在授权代数判断之前。

另有一组 `voice`（冷启动的口吻样本）：只有 `assistant_cold_start` 的轮次有它，而且那一轮只有
它——不列、不受理任何个人工具（读类也没有）与别的组。那一轮要生成一页置顶页，输入只能是
所有者自己发出的消息，所以不给它任何能读到别人内容的工具。

各组的工具在本文件末尾登记，清单不取决于别的模块有没有被导入。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core import personal_schedules as schedules
from coreman.core.assistants import (
    dispatch_tools,
    followup_tools,
    jobs_tools,
    notes_tools,
    settings_tools,
    voice_tools,
)
from coreman.core.assistants.approvals import catalog, service, tower
from coreman.core.assistants.approvals import tools as approval_tools
from coreman.core.assistants.kinds import COLD_START_KIND, SUBTASK_KIND
from coreman.core.crypto import Cipher
from coreman.core.feishu_personal import tools as personal_tools
from coreman.core.feishu_personal.files import FileRelay
from coreman.core.feishu_personal.policy import Scope

# 普通轮次（所有者的私聊、交回、结果回合）能用的三组。
GROUPS = ("knowledge", "approval", "jobs")
# 只有子任务轮次能用的一组。
SUBTASK_GROUP = "subtask"
# 只有冷启动的口吻样本那一轮有；有它的轮次没有别的任何工具（`bare`）。
VOICE_GROUP = "voice"
ALL_GROUPS = (*GROUPS, SUBTASK_GROUP, VOICE_GROUP)
# 数据在平台自己的库里、不调飞书的组：飞书授权换代之后同一轮里照常可用。
LOCAL_GROUPS = frozenset({"knowledge", "jobs", SUBTASK_GROUP, VOICE_GROUP})
_INVALID = "invalid_tool_or_arguments"


@dataclass(frozen=True)
class Context:
    """一次工具调用的环境：已核验的所有者身份，加上审批与文件中转要用的配置。"""

    session: AsyncSession
    cipher: Cipher
    scope: Scope
    files: FileRelay | None = None
    cfg: tower.Config | None = None


Definitions = Callable[[], list[dict[str, Any]]]
Dispatch = Callable[[Context, str, dict[str, Any]], Awaitable[dict[str, Any]]]

_DEFINITIONS: dict[str, list[Definitions]] = {group: [] for group in ALL_GROUPS}
_TOOLS: dict[str, tuple[str, Dispatch]] = {}


def register(group: str, definitions: Definitions, dispatch: Dispatch) -> None:
    """把一批工具登记进一个组。

    Args:
        definitions: 返回这批工具的 MCP 定义（`name` / `description` / `inputSchema`）；工具名在
            登记时取一次，之后每次列清单都重新调用
        dispatch: `(ctx, 工具名, 入参字典) -> 结果字典`；只会收到这批里的工具名

    Raises:
        ValueError: 组名不认识，或工具名与已登记的工具、个人工具、授权工具、本人定时任务工具
            重名（后两样由 MCP 入口另行分派，重名的工具永远轮不到）
    """
    if group not in _DEFINITIONS:
        raise ValueError(f"unknown tool group: {group}")
    names = [str(item["name"]) for item in definitions()]
    taken = sorted(
        {
            name
            for name in names
            if name in _TOOLS
            or name in personal_tools.TOOLS
            or name in personal_tools.AUTH_TOOLS
            or name in schedules.TOOLS
            or names.count(name) > 1
        }
    )
    if taken:
        raise ValueError(f"tool names already taken: {', '.join(taken)}")
    _DEFINITIONS[group].append(definitions)
    for name in names:
        _TOOLS[name] = (group, dispatch)


def group_of(name: str) -> str | None:
    """这个工具登记在哪个组；不是助理自有的工具返回 None。"""
    found = _TOOLS.get(name)
    return found[0] if found is not None else None


def groups_for(kind: str) -> tuple[str, ...]:
    """这类任务的一轮能用哪几组。

    口吻样本只有 voice，子任务只有 knowledge 与 subtask，其余的轮次是普通的三组。
    """
    if kind == COLD_START_KIND:
        return (VOICE_GROUP,)
    return ("knowledge", SUBTASK_GROUP) if kind == SUBTASK_KIND else GROUPS


def bare(groups: Iterable[str]) -> bool:
    """这一轮只带登记在它那几组里的工具：没有个人工具与授权工具。

    本人定时任务工具不归这里管：MCP 入口对一切无人在场的轮次都不列、不受理。
    """
    return VOICE_GROUP in frozenset(groups)


def definitions(base: list[dict[str, Any]], *, groups: Iterable[str]) -> list[dict[str, Any]]:
    """把个人工具的清单改成助理这一轮的。

    Args:
        base: `feishu_personal.tools.definitions(...)` 的结果
        groups: 这一轮能用的组（`groups_for`）
    """
    allowed = frozenset(groups)
    out: list[dict[str, Any]] = []
    # 只带本组工具的轮次：个人工具一个都不列。
    for item in [] if bare(allowed) else base:
        if not catalog.is_write(str(item["name"])):
            out.append(item)
        elif "approval" in allowed:
            out.append(approval_tools.staged(item))
    for group in ALL_GROUPS:
        if group in allowed:
            for listing in _DEFINITIONS[group]:
                out.extend(listing())
    return out


async def dispatch(
    ctx: Context, name: str, arguments: Any, *, groups: Iterable[str]
) -> dict[str, Any]:
    """分派助理的一次工具调用：组内工具、写类个人工具（提交审批）、其余个人工具。"""
    if not isinstance(arguments, dict):
        return {"error": _INVALID}
    allowed = frozenset(groups)
    found = _TOOLS.get(name)
    if found is not None:
        group, run = found
        return await run(ctx, name, arguments) if group in allowed else {"error": _INVALID}
    if bare(allowed):
        return {"error": _INVALID}
    if catalog.is_write(name):
        if "approval" not in allowed:
            return {"error": _INVALID}
        return await service.submit(
            ctx.session, ctx.cipher, ctx.scope, name, arguments, cfg=ctx.cfg, files=ctx.files
        )
    return await personal_tools.dispatch(
        ctx.session, ctx.cipher, ctx.scope, name, arguments, ctx.files
    )


async def _approval(ctx: Context, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    out = await approval_tools.own_dispatch(
        ctx.session, ctx.cipher, ctx.scope, name, arguments, ctx.cfg, ctx.files
    )
    return out if out is not None else {"error": _INVALID}


async def _notes(ctx: Context, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return await notes_tools.dispatch(ctx.session, ctx.cipher, ctx.scope, name, arguments)


async def _followups(ctx: Context, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return await followup_tools.dispatch(ctx.session, ctx.cipher, ctx.scope, name, arguments)


async def _settings(ctx: Context, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return await settings_tools.dispatch(ctx.session, ctx.cipher, ctx.scope, name, arguments)


async def _jobs(ctx: Context, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return await jobs_tools.dispatch(ctx.session, ctx.cipher, ctx.scope, name, arguments)


async def _ask_bots(ctx: Context, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return await dispatch_tools.dispatch(ctx.session, ctx.cipher, ctx.scope, name, arguments)


async def _voice(ctx: Context, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return await voice_tools.dispatch(ctx.session, ctx.cipher, ctx.scope, name, arguments)


register("knowledge", lambda: list(notes_tools.DEFINITIONS), _notes)
register("knowledge", followup_tools.definitions, _followups)
register("knowledge", lambda: list(settings_tools.DEFINITIONS), _settings)
register("approval", approval_tools.own_definitions, _approval)
register(VOICE_GROUP, lambda: list(voice_tools.DEFINITIONS), _voice)
register("jobs", jobs_tools.definitions, _jobs)
register("jobs", dispatch_tools.definitions, _ask_bots)
register(SUBTASK_GROUP, jobs_tools.subtask_definitions, _jobs)
