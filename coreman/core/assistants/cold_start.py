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

from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.assistants import notes
from coreman.core.assistants import settings as assistant_settings
from coreman.core.bus import outbox
from coreman.core.crypto import Cipher
from coreman.core.db.models import COLD_START_STEPS, AssistantSettings, Bot
from coreman.core.timeutils import aware_utc

STEPS = COLD_START_STEPS
# 第一步（voice）是平台自己拉样本，其余四步各是一道题。
QUESTIONS = STEPS[1:]
EVENT_KEY = "assistant_cold_start"
DEADLINE = timedelta(hours=24)
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
    found = selected.get(key) if isinstance(selected, dict) else None
    return [str(value) for value in found] if isinstance(found, list) else []


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
