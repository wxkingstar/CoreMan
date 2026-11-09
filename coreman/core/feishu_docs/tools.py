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
from coreman.core.feishu_personal import inflow
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
    "feishu_doc_offer_share": (
        RequestAccess,
        "Optional, after a read with can_offer_share: post a button so the asker can share"
        " these links with this group in one click. Do not wait for it; answer as usual.",
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
    """先用应用身份读（本群看得到才行），不行就直接以提问人本人身份读（他发过的链接才行）。"""
    client = access.app_client(cipher, scope)
    try:
        if await access.chat_can_see(client, link, scope.chat_id):
            app = cast(Call, access.AppCall(client))
            try:
                kind, token = await _resolve(app, link)
                return {**await read(app, kind, token, True), "read_as": "app"}
            except PersonalError:
                pass
    finally:
        await client.aclose()
    if link.token not in await access.allowed_tokens(session, scope):
        return _no_access("link_not_from_chat")
    state = await access.asker_state(session, cipher, scope)
    who = state.requester
    if who is None and state.status == "unknown_speaker":
        return _no_access("unknown_speaker")
    if who is None or not who.reaches(*access.READ_ENDPOINTS[link.kind]):
        result = await inflow.request(
            session,
            cipher,
            access.group_scope(cipher, scope),
            purpose="读取群里的飞书文档",
            endpoints_needed=access.READ_ENDPOINTS[link.kind],
        )
        if result.get("status") == "already_connected":
            # reaches() 刚判过不够，这里不该已够；真遇到了也不能当作授权成功。
            return _no_access("asker_cannot_open")
        return result
    user = Call(session, cipher, who.scope)
    try:
        kind, token = await _resolve(user, link)
        data = await read(user, kind, token, False)
    except PersonalError as exc:
        if getattr(exc, "code", "") in (
            "unsupported_type",
            "not_a_spreadsheet",
            "not_a_base",
            "table_id_required",
        ):
            raise
        return {**_no_access("asker_cannot_open"), "upstream": exc.payload()}
    return {
        **data,
        "read_as": "requester",
        "can_offer_share": who.reaches(access.SHARE_ENDPOINT),
    }


# 调用方用错了工具或参数：如实返回，不算「他打不开」。
_CALLER_ERRORS = ("unsupported_type", "not_a_spreadsheet", "not_a_base", "table_id_required")
_HINTS = {
    "link_not_from_chat": (
        "这个链接不是本群有人发过的，不能以提问人身份读取。请提问人把链接发到群里。"
    ),
    "unknown_speaker": (
        "本群看不到这篇文档，提问人也不是平台成员，没法以他的身份读。请文档所有者把它共享给本群。"
    ),
    "asker_cannot_open": (
        "已经以提问人身份试过，他本人也打不开这篇文档；应用没法替他向文档所有者申请。"
        "请他用自己的飞书打开这个链接，点页面上的「申请权限」，所有者会收到申请；"
        "所有者通过后在群里说一声就能接着读，不用再发链接。也可以请所有者直接共享给本群。"
    ),
}


def _no_access(reason: str) -> dict[str, Any]:
    return {"error": "no_access", "reason": reason, "hint": _HINTS[reason]}


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
            return await _request(session, cipher, scope, args)
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


async def _request(
    session: AsyncSession, cipher: Cipher, scope: DocsScope, args: RequestAccess
) -> dict[str, Any]:
    targets = [links.parse(url) for url in args.urls]
    who = (await access.asker_state(session, cipher, scope)).requester
    if who is None or not who.reaches(access.SHARE_ENDPOINT):
        return {"error": "share_unavailable", "hint": "提问人没有可用的飞书授权，不能代他共享。"}
    allowed = await access.allowed_tokens(session, scope)
    outside = [link.url for link in targets if link.token not in allowed]
    if outside:
        return {"error": "link_not_from_chat", "urls": outside}
    result = await consent.request(session, scope, who, targets)
    return {
        "status": result.status,
        "note": "已在群里给提问人发了「共享给本群」按钮，点不点都不影响本轮，照常回答即可。",
    }
