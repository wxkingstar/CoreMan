"""回复里的文件：模型用 `[文件名](路径)` 链接工作目录里的文件，平台把它作为飞书文件消息发出。

文件只在 Runtime 节点上。收尾时 worker 经节点的工作区通道（`workspace-read`，与管理台查看文件
同一条通道、同一套路径限制：不出工作目录、不跟链接、凭据名文件不给）分块读回来，用机器人自己的
飞书凭证上传换 file_key，再排一条出站文件消息，引用提问那条消息。正文里的链接换成一行说明，
卡片上不再留一个用户点不开的本地路径。

只处理飞书的普通 AI 员工：个人助理的工作目录不经守护进程读（会照办助理放进去的链接）。
不需要升级节点：用的是节点早已支持的读文件操作。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import posixpath
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.records import is_assistant
from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.bus import outbox
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, RelayServer
from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.core.relay.agent_client import AgentError, call_agent

log = get_logger(__name__)

# 飞书上传文件接口上限 30 MB。
MAX_BYTES = 30 * 1024 * 1024
MAX_FILES = 5
# 老节点读文件每次最多 256KB；报了 workspace_chunk 的按它读（最多 2MB）。
DEFAULT_CHUNK = 256 * 1024
MAX_CHUNK = 2 * 1024 * 1024
# 一个文件从读到上传完最多等这么久，超了这一个就算失败，回复照常发出。
FILE_SECONDS = 90.0
# 不带感叹号的 Markdown 链接；目标不是网址（file:// 除外），最后一段带扩展名才算文件。
_LINK = re.compile(r"(?<!!)\[([^\]\n]{1,200})\]\(\s*<?([^\s<>()]{1,1024})>?\s*\)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_UNSAFE_NAME = re.compile(r"[\[\]\n\r]")
# 飞书文件消息的 file_type：这些扩展名有专门类型，其余一律 stream。
_FILE_TYPES = {
    "pdf": "pdf",
    "doc": "doc",
    "docx": "doc",
    "xls": "xls",
    "xlsx": "xls",
    "csv": "xls",
    "ppt": "ppt",
    "pptx": "ppt",
    "mp4": "mp4",
    "opus": "opus",
}


@dataclass(frozen=True)
class FileLink:
    start: int
    end: int
    label: str
    path: str


def file_links(text: str) -> list[FileLink]:
    """正文里指向本地文件的链接（去重后最多 MAX_FILES 个；同一个文件只算第一次）。"""
    out: list[FileLink] = []
    seen: set[str] = set()
    for match in _LINK.finditer(text):
        target = unquote(match.group(2))
        if target.startswith("file://"):
            target = target.removeprefix("file://")
        elif _SCHEME.match(target) or target.startswith("#"):
            continue
        if not _EXT.search(target) or target in seen:
            continue
        seen.add(target)
        out.append(FileLink(match.start(), match.end(), match.group(1).strip(), target))
        if len(out) >= MAX_FILES:
            break
    return out


def relative_path(target: str, working_dir: str) -> str | None:
    """链接里的路径 → 相对工作目录的路径；认不出（不在工作目录里）返回 None。

    相对路径按工作目录算。绝对路径要能在里面找到工作目录：员工的工作目录配置成绝对路径时按前缀
    比；配置成相对名字时（节点按自己的根目录解析，平台不知道绝对位置）找路径里的这一段。
    """
    path = posixpath.normpath(target)
    if not path.startswith("/"):
        rel = path
    else:
        root = posixpath.normpath(working_dir).rstrip("/")
        if root.startswith("/"):
            if not path.startswith(root + "/"):
                return None
            rel = path[len(root) + 1 :]
        else:
            marker = "/" + root.strip("/") + "/"
            index = path.rfind(marker)
            if not root.strip("/") or index < 0:
                return None
            rel = path[index + len(marker) :]
    parts = PurePosixPath(rel).parts
    if not parts or rel.startswith("/") or ".." in parts or parts == (".",):
        return None
    return PurePosixPath(*parts).as_posix()


class ReplyFiles:
    """一轮回复一份：读文件、上传、排出站，返回换掉链接后的正文。"""

    def __init__(self, bot: Bot, relay: RelayServer, cipher: Cipher) -> None:
        self.bot, self.relay, self.cipher = bot, relay, cipher
        self._client: FeishuClient | None = None
        self._chunk: int | None = None

    @staticmethod
    def applies(bot: Bot) -> bool:
        return bot.platform == "feishu" and not is_assistant(bot)

    async def attach(
        self,
        session: AsyncSession,
        text: str,
        *,
        task_id: int,
        chat_id: str,
        reply_to: str | None,
    ) -> str:
        """把正文里的本地文件发出去；调用方在自己的事务里提交排好的出站。"""
        links = file_links(text)
        if not links:
            return text
        notes: list[str] = []
        try:
            for index, link in enumerate(links):
                name = _UNSAFE_NAME.sub(" ", PurePosixPath(link.path).name).strip() or "文件"
                rel = relative_path(link.path, self.bot.working_dir or "")
                if rel is None:
                    notes.append(f"📎 {name}（不在工作目录里，发不出来）")
                    continue
                try:
                    key, size = await asyncio.wait_for(self._upload(rel, name), FILE_SECONDS)
                except _TooLarge:
                    notes.append(f"📎 {name}（超过 30MB，飞书发不了，可以让我压缩或拆分）")
                    continue
                except (AgentError, FeishuError, TimeoutError, ValueError) as exc:
                    log.warning("reply_file_failed", error=type(exc).__name__)
                    notes.append(f"📎 {name}（文件发送失败）")
                    continue
                await outbox.add(
                    session,
                    bot_id=self.bot.id,
                    platform="feishu",
                    kind="send",
                    dedupe_key=f"{task_id}:file:{index}",
                    target={"chat_id": chat_id, "message_id": reply_to},
                    payload={"_file": {"file_key": key, "name": name, "size": size}},
                )
                notes.append(f"📎 **{name}**（已作为文件发送，见下方）")
        finally:
            if self._client is not None:
                await self._client.aclose()
        out, last = [], 0
        for link, note in zip(links, notes, strict=True):
            out.append(text[last : link.start])
            out.append(note)
            last = link.end
        out.append(text[last:])
        return "".join(out)

    async def _chunk_size(self) -> int:
        if self._chunk is None:
            info = await call_agent(self.relay, self.cipher, "ping")
            if info.get("workspace_protocol") != 1:
                raise AgentError("节点不支持读工作目录")
            raw = info.get("workspace_chunk")
            self._chunk = min(raw, MAX_CHUNK) if isinstance(raw, int) and raw > 0 else DEFAULT_CHUNK
        return self._chunk

    async def _read(self, rel: str) -> bytes:
        chunk = await self._chunk_size()
        data = bytearray()
        while True:
            result = await call_agent(
                self.relay,
                self.cipher,
                "workspace-read",
                {
                    "path": rel,
                    "offset": len(data),
                    "length": chunk,
                    "working_dir": self.bot.working_dir,
                    "bot_id": str(self.bot.id),
                },
            )
            size = result.get("size")
            if isinstance(size, int) and size > MAX_BYTES:
                raise _TooLarge
            try:
                data += base64.b64decode(str(result.get("data") or ""), validate=True)
            except binascii.Error as exc:
                raise ValueError("读回的内容无效") from exc
            if len(data) > MAX_BYTES:
                raise _TooLarge
            if result.get("eof") or not result.get("data"):
                break
        if not data:
            raise ValueError("空文件")
        return bytes(data)

    async def _upload(self, rel: str, name: str) -> tuple[str, int]:
        data = await self._read(rel)
        if self._client is None:
            creds = decrypt_json(self.cipher, self.bot.credentials_enc, CREDENTIALS_AAD)
            self._client = FeishuClient(creds.get("app_id", ""), creds.get("app_secret", ""))
        ext = PurePosixPath(name).suffix.lstrip(".").lower()
        result = await self._client.call(
            "POST",
            "/open-apis/im/v1/files",
            data={"file_type": _FILE_TYPES.get(ext, "stream"), "file_name": name},
            files={"file": (name, data, "application/octet-stream")},
        )
        key = (result.get("data") or {}).get("file_key")
        if not isinstance(key, str) or not re.fullmatch(r"file_[A-Za-z0-9_-]{1,240}", key):
            raise FeishuError(-2, "invalid file key")
        return key, len(data)


class _TooLarge(Exception):
    pass
