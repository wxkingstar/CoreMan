import uuid

from coreman.core.i18n.messages import msg
from coreman.runtime.worker.background import BackgroundPusher, Timing

T = Timing(bg_min_interval=10, bg_max_wait=120, bg_degrade_after=3, bg_degraded_interval=90)


def pusher(verbosity: int = 1) -> BackgroundPusher:
    return BackgroundPusher(
        task_id=7,
        bot_id=uuid.uuid4(),
        platform="wecom",
        chat_id="zs",
        verbosity_level=verbosity,
        session_url="http://r/session/1",
        timing=T,
        locale="zh",
        clock=lambda: 0.0,
    )


def test_boundary_driven_pushes_and_min_interval() -> None:
    p = pusher()
    p.start(now=100.0, pending_len=5)
    assert p.decide(105.0, "12345第一段", [5]) == []  # 边界 5 == offset，无新内容
    assert p.decide(105.0, "12345第一段第二段", [5, 8]) == []  # 距切换 <10s
    out = p.decide(111.0, "12345第一段第二段", [5, 8])
    assert out == [msg("bg_progress_prefix") + "第一段"] and p.offset == 8 and p.pushed_count == 1
    assert p.next_dedupe_key() == "7:send:1"
    assert p.decide(115.0, "12345第一段第二段第三段", [5, 8, 11]) == []
    assert p.decide(122.0, "12345第一段第二段第三段", [5, 8, 11]) == [
        msg("bg_progress_prefix") + "第二段"
    ]


def test_waiting_for_answer_sends_only_unseen_text_without_success_marker() -> None:
    p = pusher()
    p.start(now=0, pending_len=len("已展示的正文"))
    assert p.finish_plain("已展示的正文\n\n请选择方案") == ["\n\n请选择方案"]
    assert p.finish_plain("已展示的正文") == []
    quiet = pusher(3)
    quiet.start(now=0, pending_len=100)
    assert quiet.finish_plain("请选择方案") == ["请选择方案"]


def test_max_wait_fallback_and_degrade() -> None:
    p = pusher()
    p.start(now=0.0, pending_len=0)
    assert p.decide(119.0, "无边界文本", []) == []
    assert p.decide(121.0, "无边界文本", []) == [msg("bg_progress_prefix") + "无边界文本"]
    text = "无边界文本"
    for i in range(2):
        text += f"段{i}"
        assert len(p.decide(300.0 + i * 20, text, [len(text)])) == 1
    assert p.pushed_count == 3 and p.degraded is False
    text += "更多"
    out = p.decide(400.0, text, [len(text)])
    assert out == [msg("bg_degraded", n=3, url="http://r/session/1")] and p.degraded
    assert p.decide(450.0, text + "再多", [len(text) + 2]) == []
    out = p.decide(491.0, text + "再多", [len(text) + 2])
    assert len(out) == 1 and out[0].startswith(msg("bg_progress_prefix"))


def test_finish_and_truncation() -> None:
    p = pusher()
    p.start(now=0.0, pending_len=0)
    assert p.finish("") == []
    p2 = pusher()
    p2.start(now=0.0, pending_len=0)
    p2.decide(200.0, "已推", [2])
    assert p2.finish("已推") == [msg("bg_done_plain")]
    assert p2.finish("已推还有尾巴") == [msg("bg_done_prefix") + "还有尾巴"]
    big = "字" * 10000
    (only,) = pusher(verbosity=3).finish(big)
    assert (
        only
        and len(only.encode()) <= T.max_bytes
        and msg("bg_truncated", url="http://r/session/1") in only
    )


def test_high_verbosity_never_streams_progress() -> None:
    p = pusher(verbosity=3)
    p.start(now=0.0, pending_len=0)
    assert p.decide(500.0, "x" * 100, [10, 50]) == [] and p.pushed_count == 0
    assert p.finish("全文") == ["全文"]
    assert "http://r/session/1" in p.expired_message()


def test_finish_failed_never_claims_success() -> None:
    """失败 / 取消收尾：恒一条，带终稿与会话链接，绝不冠 ✅。"""
    nothing_pushed = pusher()
    nothing_pushed.start(now=0.0, pending_len=0)
    (only,) = nothing_pushed.finish_failed(msg("relay_error_text"))
    assert msg("relay_error_text") in only
    assert msg("session_link_suffix", url="http://r/session/1") in only
    assert not only.startswith(msg("bg_done_prefix")) and only != msg("bg_done_plain")

    pushed_before = pusher()
    pushed_before.start(now=0.0, pending_len=0)
    pushed_before.decide(200.0, "已推", [2])
    assert pushed_before.pushed_count == 1
    (only2,) = pushed_before.finish_failed("已推" + msg("task_stopped_suffix"))
    assert msg("task_stopped_suffix").strip() in only2 and "已推" in only2
    assert msg("bg_done_prefix") not in only2 and only2 != msg("bg_done_plain")


def test_finish_failed_caps_and_ignores_verbosity() -> None:
    p = pusher(verbosity=3)
    p.start(now=0.0, pending_len=0)
    (only,) = p.finish_failed("字" * 10000)
    assert len(only.encode()) <= T.max_bytes
    assert msg("bg_truncated", url="http://r/session/1") in only
    assert not only.startswith(msg("bg_done_prefix"))


def test_silence_heartbeats_fire_once_per_mark_and_rearm_on_push() -> None:
    """提交轮静默心跳：每个阈值只推一次，任何一次推送都让静默计时从头再来。"""
    timing = Timing(bg_min_interval=0.0, bg_max_wait=1000.0, silence_marks=(3.0, 6.0))
    p = BackgroundPusher(
        task_id=1,
        bot_id=uuid.uuid4(),
        platform="wecom",
        chat_id="c",
        verbosity_level=1,
        session_url="",
        timing=timing,
    )
    p.start(0.0, 0)
    assert p.heartbeat(2.0) is None
    assert p.heartbeat(3.5) == msg("submit_heartbeat", seconds=3)
    assert p.heartbeat(4.0) is None
    assert p.heartbeat(6.5) == msg("submit_heartbeat", seconds=6)
    assert p.decide(7.0, "段落", [2]) == [msg("bg_progress_prefix") + "段落"]  # 推送后静默计时重置
    assert p.heartbeat(9.0) is None and p.heartbeat(10.5) == msg("submit_heartbeat", seconds=10)
