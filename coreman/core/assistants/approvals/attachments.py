"""等待审批期间的附件：放在一个临时对象存储桶里，随机对象名，桶的自动清理规则兜底。

中转文件（`feishu_personal.files`）一小时后过期，等不到最长 72 小时的审批。提交时把它复制到
临时桶，审批单里只留一条引用（对象名、文件名、类型、大小、SHA-256）；批准后取回并校验，
再交给原来的工具。文件本体不加密（部署方的取舍）：对象名是 256 位随机数，只出现在加密的
`args_enc` 和给所有者看的预签名链接里；桶必须私有，并配置不超过 7 天的自动清理。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from coreman.core.config import Settings
from coreman.core.logging import get_logger
from coreman.core.relay.safe_transport import validate_host

log = get_logger(__name__)
# 工具参数里装中转文件编号的字段；暂存时它们的值换成引用。
ARGUMENTS = frozenset({"attachment_ids", "upload_id"})
# 预签名链接最长 7 天（SigV4 的上限）。审批期 72 小时加批准后 24 小时的取令牌窗口不到 5 天。
MAX_LINK_SECONDS = 7 * 24 * 3600
_GONE = {"NoSuchKey", "404", "NotFound"}


class Unavailable(Exception):
    """临时桶连不上或拒绝了请求：什么都没变，可以稍后再试。"""


class Missing(Exception):
    """对象不在桶里：已被清理，或从未写入。"""


class Changed(Exception):
    """取回的内容与提交时记下的大小或 SHA-256 不符。"""


@dataclass(frozen=True)
class Store:
    endpoint: str | None
    region: str
    bucket: str
    prefix: str
    access_key: str
    secret_key: str
    addressing: str


def store(settings: Settings) -> Store | None:
    """临时桶的配置；没配或配得不合法都返回 None（带文件的写操作不可用）。"""
    bucket = settings.assistant_tmp_s3_bucket
    access, secret = settings.assistant_tmp_s3_access_key, settings.assistant_tmp_s3_secret_key
    if not (bucket and access and secret):
        return None
    prefix = settings.assistant_tmp_s3_prefix.strip("/")
    endpoint = settings.assistant_tmp_s3_endpoint
    try:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise ValueError("bucket")
        if not re.fullmatch(r"[A-Za-z0-9/_-]{1,100}", prefix):
            raise ValueError("prefix")
        if not re.fullmatch(r"[a-z0-9-]{1,50}", settings.assistant_tmp_s3_region):
            raise ValueError("region")
        if endpoint:
            parts = urlsplit(endpoint)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username
                or parts.password
                or parts.query
                or parts.fragment
                or parts.path not in ("", "/")
            ):
                raise ValueError("endpoint")
            validate_host(parts.hostname)
    except ValueError as exc:
        log.error("assistant_tmp_storage_misconfigured", field=str(exc)[:20])
        return None
    return Store(
        endpoint,
        settings.assistant_tmp_s3_region,
        bucket,
        prefix + "/",
        access.get_secret_value(),
        secret.get_secret_value(),
        settings.assistant_tmp_s3_addressing,
    )


def _client(cfg: Store) -> Any:
    # 显式凭据，不走机器元数据凭据链，不继承代理；与平台自己的对象存储是两套配置。
    return boto3.session.Session().client(
        "s3",
        endpoint_url=cfg.endpoint,
        region_name=cfg.region,
        aws_access_key_id=cfg.access_key,
        aws_secret_access_key=cfg.secret_key,
        config=Config(
            signature_version="s3v4",
            ignore_configured_endpoint_urls=True,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            connect_timeout=5,
            read_timeout=30,
            retries={"total_max_attempts": 2, "mode": "standard"},
            proxies={},
            s3={"addressing_style": cfg.addressing},
        ),
    )


# 测试换成内存里的替身。
client: Callable[[Store], Any] = _client


def reference(cfg: Store, *, filename: str, mime: str, size: int, sha256: str) -> dict[str, Any]:
    """一个文件在审批单里的引用。对象名不带文件名、所有者或助理的任何标识。"""
    return {
        "key": cfg.prefix + secrets.token_urlsafe(32),
        "filename": filename,
        "mime": mime,
        "size": size,
        "sha256": sha256,
    }


def references(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """这些调用里的全部文件引用。"""
    found: list[dict[str, Any]] = []
    for call in calls:
        for name in sorted(ARGUMENTS & set(call["arguments"])):
            value = call["arguments"][name]
            found += [item for item in (value if isinstance(value, list) else [value]) if item]
    return [item for item in found if isinstance(item, dict)]


def public(arguments: dict[str, Any]) -> dict[str, Any]:
    """参与哈希与去重的参数：文件只留文件名、大小与 SHA-256，不带对象名。"""

    def slim(item: Any) -> Any:
        if not isinstance(item, dict):
            return item
        return {"filename": item["filename"], "size": item["size"], "sha256": item["sha256"]}

    out = dict(arguments)
    for name in ARGUMENTS & set(out):
        value = out[name]
        out[name] = [slim(item) for item in value] if isinstance(value, list) else slim(value)
    return out


def link(cfg: Store, ref: dict[str, Any], seconds: int) -> str:
    """给所有者在批准前查看文件的预签名下载链接；本地计算，不访问桶。"""
    name = quote(str(ref["filename"]), safe="")
    with closing(client(cfg)) as s3:
        return str(
            s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": cfg.bucket,
                    "Key": ref["key"],
                    "ResponseContentDisposition": f"attachment; filename*=UTF-8''{name}",
                },
                ExpiresIn=max(60, min(seconds, MAX_LINK_SECONDS)),
            )
        )


async def put(cfg: Store, key: str, content: bytes) -> None:
    def run() -> None:
        with closing(client(cfg)) as s3:
            # 一律按二进制流存：浏览器打开链接时只会下载，不会就地渲染。
            s3.put_object(
                Bucket=cfg.bucket,
                Key=key,
                Body=content,
                ContentLength=len(content),
                ContentType="application/octet-stream",
            )

    try:
        await asyncio.to_thread(run)
    except (BotoCoreError, ClientError) as exc:
        log.warning("assistant_attachment_put_failed", type=type(exc).__name__)
        raise Unavailable from None


async def get(cfg: Store, ref: dict[str, Any]) -> bytes:
    """取回一个文件并核对大小与 SHA-256。

    Raises:
        Missing: 对象不在了
        Changed: 内容与引用不符
        Unavailable: 桶暂时不可用
    """

    def run() -> bytes:
        with closing(client(cfg)) as s3:
            body = s3.get_object(Bucket=cfg.bucket, Key=ref["key"])["Body"]
            try:
                # 多读一个字节：比引用大的对象不整个读进内存。
                return bytes(body.read(int(ref["size"]) + 1))
            finally:
                body.close()

    try:
        content = await asyncio.to_thread(run)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code") or "")
        if code in _GONE:
            raise Missing from None
        log.warning("assistant_attachment_get_failed", code=code[:40])
        raise Unavailable from None
    except BotoCoreError as exc:
        log.warning("assistant_attachment_get_failed", type=type(exc).__name__)
        raise Unavailable from None
    if len(content) != ref["size"] or hashlib.sha256(content).hexdigest() != ref["sha256"]:
        raise Changed
    return content


async def delete(cfg: Store, keys: list[str]) -> bool:
    """尽力删除；失败只记日志并返回 False，桶的自动清理兜底。不存在的对象也算删除成功。"""
    if not keys:
        return True

    def run() -> None:
        with closing(client(cfg)) as s3:
            for key in keys:
                s3.delete_object(Bucket=cfg.bucket, Key=key)

    try:
        await asyncio.to_thread(run)
    except (BotoCoreError, ClientError) as exc:
        log.warning("assistant_attachment_delete_failed", type=type(exc).__name__)
        return False
    return True
