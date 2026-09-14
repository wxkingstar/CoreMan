from datetime import UTC, datetime, timedelta

from coreman.core.i18n.messages import msg
from coreman.core.wecom.stream_render import (
    MAX_CONTENT_BYTES,
    StreamView,
    render_wecom_stream,
    truncate_utf8,
)

T0 = datetime(2026, 9, 11, tzinfo=UTC)


def view(**over: object) -> StreamView:
    base = dict(
        thinking_md="🤔 a",
        pending_text="",
        final_text=None,
        is_complete=False,
        running_since=T0,
        session_url=None,
    )
    base.update(over)
    return StreamView(**base)  # type: ignore[arg-type]


def test_states() -> None:
    assert render_wecom_stream(view(), T0) == "<think>\n🤔 a"
    assert render_wecom_stream(view(thinking_md=""), T0) == "<think>\n" + msg("thinking_start")
    assert render_wecom_stream(
        view(pending_text="hi"), T0 + timedelta(seconds=5)
    ) == "<think>\n🤔 a\n</think>\n\nhi" + msg("running_indicator")
    assert render_wecom_stream(view(pending_text="hi"), T0 + timedelta(seconds=61)).endswith(
        msg("running_indicator_long")
    )
    assert render_wecom_stream(view(is_complete=True, final_text="done"), T0) == (
        "<think>\n🤔 a\n</think>\n\ndone"
    )
    assert (
        render_wecom_stream(view(thinking_md="", is_complete=True, final_text="done"), T0) == "done"
    )
    assert render_wecom_stream(view(thinking_md="", is_complete=True, final_text=""), T0) == msg(
        "processing_done"
    )


def test_truncation_keeps_text_and_tail_of_thinking() -> None:
    thinking = "思" * 9000  # 27000 字节
    out = render_wecom_stream(
        view(thinking_md=thinking, pending_text="正文", session_url="http://r/session/1"), T0
    )
    assert len(out.encode()) <= MAX_CONTENT_BYTES
    assert out.endswith(msg("truncated_suffix", url="http://r/session/1"))
    assert msg("thinking_truncated") in out and "正文" in out and out.count("</think>") == 1
    big_text = "字" * 8000
    out2 = render_wecom_stream(view(thinking_md="", is_complete=True, final_text=big_text), T0)
    assert len(out2.encode()) <= MAX_CONTENT_BYTES and out2.startswith("字" * 100)


def test_truncate_utf8_boundaries() -> None:
    assert truncate_utf8("a字b", 2) == "a"
    assert truncate_utf8("a字b", 4) == "a字"
    assert truncate_utf8("a字b", 2, keep="tail") == "b"
    assert truncate_utf8("abc", 10) == "abc"


def test_suffix_counts_against_the_budget() -> None:
    tail = msg("drain_suffix")
    short = render_wecom_stream(view(pending_text="hi"), T0 + timedelta(seconds=5), suffix=tail)
    assert short == "<think>\n🤔 a\n</think>\n\nhi" + msg("running_indicator") + tail
    long = render_wecom_stream(
        view(thinking_md="思" * 9000, pending_text="正文", session_url="http://r/session/1"),
        T0,
        suffix=tail,
    )
    assert long.endswith(tail) and len(long.encode()) <= MAX_CONTENT_BYTES
    assert "正文" in long and msg("thinking_truncated") in long
    # 尾缀本身就撑爆预算时宁可截尾缀也不能超限。
    huge = render_wecom_stream(view(pending_text="hi"), T0, suffix="尾" * 9000)
    assert len(huge.encode()) <= MAX_CONTENT_BYTES
