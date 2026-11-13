"""个人助理内容列的封存与还原。

对 `bots.kind='assistant'` 的记录，登记在 `COLUMNS` 里的内容列写库前用所有者的数据密钥加密、
读出后解密；普通机器人的行原样读写，调用方拿到的始终是明文。

封存形式：
- 字典列：`{"_sealed": <密文>, <明文键>…}`，密文里是去掉明文键之后的其余部分。还原时与封装
  顶层的其它键合并，顶层优先——所以不解密也能对封装增删明文键。
- 整值列：`{"_sealed": <密文>}`。
- 文本列：密文串本身。
- 二进制列：`encrypt_bytes` 的输出（`enc:v2:d<代数>:` 加原始密文字节），不套 base64。
- `{"_sealed": ""}` 是墓碑：密钥销毁之后写入，内容已丢弃。

内容本身可能长得像封装（任务结果恰好是 `{"_sealed": …}`、正文以 `enc:v2:` 开头）。所以：
- 封存不看值的形状：交来的值一律当明文整体加密，`_sealed` 键也收进密文。唯一的例外由调用方
  明说（`seal_dict(..., envelope=True)`）：flush 钩子确认这个值是从库里读出、尚未还原的封装
  改出来的。
- 还原只认库里读出的值，且 `_sealed` 必须是本模块写出的格式（墓碑或数据密钥的密文）；已经
  还原在内存里的值靠行上的标记（`_OPENED`）认出来，不再解第二次。

解不开（主密钥配错、密文被改，`DecryptError`）默认照抛，配置错误要响亮。一个会话里处理很多
机器人的行时（认领、看护），先调 `tolerate_undecryptable`：解不开的那一行按读不出来处理，
其余的行照常，不让一行坏数据把整批连同普通机器人一起卡住。
"""

from __future__ import annotations

import copy
import json
import re
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import case, event, exists, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from coreman.core.assistants import keys
from coreman.core.config import get_settings
from coreman.core.crypto import PREFIX_V2, Cipher, DecryptError, key_id_of
from coreman.core.db.models import (
    AssistantApproval,
    AssistantPage,
    AssistantSettings,
    AssistantTranscript,
    Bot,
    ChatLog,
    CloudPendingApproval,
    FeishuDelivery,
    FeishuSentMessage,
    InboundEvent,
    InteractionState,
    OutboxItem,
    Task,
    TaskStream,
)
from coreman.core.logging import get_logger, mark_private_content

log = get_logger(__name__)

