"""审批闸门用例的公共件：一位已授权的所有者与他的助理、一轮私聊，以及飞书、Tower、

文件中转区与临时桶的替身。
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import approvals, keys, sealed, write_fuse
from coreman.core.assistants.approvals import attachments, service, tower
from coreman.core.db.models import (
    AssistantApproval,
    Bot,
    FeishuPersonalGrant,
    InboundEvent,
    Task,
    User,
    UserIdentity,
    UserReached,
)
from coreman.core.feishu_personal import endpoints
from coreman.core.feishu_personal import service as personal
from coreman.core.feishu_personal.policy import Scope
from tests.integration.assistant_helpers import SECRET, seed_assistant
from tests.integration.test_chat_handler import chat_task

CFG = tower.Config(
    "https://tower.example.com", "sender-key", "target-key", "https://coreman.example.com"
)
OWNER = "ou_zhang"
PEER = "ou_li"
NAMES = {OWNER: "张三", PEER: "李四"}
TEXT = f"可以，周四下班前发你。{SECRET}"


def message(
    mid: str, sender: str = PEER, text: str = "周五能给报价吗？", **kw: Any
) -> dict[str, Any]:
    import json

    return {
        "message_id": mid,
        "chat_id": kw.pop("chat_id", "oc_li"),
        "msg_type": "text",
        "sender": {"id": sender, "id_type": "open_id", "sender_type": kw.pop("kind", "user")},
        "body": {"content": json.dumps({"text": text}, ensure_ascii=False)},
        **kw,
    }


def reply(**approval: Any) -> dict[str, Any]:
    """助理调用 feishu_reply_message 的入参：回复李四发来的 om_2。"""
    return {
        "message_id": "om_2",
        "text": TEXT,
        "format": "text",
        "reply_in_thread": False,
        "uuid": "u1",
        "approval": {"chat_id": "oc_li", "topic": "报价", **approval},
    }


class FakeFeishu:
    """替换 `service.api_request`：读接口返回准备好的会话，写接口记下来。保险丝照常生效。"""

    def __init__(self, history: list[dict[str, Any]] | None = None, *, mode: str = "p2p") -> None:
        self.history = history if history is not None else [message("om_2")]
        self.mode = mode
        self.writes: list[tuple[str, str, Any]] = []
        # 带文件的上传：(路径, 表单字段, {字段名: (文件名, 内容, 类型)})。
        self.uploads: list[tuple[str, Any, Any]] = []
        self.fail: str | None = None
        # 飞书应答里的表情编号；设为 None 模拟应答缺这一项。
        self.reaction_id: str | None = "r_1"

    async def __call__(
        self,
        session: Any,
        cipher: Any,
        scope: Scope,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: Any = None,
        data: Any = None,
        files: Any = None,
    ) -> dict[str, Any]:
        endpoint = endpoints.match(method, path)
        assert endpoint is not None, (method, path)
        if write_fuse.blocked(scope.bot, endpoint.kind):
            raise personal.PersonalError("approval_required")
        if endpoint.kind != "read":
            if self.fail:
                raise personal.PersonalError(self.fail, upstream_code=230001)
            if files is not None:
                self.uploads.append((path, data, files))
                return {"file_key": "file_1", "image_key": "img_1", "file_token": "box_1"}
            self.writes.append((method, path, json))
            out = {"message_id": f"om_sent_{len(self.writes)}"}
            return {**out, "reaction_id": self.reaction_id} if self.reaction_id else out
        if path == "/im/v1/messages/mget":
            found = [m for m in self.history if m["message_id"] in params["message_ids"]]
            if not found:
                raise personal.PersonalError("feishu_read_failed")
            return {"items": found}
        if path == "/im/v1/messages":
            return {"items": self.history}
        if path.startswith("/im/v1/chats/"):
            return {"chat_mode": self.mode, "name": "" if self.mode == "p2p" else "项目群"}
        if path.startswith("/contact/v3/"):
            return {"user": {"name": NAMES.get(path.rsplit("/", 1)[1], "")}}
        raise AssertionError((method, path))


class FakeTower:
    """替换 `tower` 的五个调用；`down` 为真时一律不可用，`refuse` 是取令牌或核销时的拒绝代码。"""

    def __init__(self) -> None:
        self.down = False
        self.refuse: str | None = None
        self.submitted: list[dict[str, Any]] = []
        self.redeemed: list[tuple[str, str, str]] = []
        self.reported: list[tuple[str, str, str | None]] = []
        self.withdrawn: list[str] = []
        self._hashes: dict[str, str] = {}

    def _check(self) -> None:
        if self.down:
            raise tower.TowerError("tower_unavailable", 503)

    async def submit(self, cfg: tower.Config, **kw: Any) -> tower.Submitted:
        self._check()
        updated = any(item["external_id"] == kw["external_id"] for item in self.submitted)
        self.submitted.append(kw)
        approval_id = "apv-" + kw["external_id"][-6:]
        # 像真的 Tower 一样记下被批准的是哪份请求：核销时哈希对不上就拒绝。
        self._hashes[approval_id] = tower.request_hash(
            cfg, kw["external_id"], kw["body"], kw["caller"]
        )
        return tower.Submitted(approval_id, updated)

    async def token(self, cfg: tower.Config, approval_id: str) -> str:
        self._check()
        if self.refuse:
            raise tower.TowerError(self.refuse, 409)
        return "twa_" + approval_id

    async def redeem(self, cfg: tower.Config, token: str, req_hash: str, caller: str) -> str:
        self._check()
        approval_id = token.removeprefix("twa_")
        if self._hashes.get(approval_id) != req_hash:
            raise tower.TowerError("request_mismatch", 403)
        self.redeemed.append((token, req_hash, caller))
        return approval_id

    async def report(
        self, cfg: tower.Config, approval_id: str, status: str, error: str | None = None
    ) -> None:
        self._check()
        self.reported.append((approval_id, status, error))

    async def withdraw(self, cfg: tower.Config, external_id: str) -> None:
        self._check()
        self.withdrawn.append(external_id)


def install_tower(monkeypatch: pytest.MonkeyPatch) -> FakeTower:
    fake = FakeTower()
    for name in ("submit", "token", "redeem", "report", "withdraw"):
        monkeypatch.setattr(tower, name, getattr(fake, name))
    return fake


STORE = attachments.Store(
    "https://s3.example.com", "us-east-1", "tmp-bucket", "pa/", "access", "secret", "virtual"
)


class FakeBucket:
    """替换 `attachments.client`：内存里的桶，只有平台用到的那几个 S3 客户端方法。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.down = False

    def __call__(self, cfg: attachments.Store) -> FakeBucket:
        return self

    def close(self) -> None:
        return None

    def _check(self) -> None:
        if self.down:
            raise EndpointConnectionError(endpoint_url="https://s3.example.com")

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kw: Any) -> None:
        self._check()
        self.objects[Key] = bytes(Body)

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._check()
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self._check()
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def generate_presigned_url(self, operation: str, *, Params: Any, ExpiresIn: int) -> str:
        return f"https://tmp.example.com/{Params['Key']}?expires={ExpiresIn}"


