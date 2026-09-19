"""云文档：搜索、云空间、文档读写与共享、评论、电子表格、多维表格、知识库。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, JsonValue, StringConstraints, model_validator

from coreman.core.feishu_personal import service
from coreman.core.feishu_personal.toolbase import (
    Arguments,
    Call,
    Identifier,
    Page,
    Short,
    TextPage,
    compact,
    page,
    text_page,
    tool,
)

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=800)]
Range = Annotated[
    str,
    StringConstraints(
        max_length=100, pattern=r"^[A-Za-z0-9_-]+(![A-Z]{1,3}[0-9]*(:[A-Z]{1,3}[0-9]*)?)?$"
    ),
]
Cell = str | int | float | bool | None
Rows = Annotated[
    list[Annotated[list[Cell], Field(max_length=100)]], Field(min_length=1, max_length=1000)
]
FieldName = Annotated[str, StringConstraints(min_length=1, max_length=255)]
FileType = Literal["docx", "doc", "sheet", "bitable", "file", "slides"]
DocType = Literal["doc", "docx", "sheet", "bitable", "mindnote", "file", "wiki", "slides"]


class SearchDocs(Page):
    query: Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)] = ""
    doc_types: list[DocType] = Field(default_factory=list, max_length=8)


class DriveFiles(Page):
    folder_token: Identifier | None = Field(default=None, description="Omit for My Space root")


class ReadDocument(TextPage):
    document_id: Identifier = Field(description="docx token, or a wiki node token for markdown")
    kind: Literal["docx", "doc", "slides", "mindnote"] = Field(
        default="docx",
        description="doc is the old document type; wiki pages of other kinds: see feishu_wiki_node",
    )
    format: Literal["markdown", "text"] = Field(
        default="markdown", description="text is plain text of a docx only"
    )
    with_block_ids: bool = Field(
        default=False, description="Markdown with block IDs, for block edits"
    )


Markdown = Annotated[str, StringConstraints(max_length=50000)]


class EditDocument(Arguments):
    document_id: Identifier = Field(description="docx token or wiki node token")
    command: Literal[
        "str_replace", "block_replace", "block_delete", "block_insert_after", "overwrite"
    ] = Field(
        description=(
            "str_replace: replace pattern with content; block_*: act on block_id (insert_after"
            " takes -1 for the end, 0 for the start); overwrite: replace the whole document"
        )
    )
    content: Markdown = Field(default="", description="Markdown")
    pattern: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = Field(
        default=None, description="Exact text to find, for str_replace"
    )
    block_id: Identifier | None = None

    @model_validator(mode="after")
    def complete(self) -> EditDocument:
        if self.command == "str_replace" and not self.pattern:
            raise ValueError("str_replace needs pattern")
        if self.command.startswith("block_") and not self.block_id:
            raise ValueError(f"{self.command} needs block_id")
        if self.command in ("block_replace", "block_insert_after", "overwrite") and not (
            self.content
        ):
            raise ValueError(f"{self.command} needs content")
        return self


class ShareDocument(Arguments):
    token: Identifier = Field(description="Document, sheet, base, folder or wiki node token")
    doc_type: Literal["docx", "doc", "sheet", "bitable", "file", "folder", "wiki", "mindnote"]
    member_open_ids: Annotated[list[Identifier], Field(min_length=1, max_length=20)]
    permission: Literal["view", "edit", "full_access"] = "view"
    notify: bool = True


FileKind = Literal["docx", "doc", "sheet", "bitable", "mindnote", "file", "slides", "folder"]


class FileRef(Arguments):
    token: Identifier
    type: FileKind


class FileInfo(Arguments):
    files: Annotated[list[FileRef], Field(min_length=1, max_length=20)]


class MoveFile(FileRef):
    folder_token: Identifier = Field(description="Destination folder")


class CopyFile(FileRef):
    name: Title
    folder_token: Identifier = Field(description="Destination folder")


class RenameFile(Arguments):
    token: Identifier
    type: Literal["docx", "sheet", "bitable", "file"]
    new_title: Title


# Base field types by name; Feishu uses numbers.
FIELD_TYPES = {
    "text": 1,
    "number": 2,
    "single_select": 3,
    "multi_select": 4,
    "date": 5,
    "checkbox": 7,
    "person": 11,
    "phone": 13,
    "url": 15,
    "attachment": 17,
}
FieldType = Literal[
    "text",
    "number",
    "single_select",
    "multi_select",
    "date",
    "checkbox",
    "person",
    "phone",
    "url",
    "attachment",
]


class NewField(Arguments):
    name: FieldName
    type: FieldType = "text"
    options: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=50, description="Choices for select fields"
    )

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"field_name": self.name, "type": FIELD_TYPES[self.type]}
        if self.options:
            body["property"] = {"options": [{"name": option} for option in self.options]}
        return body


class CreateTable(Arguments):
    app_token: Identifier
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    fields: list[NewField] = Field(default_factory=list, max_length=50)


class CreateField(NewField):
    app_token: Identifier
    table_id: Identifier


class CreateFolder(Arguments):
    name: Title
    folder_token: Identifier | None = Field(default=None, description="Omit for My Space root")


class CreateDocument(Arguments):
    title: Title
    folder_token: Identifier | None = None


class Block(Arguments):
    type: Literal[
        "text", "heading1", "heading2", "heading3", "bullet", "ordered", "code", "quote", "todo"
    ] = "text"
    text: Annotated[str, StringConstraints(min_length=1, max_length=5000)]


class AppendDocument(Arguments):
    document_id: Identifier
    blocks: list[Block] = Field(min_length=1, max_length=50)


class Comments(Page):
    file_token: Identifier
    file_type: FileType


class AddComment(Arguments):
    file_token: Identifier
    file_type: FileType
    text: Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class Spreadsheet(Arguments):
    spreadsheet_token: Identifier


class ReadSheet(Spreadsheet):
    ranges: list[Range] = Field(
        min_length=1, max_length=10, description="sheetId or sheetId!A1:D20"
    )


class WriteSheet(Spreadsheet):
    range: Range = Field(description="sheetId!A1:D20; the values must fit it")
    values: Rows


class ManageSheet(Spreadsheet):
    action: Literal["add", "rename"]
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    sheet_id: Identifier | None = Field(default=None, description="Sheet to rename")
    index: int | None = Field(default=None, ge=0, le=200, description="Position for a new sheet")

    @model_validator(mode="after")
    def target(self) -> ManageSheet:
        if self.action == "rename" and not self.sheet_id:
            raise ValueError("rename needs sheet_id")
        return self


class CreateSheet(Arguments):
    title: Title
    folder_token: Identifier | None = None


class BaseApp(Page):
    app_token: Identifier


class BaseTable(BaseApp):
    table_id: Identifier


class Condition(Arguments):
    field_name: FieldName
    operator: Literal[
        "is",
        "isNot",
        "contains",
        "doesNotContain",
        "isEmpty",
        "isNotEmpty",
        "isGreater",
        "isGreaterEqual",
        "isLess",
        "isLessEqual",
    ]
    value: list[Annotated[str, StringConstraints(max_length=1000)]] = Field(
        default_factory=list, max_length=20
    )


class Sort(Arguments):
    field_name: FieldName
    desc: bool = False


class SearchRecords(BaseTable):
    view_id: Identifier | None = None
    field_names: list[FieldName] = Field(default_factory=list, max_length=100)
    conjunction: Literal["and", "or"] = "and"
    conditions: list[Condition] = Field(default_factory=list, max_length=20)
    sort: list[Sort] = Field(default_factory=list, max_length=10)


class RecordFields(Arguments):
    app_token: Identifier
    table_id: Identifier
    fields: dict[FieldName, JsonValue] = Field(
        min_length=1, max_length=200, description="Field name to value, shaped by field type"
    )


class UpdateRecord(RecordFields):
    record_id: Identifier


class RecordRef(Arguments):
    app_token: Identifier
    table_id: Identifier
    record_id: Identifier


class WikiNode(Arguments):
    token: Identifier = Field(description="Wiki node token, e.g. from a /wiki/ link")
    obj_type: DocType | None = Field(default=None, description="Set when token is a doc token")


class WikiNodes(Page):
    space_id: Identifier
    parent_node_token: Identifier | None = None


class CreateWikiNode(Arguments):
    space_id: Identifier
    title: Short
    obj_type: Literal["docx", "sheet", "bitable", "mindnote"] = "docx"
    parent_node_token: Identifier | None = None


# docx block_type numbers and the key holding each block's text.
_BLOCKS = {
    "text": (2, "text"),
    "heading1": (3, "heading1"),
    "heading2": (4, "heading2"),
    "heading3": (5, "heading3"),
    "bullet": (12, "bullet"),
    "ordered": (13, "ordered"),
    "code": (14, "code"),
    "quote": (15, "quote"),
    "todo": (17, "todo"),
}


# Same as the official CLI's document fetch.
_FETCH_EXTRA = (
    '{"enable_user_cite_reference_map":true,"include_comments":true,"return_html5_block_data":true}'
)


def _items(data: dict[str, Any], key: str) -> dict[str, Any]:
    # Rename the list so the shared paging trims it like every other page.
    rest = {name: value for name, value in data.items() if name != key}
    return {**rest, "items": data.get(key) or []}


def _block(block: Block) -> dict[str, Any]:
    number, key = _BLOCKS[block.type]
    return {"block_type": number, key: {"elements": [{"text_run": {"content": block.text}}]}}


@tool(
    "feishu_search_docs",
    SearchDocs,
    "docs.search",
    "Search docs, sheets, bases and wiki pages the user can access.",
)
async def search_docs(args: SearchDocs, call: Call) -> dict[str, Any]:
    doc_filter: dict[str, Any] = {"doc_types": args.doc_types} if args.doc_types else {}
    body = {
        "query": args.query,
        "page_size": args.limit,
        "doc_filter": doc_filter,
        "wiki_filter": dict(doc_filter),
    }
    if args.page_token is not None:
        body["page_token"] = args.page_token
    found = await call("docs.search", json=body)
    return _items(found, "res_units")


@tool(
    "feishu_drive_files",
    DriveFiles,
    "drive.files",
    "List files in a My Space folder, most recently edited first.",
)
async def drive_files(args: DriveFiles, call: Call) -> dict[str, Any]:
    params = {
        **page(args),
        "folder_token": args.folder_token,
        "order_by": "EditedTime",
        "direction": "DESC",
    }
    return _items(await call("drive.files", params=compact(params)), "files")


@tool(
    "feishu_read_document",
    ReadDocument,
    "docx.raw",
    "Read a document as Markdown (docx or wiki page) or plain text, or an old-style doc,"
    " slides or mind note.",
)
async def read_document(args: ReadDocument, call: Call) -> dict[str, Any]:
    if args.kind == "doc":
        data = await call("doc.legacy", path={"doc_token": args.document_id})
    elif args.kind == "slides":
        data = await call("slides.get", path={"presentation_id": args.document_id})
        presentation = data.get("xml_presentation")
        if isinstance(presentation, dict) and isinstance(presentation.get("content"), str):
            data = {**presentation, **{k: v for k, v in data.items() if k != "xml_presentation"}}
    elif args.kind == "mindnote":
        return await call(
            "mindnote.nodes",
            path={"mindnote_id": args.document_id},
            params={"page_size": 500, "user_id_type": "open_id"},
        )
    elif args.format == "text":
        data = await call("docx.raw", path={"document_id": args.document_id})
    else:
        fetched = await call(
            "docs.fetch",
            path={"document_id": args.document_id},
            json={
                "format": "markdown",
                "extra_param": _FETCH_EXTRA,
                "export_option": {
                    "export_block_id": args.with_block_ids,
                    "export_style_attrs": False,
                    "export_cite_extra_data": False,
                },
            },
        )
        document = fetched.get("document")
        rest = {key: value for key, value in fetched.items() if key != "document"}
        data = {**rest, **(document if isinstance(document, dict) else {})}
    content = data.get("content")
    if isinstance(content, str):
        text, paging = text_page(content, args)
        data = {**data, "content": text, **paging}
    return data


@tool(
    "feishu_edit_document",
    EditDocument,
    "docs.update",
    "Change existing document content with Markdown: replace text or blocks, delete or insert"
    " blocks, or overwrite. Read it first (with_block_ids for block edits).",
)
async def edit_document(args: EditDocument, call: Call) -> dict[str, Any]:
    body = compact(
        {
            "format": "markdown",
            "command": args.command,
            "revision_id": -1,
            "content": args.content or None,
            "pattern": args.pattern,
            "block_id": args.block_id,
        }
    )
    return await call("docs.update", path={"document_id": args.document_id}, json=body)


@tool(
    "feishu_share_document",
    ShareDocument,
    "drive.share",
    "Give colleagues (open_id) view or edit access to a document, sheet, base or folder.",
)
async def share_document(args: ShareDocument, call: Call) -> dict[str, Any]:
    params = {"type": args.doc_type, "need_notification": "true" if args.notify else "false"}
    shared: list[str] = []
    failed: list[dict[str, Any]] = []
    for open_id in args.member_open_ids:
        try:
            await call(
                "drive.share",
                path={"token": args.token},
                params=params,
                json={
                    "member_type": "openid",
                    "member_id": open_id,
                    "perm": args.permission,
                    "type": "user",
                },
            )
            shared.append(open_id)
        except service.PersonalError as exc:
            if not shared and not failed and exc.code != "feishu_request_failed":
                raise  # Permission or authorization problems apply to everyone: say so once.
            failed.append({"open_id": open_id, **exc.payload()})
    return {"shared": shared, "failed": failed, "permission": args.permission}


@tool(
    "feishu_file_info",
    FileInfo,
    "drive.metas",
    "Look up files' titles, owners, times and links by token.",
)
async def file_info(args: FileInfo, call: Call) -> dict[str, Any]:
    docs = [{"doc_token": item.token, "doc_type": item.type} for item in args.files]
    return await call(
        "drive.metas",
        params={"user_id_type": "open_id"},
        json={"request_docs": docs, "with_url": True},
    )


@tool("feishu_move_file", MoveFile, "drive.move", "Move a file or folder to another folder.")
async def move_file(args: MoveFile, call: Call) -> dict[str, Any]:
    return await call(
        "drive.move",
        path={"file_token": args.token},
        json={"type": args.type, "folder_token": args.folder_token},
    )


@tool("feishu_copy_file", CopyFile, "drive.copy", "Copy a document, sheet, base or file.")
async def copy_file(args: CopyFile, call: Call) -> dict[str, Any]:
    return await call(
        "drive.copy",
        path={"file_token": args.token},
        params={"user_id_type": "open_id"},
        json={"name": args.name, "type": args.type, "folder_token": args.folder_token},
    )


@tool("feishu_rename_file", RenameFile, "drive.rename", "Rename a document, sheet, base or file.")
async def rename_file(args: RenameFile, call: Call) -> dict[str, Any]:
    return await call(
        "drive.rename",
        path={"file_token": args.token},
        params={"type": args.type},
        json={"new_title": args.new_title},
    )


@tool(
    "feishu_sheet_manage",
    ManageSheet,
    "sheets.structure",
    "Add a worksheet to a spreadsheet, or rename one.",
)
async def sheet_manage(args: ManageSheet, call: Call) -> dict[str, Any]:
    if args.action == "add":
        request = {"addSheet": {"properties": compact({"title": args.title, "index": args.index})}}
    else:
        request = {"updateSheet": {"properties": {"sheetId": args.sheet_id, "title": args.title}}}
    return await call(
        "sheets.structure",
        path={"spreadsheet_token": args.spreadsheet_token},
        json={"requests": [request]},
    )


@tool(
    "feishu_base_create_table",
    CreateTable,
    "bitable.create_table",
    "Add a table to a Base, optionally with its fields.",
)
async def base_create_table(args: CreateTable, call: Call) -> dict[str, Any]:
    table: dict[str, Any] = {"name": args.name}
    if args.fields:
        table["fields"] = [field.body() for field in args.fields]
    return await call(
        "bitable.create_table", path={"app_token": args.app_token}, json={"table": table}
    )


@tool(
    "feishu_base_create_field", CreateField, "bitable.create_field", "Add a field to a Base table."
)
async def base_create_field(args: CreateField, call: Call) -> dict[str, Any]:
    return await call(
        "bitable.create_field",
        path={"app_token": args.app_token, "table_id": args.table_id},
        json=args.body(),
    )


@tool(
    "feishu_create_folder",
    CreateFolder,
    "drive.folder",
    "Create a folder in My Space, at the root unless a parent folder is given.",
)
async def create_folder(args: CreateFolder, call: Call) -> dict[str, Any]:
    return await call(
        "drive.folder", json={"name": args.name, "folder_token": args.folder_token or ""}
    )


@tool(
    "feishu_create_document",
    CreateDocument,
    "docx.create",
    "Create an empty docx document, in My Space root unless a folder is given.",
)
async def create_document(args: CreateDocument, call: Call) -> dict[str, Any]:
    return await call(
        "docx.create", json=compact({"title": args.title, "folder_token": args.folder_token})
    )


@tool(
    "feishu_append_document",
    AppendDocument,
    "docx.append",
    "Append paragraphs, headings, list items, code, quotes or to-dos to the end of a docx.",
)
async def append_document(args: AppendDocument, call: Call) -> dict[str, Any]:
    return await call(
        "docx.append",
        path={"document_id": args.document_id, "block_id": args.document_id},
        params={"document_revision_id": -1},
        json={"children": [_block(block) for block in args.blocks]},
    )


@tool(
    "feishu_document_comments",
    Comments,
    "drive.comments",
    "List comments on a document, sheet or other file.",
)
async def document_comments(args: Comments, call: Call) -> dict[str, Any]:
    return await call(
        "drive.comments",
        path={"file_token": args.file_token},
        params={**page(args), "file_type": args.file_type, "user_id_type": "open_id"},
    )


@tool(
    "feishu_add_document_comment",
    AddComment,
    "drive.comment",
    "Add a whole-document comment as the user.",
)
async def add_document_comment(args: AddComment, call: Call) -> dict[str, Any]:
    return await call(
        "drive.comment",
        path={"file_token": args.file_token},
        json={"file_type": args.file_type, "reply_elements": [{"type": "text", "text": args.text}]},
    )


@tool(
    "feishu_sheet_info",
    Spreadsheet,
    "sheets.query",
    "List a spreadsheet's sheets with their IDs and sizes.",
)
async def sheet_info(args: Spreadsheet, call: Call) -> dict[str, Any]:
    return await call("sheets.query", path={"spreadsheet_token": args.spreadsheet_token})


@tool("feishu_sheet_read", ReadSheet, "sheets.read", "Read cell values from spreadsheet ranges.")
async def sheet_read(args: ReadSheet, call: Call) -> dict[str, Any]:
    return await call(
        "sheets.read",
        path={"spreadsheet_token": args.spreadsheet_token},
        params={
            "ranges": ",".join(args.ranges),
            "valueRenderOption": "ToString",
            "dateTimeRenderOption": "FormattedString",
        },
    )


@tool("feishu_sheet_write", WriteSheet, "sheets.write", "Overwrite cells in a spreadsheet range.")
async def sheet_write(args: WriteSheet, call: Call) -> dict[str, Any]:
    return await call(
        "sheets.write",
        path={"spreadsheet_token": args.spreadsheet_token},
        json={"valueRange": {"range": args.range, "values": args.values}},
    )


@tool(
    "feishu_sheet_append",
    WriteSheet,
    "sheets.append",
    "Append rows after the last non-empty row of a range.",
)
async def sheet_append(args: WriteSheet, call: Call) -> dict[str, Any]:
    return await call(
        "sheets.append",
        path={"spreadsheet_token": args.spreadsheet_token},
        params={"insertDataOption": "INSERT_ROWS"},
        json={"valueRange": {"range": args.range, "values": args.values}},
    )


@tool("feishu_create_sheet", CreateSheet, "sheets.create", "Create a spreadsheet.")
async def create_sheet(args: CreateSheet, call: Call) -> dict[str, Any]:
    return await call(
        "sheets.create", json=compact({"title": args.title, "folder_token": args.folder_token})
    )


@tool("feishu_base_tables", BaseApp, "bitable.tables", "List the tables of a Base (bitable).")
async def base_tables(args: BaseApp, call: Call) -> dict[str, Any]:
    return await call("bitable.tables", path={"app_token": args.app_token}, params=page(args))


@tool("feishu_base_fields", BaseTable, "bitable.fields", "List a Base table's fields and types.")
async def base_fields(args: BaseTable, call: Call) -> dict[str, Any]:
    return await call(
        "bitable.fields",
        path={"app_token": args.app_token, "table_id": args.table_id},
        params=page(args),
    )


@tool(
    "feishu_base_search",
    SearchRecords,
    "bitable.search",
    "Query Base records with optional view, field, filter and sort.",
)
async def base_search(args: SearchRecords, call: Call) -> dict[str, Any]:
    body: dict[str, Any] = compact(
        {"view_id": args.view_id, "field_names": args.field_names or None}
    )
    if args.conditions:
        body["filter"] = {
            "conjunction": args.conjunction,
            "conditions": [condition.model_dump() for condition in args.conditions],
        }
    if args.sort:
        body["sort"] = [item.model_dump() for item in args.sort]
    return await call(
        "bitable.search",
        path={"app_token": args.app_token, "table_id": args.table_id},
        params={**page(args), "user_id_type": "open_id"},
        json=body,
    )


@tool("feishu_base_create_record", RecordFields, "bitable.create", "Add a record to a Base table.")
async def base_create(args: RecordFields, call: Call) -> dict[str, Any]:
    return await call(
        "bitable.create",
        path={"app_token": args.app_token, "table_id": args.table_id},
        params={"user_id_type": "open_id"},
        json={"fields": args.fields},
    )


@tool(
    "feishu_base_update_record",
    UpdateRecord,
    "bitable.update",
    "Update the given fields of a Base record.",
)
async def base_update(args: UpdateRecord, call: Call) -> dict[str, Any]:
    return await call(
        "bitable.update",
        path={"app_token": args.app_token, "table_id": args.table_id, "record_id": args.record_id},
        params={"user_id_type": "open_id"},
        json={"fields": args.fields},
    )


@tool("feishu_base_delete_record", RecordRef, "bitable.delete", "Delete a Base record.")
async def base_delete(args: RecordRef, call: Call) -> dict[str, Any]:
    return await call(
        "bitable.delete",
        path={"app_token": args.app_token, "table_id": args.table_id, "record_id": args.record_id},
    )


@tool("feishu_wiki_spaces", Page, "wiki.spaces", "List wiki spaces the user can access.")
async def wiki_spaces(args: Page, call: Call) -> dict[str, Any]:
    return await call("wiki.spaces", params=page(args))


@tool(
    "feishu_wiki_node",
    WikiNode,
    "wiki.node",
    "Resolve a wiki node to its space and the underlying document token and type.",
)
async def wiki_node(args: WikiNode, call: Call) -> dict[str, Any]:
    return await call("wiki.node", params=compact({"token": args.token, "obj_type": args.obj_type}))


@tool("feishu_wiki_nodes", WikiNodes, "wiki.nodes", "List child nodes in a wiki space.")
async def wiki_nodes(args: WikiNodes, call: Call) -> dict[str, Any]:
    return await call(
        "wiki.nodes",
        path={"space_id": args.space_id},
        params=compact({**page(args), "parent_node_token": args.parent_node_token}),
    )


@tool("feishu_wiki_create_node", CreateWikiNode, "wiki.create", "Create a page in a wiki space.")
async def wiki_create(args: CreateWikiNode, call: Call) -> dict[str, Any]:
    return await call(
        "wiki.create",
        path={"space_id": args.space_id},
        json=compact(
            {
                "obj_type": args.obj_type,
                "node_type": "origin",
                "parent_node_token": args.parent_node_token,
                "title": args.title,
            }
        ),
    )


TOOLS = [
    search_docs,
    drive_files,
    create_folder,
    file_info,
    move_file,
    copy_file,
    rename_file,
    read_document,
    create_document,
    append_document,
    edit_document,
    share_document,
    document_comments,
    add_document_comment,
    sheet_info,
    sheet_read,
    sheet_write,
    sheet_append,
    create_sheet,
    sheet_manage,
    base_tables,
    base_fields,
    base_search,
    base_create_table,
    base_create_field,
    base_create,
    base_update,
    base_delete,
    wiki_spaces,
    wiki_node,
    wiki_nodes,
    wiki_create,
]
