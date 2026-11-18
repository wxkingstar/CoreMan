"""私聊里的权限申请要所有者当面点确认：助理调 `gap_request` 只发一张确认卡，所有者点「提交申请」
才真正建单（`access_requests.open_request`），点「不提交」就此作废。

卡片上写明系统、只读还是读写、理由原文；按钮值里带着这三样与一个平台签的令牌（主密钥派生的
HMAC，绑定助理、发卡片的那一轮、到期时刻、缺口与它此刻的版本、级别、理由）。点击时再核对：
卡片确实由这条记录发到这个会话（`CardActionHandler` 已核）、令牌没被改过也没过期、点的人就是
所有者（按飞书给的操作人认）、缺口版本没变。建单与「不提交」都会改缺口的更新时间，所以同一张卡
（或转发出去的副本）只能用一次。理由是给平台管理员看的明文，不放任何密文内容。
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import access_requests, gaps
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import outbox
from coreman.core.chat.identity import resolve_speaker
from coreman.core.crypto import Cipher
from coreman.core.db.models import AssistantGap, Bot, BusinessSystem, InboundEvent, User
from coreman.core.logging import get_logger
from coreman.core.timeutils import utcnow

log = get_logger(__name__)
EVENT_KEY = "assistant_gap_request"
SUBMIT = "submit"
DROP = "drop"
TTL_SECONDS = 1800
TITLE = "确认：申请业务系统权限"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
FAILED = {
    "gap_not_open": "这个缺口已经不在待开通的状态（已申请、已解决或你不开了），没有提交。",
    "owner_can_grant": (
        "这个系统现在对你的助理开放了，不用申请：在「我的助理」页这一条上点「开通」。"
    ),
    "system_not_requestable": "这个系统已停用或不再登记，没有提交；请联系平台管理员。",
    "already_pending": "已经有一张在途的申请，没有重复提交。",
    "not_business_system": "只有业务系统的缺口能申请。",
    "gap_not_found": "这个缺口已经不在了，没有提交。",
}


def version(row: AssistantGap) -> int:
    """缺口此刻的版本：更新时间的微秒数。建单、撤回、不提交都会改它。"""
    return (row.updated_at - _EPOCH) // timedelta(microseconds=1)


def _mac(
    cipher: Cipher,
    verb: str,
    bot_id: uuid.UUID,
    task_id: int,
    expires: int,
    seq: int,
    ver: int,
    write: bool,
    reason: str,
) -> str:
    digest = hashlib.sha256(reason.encode()).hexdigest()
    return cipher.mac(
        "assistant-gap-request",
        f"{verb}:{bot_id}:{task_id}:{expires}:{seq}:{ver}:{int(write)}:{digest}",
    )


def card(
    cipher: Cipher,
    bot_id: uuid.UUID,
    task_id: int,
    gap: AssistantGap,
    system: BusinessSystem,
    reason: str,
    now: float,
) -> dict[str, Any]:
    expires = int(now) + TTL_SECONDS
    write = gaps.need_write(gap)
    ver = version(gap)
    card_task = f"personal:{task_id}"

    def button(label: str, verb: str, kind: str) -> dict[str, Any]:
        token = _mac(cipher, verb, bot_id, task_id, expires, gap.seq, ver, write, reason)
        return {
            "tag": "button",
            "type": kind,
            "text": {"tag": "plain_text", "content": label},
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "task_id": card_task,
                        "event_key": EVENT_KEY,
                        "level": f"{verb}.{expires}.{token}",
                        "gap": gap.seq,
                        "version": ver,
                        "write": write,
                        "reason": reason,
                    },
                }
            ],
        }

    level = "读写" if write else "只读"
    # 系统名与理由来自登记与对话：纯文本，@ 与链接都不生效。
    summary = (
        f"缺口 {gaps.label(gap.seq)}\n系统：{system.name}（{system.key}）\n级别：{level}\n"
        f"理由（会给平台管理员看）：{reason}"
    )
    return {
        "schema": "2.0",
        "task_id": card_task,
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": TITLE}},
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": summary}},
                button("提交申请", SUBMIT, "primary"),
                button("不提交", DROP, "default"),
                {
                    "tag": "markdown",
                    "content": (
                        "<font color='grey'>只有你本人能操作。提交后由平台管理员审批，批下来平台"
                        "自动续接被它卡住的事。30 分钟内有效，只能用一次。</font>"
                    ),
                },
            ]
        },
    }


async def stage(
    session: AsyncSession,
    cipher: Cipher,
    *,
    bot: Bot,
    task_id: int,
    chat_id: str,
    gap_seq: int,
    reason: str,
) -> None:
    """核对能申请后，把确认卡发到所有者的私聊；不建单。调用方保证这一轮所有者在场。

    Raises:
        RequestError: invalid_reason / not_business_system / gap_not_open / owner_can_grant /
            system_not_requestable
        GapError: gap_not_found
    """
    text = access_requests.clean_reason(reason)
    gap, system = await access_requests.requestable(session, bot, gap_seq)
    await session.refresh(gap, ["updated_at"])
    payload = card(cipher, bot.id, task_id, gap, system, text, time.time())
    digest = hashlib.sha256(text.encode()).hexdigest()
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        # 带上理由的摘要：同一轮里改了理由再发的卡片照发，原样重发的只发一张。
        dedupe_key=f"assistant-gap-request:{task_id}:{gap.id}:{version(gap)}:{digest[:16]}",
        target={"chat_id": chat_id},
        payload={"card": payload},
    )
    log.info("assistant_gap_request_staged", bot_id=str(bot.id), gap=gaps.label(gap.seq))


async def _update(session: AsyncSession, bot: Bot, inbound: InboundEvent, text: str) -> None:
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="card_update",
        dedupe_key=f"{inbound.id}:card_update",
        target={"chat_id": inbound.chat_id, "message_id": inbound.reply_context.get("message_id")},
        payload={
            "card": {
                "schema": "2.0",
                "header": {"title": {"tag": "plain_text", "content": TITLE}},
                "body": {
                    "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": text}}]
                },
            }
        },
    )


async def decide(
    session: AsyncSession, cipher: Cipher, bot: Bot, inbound: InboundEvent, action: dict[str, Any]
) -> str:
    """确认卡的回调；调用方（`CardActionHandler`）已核对卡片由这条记录发到这个会话。

    返回给任务结果看的分支名；认不出、令牌不对、不是所有者点的一律 `ignored`，什么都不做。
    """
    if not is_assistant(bot) or not bot.enabled:
        return "ignored"
    try:
        task_id = int(str(action.get("task_id") or "").removeprefix("personal:"))
        verb, expires_raw, token = str(action.get("level") or "").split(".")
        expires = int(expires_raw)
        seq = int(action["gap"])
        ver = int(action["version"])
        write = action["write"]
        reason = action["reason"]
    except (KeyError, TypeError, ValueError):
        return "ignored"
    if verb not in (SUBMIT, DROP) or not isinstance(write, bool) or not isinstance(reason, str):
        return "ignored"
    expected = _mac(cipher, verb, bot.id, task_id, expires, seq, ver, write, reason)
    # 按字节比：compare_digest 遇到非 ASCII 的 str 会抛 TypeError。
    if not hmac.compare_digest(token.encode(), expected.encode()):
        return "ignored"
    speaker = await resolve_speaker(
        session, platform="feishu", platform_user_id=str(inbound.sender_platform_user_id or "")
    )
    if speaker.user_id is None or speaker.user_id != bot.owner_id:
        return "ignored"
    if expires < time.time():
        await _update(session, bot, inbound, "这张卡片已过期，没有提交；需要的话请让助理重新发起。")
        return "gap_request_expired"
    await gaps.lock(session, bot.id)
    try:
        gap = await gaps.get(session, bot.id, seq, lock=True)
    except gaps.GapError:
        await _update(session, bot, inbound, FAILED["gap_not_found"])
        return "gap_request_stale"
    await session.refresh(gap, ["updated_at", "status"])
    if version(gap) != ver:
        await _update(session, bot, inbound, "这张卡片已经用过或缺口有了变化，没有再提交。")
        return "gap_request_stale"
    if verb == DROP:
        # 改版本号：同一张卡（或转发出去的副本）之后再点「提交申请」也不会建单。
        gap.updated_at = utcnow()
        await session.flush()
        await _update(session, bot, inbound, "好的，不提交这份申请。")
        log.info("assistant_gap_request_dropped", bot_id=str(bot.id), gap=gaps.label(seq))
        return "gap_request_dropped"
    owner = await session.get(User, bot.owner_id)
    assert owner is not None  # resolve_speaker 刚认出这个人
    try:
        await access_requests.open_request(
            session, bot, seq, write=write, reason=reason, actor=owner
        )
    except (gaps.GapError, access_requests.RequestError) as exc:
        await _update(session, bot, inbound, FAILED.get(exc.code, "没有提交。"))
        return f"gap_request_{exc.code}"
    await _update(
        session,
        bot,
        inbound,
        "已提交，等平台管理员审批；批下来平台会自动续接被它卡住的事。"
        "要撤回可以在「我的助理」页这一条上点「撤回申请」。",
    )
    return "gap_request_submitted"
