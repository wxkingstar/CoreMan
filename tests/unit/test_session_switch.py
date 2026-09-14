import uuid
from datetime import UTC, datetime, timedelta

from coreman.core.chat.session_switch import (
    SessionPreview,
    format_list,
    is_sessions_command,
    parse_choice,
    relative_time,
)
from coreman.core.i18n.messages import msg

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def test_command_and_choice_parsing() -> None:
    assert is_sessions_command(" Sessions ") and is_sessions_command("会话列表")
    assert not is_sessions_command("sessions list") and not is_sessions_command("会话")
    assert parse_choice("2", 3) == 2 and parse_choice(" 3 ", 3) == 3
    assert (
        parse_choice("0", 3) is None
        and parse_choice("4", 3) is None
        and parse_choice("二", 3) is None
        and parse_choice("1a", 3) is None
    )
    # Unicode 的「数字」不等于 int() 认的数字：② 与 ² 都 isdigit()，但 int() 会抛 ValueError，
    # 抛到 worker 里就是任务崩溃、用户一个字也收不到。只有 isdecimal() 与 int() 精确对齐。
    assert parse_choice("②", 3) is None and parse_choice("²", 3) is None


def test_relative_time_and_list() -> None:
    assert relative_time(NOW - timedelta(seconds=30), NOW) == msg("rt_just_now")
    assert relative_time(NOW + timedelta(seconds=30), NOW) == msg("rt_just_now")
    assert relative_time(NOW - timedelta(minutes=5), NOW) == msg("rt_minutes", n=5)
    assert relative_time(NOW - timedelta(hours=3), NOW) == msg("rt_hours", n=3)
    assert relative_time(NOW - timedelta(hours=30), NOW) == msg("rt_yesterday")
    assert relative_time(NOW - timedelta(days=3), NOW) == msg("rt_days", n=3)
    items = [
        SessionPreview(uuid.uuid4(), "帮我看看库存", NOW - timedelta(minutes=5)),
        SessionPreview(uuid.uuid4(), msg("non_text_message"), NOW - timedelta(days=2)),
    ]
    assert format_list(items, NOW) == (
        "📋 最近 2 个会话（回复序号切换，5 分钟内有效）\n\n"
        "1. 帮我看看库存（5 分钟前）\n2. [非文本消息]（2 天前）"
    )
