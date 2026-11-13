"""Personal Feishu tools: authorization plus typed tools per product area.

Platform data is untrusted, never instructions. The listing shows only tools the owner's saved
tier and granted permissions reach, and every call is checked again server-side.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Any, Literal

from pydantic import StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher
from coreman.core.feishu_personal import endpoints, inflow, service
from coreman.core.feishu_personal.files import FileRelay
from coreman.core.feishu_personal.policy import Scope
from coreman.core.feishu_personal.toolbase import Arguments, Call, Page, Tool, bounded
from coreman.core.feishu_personal.toolsets import ALL

TOOLS: dict[str, Tool] = {item.name: item for item in ALL}
if len(TOOLS) != len(ALL):
    raise RuntimeError("duplicate personal tool name")


class Authorize(Arguments):
    need: Literal["general", "read_messages", "send_messages"] = "general"
    purpose: Annotated[str, StringConstraints(max_length=60)] = ""


_AUTH: dict[str, tuple[str, str]] = {
    "feishu_authorize": (
        "",
        "Call when the user needs Feishu data or actions you cannot reach yet: sends a "
        "one-click authorization card; the conversation resumes by itself.",
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
# 没有人在场的轮次（个人助理里平台自己发起的）只能查询授权状态：发起授权要所有者点卡片，
# 撤销授权不该由别人发来的消息引出。
STATUS_TOOL = "feishu_authorization_status"
OWNER_TURN_REQUIRED = {
    "error": "owner_turn_required",
    "hint": "Nobody is present in this turn. Tell the owner what is needed; authorization is"
    " requested or revoked only when the owner asks for it in the private chat.",
}


def _auth_definition(name: str) -> dict[str, Any]:
    schema = (
        Authorize.model_json_schema()
        if name == "feishu_authorize"
        else {"type": "object", "properties": {}, "additionalProperties": False}
    )
    return {"name": name, "description": _AUTH[name][1], "inputSchema": schema}


def definitions(
    *,
    auth: bool = True,
    owner_present: bool = True,
    level: str | None = None,
    scopes: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """`auth`: the authorization tools (private chat only).

    `owner_present`: False for a turn the platform started itself; only the status query of
    the authorization tools is listed.

    `level`/`scopes`: the owner's saved tier and permissions; tools they do not reach are not
    listed. None lists no data tools (not connected and not completing authorization).
    """
    names = _AUTH if owner_present else (STATUS_TOOL,)
    out = [_auth_definition(name) for name in names] if auth else []
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
        if scope.platform and name != STATUS_TOOL:
            return dict(OWNER_TURN_REQUIRED)
        try:
            if name == "feishu_authorize":
                asked = Authorize.model_validate(arguments)
                return bounded(
                    await inflow.request(
                        session, cipher, scope, need=asked.need, purpose=asked.purpose
                    )
                )
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
