"""企业微信能力授权的到期提醒。

企业微信的能力授权按子项约 7 天到期，调用不顺延，也没有查询到期时间的接口。这里按
「最近一次授权时间 + 7 天」估算（见 service.record），在估计到期前一天，通过本人最近用过的
企业微信 AI 员工私聊提醒一次；本人续期后在「我的企业微信」点「我已续期」，从那时重新起算。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.bus import outbox
from coreman.core.db.models import Bot, UserReached, WecomPersonalBinding
from coreman.core.wecom_personal import service

REMIND_BEFORE = timedelta(hours=24)
# 同一份绑定两次提醒至少隔这么久：本人没处理也不会被刷屏。
REMIND_EVERY = timedelta(hours=20)
# 估计到期后这么久还没处理，就不再提醒：本人可能已经不再使用。
REMIND_UNTIL = timedelta(days=3)
BATCH = 200
_CN = ZoneInfo("Asia/Shanghai")


def _shown(moment: datetime) -> str:
    return moment.astimezone(_CN).strftime("%m-%d %H:%M")


def message(row: WecomPersonalBinding, moment: datetime, page: str) -> str:
    expired = moment <= datetime.now(UTC)
    lead = (
        "你的企业微信授权可能已经到期"
        if expired
        else f"你的企业微信授权预计在 {_shown(moment)} 到期"
    )
    return "\n".join(
        (
            f"**{lead}**",
            "到期后，AI 员工就不能再以你的身份查询待办、日程、邮件等。" + service.renew_hint(row),
            f"续期后请到 {page} 点「我已续期」，提醒会按新的时间重新计算。"
            "（企业微信不提供到期时间的查询接口，这里的时间是按上次授权估算的。）",
        )
    )


async def _target(session: AsyncSession, row: WecomPersonalBinding) -> UserReached | None:
    """本人最近开始私聊的一个企业微信 AI 员工：提醒从那里发。"""
    reached: UserReached | None = await session.scalar(
        select(UserReached)
        .join(Bot, Bot.id == UserReached.bot_id)
        .where(
            UserReached.user_id == row.user_id,
            Bot.platform == "wecom",
            Bot.enabled.is_(True),
        )
        .order_by(UserReached.first_seen_at.desc())
        .limit(1)
    )
    return reached


async def remind(factory: async_sessionmaker[AsyncSession], base_url: str) -> int:
    """给估计一天内到期的绑定发提醒；返回发出的条数。"""
    now = datetime.now(UTC)
    page = base_url.rstrip("/") + "/my-wecom" if base_url else "CoreMan「我的企业微信」页面"
    sent = 0
    async with factory() as session:
        rows = (
            await session.scalars(
                select(WecomPersonalBinding)
                .where(
                    WecomPersonalBinding.status == "bound",
                    WecomPersonalBinding.enabled.is_(True),
                )
                .limit(BATCH)
            )
        ).all()
        for row in rows:
            moment = service.next_expiry(row)
            if moment is None or moment - now > REMIND_BEFORE or now - moment > REMIND_UNTIL:
                continue
            if row.reminded_at is not None and now - row.reminded_at < REMIND_EVERY:
                continue
            target = await _target(session, row)
            if target is None:
                continue
            await outbox.add(
                session,
                bot_id=target.bot_id,
                platform="wecom",
                kind="send",
                dedupe_key=f"wecom_personal:remind:{row.user_id}:{int(moment.timestamp())}",
                target={"chat_id": target.platform_chat_id},
                payload={"markdown": message(row, moment, page)},
            )
            row.reminded_at = now
            sent += 1
        await session.commit()
    return sent
