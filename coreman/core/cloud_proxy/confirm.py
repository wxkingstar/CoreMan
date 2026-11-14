"""个人助理所有者在场时经云签名代理的低风险写：等所有者在确认卡上确认。

结构照业务系统目录代理的确认卡（`systems_catalog.confirm`）：一轮里暂存的写合成一批
（`interaction_states`，`kind = cloud_write`，按任务号一批），本轮正常结束时给所有者发一张卡，
卡上的内容按暂存的请求渲染；点确认后由平台以所有者身份按顺序原样执行（见
`runtime/worker/cloud_write_cards.py`），结果作为续接的一轮投回原会话。只有个人助理走这里：
普通员工的低风险写照旧直接转发，高风险写照旧 428 走 Tower。

暂存的内容不另用平台密钥加密：`interaction_states.state` 已登记在 `assistants.sealed.COLUMNS`，
助理的行落库前整块用所有者的数据密钥加密（只有 `user_id` 留明文）。去重摘要用所有者的数据
密钥算（`sealed.content_mac`），审计表里的摘要拿不到密钥就无从比对参数。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import sealed
from coreman.core.assistants.records import inactive_assistant
from coreman.core.bus import outbox
from coreman.core.db.models import Bot, InteractionState, Task, User
from coreman.core.systems_catalog import confirm as systems_confirm
from coreman.core.timeutils import utcnow

KIND = "cloud_write"
CARD_EVENT = "cloud_write"
# 卡片答完后续接的那一轮，任务载荷里放结果的键。
RESULT_KEY = "cloud_write_result"
TTL = systems_confirm.TTL
MAX_ITEMS = 20
# 卡片正文（字符）与整张卡序列化后（字节）的上限。飞书卡片消息的请求体上限约 30 KB；正文
# 一个字都不截，放不下就不暂存，所以取比目录代理确认卡（正文 12000 字、超出只列抬头）更保守的值。
CARD_TEXT_MAX = systems_confirm.CARD_TEXT_MAX
CARD_BYTES_MAX = 24_000
MAC_PURPOSE = "cloud_write"
TOO_LARGE = "too_large_to_review"

HINT = (
    "已暂存，没有执行。本轮结束时平台给所有者发一张确认卡，所有者点确认后平台按这次的请求原样"
    "执行，并在本会话里告诉你结果；不要重试，也不要换别的方式（令牌直连、MCP、别的接口或工具）"
    "做同一件事。告诉所有者需要在卡片上确认，不要说已经完成。"
)
UNAVAILABLE = (
    "这一轮没法让所有者当场在确认卡上确认（不是所有者本人在与助理的私聊里打字发来的消息），"
    "云服务写操作没有执行。把要做的写操作（产品、Action、参数）告诉所有者，请他在私聊里直接"
    "发一条消息让你做。"
)
CLOSED = "本轮的确认卡已经发出，不能再追加写操作。请所有者另发一条消息再做。"
BATCH_FULL = (
    f"一次最多确认 {MAX_ITEMS} 个云服务写操作，这一个没有加入，也没有执行。告诉所有者剩下的"
    "需要另发一条消息再做。"
)
TOO_LARGE_HINT = (
    "这次请求的内容太长，确认卡上放不下全部参数（连同本轮已暂存的写操作），没有暂存，也没有"
    "执行。拆成能在卡片上完整展示的小请求再调用，或让所有者在另一条消息里接着做。"
)


@dataclass(frozen=True)
class Staged:
    """给代理调用方的回应：HTTP 状态、正文，以及记进审计的摘要（没算出来时为 None）。"""

    status: int
    body: dict[str, Any]
    digest: str | None = None


def _canonical(product: str, params: dict[str, str]) -> str:
    return json.dumps([product, params], sort_keys=True, ensure_ascii=False)


def _visible(text: str) -> str:
    from coreman.core.assistants.approvals import proxied  # 审批闸门依赖面广，延迟导入

    return proxied._visible(text)


def _param_lines(params: dict[str, str]) -> list[str]:
    from coreman.core.assistants.approvals import proxied  # 审批闸门依赖面广，延迟导入

    return proxied.cloud_param_lines(params)


def card_text(items: list[dict[str, Any]]) -> str:
    """按暂存的请求渲染，全部参数完整展示，不截断（放不下的在暂存时就拒了）。

    沿用无人在场审批单的展示规则：每个值按 JSON 字符串展示，控制、格式字符与行、段分隔符
    （`\\n` 除外）转义成可见的 `\\uXXXX`，一个值造不出独立的一行，也颠倒不了显示。
    """
    rows: list[str] = []
    for index, item in enumerate(items, 1):
        rows.append(headline(index, item))
        lines = _param_lines(item["params"])
        if lines:
            rows.append("   参数（完整）：")
            rows += ["   " + _visible(line) for line in lines]
        else:
            rows.append("   （除 Action 外没有参数）")
        for reason in item.get("reasons") or []:
            rows.append("   风险评估：" + _visible(" ".join(str(reason).split())))
    return "\n".join(rows)


def headline(index: int, item: dict[str, Any]) -> str:
    action = _visible(str(item["params"].get("Action") or ""))
    return f"{index}. [云服务 · 阿里云 {item['product']}] {action}（低风险写操作）"


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

    return {
        "schema": "2.0",
        "task_id": f"personal:{origin_task_id}",
        "header": {
            "template": "orange",
            "title": {
                "tag": "plain_text",
                "content": f"确认：执行 {len(items)} 个云服务写操作",
            },
        },
        "body": {
            "elements": [
                # 参数来自对话：纯文本，@ 与链接都不生效。
                {"tag": "div", "text": {"tag": "plain_text", "content": card_text(items)}},
                button("确认执行", "confirm", "primary"),
                button("取消", "cancel", "default"),
                {
                    "tag": "markdown",
                    "content": (
                        "<font color='grey'>只有所有者本人能操作，平台以你的身份按上面的参数原样、"
                        "按顺序执行，遇到失败即停止，10 分钟内有效。</font>"
                    ),
                },
            ]
        },
    }


def fits(items: list[dict[str, Any]]) -> bool:
    """整批放得进一张卡：正文与序列化后的卡片都不超上限。"""
    if len(card_text(items)) > CARD_TEXT_MAX:
        return False
    whole = json.dumps(card(0, uuid.UUID(int=0), items), ensure_ascii=False)
    return len(whole.encode()) <= CARD_BYTES_MAX


async def propose(
    session: AsyncSession,
    *,
    task: Task,
    bot: Bot,
    user: User,
    product: str,
    params: dict[str, str],
    reasons: tuple[str, ...],
) -> Staged:
    """把一次已判为低风险的写暂存进本轮的批次；不调阿里云。调用方记审计并提交事务。"""
    origin = await systems_confirm.confirmable_origin(session, task.id, user.id)
    if (
        origin is None
        or origin[1] != "single"
        or inactive_assistant(bot)
        or not bot.enabled
        or bot.owner_id != user.id
    ):
        return Staged(403, {"error": "confirmation_unavailable", "message": UNAVAILABLE})
    chat_id = origin[0]
    try:
        digest = await sealed.content_mac(session, bot.id, MAC_PURPOSE, _canonical(product, params))
    except sealed.ContentLocked:
        return Staged(403, {"error": sealed.LOCKED_CODE})
    state = await systems_confirm._batch(session, bot.id, task.id, kind=KIND)
    if state is not None and (state.status != "open" or state.state.get("sent")):
        return Staged(403, {"error": "confirmation_closed", "message": CLOSED}, digest)
    items: list[dict[str, Any]] = list(state.state.get("items") or []) if state else []
    if any(item.get("digest") == digest for item in items):
        # 同一轮里重复调用同一请求：不重复暂存，卡片上也只出现一次。
        return Staged(
            202,
            {
                "status": "pending_confirmation",
                "pending": len(items),
                "duplicate": True,
                "hint": HINT,
            },
            digest,
        )
    if len(items) >= MAX_ITEMS:
        return Staged(403, {"error": "confirmation_batch_full", "message": BATCH_FULL}, digest)
    item = {"product": product, "params": dict(params), "reasons": list(reasons), "digest": digest}
    batch = [*items, item]
    if not fits(batch):
        return Staged(
            403,
            {
                "error": TOO_LARGE,
                "limit": {"text_chars": CARD_TEXT_MAX, "card_bytes": CARD_BYTES_MAX},
                "hint": TOO_LARGE_HINT,
            },
            digest,
        )
    value = {
        "origin_task_id": task.id,
        "user_id": str(user.id),
        "chat_id": chat_id,
        "count": len(batch),
        "items": batch,
    }
    if state is None:
        session.add(
            InteractionState(
                bot_id=bot.id,
                kind=KIND,
                scope_key=str(task.id),
                state=value,
                status="open",
                # 发出的卡另有期限；没发出的批次只比这一轮多活一会儿。
                expires_at=utcnow() + timedelta(seconds=bot.sse_timeout_seconds) + TTL,
            )
        )
    else:
        state.state = value
    return Staged(
        202, {"status": "pending_confirmation", "pending": len(batch), "hint": HINT}, digest
    )


async def release(session: AsyncSession, *, bot_id: uuid.UUID, task_id: int, send: bool) -> bool:
    """一轮结束时：正常结束就为暂存的写发确认卡，否则作废。发出了卡时返回 True。"""
    state = await systems_confirm._batch(session, bot_id, task_id, kind=KIND)
    if state is None or state.status != "open" or state.state.get("sent"):
        return False
    items = list(state.state.get("items") or [])
    if not send or not items:
        # 没正常结束，或内容读不出来（所有者的数据密钥已销毁）：不发卡，什么都不执行。
        state.status = "cancelled"
        return False
    state.state = {**state.state, "sent": True}
    state.expires_at = utcnow() + TTL
    await outbox.add(
        session,
        bot_id=bot_id,
        platform="feishu",
        kind="send",
        dedupe_key=f"cloud-write:{state.id}",
        target={"chat_id": state.state["chat_id"]},
        payload={"card": card(task_id, state.id, items)},
    )
    return True
