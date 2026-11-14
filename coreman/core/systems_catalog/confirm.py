"""Writes through the platform proxy wait for the member's confirmation.

`systems_call` on a `write`/`destructive` operation only stages the checked request. All writes a
turn stages form one batch; when the turn ends the member gets one card listing them, rendered
from the staged requests (the model's own explanation is shown separately, as unverified text).
Confirming runs exactly what was staged, as the member, without the model in between (see
`worker/systems_write_cards.py`). Turns nobody can confirm on the spot stage nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.bus import outbox
from coreman.core.crypto import Cipher
from coreman.core.db.models import Bot, InboundEvent, InteractionState, Task
from coreman.core.systems_catalog.context import Context
from coreman.core.systems_catalog.proxy import Call, Prepared, record
from coreman.core.timeutils import utcnow

KIND = "systems_write"
CARD_EVENT = "systems_write"
# Task payload key of the turn that continues after the card was answered.
RESULT_KEY = "systems_write_result"
TTL = timedelta(minutes=10)
MAX_ITEMS = 20
MAX_VALUE = 500
# Items after this many characters are listed by operation and path only.
CARD_TEXT_MAX = 12_000
# The summary at the top of the card: short values, the first few changes of each item.
SHOW_MAX = 80
SHOW_CHANGES = 3
_HIDDEN = ("Cc", "Cf", "Zl", "Zp")
DETAIL_TITLE = "查看详情（实际发出的请求）"
AAD = "systems_write.v1"

PENDING_HINT = (
    "已加入本轮的待确认列表，没有执行。本轮还要做的写操作继续提交；本轮结束时平台给提问人发一张"
    "确认卡，点确认后才执行。告诉用户需要在卡片上确认，不要说已经完成。"
)
UNAVAILABLE = (
    "这一轮无人能当场确认（定时任务、AI 员工之间的协作、卡片按钮触发的回合、非飞书会话等），"
    "不执行写操作。告诉用户应该怎么做，或请提问人直接发一条消息说明要做的事。"
)
BATCH_FULL = (
    f"一次最多确认 {MAX_ITEMS} 个写操作，这一个没有加入。更多的修改请让用户在业务系统里用批量"
    "导入或批量编辑完成，不要拆成多轮逐条提交。"
)


def _aad(task_id: int) -> str:
    return f"{AAD}:{task_id}"


def seal(cipher: Cipher, task_id: int, items: list[dict[str, Any]]) -> str:
    """Staged requests may carry business data: kept encrypted, bound to the turn."""
    return cipher.encrypt(json.dumps(items, ensure_ascii=False), _aad(task_id))


def unseal(cipher: Cipher, task_id: int, state: dict[str, Any]) -> list[dict[str, Any]]:
    items = json.loads(cipher.decrypt(str(state["enc"]), _aad(task_id)))
    assert isinstance(items, list)
    return items


def op_digest(op: dict[str, Any]) -> str:
    """What the member confirms: a catalog refresh that changes any of this voids the card."""
    basis = {k: op.get(k) for k in ("method", "path", "risk", "validate")}
    return hashlib.sha256(
        json.dumps(basis, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def _digest(args: Call) -> str:
    basis = [args.system, args.operation_id, args.path_params, args.query, args.body]
    return hashlib.sha256(
        json.dumps(basis, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:32]


async def confirmable_origin(
    session: AsyncSession, task_id: int, user_id: uuid.UUID
) -> tuple[str, str] | None:
    """(chat_id, chat_type) when the member typed this turn's message in a Feishu chat and can
    click a card there; None for turns nobody can confirm on the spot. Locks the task row so a
    turn's proposals are staged one at a time."""
    from coreman.core.feishu_personal.policy import verified_origin_scope
    from coreman.core.personal_schedules import verified_group_origin

    task = await session.get(Task, task_id, with_for_update=True, populate_existing=True)
    if task is None or task.kind != "chat" or task.inbound_event_id is None:
        return None
    payload = task.payload or {}
    # Interjections, dispatch, alerts, API tasks and collaboration turns: no asker at the keyboard.
    if any(payload.get(key) for key in ("origin", "collaboration_id", "collaboration_phase")):
        return None
    bot = await session.get(Bot, task.bot_id)
    event = await session.get(InboundEvent, task.inbound_event_id)
    if bot is None or bot.platform != "feishu" or event is None or event.kind != "message":
        return None
    try:
        if event.chat_type == "group":
            group = await verified_group_origin(session, task, str(user_id))
            return group.chat_id, "group"
        if event.chat_type == "single":
            scope = await verified_origin_scope(session, task, str(user_id))
            return scope.chat_id, "single"
    except ValueError:
        return None
    return None


