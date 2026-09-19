"""文件：下载邮件附件、消息里的文件和图片、云空间文件；上传后作为消息发送或存进云空间。

下载只返回链接，由智能体用 curl 取到临时目录；上传先要一个上传链接，curl 上传后拿到
upload_id，再交给发送或保存的工具（发邮件的附件也用它）。
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints

from coreman.core.feishu_personal import files, service
from coreman.core.feishu_personal.service import Download
from coreman.core.feishu_personal.toolbase import (
    Arguments,
    Call,
    Identifier,
    PathId,
    Uuid,
    compact,
    tool,
)

FileName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^/\\\x00]+$"
    ),
]
UploadId = Annotated[str, StringConstraints(min_length=16, max_length=2000)]
IMAGE_LIMIT = 10 * 1024 * 1024
IM_FILE_LIMIT = 30 * 1024 * 1024
DRIVE_LIMIT = 20 * 1024 * 1024
# Feishu's IM file types; anything else is sent as a generic stream.
_IM_TYPES = {
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
    ".mp4": "mp4",
    ".opus": "opus",
}
HOW_DOWNLOAD = 'curl -fsSL -o "<temp dir>/<filename>" "<download_url>"; delete it when done'


class MailAttachment(Arguments):
    message_id: PathId
    attachment_id: PathId = Field(description="From the mail's attachments")
    filename: FileName | None = Field(default=None, description="Name to save it under")


class MessageFile(Arguments):
    message_id: Identifier
    file_key: Identifier = Field(description="file_key or image_key from the message content")
    type: Literal["file", "image"] = "file"


class DriveFile(Arguments):
    file_token: Identifier = Field(description="An uploaded file's token (not a doc or sheet)")


class PrepareUpload(Arguments):
    filename: FileName


class SendFile(Arguments):
    receive_id: Identifier
    receive_id_type: Literal["open_id", "user_id", "chat_id"]
    upload_id: UploadId
    as_image: bool = Field(default=False, description="Send an image inline instead of as a file")
    reply_to_message_id: Identifier | None = None
    uuid: Uuid


class SaveToDrive(Arguments):
    upload_id: UploadId
    folder_token: Identifier | None = Field(default=None, description="Omit for My Space root")
    name: FileName | None = None


def _fallback_name(mime: str) -> str:
    return "file" + (mimetypes.guess_extension(mime.split(";")[0].strip()) or "")


async def _link(call: Call, download: Download, name: str | None) -> dict[str, Any]:
    relay = call.relay()
    try:
        grant = await service.existing_row(call.session, call.cipher, call.scope)
        if grant is None:
            raise service.PersonalError("authorization_required")
        row = await relay.save(
            call.session,
            download.chunks(files.DOWNLOAD_LIMIT),
            filename=name or download.filename or _fallback_name(download.content_type),
            mime=download.content_type,
        )
    finally:
        await download.aclose()
    return {
        "download_url": relay.download_link(grant, row),
        "filename": row.filename,
        "size": row.size,
        "expires_in_minutes": int(files.LINK_TTL.total_seconds() // 60),
        "how": HOW_DOWNLOAD,
    }


async def uploaded(call: Call, upload_id: str, limit: int) -> tuple[str, str, bytes]:
    """Name, type and bytes of a file the agent uploaded, if it is at most `limit` bytes."""
    relay = call.relay()
    row = await relay.uploaded(call.session, call.scope, upload_id)
    if row.size > limit:
        raise service.PersonalError("file_too_large")
    return row.filename, row.content_type, await relay.read(row)


@tool(
    "feishu_download_mail_attachment",
    MailAttachment,
    "mail.attachment_url",
    "Fetch a mail attachment and get a short-lived link to download it.",
)
async def download_mail_attachment(args: MailAttachment, call: Call) -> dict[str, Any]:
    data = await call(
        "mail.attachment_url",
        path={"mailbox": "me", "message_id": args.message_id},
        params={"attachment_ids": [args.attachment_id]},
    )
    urls = {
        item.get("attachment_id"): item.get("download_url")
        for item in data.get("download_urls") or []
        if isinstance(item, dict)
    }
    url = urls.get(args.attachment_id)
    if not isinstance(url, str):
        raise service.PersonalError("attachment_not_found")
    return await _link(call, await service.signed_download(url), args.filename)


@tool(
    "feishu_download_message_file",
    MessageFile,
    "im.resource",
    "Fetch a file or image from a chat message and get a short-lived link to download it.",
)
async def download_message_file(args: MessageFile, call: Call) -> dict[str, Any]:
    download = await call.download(
        "im.resource",
        path={"message_id": args.message_id, "file_key": args.file_key},
        params={"type": args.type},
    )
    return await _link(call, download, None)


@tool(
    "feishu_download_drive_file",
    DriveFile,
    "drive.download",
    "Fetch a file uploaded to My Space (PDF, Word, zip…) and get a short-lived download link.",
)
async def download_drive_file(args: DriveFile, call: Call) -> dict[str, Any]:
    download = await call.download("drive.download", path={"file_token": args.file_token})
    return await _link(call, download, None)


@tool(
    "feishu_prepare_upload",
    PrepareUpload,
    "drive.upload",
    "Get a link to upload a local file (up to 30 MB) before attaching or sending it."
    ' Upload with: curl -fsS -T "<local file>" "<upload_url>"; the reply has upload_id.',
)
async def prepare_upload(args: PrepareUpload, call: Call) -> dict[str, Any]:
    relay = call.relay()
    grant = await service.existing_row(call.session, call.cipher, call.scope)
    if grant is None or grant.status != "connected":
        raise service.PersonalError("authorization_required")
    return {
        "upload_url": relay.upload_link(grant, args.filename, files.content_type(args.filename)),
        "method": "PUT",
        "max_bytes": files.UPLOAD_LIMIT,
        "expires_in_minutes": int(files.LINK_TTL.total_seconds() // 60),
        "then": "Pass upload_id to feishu_send_file, feishu_save_to_drive or mail attachment_ids.",
    }


@tool(
    "feishu_send_file",
    SendFile,
    "im.upload_file",
    "Send an uploaded file or image in a chat as the user, optionally as a reply."
    " Reuse uuid for retries.",
)
async def send_file(args: SendFile, call: Call) -> dict[str, Any]:
    limit = IMAGE_LIMIT if args.as_image else IM_FILE_LIMIT
    name, mime, content = await uploaded(call, args.upload_id, limit)
    if args.as_image:
        result = await call.upload(
            "im.upload_image",
            data={"image_type": "message"},
            files={"image": (name, content, mime)},
        )
        key, kind, field = result.get("image_key"), "image", "image_key"
    else:
        file_type = _IM_TYPES.get(PurePosixPath(name).suffix.lower(), "stream")
        result = await call.upload(
            "im.upload_file",
            data={"file_type": file_type, "file_name": name},
            files={"file": (name, content, mime)},
        )
        key, kind, field = result.get("file_key"), "file", "file_key"
    if not isinstance(key, str) or not key:
        raise service.PersonalError("feishu_request_failed")
    message = {"msg_type": kind, "content": json.dumps({field: key}), "uuid": args.uuid}
    if args.reply_to_message_id:
        return await call("im.reply", path={"message_id": args.reply_to_message_id}, json=message)
    return await call(
        "im.send",
        params={"receive_id_type": args.receive_id_type},
        json={"receive_id": args.receive_id, **message},
    )


@tool(
    "feishu_save_to_drive",
    SaveToDrive,
    "drive.upload",
    "Save an uploaded file (up to 20 MB) into a My Space folder.",
)
async def save_to_drive(args: SaveToDrive, call: Call) -> dict[str, Any]:
    name, mime, content = await uploaded(call, args.upload_id, DRIVE_LIMIT)
    name = args.name or name
    folder = args.folder_token
    if not folder:
        root = await call("drive.root")
        folder = root.get("token") if isinstance(root.get("token"), str) else None
        if not folder:
            raise service.PersonalError("drive_root_unavailable")
    saved = await call.upload(
        "drive.upload",
        data=compact(
            {
                "file_name": name,
                "parent_type": "explorer",
                "parent_node": folder,
                "size": str(len(content)),
            }
        ),
        files={"file": (name, content, mime)},
    )
    return {**saved, "folder_token": folder, "file_name": name}


TOOLS = [
    download_mail_attachment,
    download_message_file,
    download_drive_file,
    prepare_upload,
    send_file,
    save_to_drive,
]
