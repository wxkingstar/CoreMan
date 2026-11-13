"""等待审批期间的附件：临时桶的配置、随机对象名、取回时的校验，以及哪些工具带文件。"""

from __future__ import annotations

import base64
import hashlib
import io
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from pydantic import SecretStr

from coreman.core.assistants.approvals import attachments, catalog
from coreman.core.config import Settings
from coreman.core.feishu_personal.tools import TOOLS

BASE = {
    "assistant_tmp_s3_endpoint": "https://s3.example.com",
    "assistant_tmp_s3_region": "us-east-1",
    "assistant_tmp_s3_bucket": "tmp-bucket",
    "assistant_tmp_s3_prefix": "/pending/",
    "assistant_tmp_s3_access_key": SecretStr("access"),
    "assistant_tmp_s3_secret_key": SecretStr("secret"),
    "assistant_tmp_s3_addressing": "virtual",
}
STORE = attachments.Store(
    "https://s3.example.com", "us-east-1", "tmp-bucket", "pending/", "access", "secret", "virtual"
)
CONTENT = b"%PDF quote"
REF = {
    "key": "pending/abc",
    "filename": "报价单.pdf",
    "mime": "application/pdf",
    "size": len(CONTENT),
    "sha256": hashlib.sha256(CONTENT).hexdigest(),
}


