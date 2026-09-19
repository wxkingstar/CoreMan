"""邮件：只访问本人邮箱（me）。列表、搜索、读取、整理，起草与发送。"""

from __future__ import annotations

import base64
import binascii
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from coreman.core.feishu_personal import service
from coreman.core.feishu_personal.toolbase import (
    Arguments,
    Call,
    ISOTime,
    Page,
    PathId,
    TextPage,
    compact,
    ordered,
    page,
    text_page,
    tool,
    valid_times,
)

ME = {"mailbox": "me"}
Address = Annotated[
    str, StringConstraints(max_length=254, pattern=r"^[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[^@\s<>,;\"]+$")
]
Addresses = Annotated[list[Address], Field(max_length=50)]
Keyword = Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)]


class MailPage(Page):
    limit: int = Field(default=15, ge=1, le=15)


class ListMail(MailPage):
    folder_id: PathId | None = Field(
        default=None, description="INBOX (default), SENT, SPAM, ARCHIVED or a folder ID"
    )
    label_id: PathId | None = Field(default=None, description="IMPORTANT, FLAGGED or a label ID")
    only_unread: bool = False


class SearchMail(MailPage):
    query: Keyword = ""
    sender: list[Keyword] = Field(default_factory=list, max_length=10)
    recipient: list[Keyword] = Field(default_factory=list, max_length=10)
    subject: Keyword | None = None
    unread_only: bool = False
    has_attachment: bool = False
    start_time: ISOTime | None = None
    end_time: ISOTime | None = None

    _times = valid_times("start_time", "end_time")

    @model_validator(mode="after")
    def ordered_times(self) -> SearchMail:
        ordered(self.start_time, self.end_time)
        return self


class ReadMail(TextPage):
    message_id: PathId


class Compose(Arguments):
    to: Addresses = Field(default_factory=list)
    cc: Addresses = Field(default_factory=list)
    bcc: Addresses = Field(default_factory=list)
    subject: Annotated[str, StringConstraints(max_length=500, pattern=r"^[^\r\n]+$")]
    body: Annotated[str, StringConstraints(min_length=1, max_length=50000)]
    body_format: Literal["text", "html"] = "text"

    @model_validator(mode="after")
    def has_recipient(self) -> Compose:
        if not (self.to or self.cc or self.bcc):
            raise ValueError("at least one recipient")
        return self


class ModifyMail(Arguments):
    message_id: PathId
    mark_read: bool | None = None
    move_to_folder: PathId | None = Field(
        default=None, description="INBOX, ARCHIVED, SPAM or a folder ID"
    )

    @model_validator(mode="after")
    def has_change(self) -> ModifyMail:
        if self.mark_read is None and self.move_to_folder is None:
            raise ValueError("nothing to change")
        return self


class MailRef(Arguments):
    message_id: PathId


def _decode(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return value


def _message_id(item: Any) -> str | None:
    if isinstance(item, dict):
        item = item.get("message_id") or item.get("id")
    return item if isinstance(item, str) and item else None


async def _draft(args: Compose, call: Call) -> str:
    profile = await call("mail.profile", path=ME)
    sender = profile.get("primary_email_address") or (profile.get("user_mailbox") or {}).get(
        "primary_email_address"
    )
    if not isinstance(sender, str) or not sender:
        raise service.PersonalError("mailbox_unavailable")
    message = EmailMessage(policy=SMTP)
    message["From"] = sender
    for header, values in (("To", args.to), ("Cc", args.cc), ("Bcc", args.bcc)):
        if values:
            message[header] = ", ".join(values)
    message["Subject"] = args.subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[1])
    message.set_content(args.body, subtype="html" if args.body_format == "html" else "plain")
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    created = await call("mail.draft", path=ME, json={"raw": raw})
    draft = created.get("draft")
    nested = draft.get("draft_id") if isinstance(draft, dict) else None
    draft_id = created.get("draft_id") or created.get("id") or nested
    if not isinstance(draft_id, str) or not draft_id:
        raise service.PersonalError("mail_draft_failed")
    return draft_id