SEALED = "_sealed"
LOCKED_CODE = "content_locked"
UNDECRYPTABLE_CODE = "content_undecryptable"


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
        secret: 下划线开头却带内容的键：`underscore` 开着也进密文
    """

    plain: frozenset[str] = frozenset()
    underscore: bool = False
    mirrors: tuple[tuple[str, str], ...] = ()
    secret: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Whole:
    """整值封存的 JSONB 列；`blank` 是读不出来时给调用方的值。"""

    blank: Any = None


@dataclass(frozen=True)
class Text:
    """文本列；`blank` 是读不出来、或密钥销毁后写入时的值（非空列用空串）。"""

    blank: str | None = None


@dataclass(frozen=True)
class Blob:
    """二进制列（bytea）：整段加密，读不出来时给 None。"""


Kind = JsonSpec | Whole | Text | Blob

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
# 回复上下文里路由真正要用的键：会话坐标、被回复的消息、收到的时间与网关实例，以及下划线
# 开头的内部标记。其余的键（卡片回复按钮的文字等助理生成的内容）进密文。
REPLY_CONTEXT = JsonSpec(
    plain=frozenset(
        {
            "chat_id",
            "chat_type",
            "message_id",
            "parent_id",
            "root_id",
            "create_time",
            "req_id",
            "gateway_instance",
            "received_at",
            "requester_user_id",
        }
    ),
    underscore=True,
)
COLUMNS: dict[type, dict[str, Kind]] = {
    InboundEvent: {"payload": JsonSpec(), "reply_context": REPLY_CONTEXT},
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
        # 最近动作：说明文字（工具说明、命令首行）进密文；时间、已知等待、卡住与中断标记是
        # 规则生成的元数据，留明文，读不出来时状态行照常显示。
        "activity": JsonSpec(plain=frozenset({"at", "until", "expect", "stuck", "interrupted"})),
    },
    # `_reply_used` 带着被点的按钮文字（置灰时写回卡片），不随其它记账键留明文。
    OutboxItem: {
        "payload": JsonSpec(
            underscore=True, mirrors=(("card", "task_id"),), secret=frozenset({"_reply_used"})
        )
    },
    ChatLog: {
        "message_content": Text(),
        "quoted_content": Text(),
        "response_content": Text(),
        "file_info": Whole(),
        "error_message": Text(),
    },
    # 机器人发出的每条飞书消息的正文存档；text 非空，读不出来时给空串。
    FeishuSentMessage: {"text": Text("")},
    # 流式卡片上当前的单元布局：思考预览、各段正文、任务清单进密文；上次开启流式的时间与
    # 「已暂停增量」是网关自己的记账，留明文。
    FeishuDelivery: {"layout": JsonSpec(plain=frozenset({"since", "paused"}))},
    # 等用户下一步的状态：题面、选项、答案、暂存的参数进密文。`user_id` 被 SQL 用来数
    # 「这个人还有几条待确认」（reminders、personal_schedules），留明文。
    InteractionState: {"state": JsonSpec(plain=frozenset({"user_id"}))},
    # 审批闸门：暂存的参数（连同摘要与助理写的说明）、审批人的话、执行结果。
    AssistantApproval: {"args_enc": Whole(), "reason_enc": Text(), "result_enc": Whole()},
    # 云服务审批的拒绝理由：审批人写给这位所有者的话。
    CloudPendingApproval: {"reason": Text()},
    # 节点上 CLI 的转录：一轮结束后上传，续聊前下发。
    AssistantTranscript: {"blob_enc": Blob()},
    # 个人知识库：路径、标题、摘要、正文。四列非空，读不出来时给空串。
    AssistantPage: {
        "path_enc": Text(""),
        "title_enc": Text(""),
        "summary_enc": Text(""),
        "body_enc": Text(""),
    },
    # 所有者标记的重点群与忽略的会话：会话 id 的列表整值封存，读不出来按空列表。
    AssistantSettings: {"focus_chats_enc": Whole([]), "ignored_chats_enc": Whole([])},
}


def aad(table: str, column: str, bot_id: uuid.UUID) -> str:
    """密文绑定到表、列和助理：挪到别处就解不开。"""
    return f"{table}.{column}:{bot_id}"


# 数据密钥的标识：`d<代数>`，见 keys.data_key_id。
_DATA_KID = re.compile(r"^d[0-9]+$")


def is_token(value: object) -> bool:
    """是不是数据密钥写出的密文串（`enc:v2:d<代数>:…`）。"""
    if not isinstance(value, str) or not value.startswith(PREFIX_V2):
        return False
    kid = key_id_of(value)
    return kid is not None and _DATA_KID.match(kid) is not None


def is_sealed(value: object) -> bool:
    """库里读出的值是不是封装：`_sealed` 是墓碑（空串）或数据密钥的密文。

    `_sealed` 是别的东西（任意字符串、数字）时不算封装，按普通内容原样返回，而不是按篡改
    报错：助理的行里没封存过的值本来就原样返回（比如记录转成助理之前写下的行），能改库的人
    直接写明文也是同样的效果，报错挡不住什么，只会让一行读不出来。
    """
    if not isinstance(value, dict):
        return False
    token = value.get(SEALED)
    return token == "" or is_token(token)


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
        if k != SEALED
        and k not in spec.secret
        and (k in spec.plain or (spec.underscore and k.startswith("_")))
    }


def seal_dict(
    cipher: Cipher, aad_: str, value: dict[str, Any], spec: JsonSpec, *, envelope: bool = False
) -> dict[str, Any]:
    """封存一个字典。

    Args:
        envelope: `value` 是库里读出、尚未还原的封装（可能被追加或删掉了明文键）：先还原，
            杂散的键一起收进密文。只能由确知来历的调用方传真——不凭值的形状判断，否则内容里
            带一个 `_sealed` 键就能让自己原样落库
    """
    if envelope:
        value = open_dict(cipher, aad_, value, spec)
    keep = _keep(spec, value)
    # 内容自带的 `_sealed` 键不留在外面（哪怕下划线开头的键留明文），随其余部分进密文。
    rest = {k: v for k, v in value.items() if k not in keep}
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
    if value is None:
        return value
    return {SEALED: cipher.encrypt(_dumps(value), aad_)}


def open_whole(cipher: Cipher, aad_: str, value: Any) -> Any:
    if not is_sealed(value):
        return value
    return json.loads(_decrypt(cipher, value[SEALED], aad_))


_DATA_KID_BYTES = re.compile(rb"^enc:v2:d[0-9]+:")


def is_blob_token(value: object) -> bool:
    """是不是数据密钥写出的二进制密文。"""
    return isinstance(value, bytes) and _DATA_KID_BYTES.match(value) is not None


def seal_blob(cipher: Cipher, aad_: str, value: bytes | None) -> bytes | None:
    return cipher.encrypt_bytes(value, aad_) if value else value


def open_blob(cipher: Cipher, aad_: str, value: Any) -> Any:
    """同 `_decrypt`：另一代数据密钥加密的按已锁定处理，同一代解不开照抛。"""
    if not is_blob_token(value):
        return value
    # 只切出密钥标识，不把整段密文复制一遍。
    start = len(PREFIX_V2)
    end = value.find(b":", start)
    kid = value[start : end if end >= 0 else len(value)].decode("ascii")
    if kid != cipher.key_id:
        raise ContentLocked("内容由已作废的旧一代数据密钥加密")
    return cipher.decrypt_bytes(value, aad_)


def seal_text(cipher: Cipher, aad_: str, value: str | None) -> str | None:
    return cipher.encrypt(value, aad_) if value else value


def open_text(cipher: Cipher, aad_: str, value: Any) -> Any:
    if not is_token(value):
        return value
    return _decrypt(cipher, value, aad_)


# ---- 不能封存的说明列 ------------------------------------------------------

# `outbox.last_error`、`tasks.cancel_reason`：运维接口直接输出，控制逻辑按它的值判断，不能加密。
# 但报错文字可能带出消息片段（平台回的错误说明、异常文字），助理的行只留开头的代码。
REDACTED = "redacted"
_LEADING_CODE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}(?: [0-9]{1,9})?")


def code_only(value: str, fallback: str = REDACTED) -> str:
    """说明文字开头的代码：一个标识符，后面可以跟一个数字（`Feishu 230013`、`not_sent`、
    `TimeoutError`）。标识符后面还连着别的字符（不是空格、冒号结尾）就不算代码，给 `fallback`。"""
    match = _LEADING_CODE.match(value)
    if match is None:
        return fallback
    rest = value[match.end() :]
    return match.group(0) if rest == "" or rest[0] in ": " else fallback


def code_for_assistants(bot_id_column: Any, value: str, fallback: str = REDACTED) -> Any:
    """写说明列用的值：助理的行只留代码，其余的行原样。

    本来就是代码的值原样返回；否则是一个随所在语句判断归属的表达式，不多一次往返。

    Args:
        bot_id_column: 所在语句里这一行的 `bot_id` 列
    """
    code = code_only(value, fallback)
    if code == value:
        return value
    assistant = exists().where(Bot.id == bot_id_column, Bot.kind == "assistant")
    return case((assistant, code), else_=value)


# ---- 按行封存与还原 --------------------------------------------------------

# 行上的标记（InstanceState.info）：还原时用的数据密钥、内容读不出来。
_CIPHER = "sealed_cipher"
_LOCKED = "sealed_locked"
# 行上的标记：列名 → 已经还原（或刚由 flush 写入）的明文对象。按对象身份比对：属性从库里
# 重新加载后是另一个对象，才会再还原；仍是这个对象就说明是明文，不管它长得像不像封装。
_OPENED = "sealed_opened"
# flush 期间被换成密文的属性，flush 之后换回明文。
_RESTORE = "sealed_restore"
# 会话上的标记：解不开按读不出来处理（见 tolerate_undecryptable）。
# 行上的标记：读不出来是因为解不开。
_TOLERATE = "sealed_tolerate_undecryptable"
_UNDECRYPTABLE = "sealed_undecryptable"
_master: Cipher | None = None
# bot_id → (所有者；普通机器人为 None, 过期时刻)。
_OWNERS: dict[uuid.UUID, tuple[uuid.UUID | None, float]] = {}


def use_master(cipher: Cipher | None) -> None:
    """指定平台主密钥（测试用）；None 表示回到配置里的那一把。"""
    global _master  # noqa: PLW0603
    _master = cipher


def master() -> Cipher:
    return _master or get_settings().build_cipher()


def forget() -> None:
    """清本进程的所有者缓存。"""
    _OWNERS.clear()


def tolerate_undecryptable(session: AsyncSession) -> None:
    """这个会话从此把「解不开」（`DecryptError`）当作这一行读不出来，不再抛错。

    只给一个会话里处理很多机器人的行的地方用（认领、看护），会话要是这批处理专用的。读：内容
    列置空，`is_locked` 与 `is_undecryptable` 为真；写：内容丢弃（同密钥已销毁）。每次记一条
    `assistant_content_undecryptable` 错误日志，只带表名、行号与机器人。
    """
    session.info[_TOLERATE] = True


def _tolerated(session: AsyncSession, table: str, bot_id: uuid.UUID, row_id: Any) -> bool:
    """解不开时：会话声明了容忍就记一条日志（不带内容与异常文字）返回真，调用方置空；否则照抛。"""
    if not session.info.get(_TOLERATE):
        return False
    log.error(
        "assistant_content_undecryptable",
        table=table,
        row_id=None if row_id is None else str(row_id),
        bot_id=str(bot_id),
    )
    return True


async def owner_of(session: AsyncSession, bot_id: uuid.UUID) -> uuid.UUID | None:
    """助理记录的所有者；普通机器人（或记录不存在）返回 None。结果在进程内缓存一个 TTL。"""
    now = time.monotonic()
    hit = _OWNERS.get(bot_id)
    if hit is not None and hit[1] > now:
        return hit[0]
    row = (await session.execute(select(Bot.kind, Bot.owner_id).where(Bot.id == bot_id))).first()
    if row is None:
        # 不缓存「不存在」：记录可能只是还没提交。
        return None
    owner: uuid.UUID | None = row[1] if row[0] == "assistant" else None
    _OWNERS[bot_id] = (owner, now + keys.KEY_TTL_SECONDS)
    return owner


async def guard_logs(session: AsyncSession, bot_id: uuid.UUID | None) -> bool:
    """开始处理某个机器人的内容时调用：是个人助理就让这段处理里的日志不带异常文字。

    返回是不是个人助理，调用方据此决定自己的日志字段写原文还是只写代码与长度。
    """
    private = bot_id is not None and await owner_of(session, bot_id) is not None
    mark_private_content(private)
    return private


async def routing_context(
    session: AsyncSession, bot_id: uuid.UUID, reply_context: dict[str, Any]
) -> dict[str, Any]:
    """流上的回复上下文：助理的只留路由用的键，普通机器人的原样返回。

    流的回复上下文抄自入站事件，网关只按它找会话与被回复的消息；入站带来的内容键（回复按钮
    的文字）不必再存一份，也就不必给这一列加封装。
    """
    if await owner_of(session, bot_id) is None:
        return reply_context
    return _keep(REPLY_CONTEXT, reply_context)


async def key_ready(session: AsyncSession, bot_id: uuid.UUID) -> bool:
    """这条助理记录的内容现在解得开：数据密钥在、没销毁、主密钥解得开包裹。不签发新密钥。

    普通机器人返回 False。只看密钥，不看某一段具体的密文。
    """
    try:
        return await _cipher(session, bot_id, create_key=False) is not None
    except (ContentLocked, DecryptError):
        return False


async def _cipher(session: AsyncSession, bot_id: uuid.UUID, *, create_key: bool) -> Cipher | None:
    """这条记录的数据密钥；普通机器人返回 None。取不到时抛 ContentLocked。"""
    owner = await owner_of(session, bot_id)
    if owner is None:
        return None
    hit = keys.cached(owner)
    if hit is not None:
        return hit
    try:
        return await keys.data_cipher(session, master(), owner)
    except keys.KeyDestroyed:
        raise ContentLocked("所有者的数据密钥已销毁") from None
    except keys.KeyMissing:
        if not create_key:
            raise ContentLocked("所有者没有数据密钥") from None
    # 首次用到时生成第 1 代。reissue=False：已销毁的密钥只能由开通流程显式重新签发。
    await keys.ensure_key(session, master(), owner, reissue=False)
    try:
        return await keys.data_cipher(session, master(), owner)
    except keys.KeyUnavailable:
        raise ContentLocked("所有者的数据密钥已销毁") from None


async def content_mac(session: AsyncSession, bot_id: uuid.UUID, purpose: str, message: str) -> str:
    """用所有者的数据密钥给一段内容算带密钥的摘要（去重键用）。

    只拿到数据库的人没有密钥，无法拿猜测的内容来比对。密钥重新签发后摘要随之改变。

    Raises:
        ContentLocked: 数据密钥已销毁
        ValueError: 不是个人助理的记录
    """
    cipher = await _cipher(session, bot_id, create_key=True)
    if cipher is None:
        raise ValueError("not an assistant record")
    return cipher.mac(f"{purpose}:{bot_id}", message)


def _seal(cipher: Cipher, aad_: str, kind: Kind, value: Any) -> Any:
    if isinstance(kind, JsonSpec):
        return seal_dict(cipher, aad_, value, kind)
    if isinstance(kind, Whole):
        return seal_whole(cipher, aad_, value)
    if isinstance(kind, Blob):
        return seal_blob(cipher, aad_, value)
    return seal_text(cipher, aad_, value)


def _open(cipher: Cipher, aad_: str, kind: Kind, value: Any) -> Any:
    if isinstance(kind, JsonSpec):
        return open_dict(cipher, aad_, value, kind)
    if isinstance(kind, Whole):
        return open_whole(cipher, aad_, value)
    if isinstance(kind, Blob):
        return open_blob(cipher, aad_, value)
    return open_text(cipher, aad_, value)


def _looks_sealed(kind: Kind, value: Any) -> bool:
    if isinstance(kind, Text):
        return is_token(value)
    if isinstance(kind, Blob):
        return is_blob_token(value)
    return is_sealed(value)


def _raw(state: Any, column: str) -> bool:
    """这一列在内存里的值是库里读出的原样：没还原过，也不是还没落库的改动。"""
    value = state.dict.get(column)
    return (
        value is not None
        and value is not state.info.get(_OPENED, {}).get(column)
        and column not in state.committed_state
    )


def _nothing(kind: Kind, value: Any) -> bool:
    """没有内容可封存：字典列哪怕是空的也要封（靠封装认出这是助理的行）。"""
    if value is None:
        return True
    if isinstance(kind, JsonSpec):
        return not isinstance(value, dict)
    if isinstance(kind, Blob):
        return isinstance(value, bytes) and not value
    return value in ("", [], {})


def _blank(kind: Kind, value: Any) -> Any:
    """读不出来时给调用方的值：字典只剩明文键，其余为该列的空值。"""
    if isinstance(kind, JsonSpec):
        return _keep(kind, value) if isinstance(value, dict) else {}
    if isinstance(kind, Blob):
        return None
    return copy.copy(kind.blank)


def _dropped(kind: Kind, value: Any) -> Any:
    """密钥销毁之后写入：内容丢弃，JSONB 列留下墓碑。"""
    if isinstance(kind, JsonSpec):
        return {**_keep(kind, value), SEALED: ""}
    if isinstance(kind, Whole):
        return {SEALED: ""}
    if isinstance(kind, Blob):
        return None
    return kind.blank


async def seal_values(
    session: AsyncSession, model: type, bot_id: uuid.UUID | None, values: dict[str, Any]
) -> dict[str, Any]:
    """把 `values` 里的内容列封存后返回新字典；普通机器人、没有内容要写时原样返回。

    首次为某个所有者写入时生成其数据密钥。密钥已销毁时不抛错：内容丢弃，其余列照常返回，
    调用方的状态机照走。主密钥解不开数据密钥时照抛 `DecryptError`，会话声明了
    `tolerate_undecryptable` 的同样丢弃内容。
    """
    spec = COLUMNS[model]
    touched = [c for c, kind in spec.items() if c in values and not _nothing(kind, values[c])]
    if not touched or bot_id is None:
        return values
    table = str(model.__tablename__)  # type: ignore[attr-defined]
    try:
        cipher = await _cipher(session, bot_id, create_key=True)
    except ContentLocked:
        log.warning("assistant_content_dropped", table=table, bot_id=str(bot_id))
        return {**values, **{c: _dropped(spec[c], values[c]) for c in touched}}
    except DecryptError:
        if not _tolerated(session, table, bot_id, None):
            raise
        return {**values, **{c: _dropped(spec[c], values[c]) for c in touched}}
    if cipher is None:
        return values
    return {
        **values,
        **{c: _seal(cipher, aad(table, c, bot_id), spec[c], values[c]) for c in touched},
    }


def keep_plain(
    session: AsyncSession, model: type, plain: dict[str, Any], stored: dict[str, Any]
) -> None:
    """Core 的 UPDATE 写了封存值之后调用：会话里已经加载的同一行换回明文。

    经会话执行的 `update(<模型>)` 会把写入的值同步到已加载的对象上——对助理的行那是密文，
    调用方不重取就读会读到封装。密文每次加密都不同，所以「对象上这一列等于刚写的密文」就是
    这条语句同步过去的。密钥已销毁时写下的是墓碑，换成该列的空值。普通机器人的行（没有封存
    任何列）直接返回。

    Args:
        plain: 交给 `seal_values` 之前的值
        stored: `seal_values` 返回的值
    """
    spec = COLUMNS[model]
    sealed_columns = [c for c in spec if c in stored and stored[c] is not plain.get(c)]
    if not sealed_columns:
        return
    for row in list(session.sync_session.identity_map.values()):
        if type(row) is not model:
            continue
        state: Any = sa_inspect(row)
        for column in sealed_columns:
            written = stored[column]
            if written is None or column not in state.dict or state.dict[column] != written:
                continue
            dropped = isinstance(written, dict) and written.get(SEALED) == ""
            kept = _blank(spec[column], plain[column]) if dropped else plain[column]
            set_committed_value(row, column, kept)
            state.info.setdefault(_OPENED, {})[column] = kept


async def open_row(session: AsyncSession, row: Any, *, viewer: uuid.UUID | None = None) -> bool:
    """把一行里封存的内容列还原成明文（不标脏）；返回内容是否可读。

    普通机器人的行、已经还原过的行原样返回。读不出来（密钥已销毁、墓碑、`viewer` 不是所有者）
    时不抛错：内容列置空，`is_locked(row)` 为真。主密钥配错、密文被改仍抛 `DecryptError`；
    会话声明了 `tolerate_undecryptable` 的按读不出来处理，`is_undecryptable(row)` 也为真。

    Args:
        viewer: 给了就只对所有者本人还原（接口输出正文时用）；不给按运行时对待，一律还原
    """
    spec = COLUMNS.get(type(row)) if row is not None else None
    if spec is None:
        return True
    state = sa_inspect(row)
    values = state.dict
    pending = [c for c, kind in spec.items() if _raw(state, c) and _looks_sealed(kind, values[c])]
    bot_id = values.get("bot_id")
    if bot_id is None or (not pending and _CIPHER not in state.info):
        return not state.info.get(_LOCKED, False)
    targets = pending
    table = str(row.__tablename__)
    undecryptable = False
    try:
        if viewer is not None and await owner_of(session, bot_id) not in (None, viewer):
            # 不是本人：连已经还原在内存里的也收回去。
            targets = [c for c in spec if c in values]
            raise ContentLocked("只有所有者本人可以读取")
        cipher = await _cipher(session, bot_id, create_key=False)
        if cipher is None:
            # 普通机器人的正文恰好长得像密文。
            return True
        opened = {c: _open(cipher, aad(table, c, bot_id), spec[c], values[c]) for c in pending}
    except ContentLocked:
        pass
    except DecryptError:
        identity = state.identity
        row_id = identity[0] if identity and len(identity) == 1 else identity
        if not _tolerated(session, table, bot_id, row_id):
            raise
        undecryptable = True
    else:
        for column, value in opened.items():
            set_committed_value(row, column, value)
            state.info.setdefault(_OPENED, {})[column] = value
        state.info[_CIPHER] = cipher
        state.info.pop(_LOCKED, None)
        state.info.pop(_UNDECRYPTABLE, None)
        return True
    for column in targets:
        set_committed_value(row, column, _blank(spec[column], values[column]))
    state.info.pop(_CIPHER, None)
    state.info.pop(_OPENED, None)
    state.info[_LOCKED] = True
    if undecryptable:
        state.info[_UNDECRYPTABLE] = True
    else:
        state.info.pop(_UNDECRYPTABLE, None)
    return False


async def open_rows(
    session: AsyncSession, rows: Iterable[Any], *, viewer: uuid.UUID | None = None
) -> None:
    for row in rows:
        await open_row(session, row, viewer=viewer)


async def open_value(
    session: AsyncSession,
    model: type,
    column: str,
    bot_id: uuid.UUID,
    value: Any,
    *,
    row_id: Any = None,
) -> Any:
    """只取了一列时还原这个值；读不出来返回该列的空值。

    解不开同 `open_row`：默认照抛，会话声明了 `tolerate_undecryptable` 的返回空值。

    Args:
        row_id: 这一列所在行的主键，只用于解不开时的日志
    """
    kind = COLUMNS[model][column]
    if not _looks_sealed(kind, value):
        return value
    table = str(model.__tablename__)  # type: ignore[attr-defined]
    try:
        cipher = await _cipher(session, bot_id, create_key=False)
        if cipher is None:
            return value
        return _open(cipher, aad(table, column, bot_id), kind, value)
    except ContentLocked:
        return _blank(kind, value)
    except DecryptError:
        if not _tolerated(session, table, bot_id, row_id):
            raise
        return _blank(kind, value)


def is_locked(row: Any) -> bool:
    """这一行的内容读不出来（已置空）：密钥已销毁，或在声明了容忍的会话里解不开。"""
    return row is not None and bool(sa_inspect(row).info.get(_LOCKED))


def is_undecryptable(row: Any) -> bool:
    """这一行读不出来是因为解不开（主密钥配错、密文被改），不是密钥已销毁。"""
    return row is not None and bool(sa_inspect(row).info.get(_UNDECRYPTABLE))


def is_opened(row: Any) -> bool:
    """这一行是助理的行，且内容已还原成明文。"""
    return row is not None and _CIPHER in sa_inspect(row).info


def _owner_sync(session: Session, bot_id: uuid.UUID) -> uuid.UUID | None:
    """`owner_of` 的同步版：给 flush 钩子用。缓存没有时走会话当前事务的同步连接查一次。"""
    now = time.monotonic()
    hit = _OWNERS.get(bot_id)
    if hit is not None and hit[1] > now:
        return hit[0]
    for pending in session.new:
        # 记录与它的内容行在同一次 flush 里新建：库里还查不到，也不进缓存。
        if isinstance(pending, Bot) and pending.id == bot_id:
            return pending.owner_id if pending.kind == "assistant" else None
    row = (
        session.connection().execute(select(Bot.kind, Bot.owner_id).where(Bot.id == bot_id)).first()
    )
    if row is None:
        return None
    owner: uuid.UUID | None = row[1] if row[0] == "assistant" else None
    _OWNERS[bot_id] = (owner, now + keys.KEY_TTL_SECONDS)
    return owner


def _changed(state: Any, spec: dict[str, Kind], *, new: bool) -> list[str]:
    """这次 flush 要写的、有内容的内容列。新建的行看有没有值，已有的行看属性历史。"""
    out: list[str] = []
    for column, kind in spec.items():
        if column not in state.dict or _nothing(kind, state.dict[column]):
            continue
        if new or state.attrs[column].history.has_changes():
            out.append(column)
    return out


def _from_envelope(state: Any, column: str, value: Any) -> bool:
    """要写的字典是不是从库里读出、尚未还原的封装改出来的（追加或删掉了明文键）。

    凭行的状态判断，不凭值的形状：这一列改动之前的值必须是库里读出的原样封装（不是还原过的
    明文），且新值带着同一段密文。
    """
    old = state.committed_state.get(column)
    return (
        isinstance(value, dict)
        and old is not state.info.get(_OPENED, {}).get(column)
        and is_sealed(old)
        and old[SEALED] != ""
        and value.get(SEALED) == old[SEALED]
    )


@event.listens_for(Session, "before_flush")
def _reseal(session: Session, flush_context: Any, instances: Any) -> None:
    """助理的行经 ORM 新建或改了内容列：落库前换成密文。

    按行的 `bot_id` 判断归属，不看行上有没有还原标记——新建的行、读出时内容为空的行、已锁定
    的行都要管。没有内容列要写的行直接跳过，不查库；归属与数据密钥先查进程内缓存，缓存没有
    才用当前事务的同步连接各查一次（只取用，不签发）。密钥取不到时内容丢弃：JSONB 留墓碑、
    文本写空，内存里同样置空并标为已锁定。
    """
    restore: list[tuple[Any, str, Any]] = []
    session.info[_RESTORE] = restore
    for rows, new in ((session.new, True), (session.dirty, False)):
        for row in rows:
            spec = COLUMNS.get(type(row))
            if spec is None:
                continue
            state = sa_inspect(row)
            columns = _changed(state, spec, new=new)
            if not columns:
                continue
            bot_id = row.bot_id
            owner = _owner_sync(session, bot_id) if bot_id is not None else None
            if owner is None:
                continue
            table = str(row.__tablename__)
            try:
                cipher: Cipher | None = keys.data_cipher_sync(session, master(), owner)
            except keys.KeyUnavailable:
                cipher = None
            dropped = False
            for column in columns:
                kind = spec[column]
                plain = state.dict[column]
                try:
                    if cipher is None:
                        raise ContentLocked("所有者的数据密钥不可用")
                    aad_ = aad(table, column, bot_id)
                    if isinstance(kind, JsonSpec) and _from_envelope(state, column, plain):
                        stored = seal_dict(cipher, aad_, plain, kind, envelope=True)
                        kept = open_dict(cipher, aad_, stored, kind)
                    else:
                        stored = _seal(cipher, aad_, kind, plain)
                        kept = plain
                except ContentLocked:
                    stored, kept, dropped = _dropped(kind, plain), _blank(kind, plain), True
                setattr(row, column, stored)
                restore.append((row, column, kept))
            if dropped:
                log.warning("assistant_content_dropped", table=table, bot_id=str(bot_id))
                state.info.pop(_CIPHER, None)
                state.info[_LOCKED] = True
            elif cipher is not None:
                state.info[_CIPHER] = cipher


@event.listens_for(Session, "after_flush_postexec")
def _unseal_after_flush(session: Session, flush_context: Any) -> None:
    for row, column, plain in session.info.pop(_RESTORE, []):
        set_committed_value(row, column, plain)
        sa_inspect(row).info.setdefault(_OPENED, {})[column] = plain
