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
from coreman.core.feishu_docs import access, consent, links, write_policy, write_tools
from coreman.core.feishu_docs.links import Link, LinkError
from coreman.core.feishu_docs.policy import DocsScope
from coreman.core.feishu_personal import inflow
from coreman.core.feishu_personal.files import FileRelay
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
from coreman.core.feishu_personal.tools import TOOLS
from coreman.core.feishu_personal.toolsets import docs
from coreman.core.feishu_personal.toolsets import files as file_tools

Url = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
# 正文读出来按什么身份：应用身份只有纯文本接口确定支持，本人身份读 Markdown。
Reader = Callable[[Call, str, str, bool], Awaitable[dict[str, Any]]]
# 一次最多取几张图：每张都要以提问人身份下载、转存一份。
MAX_MEDIA = 10
_MEDIA_HINT = (
    "图片和附件是 https://feishu.cn/file/<token> 链接，要登录飞书才能打开；"
    "要看时用 feishu_doc_media，url 传这篇文档的链接，tokens 传链接里的 token。"
)
_APP_TEXT_HINT = (
    "以应用身份只读到纯文本，图片和附件不在里面。确实需要看文档里的图时才用 feishu_doc_media"
    "（会以提问人身份读，他可能要先授权）。"
)


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


class DocMedia(Arguments):
    url: Url = Field(description="The document link as posted in the chat")
    tokens: list[Identifier] = Field(
        default_factory=list,
        max_length=MAX_MEDIA,
        description="The <token> of https://feishu.cn/file/<token> links in the document;"
        " omit to get its first images",
    )


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
    "feishu_doc_media": (
        DocMedia,
        "Get short-lived download links for images or attachments in a document link, e.g. to"
        " look at its screenshots. Always read as the asker.",
    ),
    "feishu_doc_offer_share": (
        RequestAccess,
        "Optional, after a read with can_offer_share: post a button so the asker can share"
        " these links with this group in one click. Do not wait for it; answer as usual.",
    ),
}


def definitions() -> list[dict[str, Any]]:
    return [_definition(name, *spec) for name, spec in _SPECS.items()] + [
        TOOLS[name].definition() for name in write_policy.GROUP_TOOLS
    ]


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
    session: AsyncSession,
    cipher: Cipher,
    scope: DocsScope,
    link: Link,
    read: Reader,
    *,
    files: FileRelay | None = None,
    as_app: bool = True,
    also: tuple[str, ...] = (),
) -> dict[str, Any]:
    """先用应用身份读（本群看得到才行），不行就直接以提问人本人身份读。

    以他的身份只读本群真人发过的链接，和本机器人近期在本群为他新建的文档。

    as_app 为假时只以提问人身份读（文件中转的下载链接要绑定他的授权）；also 是除读正文外
    还要用到的接口。
    """
    if as_app:
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
    if link.token not in await access.allowed_tokens(
        session, scope
    ) and link.token not in await access.own_documents(session, scope):
        return _no_access("link_not_from_chat")
    state = await access.asker_state(session, cipher, scope)
    who = state.requester
    if who is None and state.status == "unknown_speaker":
        return _no_access("unknown_speaker")

    needed = access.READ_ENDPOINTS[link.kind] + also

    async def ask_authorization() -> dict[str, Any]:
        result = await inflow.request(
            session,
            cipher,
            access.group_scope(cipher, scope),
            purpose="读取群里的飞书文档",
            endpoints_needed=needed,
        )
        if result.get("status") == "already_connected":
            # 刚判过不够，这里不该已够；真遇到了也不能当作授权成功。
            return _no_access("asker_cannot_open")
        return result

    if who is None or not who.reaches(*needed):
        return await ask_authorization()
    user = Call(session, cipher, who.scope, files)
    try:
        kind, token = await _resolve(user, link)
        data = await read(user, kind, token, False)
    except PersonalError as exc:
        if exc.code == "authorization_required":
            # 令牌刷新失败等：授权已失效，等同没连接。
            await session.flush()
            return await ask_authorization()
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
        "这个链接不是本群有人发过的，也不是你近 7 天在本群为提问人新建的，"
        "不能以提问人身份读取。请提问人把链接发到群里。"
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
    data = await docs.read_document.run(
        docs.ReadDocument(
            document_id=token,
            format="text" if as_app else "markdown",
            text_offset=args.text_offset,
            text_limit=args.text_limit,
        ),
        call,
    )
    if as_app:
        return {**data, "media_hint": _APP_TEXT_HINT}
    if "media_hint" in data:
        # 个人工具的提示指向私聊里的下载工具，群里换成本组的。
        return {**data, "media_hint": _MEDIA_HINT}
    return data


async def _media(call: Call, kind: str, token: str, wanted: list[str]) -> dict[str, Any]:
    """文档里的图片和附件：只取这篇文档正文里出现的，逐个以提问人身份下载、给中转链接。"""
    if kind != "docx":
        raise PersonalError("unsupported_type")
    content = (await docs.fetch_markdown(call, token)).get("content")
    found = docs.media_tokens(content) if isinstance(content, str) else []
    picked = list(dict.fromkeys(wanted)) or found[:MAX_MEDIA]
    outside = [item for item in picked if item not in found]
    if outside:
        # 防止借这篇文档的放行去取提问人别处的文件。
        return {
            "error": "media_not_in_document",
            "tokens": outside,
            "hint": "只能取这篇文档正文里 https://feishu.cn/file/<token> 链接中的 token。",
        }
    items: list[dict[str, Any]] = []
    for item in picked:
        try:
            got = await file_tools.download_doc_media.run(
                file_tools.DocMedia(file_token=item), call
            )
        except PersonalError as exc:
            if exc.code == "authorization_required":
                raise
            got = exc.payload()
        items.append({"token": item, **{key: value for key, value in got.items() if key != "how"}})
    result: dict[str, Any] = {"items": items, "total": len(found), "how": file_tools.HOW_DOWNLOAD}
    rest = [item for item in found if item not in picked]
    if rest and not wanted:
        result["more_tokens"] = rest[:100]
    return result


async def dispatch(
    session: AsyncSession,
    cipher: Cipher,
    scope: DocsScope,
    name: str,
    arguments: Any,
    files: FileRelay | None = None,
) -> dict[str, Any]:
    if name in write_policy.GROUP_TOOLS and isinstance(arguments, dict):
        try:
            return await write_tools.dispatch(session, cipher, scope, name, arguments, files)
        except PersonalError as exc:
            return exc.payload()
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
        assert isinstance(args, (DocRead, SheetRead, BaseRecords, DocMedia))
        link = links.parse(args.url)
    except LinkError as exc:
        return {"error": exc.code}
    data: dict[str, Any]
    media = isinstance(args, DocMedia)
    if media and link.kind not in ("docx", "wiki"):
        return {"error": "unsupported_type"}
    if isinstance(args, DocRead):

        async def read(call: Call, kind: str, token: str, as_app: bool) -> dict[str, Any]:
            return await _document(call, kind, token, as_app, args)

    elif isinstance(args, DocMedia):

        async def read(call: Call, kind: str, token: str, as_app: bool) -> dict[str, Any]:
            return await _media(call, kind, token, args.tokens)

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
        # 图片的下载链接要绑定提问人的授权，所以只以他的身份取。
        data = await _read_any(
            session,
            cipher,
            scope,
            link,
            read,
            files=files,
            as_app=not media,
            also=("docs.media",) if media else (),
        )
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
