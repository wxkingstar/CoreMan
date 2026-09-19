"""邮件：只访问本人邮箱（me）。列表、搜索、读取、整理，起草、发送、回复与转发。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import re
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, StringConstraints, model_validator

from coreman.core.feishu_personal import sends, service
from coreman.core.feishu_personal.toolbase import (
    Arguments,
    Call,
    ISOTime,
    Page,
    PathId,
    TextPage,
    Uuid,
    compact,
    ordered,
    page,
    text_page,
    tool,
    valid_times,
)
from coreman.core.feishu_personal.toolsets.files import UploadId, uploaded

ME = {"mailbox": "me"}
# Feishu refuses a mail over 25 MB; base64 makes attachments about a third larger.
MAX_EML = 25 * 1024 * 1024
MAIL_ATTACHMENT_LIMIT = 18 * 1024 * 1024
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
    subject: Annotated[str, StringConstraints(max_length=500, pattern=r"^[^\r\n]+$")] | None = (
        Field(default=None, description="Required unless replying or forwarding")
    )
    body: Annotated[str, StringConstraints(min_length=1, max_length=20000)]
    body_format: Literal["text", "html"] = "text"
    reply_to_message_id: PathId | None = Field(
        default=None,
        description="Reply to this mail: recipients, subject, quote and threading come from it",
    )
    reply_all: bool = Field(default=False, description="With reply: also everyone on To/Cc")
    forward_message_id: PathId | None = Field(
        default=None, description="Forward this mail below the body, with its attachments"
    )
    attachment_ids: list[UploadId] = Field(
        default_factory=list,
        max_length=10,
        description="upload_id values from feishu_prepare_upload; 18 MB in total",
    )

    @model_validator(mode="after")
    def consistent(self) -> Compose:
        if self.reply_to_message_id and self.forward_message_id:
            raise ValueError("reply or forward, not both")
        if self.reply_all and not self.reply_to_message_id:
            raise ValueError("reply_all needs reply_to_message_id")
        if not self.reply_to_message_id and not (self.to or self.cc or self.bcc):
            raise ValueError("at least one recipient")
        if not (self.reply_to_message_id or self.forward_message_id or self.subject):
            raise ValueError("subject is required")
        return self


class SendMail(Compose):
    uuid: Uuid


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


_ADDRESS = re.compile(r"[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[^@\s<>,;\"]+\Z")
_PREFIX = re.compile(r"^\s*(re|fwd?|回复|转发)\s*[:：]\s*", re.IGNORECASE)
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _clean(value: Any) -> str:
    # Values from someone else's mail: never let them add header lines.
    return re.sub(r"[\r\n]+", " ", value).strip() if isinstance(value, str) else ""


def _address(value: Any) -> str | None:
    found = _clean(value.get("mail_address") if isinstance(value, dict) else value)
    return found if _ADDRESS.match(found) else None


def _addresses(values: Any) -> list[str]:
    return [a for a in map(_address, values if isinstance(values, list) else []) if a]


def _unique(values: list[str], skip: set[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value.lower() not in skip and value.lower() not in {v.lower() for v in out}:
            out.append(value)
    return out


def _person(value: Any) -> str:
    address = _address(value) or ""
    name = _clean(value.get("name")) if isinstance(value, dict) else ""
    return f"{name} <{address}>" if name and address else address or name


class _Original:
    def __init__(self, message: dict[str, Any]) -> None:
        self.message_id = _clean(message.get("message_id"))
        self.subject = _clean(message.get("subject"))
        self.sender = message.get("head_from")
        self.reply_to = _address(message.get("reply_to"))
        self.to = _addresses(message.get("to"))
        self.cc = _addresses(message.get("cc"))
        self.smtp_id = _clean(message.get("smtp_message_id")).strip("<>")
        references = message.get("references")
        self.references = (
            [_clean(ref).strip("<>") for ref in references if isinstance(ref, str) and _clean(ref)]
            if isinstance(references, list)
            else []
        )
        body = _decode(message.get("body_plain_text"))
        self.body = body if isinstance(body, str) else ""
        # Regular attachments travel with a forward; inline images belong to the HTML body and
        # large attachments are links Feishu keeps elsewhere.
        self.files: list[tuple[str, str]] = []
        self.left_out: list[str] = []
        for item in message.get("attachments") or []:
            if not isinstance(item, dict) or item.get("is_inline"):
                continue
            name = _clean(item.get("filename")) or "attachment"
            if isinstance(item.get("id"), str) and item["id"] and item.get("attachment_type") != 2:
                self.files.append((item["id"], name))
            else:
                self.left_out.append(name)
        self.chinese = bool(_CJK.search(self.subject))
        stamp = message.get("internal_date")
        self.date = ""
        if isinstance(stamp, (str, int)) and str(stamp).isdigit():
            moment = datetime.fromtimestamp(int(stamp) / 1000, UTC)
            self.date = moment.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")

    def subject_for(self, forward: bool) -> str:
        base = self.subject
        while _PREFIX.match(base):
            base = _PREFIX.sub("", base, count=1)
        if self.chinese:
            return ("转发：" if forward else "回复：") + base
        return ("Fwd: " if forward else "Re: ") + base

    def quoted(self, forward: bool) -> str:
        if forward:
            labels = (
                ("---------- 转发的邮件 ----------", "发件人", "日期", "主题", "收件人")
                if self.chinese
                else ("---------- Forwarded message ----------", "From", "Date", "Subject", "To")
            )
            head = [
                labels[0],
                f"{labels[1]}: {_person(self.sender)}",
                f"{labels[2]}: {self.date}",
                f"{labels[3]}: {self.subject}",
                f"{labels[4]}: {', '.join(self.to)}",
            ]
            return "\n\n" + "\n".join(head) + "\n\n" + self.body
        intro = (
            f"在 {self.date}，{_person(self.sender)} 写道："
            if self.chinese
            else f"On {self.date}, {_person(self.sender)} wrote:"
        )
        return "\n\n" + intro + "\n" + "\n".join("> " + line for line in self.body.splitlines())


async def _original(call: Call, message_id: str) -> _Original:
    data = await call(
        "mail.get", path={**ME, "message_id": message_id}, params={"format": "plain_text_full"}
    )
    message = data.get("message")
    if not isinstance(message, dict):
        raise service.PersonalError("mail_not_found")
    return _Original(message)


async def _forwarded(
    call: Call, original: _Original, budget: int
) -> tuple[list[tuple[str, str, bytes]], list[str]]:
    """The original's attachments that fit in `budget` bytes, and the names left out."""
    attached: list[tuple[str, str, bytes]] = []
    left_out = list(original.left_out)
    if not original.files:
        return attached, left_out
    data = await call(
        "mail.attachment_url",
        path={**ME, "message_id": original.message_id},
        params={"attachment_ids": [found for found, _ in original.files[:20]]},
    )
    urls = {
        item.get("attachment_id"): item.get("download_url")
        for item in data.get("download_urls") or []
        if isinstance(item, dict)
    }
    for index, (attachment_id, name) in enumerate(original.files):
        url = urls.get(attachment_id) if index < 20 else None
        if not isinstance(url, str) or budget <= 0:
            left_out.append(name)
            continue
        try:
            download = await service.signed_download(url)
            content = bytearray()
            async for chunk in download.chunks(budget):
                content.extend(chunk)
        except service.PersonalError:
            left_out.append(name)
            continue
        budget -= len(content)
        attached.append((name, download.content_type, bytes(content)))
    return attached, left_out


