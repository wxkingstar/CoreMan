"""个人助理在个人工具 MCP 上的那一层：写类工具改为提交审批，另加审批单管理与「处理中」表情。

MCP 入口对助理记录调这里的 `definitions` / `dispatch`，对普通机器人仍调
`feishu_personal.tools`。读类工具与授权工具原样转交。
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.approvals import catalog, marks, service, tower
from coreman.core.crypto import Cipher
from coreman.core.feishu_personal import tools as personal_tools
from coreman.core.feishu_personal.files import FileRelay
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolbase import Arguments, Identifier, Short, input_schema

STAGED_NOTE = (
    " Not executed now: staged for the owner's approval and returns pending_approval."
    " Put chat_id, anchor_message_id, background and research in `approval`."
)


def definitions(base: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把普通清单改成助理的：写类工具的入参多一个 `approval`，其余原样。"""
    meta = input_schema(service.Meta)
    out: list[dict[str, Any]] = []
    for item in base:
        name = str(item["name"])
        if not catalog.is_write(name):
            out.append(item)
            continue
        staged = copy.deepcopy(item)
        staged["description"] = str(item["description"]) + STAGED_NOTE
        staged["inputSchema"].setdefault("properties", {})[service.META] = meta
        out.append(staged)
    return out + own_definitions()


Status = Literal[
    "pending", "approved", "rejected", "withdrawn", "stale", "executed", "failed", "expired"
]


class Revise(Arguments):
    approval_id: Short
    arguments: dict[str, Any] = Field(
        description="The complete new arguments for the same tool, including `approval`"
    )


class Withdraw(Arguments):
    approval_id: Short
    reason: Annotated[str, StringConstraints(max_length=500)] = ""


class Listing(Arguments):
    status: Status | None = None
    topic: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] = ""
    limit: int = Field(default=20, ge=1, le=50)


class Mark(Arguments):
    message_id: Identifier
    done: bool = Field(default=False, description="true removes the mark")


OWN: dict[str, tuple[type[Arguments], str]] = {
    "approval_revise": (
        Revise,
        "Replace a pending approval with new arguments (same tool). The owner is notified again.",
    ),
    "approval_withdraw": (Withdraw, "Withdraw a pending approval that is no longer needed."),
    "approval_list": (
        Listing,
        "List this owner's approvals, newest first, optionally by status or topic.",
    ),
    "assistant_mark_processing": (
        Mark,
        "Put (or with done=true remove) the 'on it' reaction on a message someone sent the"
        " owner. The only write that needs no approval; the emoji is fixed.",
    ),
}


def own_definitions() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": text, "inputSchema": input_schema(args)}
        for name, (args, text) in OWN.items()
    ]


async def own_dispatch(
    session: AsyncSession,
    cipher: Cipher,
    scope: Scope,
    name: str,
    arguments: dict[str, Any],
    cfg: tower.Config | None,
    files: FileRelay | None = None,
) -> dict[str, Any] | None:
    """助理专有的工具；不是这里的工具返回 None。"""
    found = OWN.get(name)
    if found is None:
        return None
    try:
        args: Any = found[0].model_validate(arguments)
    except ValidationError as exc:
        return {"error": "invalid_tool_or_arguments", "invalid": service._problems(exc)}
    if name == "approval_revise":
        return await service.revise(
            session, cipher, scope, args.approval_id, args.arguments, cfg=cfg, files=files
        )
    if name == "approval_withdraw":
        return await service.withdraw(session, scope, args.approval_id, args.reason, cfg=cfg)
    if name == "approval_list":
        return await service.listing(
            session, scope, status=args.status, topic=args.topic or None, limit=args.limit
        )
    return await marks.mark(session, cipher, scope, args.message_id, done=args.done)


async def dispatch(
    session: AsyncSession,
    cipher: Cipher,
    scope: Scope,
    name: str,
    arguments: Any,
    files: FileRelay | None,
    cfg: tower.Config | None,
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {"error": "invalid_tool_or_arguments"}
    own = await own_dispatch(session, cipher, scope, name, arguments, cfg, files)
    if own is not None:
        return own
    if catalog.is_write(name):
        return await service.submit(session, cipher, scope, name, arguments, cfg=cfg, files=files)
    return await personal_tools.dispatch(session, cipher, scope, name, arguments, files)
