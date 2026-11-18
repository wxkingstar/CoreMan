"""不托管的会话：所有者划出去的私聊与群，助理与平台都不读、不写。

两种范围（`assistant_settings.chat_scope`）：
- `all`：托管全部会话，`ignored_chats_enc` 里的除外；
- `listed`：只托管 `managed_chats_enc` 里的会话（另加所有者与助理的私聊），其余一律不托管。

拦在个人令牌的统一入口（`feishu_personal.service`），与写操作保险丝在同一处：盯消息、助理的
工具、审批通过后的执行、平台打的「处理中」表情都经过这里，以后新增的工具也绕不过去。

- 请求里点名了会话的（路径里的 chat_id、读历史的容器、发送对象、搜索的会话筛选）：点名的是
  不托管的会话就拒绝（`chat_not_managed`）。
- 请求里只有消息编号的（回复、转发、撤回、表情、置顶、已读、编辑、下载消息里的文件）：先以
  所有者身份取一次这条消息看它在哪个会话，再按上一条办。
- 读接口的应答里带会话编号的条目（会话列表、批量取消息、读历史、搜索）：不托管的整条去掉；
  取单条消息（合并转发）看最外层那条在哪个会话。
- 以 open_id 给某人发新消息：发之前不知道会落在哪个单聊，放行（所有者已在审批单上批过）。

授权的消息范围（`feishu_personal_grants.message_range`）是只群聊或只单聊时，另一类会话同样
不托管：飞书那边已经少申请了那一类的读权限，这里再按会话类型拦一道（以 open_id 发新消息
算单聊）。会话类型从会话列表的应答里直接读，别处以所有者身份查一次会话信息。

只对个人助理生效；普通员工的个人工具不受影响。所有者在「我的助理」页挑会话时要看到全部会话
的名字，那一处用 `bypass()`，只回会话编号、名字与类型，不读消息。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants.records import is_assistant
from coreman.core.db.models import AssistantSettings, Bot
from coreman.core.feishu_personal import endpoints
from coreman.core.feishu_personal.errors import PersonalError

ERROR = "chat_not_managed"

_BYPASS: ContextVar[bool] = ContextVar("assistant_chat_guard_bypass", default=False)
# 只带消息编号、要先查出会话的接口；取单条消息（im.get）自己就是查询，看应答。
_BY_MESSAGE = frozenset(
    {
        "im.reply",
        "im.forward",
        "im.recall",
        "im.reaction",
        "im.reaction_delete",
        "im.read_users",
        "im.edit",
        "im.resource",
    }
)
_KEYS = {id(endpoint): key for key, endpoint in endpoints.ENDPOINTS.items()}


KINDS = frozenset({"p2p", "group"})
# 消息范围 → 托管的会话类型。
RANGE_KINDS = {"all": KINDS, "group": frozenset({"group"}), "p2p": frozenset({"p2p"})}


@dataclass(frozen=True)
class Rules:
    listed: bool
    ignored: frozenset[str]
    managed: frozenset[str]
    kinds: frozenset[str] = KINDS

    def denies(self, chat_id: Any) -> bool:
        if not isinstance(chat_id, str) or not chat_id:
            return False
        return chat_id not in self.managed if self.listed else chat_id in self.ignored

    def denies_kind(self, kind: str | None) -> bool:
        """这一类会话不托管；类型不明时，只要范围有限制就当不托管。"""
        return self.kinds != KINDS and kind not in self.kinds

    @property
    def active(self) -> bool:
        return self.listed or bool(self.ignored) or self.kinds != KINDS


def kind_of_mode(mode: Any) -> str:
    """飞书会话的 `chat_mode` → `p2p` 或 `group`（话题群也算群）。"""
    return "p2p" if mode == "p2p" else "group"


def rules_of(row: AssistantSettings, message_range: str = "all") -> Rules:
    """`row` 的名单列要已还原；`message_range` 取自所有者的飞书授权。"""
    managed = set(row.managed_chats_enc or [])
    if row.chat_id:
        managed.add(row.chat_id)
    return Rules(
        listed=row.chat_scope == "listed",
        ignored=frozenset(row.ignored_chats_enc or []),
        managed=frozenset(managed),
        kinds=RANGE_KINDS.get(message_range, KINDS),
    )


@contextmanager
def bypass() -> Iterator[None]:
    """只给「我的助理」页列会话名用。"""
    token = _BYPASS.set(True)
    try:
        yield
    finally:
        _BYPASS.reset(token)


async def rules(session: AsyncSession, bot: Bot, message_range: str = "all") -> Rules | None:
    """这次请求要守的名单；不是个人助理、在挑会话、或者没有任何限制时返回 None。"""
    if _BYPASS.get() or not is_assistant(bot):
        return None
    row = await session.get(AssistantSettings, bot.id)
    if row is None:
        return None
    found = rules_of(row, message_range)
    return found if found.active else None


def key_of(endpoint: endpoints.Endpoint) -> str | None:
    return _KEYS.get(id(endpoint))


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def named_chats(key: str, path_values: dict[str, str], params: Any, json: Any) -> list[str]:
    """请求里直接点名的会话。"""
    params, json = _dict(params), _dict(json)
    found: list[str] = []
    if "chat_id" in path_values:
        found.append(path_values["chat_id"])
    if key == "im.history" and params.get("container_id_type") == "chat":
        found.append(str(params.get("container_id") or ""))
    if key in ("im.send", "im.forward") and params.get("receive_id_type") == "chat_id":
        found.append(str(json.get("receive_id") or ""))
    if key == "im.search":
        chats = _dict(json.get("filter")).get("chat_ids")
        if isinstance(chats, list):
            found.extend(str(chat) for chat in chats if chat)
    return [chat for chat in found if chat]


def named_messages(key: str, path_values: dict[str, str], json: Any) -> list[str]:
    """请求里只给了消息编号、要先查出在哪个会话的那些消息。"""
    json = _dict(json)
    if key in _BY_MESSAGE:
        return [path_values["message_id"]] if path_values.get("message_id") else []
    if key == "im.pin":
        found = json.get("message_id")
        return [found] if isinstance(found, str) and found else []
    if key == "im.reactions":
        queries = json.get("queries")
        if not isinstance(queries, list):
            return []
        ids = (_dict(query).get("message_id") for query in queries)
        return [one for one in ids if isinstance(one, str) and one]
    return []


def _chat_of(item: Any) -> Any:
    item = _dict(item)
    return item.get("chat_id") or _dict(item.get("meta_data")).get("chat_id")


Resolve = Callable[[str], Awaitable[Any]]
KindOf = Callable[[str], Awaitable[str | None]]


async def _barred(rules: Rules, chat: Any, kind_of: KindOf, kind: str | None = None) -> bool:
    """这个会话不托管：按名单，或（范围有限制时）按会话类型。"""
    if rules.denies(chat):
        return True
    if rules.kinds == KINDS:
        return False
    if kind is None:
        kind = await kind_of(chat) if isinstance(chat, str) and chat else None
    return rules.denies_kind(kind)


async def screen(key: str, rules: Rules, data: dict[str, Any], kind_of: KindOf) -> dict[str, Any]:
    """读接口的应答去掉不托管的会话；取单条消息落在不托管的会话里时拒绝。

    Args:
        kind_of: 以所有者身份查会话类型（查不到返回 None）
    """
    if not key.startswith("im."):
        return data
    items = data.get("items")
    if not isinstance(items, list):
        return data
    if key == "im.get":
        if items and await _barred(rules, _chat_of(items[0]), kind_of):
            raise PersonalError(ERROR)
        return data
    listing = key in ("im.chats", "im.chat_search")
    kept = []
    for item in items:
        mode = _dict(item).get("chat_mode") if listing else None
        kind = kind_of_mode(mode) if mode else None
        chat = _chat_of(item)
        if chat is not None and await _barred(rules, chat, kind_of, kind):
            continue
        kept.append(item)
    return {**data, "items": kept}


async def check(
    endpoint: endpoints.Endpoint,
    path: str,
    rules: Rules,
    params: Any,
    json: Any,
    resolve: Resolve,
    kind_of: KindOf,
) -> None:
    """请求点名的会话、或所给消息所在的会话不托管时拒绝。

    Args:
        resolve: 以所有者身份取一条消息、返回它所在会话的编号（取不到返回 None）
        kind_of: 以所有者身份查会话类型（查不到返回 None）
    """
    key = key_of(endpoint)
    if key is None or not key.startswith("im."):
        return
    found = endpoint.pattern.match(path)
    values = found.groupdict() if found is not None else {}
    for chat in named_chats(key, values, params, json):
        if await _barred(rules, chat, kind_of):
            raise PersonalError(ERROR)
    if (
        key in ("im.send", "im.forward")
        and _dict(params).get("receive_id_type") in ("open_id", "user_id")
        and rules.denies_kind("p2p")
    ):
        # 发给某个人就是单聊。
        raise PersonalError(ERROR)
    for message_id in named_messages(key, values, json):
        chat = await resolve(message_id)
        # 查不到在哪个会话：有名单或类型限制时宁可不做；只按排除名单时照原请求去调，由飞书
        # 自己报错。
        if chat is None:
            if rules.listed or rules.kinds != KINDS:
                raise PersonalError(ERROR)
            continue
        if await _barred(rules, chat, kind_of):
            raise PersonalError(ERROR)
