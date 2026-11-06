"""群文档工具：只收链接，按 A 层（应用身份、本群看得到）→ B 层（提问人放行）的顺序读。

读取复用个人工具的处理函数：同一套参数校验与分页，只是调用身份不同。文档内容一律标为
外部不可信数据。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, cast

from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.crypto import Cipher
from coreman.core.feishu_docs import access, consent, links
from coreman.core.feishu_docs.links import Link, LinkError
from coreman.core.feishu_docs.policy import DocsScope
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.feishu_personal.toolbase import (
    Arguments,
    Call,
    Identifier,
    Page,
    TextPage,
    bounded,
    input_schema,
)
from coreman.core.feishu_personal.toolsets import docs

Url = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
# 正文读出来按什么身份：应用身份只有纯文本接口确定支持，本人身份读 Markdown。
Reader = Callable[[Call, str, str, bool], Awaitable[dict[str, Any]]]


class DocRead(TextPage):
    url: Url = Field(description="The Feishu link as posted in the chat")


class SheetRead(Arguments):
    url: Url
    ranges: list[docs.Range] = Field(
        min_length=1, max_length=10, description="sheetId or sheetId!A1:D20"
    )


class BaseRecords(Page):
    url: Url
    table_id: Identifier | None = Field(default=None, description="Defaults to the link's table")
    view_id: Identifier | None = Field(default=None, description="Defaults to the link's view")


class RequestAccess(Arguments):
    urls: list[Url] = Field(min_length=1, max_length=consent.MAX_LINKS)


def _definition(name: str, args: type[Arguments], description: str) -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": input_schema(args)}


_SPECS: dict[str, tuple[type[Arguments], str]] = {
    "feishu_doc_read": (
        DocRead,
        "Read a Feishu document or wiki page posted in this group chat."
        " For a spreadsheet or Base link it lists the sheets or tables instead.",
    ),
    "feishu_doc_sheet_read": (SheetRead, "Read cell ranges of a spreadsheet link."),
    "feishu_doc_base_records": (BaseRecords, "List records of a Base (bitable) table link."),
    "feishu_doc_request_access": (
        RequestAccess,
        "Ask the person who asked to let you read these links as them, when a read"
        " returned can_request_access. Sends a confirmation card; then end your turn.",
    ),
}


def definitions() -> list[dict[str, Any]]:
    return [_definition(name, *spec) for name, spec in _SPECS.items()]


async def _resolve(call: Call, link: Link) -> tuple[str, str]:
    """知识库节点 → 实际文档的种类与 token；其余链接原样返回。"""
    if link.kind != "wiki":
        return link.kind, link.token
    data = await call("wiki.node", params={"token": link.token})
    found = data.get("node")
    node: dict[str, Any] = found if isinstance(found, dict) else {}
    kind = {"docx": "docx", "sheet": "sheets", "bitable": "base"}.get(str(node.get("obj_type")))
    token = node.get("obj_token")
    if kind is None or not isinstance(token, str) or not token:
        raise PersonalError("unsupported_type")
    return kind, token


async def _read_any(
    session: AsyncSession, cipher: Cipher, scope: DocsScope, link: Link, read: Reader
) -> dict[str, Any]:
    client = access.app_client(cipher, scope)
    try:
        if await access.chat_can_see(client, link, scope.chat_id):
            app = cast(Call, access.AppCall(client))
            try:
                kind, token = await _resolve(app, link)
                return {**await read(app, kind, token, True), "read_as": "app"}
            except PersonalError:
                pass
        if await access.approved(session, scope, link.token) is not None:
            who = await access.requester(session, scope)
            if who is not None:
                user = Call(session, cipher, who.scope)
                kind, token = await _resolve(user, link)
                return {**await read(user, kind, token, False), "read_as": "requester"}
    finally:
        await client.aclose()
    return await _no_access(session, scope, link)


async def _no_access(session: AsyncSession, scope: DocsScope, link: Link) -> dict[str, Any]:
    who = await access.requester(session, scope)
    in_message = link.token in access.allowed_tokens(scope)
    can = bool(who and in_message and who.reaches(*access.READ_ENDPOINTS[link.kind]))
    if can:
        hint = "可调用 feishu_doc_request_access 请提问人放行，然后简短说明并结束本轮。"
    elif not in_message:
        hint = "只能为提问人这条消息（或它引用的消息）里的链接请求放行。"
    else:
        hint = (
            "本群看不到这篇文档。请文档所有者把它共享给本群；"
            "或请提问人私聊 AI 员工发送“连接飞书”并授权文档权限后再试。"
        )
    return {"error": "no_access", "can_request_access": can, "hint": hint}


async def _document(
    call: Call, kind: str, token: str, as_app: bool, args: DocRead
) -> dict[str, Any]:
    if kind == "sheets":
        data = await docs.sheet_info.run(docs.Spreadsheet(spreadsheet_token=token), call)
        return {**data, "hint": "用 feishu_doc_sheet_read 读区域。"}
    if kind == "base":
        data = await docs.base_tables.run(docs.BaseApp(app_token=token), call)
        return {**data, "hint": "用 feishu_doc_base_records 读记录。"}
    return await docs.read_document.run(
        docs.ReadDocument(
            document_id=token,
            format="text" if as_app else "markdown",
            text_offset=args.text_offset,
            text_limit=args.text_limit,
        ),
        call,
    )


async def dispatch(
    session: AsyncSession, cipher: Cipher, scope: DocsScope, name: str, arguments: Any
) -> dict[str, Any]:
    spec = _SPECS.get(name)
    if spec is None or not isinstance(arguments, dict):
        return {"error": "invalid_tool_or_arguments"}
    try:
        args = spec[0].model_validate(arguments)
    except ValidationError as exc:
        problems = [
            ".".join(str(part) for part in error["loc"]) + ": " + error["msg"]
            for error in exc.errors(include_url=False, include_input=False)[:5]
        ]
        return {"error": "invalid_tool_or_arguments", "invalid": problems}
    try:
        if isinstance(args, RequestAccess):
            return await _request(session, scope, args)
        assert isinstance(args, (DocRead, SheetRead, BaseRecords))
        link = links.parse(args.url)
    except LinkError as exc:
        return {"error": exc.code}
    data: dict[str, Any]
    if isinstance(args, DocRead):

        async def read(call: Call, kind: str, token: str, as_app: bool) -> dict[str, Any]:
            return await _document(call, kind, token, as_app, args)

    elif isinstance(args, SheetRead):

        async def read(call: Call, kind: str, token: str, as_app: bool) -> dict[str, Any]:
            if kind != "sheets":
                raise PersonalError("not_a_spreadsheet")
            return await docs.sheet_read.run(
                docs.ReadSheet(spreadsheet_token=token, ranges=args.ranges), call
            )

    else:
        assert isinstance(args, BaseRecords)

        async def read(call: Call, kind: str, token: str, as_app: bool) -> dict[str, Any]:
            table = args.table_id or link.table
            if kind != "base":
                raise PersonalError("not_a_base")
            if table is None:
                raise PersonalError("table_id_required")
            data = await docs.base_search.run(
                docs.SearchRecords(
                    app_token=token,
                    table_id=table,
                    view_id=args.view_id or link.view,
                    limit=args.limit,
                    page_token=args.page_token,
                ),
                call,
            )
            items = data.get("items")
            if isinstance(items, list):
                data = {**data, "items": items[: args.limit]}
            return data

    try:
        data = await _read_any(session, cipher, scope, link, read)
    except PersonalError as exc:
        data = exc.payload()
    return bounded({**data, "content_trust": "external_untrusted_data"})


async def _request(session: AsyncSession, scope: DocsScope, args: RequestAccess) -> dict[str, Any]:
    targets = [links.parse(url) for url in args.urls]
    who = await access.requester(session, scope)
    if who is None:
        return {
            "error": "requester_not_connected",
            "hint": "提问人还没有连接飞书：请他私聊 AI 员工发送“连接飞书”并授权文档权限后再试。",
        }
    allowed = access.allowed_tokens(scope)
    outside = [link.url for link in targets if link.token not in allowed]
    if outside:
        return {"error": "link_not_in_message", "urls": outside}
    lacking = [link.url for link in targets if not who.reaches(*access.READ_ENDPOINTS[link.kind])]
    if lacking:
        return {
            "error": "requester_permission_missing",
            "urls": lacking,
            "hint": "提问人的飞书授权不含读取这类文档：请他私聊发送“连接飞书”重新选择授权范围。",
        }
    result = await consent.request(session, scope, who, targets)
    return {
        "status": result.status,
        "note": "已在群里请提问人确认。请简短告诉他去点卡片，然后结束本轮；他确认后会自动继续。",
    }