async def _batch(
    session: AsyncSession, bot_id: Any, task_id: int, *, kind: str = KIND
) -> InteractionState | None:
    """This turn's batch, locked; `kind` lets the cloud proxy's card reuse it."""
    state: InteractionState | None = await session.scalar(
        select(InteractionState)
        .where(
            InteractionState.kind == kind,
            InteractionState.bot_id == bot_id,
            InteractionState.scope_key == str(task_id),
        )
        .with_for_update()
    )
    return state


async def propose(ctx: Context, p: Prepared, args: Call) -> dict[str, Any]:
    """Stage a checked write into this turn's batch."""
    session, member = ctx.session, ctx.member
    assert member.task_id is not None
    origin = await confirmable_origin(session, member.task_id, member.user.id)
    if origin is None:
        await session.commit()
        return {"error": "confirmation_unavailable", "message": UNAVAILABLE}
    chat_id, chat_type = origin
    state = await _batch(session, member.bot_id, member.task_id)
    if state is not None and (state.status != "open" or state.state.get("sent")):
        await session.commit()
        return {
            "error": "confirmation_closed",
            "message": "本轮的确认卡已经发出，不能再追加写操作。请用户另发一条消息再做。",
        }
    cipher = ctx.issuance.cipher
    items = unseal(cipher, member.task_id, state.state) if state is not None else []
    digest = _digest(args)
    if any(item["digest"] == digest for item in items):
        await session.commit()
        return {"status": "pending_confirmation", "pending": len(items), "hint": PENDING_HINT}
    if len(items) >= MAX_ITEMS:
        await session.commit()
        return {"error": "confirmation_batch_full", "message": BATCH_FULL}
    items.append(
        {
            "system": p.system.key,
            "system_name": p.system.name,
            "operation_id": p.operation_id,
            "summary": p.op.get("summary") or "",
            "method": p.op["method"],
            "path": p.op["path"],
            "risk": p.op["risk"],
            "op_digest": op_digest(p.op),
            "path_params": args.path_params,
            "query": args.query,
            "body": args.body,
            "note": args.note.model_dump(exclude_none=True) if args.note else None,
            # Fixed now, so running the item twice reaches the system with the same key.
            "key": str(uuid.uuid4()),
            "digest": digest,
        }
    )
    sealed = {
        "origin_task_id": member.task_id,
        "user_id": str(member.user.id),
        "chat_id": chat_id,
        "chat_type": chat_type,
        "count": len(items),
        "enc": seal(cipher, member.task_id, items),
    }
    if state is None:
        session.add(
            InteractionState(
                bot_id=member.bot_id,
                kind=KIND,
                scope_key=str(member.task_id),
                state=sealed,
                status="open",
                # Sent cards get their own deadline; an unsent batch outlives its turn only briefly.
                expires_at=utcnow() + timedelta(seconds=ctx.token_ttl) + TTL,
            )
        )
    else:
        state.state = sealed
    await record(ctx, p.system, p.operation_id, p.op, "proposed")
    return {"status": "pending_confirmation", "pending": len(items), "hint": PENDING_HINT}


