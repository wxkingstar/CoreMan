"""个人助理在个人工具 MCP 上的那一层：写类工具改为提交审批，另加审批单管理与「处理中」表情。

MCP 入口对助理记录调这里的 `definitions` / `dispatch`，对普通机器人仍调
`feishu_personal.tools`。读类工具与授权工具原样转交。
"""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.approvals import catalog, service, tower
from coreman.core.crypto import Cipher
from coreman.core.feishu_personal import tools as personal_tools
from coreman.core.feishu_personal.files import FileRelay
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolbase import input_schema

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


def own_definitions() -> list[dict[str, Any]]:
    return []


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
    return None


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
