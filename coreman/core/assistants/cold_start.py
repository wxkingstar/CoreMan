"""冷启动：授权完成之后，平台了解所有者的口吻与几件基本的事，做完才开始盯消息。

状态在 `assistant_settings` 的四列里：`cold_start_started_at`（开始时刻）、`cold_start_step`
（`voice` → `people` → `duties` → `focus` → `hours`）、`cold_start_next_at`（非空表示该平台
出手：拉口吻样本、发卡片；空表示在等所有者）、`cold_start_outcome`。做完、跳过、满 24 小时
超时都写 `cold_start_done_at`，盯消息（`watch`）与投递（`delivery`）只认这一列。

四道题由平台直接发表单卡片、直接把答案写进确定的页面与设置列，不经模型：要每一步幂等、
可跳过。卡片回调走现有的 `CardActionHandler`（它已核对卡片由这条记录发到这个会话），这里
另用平台签的令牌绑定记录、这一次冷启动、哪一题、哪个按钮。
"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coreman.core.assistants import notes, sealed, sent, voice, watch
from coreman.core.assistants import settings as assistant_settings
from coreman.core.assistants import watch_feed as feed
from coreman.core.assistants.records import is_assistant
from coreman.core.bus import outbox
from coreman.core.crypto import Cipher
from coreman.core.db.models import COLD_START_STEPS, AssistantSettings, Bot
from coreman.core.feishu_personal.service import PersonalError
from coreman.core.feishu_personal.toolbase import Call
from coreman.core.logging import get_logger
from coreman.core.timeutils import aware_utc

log = get_logger(__name__)

STEPS = COLD_START_STEPS
# 第一步（voice）是平台自己拉样本，其余四步各是一道题。
QUESTIONS = STEPS[1:]
# 卡片回调的分流键。不与任务类型 `assistant_cold_start` 同名：那个字面量只许出现在登记处。
EVENT_KEY = "assistant_cold_start_card"
DEADLINE = timedelta(hours=24)
# 口吻样本拉不到：隔这么久重试；从开始算起满这么久还拉不到就放弃这一步，往下问。
VOICE_RETRY = timedelta(minutes=10)
VOICE_WINDOW = timedelta(hours=1)
# 一次循环最多推进这么多位助理。
BATCH = 50
MAX_ANSWER = 500
MAX_GROUPS = 20
MAX_NAME = 30
# 两道文字题的答案各成一页置顶页：（路径, 标题, 摘要）。
PAGES: dict[str, tuple[str, str, str]] = {
    "people": ("profile/people.md", "上级与主要协作人", "所有者的上级，以及平时主要一起做事的人"),
    "duties": ("profile/duties.md", "负责的事", "所有者负责哪些事"),
}
HOURS = (("09:00", "18:00"), ("09:30", "19:00"), ("10:00", "19:00"), ("10:00", "20:00"))
ZONES = (
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Asia/Singapore",
    "Europe/London",
    "Europe/Berlin",
    "America/New_York",
    "America/Los_Angeles",
    "UTC",
)
PROMPTS = {
    "people": "你的上级是谁？平时主要和哪些人一起做事？写名字和各自管什么就行。",
    "duties": "你主要负责哪些事？有人来问这些事时，我会优先交回给你。",
    "focus": "哪些群要重点盯？重点群里的每条消息我都会立刻交回给你（最多选 5 个）。",
    "hours": "你的工作时段和时区？我只在工作时段里看消息。工作日默认周一到周五，"
    "别的时段或时区之后直接告诉我。",
}
FOOTER = "每一题都可以跳过；发「跳过」可以全部跳过。之后随时在这里告诉我怎么改。"
SAVED = "已记下，存在知识库的 {path}。"
NOT_SAVED = "这一题没有记下，之后可以直接在这里告诉我。"
HEADS = {
    "completed": "认识完了。",
    "skipped": "好的，先跳过。",
    "timeout": "开通满 24 小时，我先按默认设置开始工作。",
}
_WEEKDAYS = "一二三四五六日"
# （会话 id, 群名）
Group = tuple[str, str]


def pending(row: AssistantSettings | None) -> bool:
    """冷启动开始了、还没做完。"""
    return (
        row is not None and row.cold_start_started_at is not None and row.cold_start_done_at is None
    )


async def begin(session: AsyncSession, bot_id: uuid.UUID, now: datetime) -> bool:
    """开始冷启动；已经开始过、做完了、或没记下身份（跑不了平台来源的轮次）时什么都不做。

    不建设置行、不重读：开通流程在同一个事务里刚写了身份还没落库，这里要看到那些改动。
    """
    row = await session.get(AssistantSettings, bot_id)
    if (
        row is None
        or not assistant_settings.identified(row)
        or row.cold_start_started_at is not None
        or row.cold_start_done_at is not None
    ):
        return False
    row.cold_start_started_at, row.cold_start_step, row.cold_start_next_at = now, STEPS[0], now
    row.updated_at = now
    return True


def nonce(row: AssistantSettings) -> int:
    """这一次冷启动的编号：开始时刻的毫秒数。卡片的 `task_id` 与令牌都带它。"""
    assert row.cold_start_started_at is not None
    return int(aware_utc(row.cold_start_started_at).timestamp() * 1000)


def _mac(cipher: Cipher, bot_id: uuid.UUID, row: AssistantSettings, step: str, verb: str) -> str:
    return cipher.mac("assistant-cold-start", f"{bot_id}:{nonce(row)}:{step}:{verb}")


def token(cipher: Cipher, bot_id: uuid.UUID, row: AssistantSettings, step: str, verb: str) -> str:
    return f"{step}.{verb}.{_mac(cipher, bot_id, row, step, verb)}"


def parse(
    cipher: Cipher, bot_id: uuid.UUID, row: AssistantSettings, level: str
) -> tuple[str, str] | None:
    """卡片按钮上的令牌 → （哪一题, `answer` / `skip`）；不是平台为这一次冷启动签的返回 None。"""
    step, _, rest = level.partition(".")
    verb, _, mac = rest.partition(".")
    if step not in QUESTIONS or verb not in ("answer", "skip"):
        return None
    if row.cold_start_started_at is None:
        return None
    # 按字节比：compare_digest 遇到非 ASCII 的 str 会抛 TypeError。
    if not hmac.compare_digest(mac.encode(), _mac(cipher, bot_id, row, step, verb).encode()):
        return None
    return step, verb


def _plain(text: str) -> dict[str, Any]:
    return {"tag": "plain_text", "content": text}


def _input(label: str, placeholder: str) -> dict[str, Any]:
    return {
        "tag": "input",
        "name": "answer",
        "required": True,
        "max_length": MAX_ANSWER,
        "label": _plain(label),
        "label_position": "top",
        "placeholder": _plain(placeholder),
    }


def _select(
    tag: str, name: str, placeholder: str, options: list[tuple[str, str]]
) -> dict[str, Any]:
    return {
        "tag": tag,
        "name": name,
        "placeholder": _plain(placeholder),
        "options": [{"text": _plain(text), "value": value} for value, text in options],
    }


def _fields(step: str, groups: Sequence[Group]) -> list[dict[str, Any]]:
    if step == "people":
        return [_input("上级与主要协作人", "例如：上级李四；和王五一起负责报价")]
    if step == "duties":
        return [_input("负责的事", "例如：华东区客户的报价与交付")]
    if step == "focus":
        # 群名是别人起的：只截短，只放进纯文本。
        options = [(chat_id, name[:MAX_NAME] or chat_id) for chat_id, name in groups[:MAX_GROUPS]]
        return [_select("multi_select_static", "focus", "选择重点群", options)]
    return [
        _select("select_static", "hours", "工作时段", [(f"{a}-{b}", f"{a}–{b}") for a, b in HOURS]),
        _select("select_static", "timezone", "时区", [(name, name) for name in ZONES]),
    ]


def _title(step: str) -> str:
    return f"认识一下（{QUESTIONS.index(step) + 1}/{len(QUESTIONS)}）"


def card(
    cipher: Cipher,
    bot_id: uuid.UUID,
    row: AssistantSettings,
    step: str,
    *,
    groups: Sequence[Group] = (),
) -> dict[str, Any]:
    """这一题的表单卡片：填写项、提交、跳过这一题。"""
    task_id = f"personal:{nonce(row)}"

    def value(verb: str) -> dict[str, str]:
        return {
            "task_id": task_id,
            "event_key": EVENT_KEY,
            "level": token(cipher, bot_id, row, step, verb),
        }

    return {
        "schema": "2.0",
        "task_id": task_id,
        "header": {"template": "turquoise", "title": _plain(_title(step))},
        "body": {
            "elements": [
                {"tag": "markdown", "content": PROMPTS[step]},
                {
                    "tag": "form",
                    "name": "cold_start_form",
                    "elements": [
                        *_fields(step, groups),
                        {
                            "tag": "button",
                            "name": "submit",
                            "form_action_type": "submit",
                            "type": "primary",
                            "text": _plain("提交"),
                            "behaviors": [{"type": "callback", "value": value("answer")}],
                        },
                    ],
                },
                {
                    "tag": "button",
                    "type": "default",
                    "text": _plain("跳过这一题"),
                    "behaviors": [{"type": "callback", "value": value("skip")}],
                },
                {"tag": "markdown", "content": FOOTER},
            ]
        },
    }


def answered_card(step: str, text: str) -> dict[str, Any]:
    """处理过的一题：没有按钮。`text` 是平台写的一句话，不回显答案。"""
    return {
        "schema": "2.0",
        "header": {"title": _plain(_title(step))},
        "body": {
            "elements": [
                {"tag": "markdown", "content": PROMPTS[step]},
                {"tag": "markdown", "content": text},
            ]
        },
    }


async def send(
    session: AsyncSession,
    cipher: Cipher,
    bot: Bot,
    row: AssistantSettings,
    step: str,
    now: datetime,
    *,
    groups: Sequence[Group] = (),
) -> None:
    """把这一题的卡片发到所有者的私聊（同一题只发一张），之后等所有者。"""
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=f"assistant-cold-start:{bot.id}:{nonce(row)}:{step}",
        target={"chat_id": row.chat_id},
        payload={"card": card(cipher, bot.id, row, step, groups=groups)},
    )
    row.cold_start_next_at, row.updated_at = None, now


def _values(selected: Any, key: str) -> list[str]:
    """回调里这一项的值。形状没在真实租户里核对过：单个字符串、字符串列表都认，别的当没填。"""
    found = selected.get(key) if isinstance(selected, dict) else None
    if isinstance(found, str):
        return [found]
    return [value for value in found if isinstance(value, str)] if isinstance(found, list) else []


async def _page(session: AsyncSession, bot_id: uuid.UUID, step: str, body: str) -> bool:
    """把一道文字题的答案写成置顶页。

    `updated_by="owner"`、可以置顶：正文是所有者本人在卡片里打的字（回调的操作人经飞书核对
    就是所有者），不是模型写的，也不来自别人的消息。
    """
    path, title, summary = PAGES[step]
    try:
        existing = await notes.read(session, bot_id, path)
        await notes.write(
            session,
            bot_id,
            path,
            body=body,
            summary=summary,
            title=title,
            pinned=True,
            version=existing.version if existing is not None else 0,
            updated_by="owner",
            owner_present=True,
        )
    except notes.NotesError:
        return False
    return True


async def _focus(session: AsyncSession, bot_id: uuid.UUID, chosen: list[str], now: datetime) -> str:
    if not chosen:
        return "没有选重点群。"
    try:
        await assistant_settings.update(session, bot_id, now=now, focus_chats=chosen)
    except assistant_settings.SettingsError:
        return NOT_SAVED
    return f"已把 {len(chosen)} 个群设为重点群。"


async def _hours(
    session: AsyncSession, bot_id: uuid.UUID, row: AssistantSettings, selected: Any, now: datetime
) -> str:
    """只认卡片上给出的选项：回调里的值不在选项里就当没选。"""
    changes: dict[str, Any] = {}
    picked = (_values(selected, "hours") or [""])[0]
    if picked in {f"{a}-{b}" for a, b in HOURS}:
        changes["work_start"], changes["work_end"] = picked.split("-")
    zone_name = (_values(selected, "timezone") or [""])[0]
    if zone_name in ZONES:
        changes["timezone"] = zone_name
    if not changes:
        return NOT_SAVED
    try:
        await assistant_settings.update(session, bot_id, now=now, **changes)
    except assistant_settings.SettingsError:
        return NOT_SAVED
    return f"工作时段 {row.work_start}–{row.work_end}，时区 {assistant_settings.zone(row).key}。"


async def _next(
    session: AsyncSession, bot: Bot, row: AssistantSettings, step: str, now: datetime
) -> None:
    index = STEPS.index(step)
    if index + 1 < len(STEPS):
        row.cold_start_step, row.cold_start_next_at = STEPS[index + 1], now
        row.updated_at = now
    else:
        await complete(session, bot, row, "completed", now)


async def answer(
    session: AsyncSession, bot: Bot, row: AssistantSettings, step: str, selected: Any, now: datetime
) -> str:
    """所有者提交了这一题：把答案写下并前进。返回换到卡片上的一句话（平台写的）。

    写不下（没填、值不在选项里、密钥不可用）不卡住：照样前进，所有者之后在私聊里说。
    """
    if step in PAGES:
        text = (_values(selected, "answer") or [""])[0].strip()[:MAX_ANSWER]
        saved = bool(text) and await _page(session, bot.id, step, text)
        said = SAVED.format(path=PAGES[step][0]) if saved else NOT_SAVED
    elif step == "focus":
        chosen = _values(selected, "focus")[: assistant_settings.MAX_FOCUS]
        said = await _focus(session, bot.id, chosen, now)
    else:
        said = await _hours(session, bot.id, row, selected, now)
    await _next(session, bot, row, step, now)
    return said


async def skip(
    session: AsyncSession, bot: Bot, row: AssistantSettings, step: str, now: datetime
) -> None:
    """跳过这一步（一道题，或拉不到样本的口吻步骤）。"""
    await _next(session, bot, row, step, now)


def done_text(row: AssistantSettings, outcome: str) -> str:
    days = "、".join(f"周{_WEEKDAYS[day - 1]}" for day in sorted(row.workdays))
    hours = f"{days} {row.work_start}–{row.work_end}，{assistant_settings.zone(row).key}"
    return (
        f"{HEADS[outcome]}从现在起我在你的工作时段（{hours}）里看你的飞书消息，"
        "有要你处理的会在这里告诉你；对外的操作仍然先提交审批。\n"
        "随时发「上班」「下班」临时切换；要改工作时段、时区、重点群或不想看的会话，"
        "直接在这里告诉我。"
    )


async def complete(
    session: AsyncSession, bot: Bot, row: AssistantSettings, outcome: str, now: datetime
) -> bool:
    """冷启动到此为止（做完、跳过或超时）：写 `cold_start_done_at` 并私信告知；已结束的返回假。"""
    if row.cold_start_done_at is not None or row.cold_start_started_at is None:
        return False
    row.cold_start_done_at, row.cold_start_outcome = now, outcome
    row.cold_start_step, row.cold_start_next_at, row.updated_at = None, None, now
    await outbox.add(
        session,
        bot_id=bot.id,
        platform="feishu",
        kind="send",
        dedupe_key=f"assistant-cold-start:{bot.id}:{nonce(row)}:done",
        target={"chat_id": row.chat_id},
        payload={"markdown": done_text(row, outcome)},
    )
    return True


async def stop(session: AsyncSession, bot_id: uuid.UUID) -> None:
    """停用时调用：没做完的冷启动不再推进，「上班」「下班」的临时切换清掉。调用方提交。"""
    row = await session.get(AssistantSettings, bot_id)
    if row is None:
        return
    row.cold_start_step, row.cold_start_next_at = None, None
    assistant_settings.clear_override(row)


async def _reader(
    session: AsyncSession, cipher: Cipher, bot: Bot, row: AssistantSettings
) -> feed.Reader | None:
    """以所有者身份读飞书的调用器；身份核对不过（授权没了、账号停用、身份对不上）返回 None。"""
    scope, _code = await watch.owner_scope(session, bot, row)
    return Call(session, cipher, scope) if scope is not None else None


async def _groups(call: feed.Reader, row: AssistantSettings) -> list[Group]:
    """所有者最近活跃的群（给「重点群」那一题当选项）：有名字、不是单聊、不是与助理的私聊。"""
    listed = await feed.list_chats(call)
    found = [
        (chat_id, listed.names.get(chat_id, ""))
        for chat_id in listed.order
        if chat_id != row.chat_id and listed.kinds.get(chat_id) != "p2p"
    ]
    return [(chat_id, name) for chat_id, name in found if name][:MAX_GROUPS]


async def _voice_step(
    session: AsyncSession, cipher: Cipher, bot: Bot, row: AssistantSettings, now: datetime
) -> str:
    """拉所有者自己发出的消息、排一轮口吻样本；不等那一轮，直接进入第一题。"""
    assert row.cold_start_started_at is not None
    call = await _reader(session, cipher, bot, row)
    samples: list[str] = []

    async def ours(message_ids: list[str]) -> frozenset[str]:
        # 平台经审批代所有者发出的消息是助理起草的：不进口吻样本。
        return await sent.among(session, bot.id, message_ids)

    if call is not None:
        try:
            samples = await voice.collect(call, row, now, ours=ours)
        except PersonalError:
            if aware_utc(now) - aware_utc(row.cold_start_started_at) < VOICE_WINDOW:
                row.cold_start_next_at, row.updated_at = now + VOICE_RETRY, now
                return "voice_retry"
    queued = len(samples) >= voice.MIN_MESSAGES
    if queued:
        await voice.enqueue(session, bot, row, samples, nonce(row))
    await skip(session, bot, row, "voice", now)
    return "voice_queued" if queued else "voice_skipped"


async def _question_step(
    session: AsyncSession,
    cipher: Cipher,
    bot: Bot,
    row: AssistantSettings,
    step: str,
    now: datetime,
) -> str:
    groups: list[Group] = []
    if step == "focus":
        call = await _reader(session, cipher, bot, row)
        if call is not None:
            try:
                groups = await _groups(call, row)
            except Exception as exc:  # noqa: BLE001 列不出来（含字段形状不符）就当没有群；只记类型
                # 不往外抛：这一次里可能刚刷新过令牌，事务要照常提交。
                log.warning("assistant_cold_start_groups_failed", error=type(exc).__name__)
                groups = []
        if not groups:
            # 列不出群就没有可选的：这一题自动跳过，所有者之后在私聊里说。
            await skip(session, bot, row, step, now)
            return "focus_skipped"
    await send(session, cipher, bot, row, step, now, groups=groups)
    return "asked"


async def advance(
    session: AsyncSession, cipher: Cipher, bot: Bot, row: AssistantSettings, now: datetime
) -> str | None:
    """一位助理的一次推进：满 24 小时兜底完成；到点了就拉样本或发下一题。调用方提交。

    调过飞书之后调用方不要回滚：刷新令牌一次性使用，换到的新令牌必须落库（`_visit`）。
    """
    if not pending(row):
        return None
    assert row.cold_start_started_at is not None
    if aware_utc(now) - aware_utc(row.cold_start_started_at) >= DEADLINE:
        await complete(session, bot, row, "timeout", now)
        return "timeout"
    due = row.cold_start_next_at
    if due is None or aware_utc(due) > aware_utc(now) or row.cold_start_step is None:
        return None
    if row.cold_start_step == "voice":
        return await _voice_step(session, cipher, bot, row, now)
    return await _question_step(session, cipher, bot, row, row.cold_start_step, now)


def _recover(row: AssistantSettings, now: datetime) -> None:
    """这一位出了意外的错：往后推，不让循环每 5 秒撞一次；口吻那一步拖满一小时就放弃它。

    24 小时兜底不受影响：`advance` 先看超时，再做别的。
    """
    if not pending(row) or row.cold_start_step is None or row.cold_start_started_at is None:
        return
    stuck = aware_utc(now) - aware_utc(row.cold_start_started_at) >= VOICE_WINDOW
    if row.cold_start_step == STEPS[0] and stuck:
        row.cold_start_step, row.cold_start_next_at = STEPS[1], now
    else:
        row.cold_start_next_at = now + VOICE_RETRY
    row.updated_at = now


async def _visit(
    factory: async_sessionmaker[AsyncSession], cipher: Cipher, bot_id: uuid.UUID, now: datetime
) -> str | None:
    """一位助理一个短事务：加锁、推进、提交。"""
    async with factory() as session:
        await sealed.guard_logs(session, bot_id)
        bot = await session.get(Bot, bot_id)
        # 所有者正点着卡片（回调握着行锁）：这一次跳过，5 秒后再来。
        row = await session.get(
            AssistantSettings,
            bot_id,
            with_for_update={"skip_locked": True},
            populate_existing=True,
        )
        code: str | None = None
        if (
            bot is not None
            and row is not None
            and bot.enabled
            and is_assistant(bot)
            and bot.assistant_state == "active"
        ):
            try:
                code = await advance(session, cipher, bot, row, now)
            except Exception as exc:  # noqa: BLE001 只记类型
                # 意外出错也先试着提交：这一次里可能已经刷新过令牌（旧的刷新令牌已经用掉），
                # 回滚会让授权就此作废。会话本身坏了时提交会再抛，由 `tick` 另开事务往后推。
                log.warning(
                    "assistant_cold_start_failed", bot_id=str(bot_id), error=type(exc).__name__
                )
                _recover(row, now)
                code = "failed"
        await session.commit()
        return code


async def _push_back(
    factory: async_sessionmaker[AsyncSession], bot_id: uuid.UUID, now: datetime
) -> None:
    """`_visit` 连提交都没成（事务已回滚）：另开事务把这一位往后推。"""
    try:
        async with factory() as session:
            row = await session.get(
                AssistantSettings, bot_id, with_for_update={"skip_locked": True}
            )
            if row is not None:
                _recover(row, now)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 兜底本身出错也不能让循环停下
        log.warning("assistant_cold_start_push_back_failed", error=type(exc).__name__)


async def tick(
    factory: async_sessionmaker[AsyncSession], cipher: Cipher, now: datetime
) -> dict[str, int]:
    """循环入口：冷启动开始了、没做完、且到点或超时的助理逐个推进，每位一个事务。"""
    async with factory() as session:
        ids = list(
            await session.scalars(
                select(AssistantSettings.bot_id)
                .join(Bot, Bot.id == AssistantSettings.bot_id)
                .where(
                    Bot.kind == "assistant",
                    Bot.enabled.is_(True),
                    Bot.assistant_state == "active",
                    AssistantSettings.cold_start_done_at.is_(None),
                    AssistantSettings.cold_start_started_at.is_not(None),
                    or_(
                        AssistantSettings.cold_start_next_at <= now,
                        AssistantSettings.cold_start_started_at <= now - DEADLINE,
                    ),
                )
                .order_by(AssistantSettings.cold_start_started_at)
                .limit(BATCH)
            )
        )
    counts: dict[str, int] = {}
    for bot_id in ids:
        code: str | None
        try:
            # 被叫停时等这一位提交完再退：这一次里可能刚刷新过令牌（见 `watch.finish_round`）。
            code = await watch.finish_round(_visit(factory, cipher, bot_id, now))
        except Exception as exc:  # noqa: BLE001 一位助理出错不挡别人；只记类型
            log.warning("assistant_cold_start_failed", bot_id=str(bot_id), error=type(exc).__name__)
            await _push_back(factory, bot_id, now)
            code = "failed"
        if code is not None:
            counts[code] = counts.get(code, 0) + 1
    return counts