@tool("feishu_mail_folders", Arguments, "mail.folders", "List the user's mail folders.")
async def folders(_: Arguments, call: Call) -> dict[str, Any]:
    return await call("mail.folders", path=ME)


@tool(
    "feishu_mail_list",
    ListMail,
    "mail.list",
    "List mails in a folder or label, newest first, with sender, subject and time.",
)
async def list_mail(args: ListMail, call: Call) -> dict[str, Any]:
    params = {
        **page(args),
        "folder_id": args.folder_id or (None if args.label_id else "INBOX"),
        "label_id": args.label_id,
        "only_unread": "true" if args.only_unread else None,
    }
    listed = await call("mail.list", path=ME, params=compact(params))
    ids = [found for found in map(_message_id, listed.get("items") or []) if found][: args.limit]
    result = {"has_more": bool(listed.get("has_more")), "page_token": listed.get("page_token")}
    if not ids:
        return {**result, "items": []}
    found = await call("mail.batch_get", path=ME, json={"message_ids": ids, "format": "metadata"})
    return {**result, "items": found.get("messages") or found.get("items") or []}


@tool(
    "feishu_mail_search",
    SearchMail,
    "mail.search",
    "Search the user's mail by keyword, sender, recipient, subject or time.",
)
async def search_mail(args: SearchMail, call: Call) -> dict[str, Any]:
    times = compact({"start_time": args.start_time, "end_time": args.end_time})
    filters = compact(
        {
            "from": args.sender or None,
            "to": args.recipient or None,
            "subject": args.subject,
            "is_unread": True if args.unread_only else None,
            "has_attachment": True if args.has_attachment else None,
            "create_time": times or None,
        }
    )
    body = compact({"query": args.query or None, "filter": filters or None})
    return await call("mail.search", path=ME, params=page(args), json=body)


@tool("feishu_mail_read", ReadMail, "mail.get", "Read one mail's headers and plain-text body.")
async def read_mail(args: ReadMail, call: Call) -> dict[str, Any]:
    data = await call(
        "mail.get",
        path={**ME, "message_id": args.message_id},
        params={"format": "plain_text_full"},
    )
    message = dict(data.get("message") or {})
    message.pop("body_html", None)
    body = _decode(message.get("body_plain_text"))
    paging: dict[str, Any] = {}
    if isinstance(body, str):
        message["body_plain_text"], paging = text_page(body, args)
    return {"message": message, **paging}


@tool(
    "feishu_mail_create_draft",
    Compose,
    "mail.draft",
    "Save a mail as a draft in the user's mailbox without sending it.",
)
async def create_draft(args: Compose, call: Call) -> dict[str, Any]:
    return {"draft_id": await _draft(args, call), "sent": False}


@tool(
    "feishu_mail_send",
    Compose,
    "mail.send_draft",
    "Send a mail from the user's mailbox. Never retry after an unclear result: check Sent.",
)
async def send_mail(args: Compose, call: Call) -> dict[str, Any]:
    draft_id = await _draft(args, call)
    sent = await call("mail.send_draft", path={**ME, "draft_id": draft_id}, json={})
    return {**sent, "draft_id": draft_id, "sent": True}


@tool(
    "feishu_mail_modify",
    ModifyMail,
    "mail.modify",
    "Mark a mail read or unread, or move it to another folder.",
)
async def modify_mail(args: ModifyMail, call: Call) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if args.mark_read is not None:
        body["remove_label_ids" if args.mark_read else "add_label_ids"] = ["UNREAD"]
    if args.move_to_folder is not None:
        body["add_folder"] = args.move_to_folder
    return await call("mail.modify", path={**ME, "message_id": args.message_id}, json=body)


@tool("feishu_mail_trash", MailRef, "mail.trash", "Move a mail to the trash.")
async def trash_mail(args: MailRef, call: Call) -> dict[str, Any]:
    return await call("mail.trash", path={**ME, "message_id": args.message_id})


TOOLS = [
    folders,
    list_mail,
    search_mail,
    read_mail,
    create_draft,
    send_mail,
    modify_mail,
    trash_mail,
]