def _value(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text[:MAX_VALUE] + ("…" if len(text) > MAX_VALUE else "")


def _plain(value: Any, limit: int = SHOW_MAX) -> str:
    """A value for the summary: one line without control or format characters, so it can neither
    forge another line nor flip the text direction; strings are shown without quotes."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in _HIDDEN)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    # Cut in the middle: the end of a link or a name often matters as much as its start.
    tail = limit // 3
    return text[: limit - tail - 1] + "…" + text[-tail:]


def title(item: dict[str, Any]) -> str:
    """The catalog's name for the operation, without the technical remark in brackets."""
    name = str(item.get("summary") or item["operation_id"])
    short = re.split(r"[（(]", name, maxsplit=1)[0].strip()
    return _plain(short or name, 40)


def headline(index: int, item: dict[str, Any], *, numbered: bool = True) -> str:
    mark = "（删除后无法恢复）" if item["risk"] == "destructive" else ""
    return (f"{index}. " if numbered else "") + title(item) + mark


def _change(change: dict[str, Any]) -> str | None:
    field = _plain(change.get("field"), 30)
    before, after = change.get("before"), change.get("after")
    if before is None and after is None:
        return None
    if before is None:
        return f"{field}：{_plain(after)}"
    if after is None:
        return f"{field}：原为 {_plain(before)}"
    return f"{field}：{_plain(before)} → {_plain(after)}"


def _summary_lines(index: int, item: dict[str, Any], *, numbered: bool) -> list[str]:
    """What the member decides on: the operation, the object and the values that change."""
    # A single item's name is the card title already; only its warning is repeated.
    if numbered:
        lines = [headline(index, item)]
    else:
        lines = ["⚠️ 删除后无法恢复"] if item["risk"] == "destructive" else []
    indent = "   " if numbered else ""
    note = item.get("note") or {}
    if note.get("target"):
        lines.append(f"{indent}对象：{_plain(note['target'])}")
    changes = [line for change in note.get("changes") or [] if (line := _change(change))]
    lines += [indent + line for line in changes[:SHOW_CHANGES]]
    if len(changes) > SHOW_CHANGES:
        lines.append(f"{indent}…另有 {len(changes) - SHOW_CHANGES} 项改动，见详情")
    if not note:
        params = item.get("path_params") or {}
        lines += [f"{indent}{_plain(k, 30)}：{_plain(v)}" for k, v in params.items()]
    return lines


def summary_text(items: list[dict[str, Any]]) -> str:
    numbered = len(items) > 1
    return "\n".join(
        line
        for index, item in enumerate(items, 1)
        for line in _summary_lines(index, item, numbered=numbered)
    )


def _detail_lines(index: int, item: dict[str, Any], *, full: bool) -> list[str]:
    lines = [f"{index}. {item['system_name']} · {item['method']} {item['path']}"]
    for key, value in (item.get("path_params") or {}).items():
        lines.append(f"   {key} = {_value(value)}")
    if not full:
        return lines
    for key, value in (item.get("query") or {}).items():
        lines.append(f"   ?{key} = {_value(value)}")
    if item.get("body") is not None:
        lines.append(f"   请求体 = {_value(item['body'])}")
    note = item.get("note") or {}
    if note.get("impact"):
        lines.append(f"   影响（AI 说明）：{_value(note['impact'])}")
    if note.get("rollback"):
        lines.append(f"   恢复方法（AI 说明）：{_value(note['rollback'])}")
    return lines


def card_text(items: list[dict[str, Any]]) -> str:
    """The exact requests, rendered from what was staged, never from model-written text alone.
    JSON keeps each value on one line, so a value cannot forge another item."""
    rows: list[str] = []
    size = 0
    for index, item in enumerate(items, 1):
        text = "\n".join(_detail_lines(index, item, full=True))
        if size + len(text) > CARD_TEXT_MAX:
            text = "\n".join(_detail_lines(index, item, full=False))
        rows.append(text)
        size += len(text)
    return "\n".join(rows)


def card(origin_task_id: int, state_id: uuid.UUID, items: list[dict[str, Any]]) -> dict[str, Any]:
    def button(label: str, verb: str, kind: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": kind,
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "task_id": f"personal:{origin_task_id}",
                        "event_key": CARD_EVENT,
                        "level": f"{verb}:{state_id}",
                    },
                }
            ],
        }

    destructive = any(item["risk"] == "destructive" for item in items)
    heading = f"请确认：{title(items[0])}" if len(items) == 1 else f"请确认 {len(items)} 项操作"
    systems = "、".join(dict.fromkeys(str(item["system_name"]) for item in items))
    return {
        "schema": "2.0",
        "task_id": f"personal:{origin_task_id}",
        "header": {
            "template": "red" if destructive else "orange",
            "title": {"tag": "plain_text", "content": heading},
            "subtitle": {"tag": "plain_text", "content": _plain(systems)},
        },
        "body": {
            "elements": [
                # Values come from the conversation: plain text, so no @mention or link fires.
                {"tag": "div", "text": {"tag": "plain_text", "content": summary_text(items)}},
                {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {"title": {"tag": "markdown", "content": DETAIL_TITLE}},
                    "background_color": "grey",
                    "border": {"color": "grey", "corner_radius": "8px"},
                    "padding": "8px",
                    "elements": [
                        {
                            "tag": "div",
                            "text": {
                                "tag": "plain_text",
                                "content": card_text(items),
                                "text_size": "notation",
                            },
                        }
                    ],
                },
                {
                    "tag": "column_set",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                button("确认", "confirm", "danger" if destructive else "primary")
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [button("取消", "cancel", "default")],
                        },
                    ],
                },
                {
                    "tag": "markdown",
                    "content": (
                        "<font color='grey'>改动说明由 AI 整理，以详情里的请求为准。"
                        "只有发起人能确认，10 分钟内有效。</font>"
                    ),
                    "text_size": "notation",
                },
            ]
        },
    }


async def release(
    session: AsyncSession, cipher: Cipher, *, bot_id: uuid.UUID, task_id: int, send: bool
) -> bool:
    """At the end of a turn: send the card for its staged writes, or drop them when the turn did
    not finish normally. True when a card was queued."""
    state = await _batch(session, bot_id, task_id)
    if state is None or state.status != "open" or state.state.get("sent"):
        return False
    if not send:
        state.status = "cancelled"
        return False
    items = unseal(cipher, task_id, state.state)
    state.state = {**state.state, "sent": True}
    state.expires_at = utcnow() + TTL
    await outbox.add(
        session,
        bot_id=bot_id,
        platform="feishu",
        kind="send",
        dedupe_key=f"systems-write:{state.id}",
        target={"chat_id": state.state["chat_id"]},
        payload={"card": card(task_id, state.id, items)},
    )
    return True