def _attach(message: EmailMessage, name: str, mime: str, content: bytes) -> None:
    main, _, sub = mime.split(";")[0].strip().partition("/")
    if not main or not sub or main == "multipart":
        main, sub = "application", "octet-stream"
    message.add_attachment(content, maintype=main, subtype=sub, filename=name)


async def _draft(args: Compose, call: Call) -> tuple[str, dict[str, Any]]:
    profile = await call("mail.profile", path=ME)
    sender = profile.get("primary_email_address") or (profile.get("user_mailbox") or {}).get(
        "primary_email_address"
    )
    if not isinstance(sender, str) or not sender:
        raise service.PersonalError("mailbox_unavailable")
    to, cc, subject, body = list(args.to), list(args.cc), args.subject, args.body
    headers: dict[str, str] = {}
    notes: dict[str, Any] = {}
    attachments = [
        await uploaded(call, upload_id, MAIL_ATTACHMENT_LIMIT) for upload_id in args.attachment_ids
    ]
    budget = MAIL_ATTACHMENT_LIMIT - sum(len(content) for _, _, content in attachments)
    if budget < 0:
        raise service.PersonalError("file_too_large")
    source = args.reply_to_message_id or args.forward_message_id
    if source:
        original = await _original(call, source)
        forward = args.forward_message_id is not None
        subject = subject or original.subject_for(forward)
        quote = original.quoted(forward)
        if args.body_format == "html":
            quote = "<br><br><blockquote>" + html.escape(quote.strip()).replace("\n", "<br>")
            quote += "</blockquote>"
        body += quote
        if forward:
            carried, left_out = await _forwarded(call, original, budget)
            attachments += carried
            if left_out:
                notes["attachments_not_forwarded"] = left_out
        if not forward:
            me = {sender.lower()}
            first = original.reply_to or _address(original.sender)
            to = _unique(([first] if first else []) + to, me)
            if args.reply_all:
                cc = _unique(original.to + original.cc + cc, me | {a.lower() for a in to})
            if original.smtp_id:
                headers["In-Reply-To"] = f"<{original.smtp_id}>"
                chain = [*original.references, original.smtp_id]
                headers["References"] = " ".join(f"<{ref}>" for ref in chain)
            if original.message_id:
                headers["X-LMS-Reply-To-Message-Id"] = original.message_id
        if not (to or cc or args.bcc):
            raise service.PersonalError("mail_recipient_unknown")
    message = EmailMessage(policy=SMTP)
    message["From"] = sender
    for header, values in (("To", to), ("Cc", cc), ("Bcc", args.bcc)):
        if values:
            message[header] = ", ".join(values)
    message["Subject"] = subject or ""
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[1])
    for name, value in headers.items():
        message[name] = value
    message.set_content(body, subtype="html" if args.body_format == "html" else "plain")
    for name, mime, content in attachments:
        _attach(message, name, mime, content)
    eml = message.as_bytes()
    if len(eml) > MAX_EML:
        raise service.PersonalError("file_too_large")
    raw = base64.urlsafe_b64encode(eml).decode("ascii")
    created = await call("mail.draft", path=ME, json={"raw": raw})
    draft = created.get("draft")
    nested = draft.get("draft_id") if isinstance(draft, dict) else None
    draft_id = created.get("draft_id") or created.get("id") or nested
    if not isinstance(draft_id, str) or not draft_id:
        raise service.PersonalError("mail_draft_failed")
    if attachments:
        notes["attachments"] = [name for name, _, _ in attachments]
    return draft_id, {"to": to, "cc": cc, "subject": subject, **notes}


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
    "Save a new mail, a reply or a forward as a draft in the user's mailbox without sending.",
)
async def create_draft(args: Compose, call: Call) -> dict[str, Any]:
    draft_id, summary = await _draft(args, call)
    return {"draft_id": draft_id, "sent": False, **summary}