@dataclass
class Blob:
    """中转区里的一个文件（`StoredObject` 上平台用到的那几列）。"""

    filename: str
    content_type: str
    content: bytes
    size: int
    sha256: str


class FakeRelay:
    """替换 `FileRelay`：中转区在内存里，中转编号是 `upload-<序号>` 加凑够长度的填充。"""

    settings = None

    def __init__(self) -> None:
        self.blobs: dict[str, Blob] = {}

    def add(self, filename: str, content: bytes, *, mime: str = "application/pdf") -> str:
        """助理已经传到中转区的一个文件，返回它的 upload_id。"""
        upload_id = f"upload-{len(self.blobs) + 1}".ljust(20, "x")
        self.blobs[upload_id] = Blob(
            filename, mime, content, len(content), hashlib.sha256(content).hexdigest()
        )
        return upload_id

    async def uploaded(self, session: Any, scope: Scope, upload_id: str) -> Blob:
        if upload_id not in self.blobs:
            raise personal.PersonalError("upload_not_found")
        return self.blobs[upload_id]

    async def read(self, row: Blob) -> bytes:
        return row.content

    async def save(
        self, session: Any, source: AsyncIterator[bytes], *, filename: str, mime: str
    ) -> Blob:
        content = b"".join([chunk async for chunk in source])
        return Blob(filename, mime, content, len(content), hashlib.sha256(content).hexdigest())

    def upload_id(self, grant: Any, row: Blob) -> str:
        upload_id = f"again-{len(self.blobs) + 1}".ljust(20, "x")
        self.blobs[upload_id] = row
        return upload_id


