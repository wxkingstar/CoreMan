"""个人助理内容列的封存与还原。

对 `bots.kind='assistant'` 的记录，登记在 `COLUMNS` 里的内容列写库前用所有者的数据密钥加密、
读出后解密；普通机器人的行原样读写，调用方拿到的始终是明文。

封存形式：
- 字典列：`{"_sealed": <密文>, <明文键>…}`，密文里是去掉明文键之后的其余部分。还原时与封装
  顶层的其它键合并，顶层优先——所以不解密也能对封装增删明文键。
- 整值列：`{"_sealed": <密文>}`。
- 文本列：密文串本身。
- `{"_sealed": ""}` 是墓碑：密钥销毁之后写入，内容已丢弃。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from coreman.core.crypto import Cipher, is_encrypted, key_id_of
from coreman.core.db.models import (
    ChatLog,
    FeishuSentMessage,
    InboundEvent,
    OutboxItem,
    Task,
    TaskStream,
)

SEALED = "_sealed"
LOCKED_CODE = "content_locked"


class ContentLocked(Exception):
    """内容读不出来：数据密钥已销毁或不存在、内容由已作废的旧一代密钥加密，或是销毁之后写下的墓碑。"""


@dataclass(frozen=True)
class JsonSpec:
    """字典列：哪些键留在密文外面。

    Attributes:
        plain: 留明文的顶层键（被 SQL 直接读取的控制字段）
        underscore: 下划线开头的键是否留明文（出站的内部记账键）
        mirrors: `(根键, 子键)`：把 `value[根键][子键]` 在封装顶层另存一份供 SQL 过滤，
            还原时以密文里的为准
    """

    plain: frozenset[str] = frozenset()
    underscore: bool = False
    mirrors: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Whole:
    """整值封存的 JSONB 列；`blank` 是读不出来时给调用方的值。"""

    blank: Any = None


@dataclass(frozen=True)
class Text:
    """文本列；`blank` 是读不出来、或密钥销毁后写入时的值（非空列用空串）。"""

    blank: str | None = None


Kind = JsonSpec | Whole | Text

# tasks.payload 里被 SQL 直接读取的键：认领语句、排队判断、媒体补偿、技能安装、文档链接。
TASK_PLAIN = frozenset(
    {
        "serialize_session",
        "stale_hold",
        "platform_user_id",
        "origin",
        "busy_offer",
        "escalation_id",
        "message_id",
        "skill_id",
        "doc_links",
    }
)
COLUMNS: dict[type, dict[str, Kind]] = {
    InboundEvent: {"payload": JsonSpec()},
    Task: {
        "payload": JsonSpec(plain=TASK_PLAIN),
        "result": Whole(),
        "error_message": Text(),
    },
    TaskStream: {
        "thinking_md": Text(""),
        "pending_text": Text(""),
        "final_text": Text(),
        "pending_card": Whole(),
        "plan": Whole([]),
    },
    OutboxItem: {"payload": JsonSpec(underscore=True, mirrors=(("card", "task_id"),))},
    ChatLog: {
        "message_content": Text(),
        "quoted_content": Text(),
        "response_content": Text(),
        "file_info": Whole(),
        "error_message": Text(),
    },
    # 机器人发出的每条飞书消息的正文存档；text 非空，读不出来时给空串。
    FeishuSentMessage: {"text": Text("")},
}


def aad(table: str, column: str, bot_id: uuid.UUID) -> str:
    """密文绑定到表、列和助理：挪到别处就解不开。"""
    return f"{table}.{column}:{bot_id}"


def is_sealed(value: object) -> bool:
    return isinstance(value, dict) and isinstance(value.get(SEALED), str)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decrypt(cipher: Cipher, token: str, aad_: str) -> str:
    """解一段内容密文。

    密文里的密钥标识（`d<代数>`）与当前数据密钥不是同一代：密钥销毁后重新签发过，这段内容
    永久读不出来，按已锁定处理。同一代却解不开才是密文被改或被挪动，由 `Cipher` 抛
    `DecryptError`。

    Raises:
        ContentLocked: 墓碑，或由另一代数据密钥加密
        DecryptError: 同一代密钥解不开
    """
    if not token:
        raise ContentLocked("内容在密钥销毁之后写入，已丢弃")
    if key_id_of(token) != cipher.key_id:
        raise ContentLocked("内容由已作废的旧一代数据密钥加密")
    return cipher.decrypt(token, aad_)


def _keep(spec: JsonSpec, value: dict[str, Any]) -> dict[str, Any]:
    """留在密文外面的那部分。"""
    return {
        k: v
        for k, v in value.items()
        if k != SEALED and (k in spec.plain or (spec.underscore and k.startswith("_")))
    }


def seal_dict(cipher: Cipher, aad_: str, value: dict[str, Any], spec: JsonSpec) -> dict[str, Any]:
    if is_sealed(value):
        # 已经是封装（可能被追加了别的键）：先还原，杂散的键一起收进密文。
        value = open_dict(cipher, aad_, value, spec)
    keep = _keep(spec, value)
    rest = {k: v for k, v in value.items() if k not in keep and k != SEALED}
    out: dict[str, Any] = {SEALED: cipher.encrypt(_dumps(rest), aad_), **keep}
    for root, child in spec.mirrors:
        inner = rest.get(root)
        if isinstance(inner, dict) and child in inner:
            out[root] = {child: inner[child]}
    return out


def open_dict(cipher: Cipher, aad_: str, value: dict[str, Any], spec: JsonSpec) -> dict[str, Any]:
    if not is_sealed(value):
        return value
    rest: dict[str, Any] = json.loads(_decrypt(cipher, value[SEALED], aad_))
    roots = {root for root, _ in spec.mirrors}
    extra = {k: v for k, v in value.items() if k != SEALED and not (k in roots and k in rest)}
    return {**rest, **extra}


def seal_whole(cipher: Cipher, aad_: str, value: Any) -> Any:
    if value is None or is_sealed(value):
        return value
    return {SEALED: cipher.encrypt(_dumps(value), aad_)}


def open_whole(cipher: Cipher, aad_: str, value: Any) -> Any:
    if not is_sealed(value):
        return value
    return json.loads(_decrypt(cipher, value[SEALED], aad_))


def seal_text(cipher: Cipher, aad_: str, value: str | None) -> str | None:
    return cipher.encrypt(value, aad_) if value else value


def open_text(cipher: Cipher, aad_: str, value: Any) -> Any:
    if not isinstance(value, str) or not is_encrypted(value):
        return value
    return _decrypt(cipher, value, aad_)