@tool(
    "feishu_mail_send",
    SendMail,
    "mail.send_draft",
    "Send a new mail, reply (reply_to_message_id) or forward (forward_message_id) from the"
    " user's mailbox. Retry with the same uuid: it never sends twice."
    " On mail_send_unconfirmed the mail may already be out: ask the user to check Sent.",
)
async def send_mail(args: SendMail, call: Call) -> dict[str, Any]:
    content = args.model_dump(exclude={"uuid"})
    fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
    record = await sends.claim(call.session, call.scope, args.uuid, fingerprint)
    if record.status == "sent":
        return {**(record.result or {}), "draft_id": record.draft_id, "sent": True, "repeat": True}
    retry = record.draft_id is not None
    summary: dict[str, Any] = {}
    if record.draft_id:
        draft_id = record.draft_id
    else:
        draft_id, summary = await _draft(args, call)
    if not retry:
        # Kept even if sending fails below, so a retry sends this draft instead of a new one.
        record.draft_id = draft_id
        await call.session.flush()
    try:
        sent = await call("mail.send_draft", path={**ME, "draft_id": draft_id}, json={})
    except service.PersonalError as exc:
        # An earlier attempt may have sent this draft and only lost the reply; Feishu then
        # refuses the draft. Say so, or the model composes the mail again under a new uuid.
        if retry and exc.code in ("feishu_request_failed", "upstream_unavailable"):
            raise service.PersonalError(
                "mail_send_unconfirmed", upstream_code=exc.upstream_code
            ) from exc
        raise
    record.status, record.result = "sent", {**sent, **summary}
    return {**sent, **summary, "draft_id": draft_id, "sent": True}


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
