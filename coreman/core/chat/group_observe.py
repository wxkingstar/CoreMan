"""飞书群聊旁听：入站时记账与防抖排队、群消息摘录、静音。

开了旁听的机器人能收到群里所有消息。没 @ 它的消息不建对话任务（群聊的会话键就是 chat_id，
每条都建任务会把正在进行的回复替代掉），只更新这里的状态，再排一个延后的 `group_observe`
任务：群里安静一会儿再判断要不要开口。同一个群连着来很多消息时，只有最后那条的任务会真正
判断，其余的到点发现自己不是最新的就退出（一直有人说话时，最长等 `max_wait` 也会判断一次）。

别的机器人发的 Card JSON 2.0 卡片，事件里只有一句「请升级至最新版本客户端」。摘录时先按
message_id 查发出消息的存档（同平台 AI 员工发的都在），查不到的由 `read_cards` 走官方
「获取指定消息的内容」接口重读；读不到就保留那句占位。重读要发 HTTP 请求，不能在持有行锁
时做：判断任务放开锁之后读，对话回合在开轮之前读好、开轮时用 `known_cards` 取。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bots.secrets import CREDENTIALS_AAD, decrypt_json
from coreman.core.bus.tasks import NewTask, enqueue
from coreman.core.crypto import Cipher
from coreman.core.db.models import (
    Bot,
    FeishuSentMessage,
    GroupObserveState,
    InboundEvent,
    User,
    UserIdentity,
)
from coreman.core.logging import get_logger
from coreman.core.platforms.feishu import FeishuClient, FeishuError
from coreman.core.platforms.feishu_content import UPGRADE_PLACEHOLDER, mention_names, render
from coreman.core.wecom.messages import InboundMessage

# 入站时先按默认安静时长排队，worker 到点再按管理台设置决定是否再等。
FIRST_DELAY_SECONDS = 3
# 判断与插话带的最近群消息：最多这么多条，且只取这段时间内的。
CONTEXT_LIMIT = 20
CONTEXT_WINDOW = timedelta(minutes=30)
TEXT_LIMIT = 1000
MUTE = timedelta(hours=2)
# 插话回合跑了这么久还没说完，先在群里回一句「我看一下」，免得大家以为没人管、各自去做。
INTERJECT_ACK_SECONDS = 10
# 插话只该是快速回答；超过这么久就停下，改为认领：说明要花时间，请对方 @ 它再接着做。
INTERJECT_LIMIT_SECONDS = 90
# 插话回合里 agent 判断不该说话时输出的标记；收尾时不发送。
NO_REPLY = "<NO_REPLY>"
# 存档里没有的卡片走飞书接口重读：每次取群聊记录最多读最近这么多张，合计不超过这么久。
CARD_READ_LIMIT = 5
CARD_READ_TIMEOUT_SECONDS = 6
# 读到的正文记这么久：流式卡片可能还在写，过后再读一次；同平台员工写完后存档里就有了。
CARD_TTL_SECONDS = 120
# 读不到的（多半是应用没开通读群消息的权限）隔这么久再试。
CARD_RETRY_SECONDS = 60
_CARDS_MAX = 2000
# (机器人, 群, 消息) → (到期时间, 正文；None = 读不到)。
_CARDS: OrderedDict[tuple[str, str, str], tuple[float, str | None]] = OrderedDict()
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")

log = get_logger(__name__)


def observing(bot: Bot) -> bool:
    return bot.platform == "feishu" and bot.group_observe != "off"


def task_session_key(chat_id: str) -> str:
    """判断任务的会话键：不能和群聊对话共用 chat_id，否则对话的替代会把判断任务取消。"""
    return f"observe:{chat_id}"


async def _addressed_to_colleague(
    session: AsyncSession, bot_id: uuid.UUID, message: InboundMessage
) -> bool:
    """调度员收到的这条消息是不是 @ 了某位 AI 员工：那位员工自己会回答，调度员旁观就好。

    飞书的 open_id 按应用区分，调度员拿到的对不上员工的记录，所以按名字认。
    """
    event = message.raw.get("event") or {}
    names = set(mention_names((event.get("message") or {}).get("mentions")).values())
    if not names or await session.scalar(select(Bot.group_role).where(Bot.id == bot_id)) != (
        "dispatcher"
    ):
        return False
    found = await session.scalar(
        select(Bot.id)
        .where(
            Bot.platform == "feishu",
            Bot.enabled.is_(True),
            Bot.group_role == "member",
            Bot.name.in_(names),
        )
        .limit(1)
    )
    return found is not None


async def note(
    session: AsyncSession, bot_id: uuid.UUID, message: InboundMessage, event_id: int
) -> None:
    """一条群消息落库后记账：人说话清零机器人连发计数；人没 @ 它的消息排一个判断任务。

    @ 机器人的消息照常走对话，清掉待判断：回答完不再主动补一句。
    """
    now = datetime.now(UTC)
    human = message.sender.sender_type != "bot"
    # 别的机器人发的消息（插话、答复、AI 员工之间求助）只记上下文，不排判断：
    # 该不该接话只看人说了什么；人那条还没判断的照旧等着。
    # 调度员收到 @ 了 AI 员工的消息也一样只记上下文：人已经点名了，不用它安排。
    if not message.mentions_bot and (
        not human or await _addressed_to_colleague(session, bot_id, message)
    ):
        stmt = insert(GroupObserveState).values(
            bot_id=bot_id,
            chat_id=message.chat_id,
            bot_streak=0 if human else 1,
            last_message_at=now,
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[GroupObserveState.bot_id, GroupObserveState.chat_id],
                set_={
                    "bot_streak": 0 if human else GroupObserveState.__table__.c.bot_streak + 1,
                    "last_message_at": now,
                },
            )
        )
        return
    pending = not message.mentions_bot
    # 用数据库的 now()：和这条消息的 received_at 同一事务、同一个值，判断时按它认出这一批。
    values: dict[str, Any] = {
        "bot_id": bot_id,
        "chat_id": message.chat_id,
        "bot_streak": 0 if human else 1,
        "pending_since": func.now() if pending else None,
        "last_event_id": event_id if pending else None,
        "last_message_at": now,
    }
    stmt = insert(GroupObserveState).values(**values)
    table = GroupObserveState.__table__
    stmt = stmt.on_conflict_do_update(
        index_elements=[GroupObserveState.bot_id, GroupObserveState.chat_id],
        set_={
            "bot_streak": 0 if human else table.c.bot_streak + 1,
            "pending_since": func.coalesce(table.c.pending_since, func.now()) if pending else None,
            "last_event_id": event_id if pending else table.c.last_event_id,
            "last_message_at": now,
        },
    )
    await session.execute(stmt)
    if not pending:
        return
    await enqueue(
        session,
        NewTask(
            bot_id=bot_id,
            kind="group_observe",
            lane="fast",
            payload={"chat_id": message.chat_id, "event_id": event_id},
            session_key=task_session_key(message.chat_id),
            inbound_event_id=event_id,
            run_after=now + timedelta(seconds=FIRST_DELAY_SECONDS),
            dedupe_key=f"observe:{event_id}",
        ),
    )


async def set_muted(session: AsyncSession, bot_id: uuid.UUID, chat_id: str, muted: bool) -> None:
    until = datetime.now(UTC) + MUTE if muted else None
    stmt = insert(GroupObserveState).values(bot_id=bot_id, chat_id=chat_id, muted_until=until)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[GroupObserveState.bot_id, GroupObserveState.chat_id],
            set_={"muted_until": until, "pending_since": None},
        )
    )


async def mark_spoke(session: AsyncSession, bot_id: uuid.UUID, chat_id: str) -> None:
    """主动插话发出去了：自己的消息也算一条机器人消息（飞书不会把它推回来）。"""
    await session.execute(
        update(GroupObserveState)
        .where(GroupObserveState.bot_id == bot_id, GroupObserveState.chat_id == chat_id)
        .values(
            bot_streak=GroupObserveState.bot_streak + 1,
            last_interjected_at=datetime.now(UTC),
        )
    )


@dataclass(frozen=True)
class Line:
    """群消息摘录的一行。`event_id` 只有别人发的消息才有，用来定插话回复哪一条。

    `new`：这一批待判断的消息（上次判断之后别人新发的）。
    `unread_card`：别的机器人发的卡片还没读到正文（`text` 是事件里的占位）时，它的消息 ID。
    """

    at: datetime
    sender: str
    sender_kind: str  # human / bot / self
    text: str
    event_id: int | None = None
    new: bool = False
    unread_card: str | None = None

    def out(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "sender": self.sender,
            "sender_kind": self.sender_kind,
            "text": self.text,
        }
        if self.new:
            out["new"] = True
        return out


async def transcript(
    session: AsyncSession,
    bot: Bot,
    chat_id: str,
    *,
    now: datetime | None = None,
    new_since: datetime | None = None,
) -> list[Line]:
    """最近的群消息（别人发的入站消息 + 本机器人自己发的），按时间排好。

    发言人用通讯录名字，认不出的按出场顺序叫「同事 N」，其他机器人叫「机器人 N」。
    给了 `new_since`（这一批的 `pending_since`）时，此后别人发的消息标为 `new`。
    别的机器人发的卡片事件里没有正文：存档里有的直接换成正文，没有的标上 `unread_card`，
    由调用方用 `read_cards`（不持锁时）或 `known_cards` 补上。
    """
    since = (now or datetime.now(UTC)) - CONTEXT_WINDOW
    inbound = (
        await session.execute(
            select(InboundEvent.id, InboundEvent.received_at, InboundEvent.payload)
            .where(
                InboundEvent.bot_id == bot.id,
                InboundEvent.chat_id == chat_id,
                InboundEvent.kind == "message",
                InboundEvent.received_at >= since,
            )
            .order_by(InboundEvent.id.desc())
            .limit(CONTEXT_LIMIT)
        )
    ).all()
    sent = (
        await session.execute(
            select(FeishuSentMessage.created_at, FeishuSentMessage.text)
            .where(
                FeishuSentMessage.bot_id == bot.id,
                FeishuSentMessage.chat_id == chat_id,
                FeishuSentMessage.created_at >= since,
            )
            .order_by(FeishuSentMessage.created_at.desc())
            .limit(CONTEXT_LIMIT)
        )
    ).all()
    # open_id 按应用区分，通讯录里存的是同步应用的 open_id，对不上机器人自己的应用；
    # user_id 全企业一致，按它认人。
    user_ids = {
        str(uid)
        for _, _, payload in inbound
        if (uid := ((payload or {}).get("sender") or {}).get("platform_user_id"))
    }
    names: dict[str, str] = {}
    if user_ids:
        rows = await session.execute(
            select(UserIdentity.platform_user_id, User.display_name)
            .join(User, User.id == UserIdentity.user_id)
            .where(UserIdentity.platform == "feishu", UserIdentity.platform_user_id.in_(user_ids))
        )
        names = {str(uid): str(name) for uid, name in rows.all() if uid and name}
    cards = {
        int(event_id): mid for event_id, _, payload in inbound if (mid := _unread_card(payload))
    }
    archived = await _archived_cards(session, chat_id, set(cards.values()))
    aliases: dict[str, str] = {}
    lines: list[Line] = []
    for event_id, received_at, payload in reversed(inbound):
        payload = payload or {}
        sender = payload.get("sender") or {}
        kind = "bot" if sender.get("sender_type") == "bot" else "human"
        key = str(sender.get("platform_user_id") or sender.get("open_id") or event_id)
        if key in names:
            label = names[key]
        else:
            if key not in aliases:
                prefix = "机器人" if kind == "bot" else "同事"
                count = sum(1 for alias in aliases.values() if alias.startswith(prefix))
                aliases[key] = f"{prefix} {count + 1}"
            label = aliases[key]
        card = cards.get(int(event_id))
        text = archived.get(card, "") if card else ""
        if text:
            card = None
        else:
            text = _message_text(payload)
        if text:
            new = new_since is not None and received_at >= new_since
            lines.append(
                Line(received_at, label, kind, text[:TEXT_LIMIT], int(event_id), new, card)
            )
    for created_at, text in reversed(sent):
        if text:
            lines.append(Line(created_at, bot.name, "self", str(text)[:TEXT_LIMIT]))
    lines.sort(key=lambda line: line.at)
    return lines[-CONTEXT_LIMIT:]


def _event_message(payload: dict[str, Any]) -> dict[str, Any]:
    message = ((payload.get("raw") or {}).get("event") or {}).get("message")
    return message if isinstance(message, dict) else {}


def _unread_card(payload: dict[str, Any] | None) -> str | None:
    """别的机器人发的卡片、事件原文里读不出正文（Card JSON 2.0 只给占位）：返回它的消息 ID。"""
    payload = payload or {}
    if (payload.get("sender") or {}).get("sender_type") != "bot":
        return None
    message = _event_message(payload)
    if message.get("message_type") != "interactive":
        return None
    content = message.get("content")
    if not _degraded(content) and render("interactive", content) is not None:
        return None
    mid = message.get("message_id")
    return mid if isinstance(mid, str) and _MESSAGE_ID.fullmatch(mid) else None


def _degraded(content: object) -> bool:
    """卡片内容是 Card JSON 2.0 的降级骨架：某一行里有那句占位。

    真实骨架里占位旁边还有头像图片，卡片有标题时标题也在，渲染出来不是空的，不能按
    「渲染不出文字」来认。
    """
    if isinstance(content, str):
        if len(content) > 256_000:
            return False
        try:
            content = json.loads(content)
        except ValueError:
            return False
    rows = content.get("elements") if isinstance(content, dict) else None
    return any(
        isinstance(node, dict) and node.get("text") == UPGRADE_PLACEHOLDER
        for row in (rows if isinstance(rows, list) else [])[:200]
        for node in (row if isinstance(row, list) else [])[:100]
    )


async def _archived_cards(
    session: AsyncSession, chat_id: str, message_ids: set[str]
) -> dict[str, str]:
    """同平台 AI 员工在这个群发出的消息的正文存档（发出时记下的原文）。

    个人助理的存档按所有者加密，不替别的机器人解开：它们的卡片和平台外的机器人一样走接口重读。
    """
    if not message_ids:
        return {}
    rows = await session.execute(
        select(FeishuSentMessage.message_id, FeishuSentMessage.text)
        .join(Bot, Bot.id == FeishuSentMessage.bot_id)
        .where(
            FeishuSentMessage.message_id.in_(message_ids),
            FeishuSentMessage.chat_id == chat_id,
            Bot.kind == "standard",
        )
    )
    return {str(mid): text for mid, text in rows.all() if isinstance(text, str) and text.strip()}


async def read_cards(lines: list[Line], bot: Bot, chat_id: str, cipher: Cipher) -> list[Line]:
    """把 `unread_card` 的行换成从飞书读回的卡片正文；读不到的原样保留（占位），从不报错。

    会发 HTTP 请求：调用方不要在持有行锁时调。每次最多读最近 `CARD_READ_LIMIT` 张、合计
    `CARD_READ_TIMEOUT_SECONDS` 秒，读过的（含读不到的）短时间内不重读。
    """
    wanted = [line.unread_card for line in lines if line.unread_card]
    if not wanted:
        return lines
    now = time.monotonic()
    missing = [
        mid
        for mid in dict.fromkeys(reversed(wanted))
        if (cached := _CARDS.get((str(bot.id), chat_id, mid))) is None or cached[0] <= now
    ]
    for mid, text in (await _fetch_cards(bot, chat_id, cipher, missing[:CARD_READ_LIMIT])).items():
        ttl = CARD_TTL_SECONDS if text else CARD_RETRY_SECONDS
        _CARDS[(str(bot.id), chat_id, mid)] = (time.monotonic() + ttl, text)
    while len(_CARDS) > _CARDS_MAX:
        _CARDS.popitem(last=False)
    return known_cards(lines, bot, chat_id)


def known_cards(lines: list[Line], bot: Bot, chat_id: str) -> list[Line]:
    """同 `read_cards`，但只用已经读到的正文，不发请求：持有行锁的地方用它。"""
    now = time.monotonic()
    out: list[Line] = []
    for line in lines:
        cached = _CARDS.get((str(bot.id), chat_id, line.unread_card or ""))
        if cached is not None and cached[0] > now and cached[1]:
            line = replace(line, text=cached[1][:TEXT_LIMIT], unread_card=None)
        out.append(line)
    return out


async def _fetch_cards(
    bot: Bot, chat_id: str, cipher: Cipher, message_ids: list[str]
) -> dict[str, str | None]:
    if not message_ids:
        return {}
    try:
        credentials = decrypt_json(cipher, bot.credentials_enc, CREDENTIALS_AAD)
        app_id, secret = credentials.get("app_id"), credentials.get("app_secret")
    except Exception:  # noqa: BLE001 凭证坏了只是读不到卡片，群聊记录照常给
        app_id = secret = None
    if not isinstance(app_id, str) or not app_id or not isinstance(secret, str) or not secret:
        return {}
    found: dict[str, str | None] = dict.fromkeys(message_ids)

    async def one(client: FeishuClient, mid: str) -> None:
        try:
            body = await client.call(
                "GET",
                f"/open-apis/im/v1/messages/{mid}",
                # 不带这个参数，Card JSON 2.0 卡片读回来还是那句占位。
                params={"card_msg_content_type": "user_card_content"},
            )
            found[mid] = _card_text(body, chat_id, mid)
        except FeishuError as exc:
            # 群里读别人的消息需要应用开通 im:message.group_msg；缺了是 230027，见 docs/feishu.md。
            log.info("group_observe_card_read_failed", code=exc.code)
        except Exception as exc:  # noqa: BLE001 网络、超时和畸形响应统一降级
            log.info("group_observe_card_read_failed", error=type(exc).__name__)

    client = FeishuClient(app_id, secret)
    try:
        async with asyncio.timeout(CARD_READ_TIMEOUT_SECONDS):
            await asyncio.gather(*(one(client, mid) for mid in message_ids))
    except TimeoutError:
        log.info("group_observe_card_read_failed", error="TimeoutError")
    finally:
        await client.aclose()
    return found


def _card_text(body: object, chat_id: str, message_id: str) -> str | None:
    """接口返回里那张卡片的正文；不是这个群的、撤回的、仍然读不出正文的都不认。"""
    data = body.get("data") if isinstance(body, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    item = next(
        (
            i
            for i in (items if isinstance(items, list) else [])[:50]
            if isinstance(i, dict) and i.get("message_id") == message_id
        ),
        None,
    )
    if item is None or item.get("chat_id") != chat_id or item.get("deleted"):
        return None
    if item.get("msg_type") != "interactive":
        return None
    inner = item.get("body")
    content = inner.get("content") if isinstance(inner, dict) else None
    if _degraded(content):
        return None
    rendered = render("interactive", content, names=mention_names(item.get("mentions")))
    return (rendered.text() if rendered else "") or None


def _message_text(payload: dict[str, Any]) -> str:
    message = _event_message(payload)
    rendered = render(
        str(message.get("message_type") or ""),
        message.get("content"),
        names=mention_names(message.get("mentions")),
    )
    if rendered is not None:
        return rendered.text().strip()
    parts = [p for p in payload.get("parts") or [] if isinstance(p, dict)]
    return "\n".join(str(p.get("text") or "") for p in parts if p.get("type") == "text").strip()


def transcript_note(lines: list[Line], *, interject: bool = False) -> str:
    """交给 agent 的本轮块：最近群聊记录。只有开了旁听的机器人才有（以前只看得到 @ 它的话）。

    记录只是背景：本轮要处理的是本轮块之后那条消息（调用方要先把它从 `lines` 里去掉）。
    @ 它的回合尤其要说清楚，否则模型会把记录里别人更早的问题当成本轮需求一起做掉。
    """
    if not lines:
        return ""
    body = "\n".join(
        f"- {'【新】' if line.new else ''}{line.sender}"
        f"{'（你）' if line.sender_kind == 'self' else ''}"
        f"{'（机器人）' if line.sender_kind == 'bot' else ''}: {line.text}"
        for line in lines
    )
    if any(line.new for line in lines):
        # 插话或调度回合，判断的是好几条新消息（例如它忙着回复上一条时群里陆续发的）。
        head = (
            "这个群在本轮消息之前的聊天记录（从早到晚）。标【新】的和本轮块之后那条消息，"
            "是你上次看群之后新来的一批（可能是你忙着回复别的消息时陆续发的，你那时的回复"
            "没看到它们）：这一批里需要你回应的，在这一轮里一并处理，几件事就分别说清对应哪条；"
            "记录里其他内容用来理解上下文，不要回应："
        )
    elif interject:
        head = (
            "这个群在本轮消息之前的聊天记录（从早到晚）。本轮要回应的是本轮块之后那条消息"
            "所在的话题；记录用来理解这个话题，其他话题不要回应："
        )
    else:
        head = (
            "这个群在本轮消息之前的聊天记录（从早到晚，只作背景参考）。本轮只处理本轮块之后"
            "当前发言人发来的这条消息；记录里更早的问题和请求不是本轮任务，不要去处理或回答，"
            "除非这条消息是在接着记录里的内容说（例如「上面那个」「刚才说的」「你看下」，"
            "这时指的通常是记录里最近的相关消息）："
        )
    return head + "\n" + body


INTERJECT_NOTE = (
    "这一轮没有人 @ 你：是你看了群聊记录后判断需要你，主动加入讨论。像一位旁听的同事那样，"
    "只答不做：可以做只读的查询来回答问题，然后只说结论或关键信息，一两句话；不寒暄，"
    "不复述别人的话，不追问无关细节，需要展开就等同事追问或 @ 你。"
    "不要做任何有后果的事：不改数据、不提交或发布、不建定时任务、不索要凭证、不找同事求助、"
    "不开始要花几分钟的工作。确实需要你动手时，用一句话认领并说明要做什么，"
    "例如「这个我可以处理：把这 3 笔订单取消。需要的话 @我 确认就开始」，然后停下，"
    "等对方 @ 你再做。"
    f"如果看完上下文发现其实不需要你说话，只输出 {NO_REPLY}，不要输出任何其他内容。"
)