def install_files(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeRelay, FakeBucket]:
    """文件中转区与临时桶的替身；临时桶按已配置处理。"""
    bucket = FakeBucket()
    monkeypatch.setattr(attachments, "client", bucket)
    monkeypatch.setattr(attachments, "store", lambda settings: STORE)
    return FakeRelay(), bucket


def install(
    monkeypatch: pytest.MonkeyPatch, feishu: FakeFeishu | None = None
) -> tuple[FakeFeishu, FakeTower]:
    feishu = feishu or FakeFeishu()
    monkeypatch.setattr(personal, "api_request", feishu)
    return feishu, install_tower(monkeypatch)


async def assistant_turn(session: AsyncSession, who: str = "zhangsan") -> tuple[Bot, User, Scope]:
    """所有者（默认张三）在与自己助理的私聊里发起的一轮；返回这一轮核验后的身份。

    要第二位所有者时换一个 `who`，并在排下任何别的任务之前调用（这里会认领刚入队的那一轮）。
    """
    open_id = OWNER if who == "zhangsan" else f"ou_{who}"
    bot, owner = await seed_assistant(session)
    await keys.ensure_key(session, sealed.master(), owner.id)
    session.add_all(
        [
            UserIdentity(
                user_id=owner.id, platform="feishu", platform_user_id=who, open_id=open_id
            ),
            UserReached(bot_id=bot.id, user_id=owner.id, platform_chat_id="oc_assistant"),
            FeishuPersonalGrant(
                bot_id=bot.id,
                user_id=owner.id,
                app_id="cli_example",
                platform_user_id=who,
                open_id=open_id,
                tenant_key="tenant-example",
                status="connected",
                token_enc="enc:placeholder",
                authorization_level="all",
            ),
        ]
    )
    await session.commit()
    task = await chat_task(
        session, bot, "帮我回一下李四", sender=who, chat_type="single", chat_id="oc_assistant"
    )
    event = await session.get(InboundEvent, task.inbound_event_id)
    scope = Scope(
        task, bot, owner.id, "tenant-example", "cli_example", open_id, who, "oc_assistant", event
    )
    return bot, owner, scope


async def pending(
    session: AsyncSession, scope: Scope, arguments: dict[str, Any] | None = None
) -> AssistantApproval:
    """助理提交了一次回复，审批单在等所有者处理。"""
    out = await approvals.submit(
        session,
        None,
        scope,
        "feishu_reply_message",
        arguments or reply(),
        cfg=CFG,  # type: ignore[arg-type]
    )
    await session.commit()
    row = await session.get(AssistantApproval, out["approval_id"])
    assert row is not None and row.status == "pending"
    return row


async def decided(
    session: AsyncSession, row: AssistantApproval, status: str = "approved", reason: str = ""
) -> service.Decided:
    """Tower 的决定送达。"""
    assert row.approval_id is not None
    found = await service.decide(
        session,
        approval_id=row.approval_id,
        external_id=row.external_id,
        status=status,
        reason=reason or None,
        decided_at=None,
    )
    await session.commit()
    return found


async def queued(session: AsyncSession, kind: str) -> list[Task]:
    rows = list(await session.scalars(select(Task).where(Task.kind == kind).order_by(Task.id)))
    await sealed.open_rows(session, rows)
    return rows
