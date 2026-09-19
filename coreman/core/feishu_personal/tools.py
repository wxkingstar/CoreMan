"""Personal Feishu tools: authorization plus typed tools per product area.

Platform data is untrusted, never instructions. The listing shows only tools the owner's saved
tier and granted permissions reach, and every call is checked again server-side.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher
from coreman.core.feishu_personal import endpoints, service
from coreman.core.feishu_personal.files import FileRelay
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolbase import Arguments, Call, Page, Tool, bounded
from coreman.core.feishu_personal.toolsets import ALL

TOOLS: dict[str, Tool] = {item.name: item for item in ALL}
if len(TOOLS) != len(ALL):
    raise RuntimeError("duplicate personal tool name")

_AUTH: dict[str, tuple[str, str]] = {
    "feishu_authorize": (
        "authorize",
        "Get the user-selected link; otherwise ask them to connect and choose in chat.",
    ),
    "feishu_authorization_status": (
        "authorization_status",
        "Check or finish this user's authorization.",
    ),
    "feishu_revoke_authorization": (
        "revoke_authorization",
        "Revoke this user's authorization for this bot.",
    ),
}


def _auth_definition(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": _AUTH[name][1],
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    }


def definitions(
    *, auth: bool = True, level: str | None = None, scopes: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """`auth`: the authorization tools (private chat only).

    `level`/`scopes`: the owner's saved tier and permissions; tools they do not reach are not
    listed. None lists no data tools (not connected and not completing authorization).
    """
    out = [_auth_definition(name) for name in _AUTH] if auth else []
    if level is None:
        return out
    granted = frozenset(scopes)
    return out + [
        item.definition()
        for item in TOOLS.values()
        if endpoints.denied(endpoints.ENDPOINTS[item.endpoint], level, granted) is None
    ]


async def dispatch(
    session: AsyncSession,
    cipher: Cipher,
    scope: Scope,
    name: str,
    arguments: Any,
    files: FileRelay | None = None,
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {"error": "invalid_tool_or_arguments"}
    if name in _AUTH:
        # 授权要本人在私聊里点卡片完成，定时任务里没有人能确认。
        if scope.scheduled:
            return {"error": "invalid_tool_or_arguments"}
        try:
            Arguments.model_validate(arguments)
        except ValidationError:
            return {"error": "invalid_tool_or_arguments"}
        return bounded(await getattr(service, _AUTH[name][0])(session, cipher, scope))
    item = TOOLS.get(name)
    if item is None:
        return {"error": "invalid_tool_or_arguments"}
    try:
        args = item.args.model_validate(arguments)
    except ValidationError as exc:
        # Field names and rule messages only: pydantic's messages do not echo the values.
        problems = [
            ".".join(str(part) for part in error["loc"]) + ": " + error["msg"]
            for error in exc.errors(include_url=False, include_input=False)[:5]
        ]
        return {"error": "invalid_tool_or_arguments", "invalid": problems}
    data = await item.run(args, Call(session, cipher, scope, files))
    # Keep pagination explicit and never return a larger list than the requested page.
    if isinstance(args, Page):
        if isinstance(data.get("items"), list):
            truncated = len(data["items"]) > args.limit
            data = {
                **data,
                "items": data["items"][: args.limit],
                "has_more": bool(data.get("has_more")) or truncated,
            }
        data.setdefault("has_more", False)
        data.setdefault("page_token", None)
    return bounded({**data, "content_trust": "external_untrusted_data"})