class Bucket:
    """内存里的桶：`attachments` 用到的那几个 S3 客户端方法。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.error: Exception | None = None
        self.put_args: dict[str, Any] = {}

    def __call__(self, cfg: attachments.Store) -> Bucket:
        return self

    def close(self) -> None:
        return None

    def put_object(self, **kw: Any) -> None:
        if self.error:
            raise self.error
        self.put_args = kw
        self.objects[kw["Key"]] = kw["Body"]

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if self.error:
            raise self.error
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        if self.error:
            raise self.error
        self.objects.pop(Key, None)


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> Bucket:
    fake = Bucket()
    monkeypatch.setattr(attachments, "client", fake)
    return fake


def test_store_needs_bucket_and_credentials_and_a_sane_shape() -> None:
    assert attachments.store(Settings.model_construct(**BASE)) == STORE
    for missing in (
        "assistant_tmp_s3_bucket",
        "assistant_tmp_s3_access_key",
        "assistant_tmp_s3_secret_key",
    ):
        assert attachments.store(Settings.model_construct(**{**BASE, missing: None})) is None
    # 配得不合法按没配处理（带文件的写操作不可用），不带着错的配置去连。
    for bad in (
        {"assistant_tmp_s3_endpoint": "http://s3.example.com"},
        {"assistant_tmp_s3_endpoint": "https://user:pw@s3.example.com"},
        {"assistant_tmp_s3_endpoint": "https://s3.example.com/path"},
        {"assistant_tmp_s3_endpoint": "https://localhost"},
        {"assistant_tmp_s3_bucket": "Bad_Bucket"},
        {"assistant_tmp_s3_prefix": "a b"},
        {"assistant_tmp_s3_prefix": "/"},
        {"assistant_tmp_s3_region": "US EAST"},
    ):
        assert attachments.store(Settings.model_construct(**{**BASE, **bad})) is None, bad
    # 不填端点：用 SDK 按区域推出的默认端点。
    found = attachments.store(
        Settings.model_construct(**{**BASE, "assistant_tmp_s3_endpoint": None})
    )
    assert found is not None and found.endpoint is None


def test_object_names_are_random_and_carry_nothing_about_the_file() -> None:
    made = [
        attachments.reference(
            STORE, filename="报价单.pdf", mime="application/pdf", size=3, sha256="f"
        )
        for _ in range(50)
    ]
    keys = {item["key"] for item in made}
    assert len(keys) == 50
    for key in keys:
        # 256 位随机数的 URL 安全编码：43 个字符。
        assert key.startswith("pending/") and len(key) == len("pending/") + 43
        assert "报价" not in key and "pdf" not in key.lower().split("/")[0]
    assert set(made[0]) == {"key", "filename", "mime", "size", "sha256"}


def test_references_and_public_form() -> None:
    other = {**REF, "key": "pending/def", "filename": "b.pdf"}
    calls = [
        {"arguments": {"upload_id": REF, "name": "x"}},
        {"arguments": {"attachment_ids": [REF, other], "subject": "s"}},
        {"arguments": {"attachment_ids": [], "text": "无附件"}},
    ]
    assert attachments.references(calls) == [REF, REF, other]
    assert attachments.references([{"arguments": {"upload_id": "still-an-upload-id"}}]) == []
    slim = {"filename": "报价单.pdf", "size": len(CONTENT), "sha256": REF["sha256"]}
    assert attachments.public(calls[0]["arguments"]) == {"upload_id": slim, "name": "x"}
    assert attachments.public(calls[1]["arguments"])["attachment_ids"] == [
        slim,
        {**slim, "filename": "b.pdf"},
    ]
    # 没有文件的参数原样返回；原参数不被改动。
    assert attachments.public({"text": "好"}) == {"text": "好"}
    assert calls[0]["arguments"]["upload_id"] is REF


def test_link_is_presigned_for_this_object_and_expires() -> None:
    """用真的 SDK 客户端离线签名：不访问网络。"""
    url = urlsplit(attachments.link(STORE, REF, 72 * 3600))
    query = parse_qs(url.query)
    assert url.scheme == "https" and url.hostname == "tmp-bucket.s3.example.com"
    assert url.path == "/pending/abc" and query["X-Amz-Expires"] == ["259200"]
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"] and "X-Amz-Signature" in query
    # 下载时带回原文件名；密钥本身不在链接里。
    assert query["response-content-disposition"] == [
        "attachment; filename*=UTF-8''%E6%8A%A5%E4%BB%B7%E5%8D%95.pdf"
    ]
    assert "secret" not in url.query
    # 不超过签名算法允许的 7 天；路径风格的端点把桶名放在路径里。
    week = parse_qs(urlsplit(attachments.link(STORE, REF, 30 * 24 * 3600)).query)
    assert week["X-Amz-Expires"] == [str(attachments.MAX_LINK_SECONDS)]
    path_style = attachments.Store(**{**STORE.__dict__, "addressing": "path"})
    assert urlsplit(attachments.link(path_style, REF, 60)).path == "/tmp-bucket/pending/abc"


async def test_put_get_delete_round_trip(bucket: Bucket) -> None:
    await attachments.put(STORE, REF["key"], CONTENT)
    assert bucket.put_args["Bucket"] == "tmp-bucket"
    assert bucket.put_args["ContentType"] == "application/octet-stream"
    assert await attachments.get(STORE, REF) == CONTENT
    assert await attachments.delete(STORE, [REF["key"]]) is True and bucket.objects == {}
    # 再删一次、删空列表都算成功。
    assert await attachments.delete(STORE, [REF["key"]]) is True
    assert await attachments.delete(STORE, []) is True


async def test_get_refuses_what_is_not_the_staged_file(bucket: Bucket) -> None:
    with pytest.raises(attachments.Missing):
        await attachments.get(STORE, REF)
    bucket.objects[REF["key"]] = b"%PDF fraud"
    with pytest.raises(attachments.Changed):
        await attachments.get(STORE, REF)
    bucket.objects[REF["key"]] = CONTENT + b" and more"
    with pytest.raises(attachments.Changed):
        await attachments.get(STORE, REF)


async def test_bucket_errors_are_unavailable_and_delete_only_logs(bucket: Bucket) -> None:
    bucket.objects[REF["key"]] = CONTENT
    for error in (
        EndpointConnectionError(endpoint_url="https://s3.example.com"),
        ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject"),
    ):
        bucket.error = error
        with pytest.raises(attachments.Unavailable):
            await attachments.put(STORE, "pending/x", b"x")
        with pytest.raises(attachments.Unavailable):
            await attachments.get(STORE, REF)
        assert await attachments.delete(STORE, [REF["key"]]) is False


def test_tools_that_take_uploaded_files_are_all_listed() -> None:
    """入参里有中转文件编号的工具必须登记在 `FILE_TOOLS`：新增这类工具时这里先失败。"""
    with_files = {
        name for name, item in TOOLS.items() if attachments.ARGUMENTS & set(item.args.model_fields)
    }
    assert with_files == catalog.FILE_TOOLS
    assert all(catalog.is_write(name) for name in catalog.FILE_TOOLS)


def test_half_configured_bucket_stops_the_process_at_startup() -> None:
    values = {
        "DATABASE_URL": "postgresql+asyncpg://u@db.example.com/coreman",
        "PUBLIC_BASE_URL": "https://coreman.example.com",
        "MASTER_KEY": base64.b64encode(b"k" * 32).decode(),
        "SESSION_SECRET": "s" * 32,
        "ASSISTANT_TMP_S3_BUCKET": "tmp-bucket",
    }
    with pytest.raises(ValueError, match="ASSISTANT_TMP_S3_BUCKET"):
        Settings(_env_file=None, **values)
    assert (
        Settings(
            _env_file=None,
            **values,
            ASSISTANT_TMP_S3_ACCESS_KEY="access",
            ASSISTANT_TMP_S3_SECRET_KEY="secret",
        ).assistant_tmp_s3_addressing
        == "virtual"
    )
