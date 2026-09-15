import asyncio
import threading
import time
from datetime import UTC, datetime

import pytest

from coreman.core.cron import precheck
from coreman.core.cron.precheck import PrecheckError, run_precheck, run_precheck_in_thread
from coreman.core.cron.schedule import next_run


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def test_schedule_timezone_and_dst() -> None:
    assert next_run("0 9 * * 1-5", "Asia/Shanghai", dt("2026-09-11T02:00")) == dt(
        "2026-09-14T01:00"
    )
    # 春季 02:30 不存在，跳过；秋季 01:30 只执行第一次。
    assert next_run("30 2 * * *", "America/New_York", dt("2026-03-08T06:00")) == dt(
        "2026-03-09T06:30"
    )
    assert next_run("30 1 * * *", "America/New_York", dt("2026-11-01T04:00")) == dt(
        "2026-11-01T05:30"
    )
    assert next_run("30 1 * * *", "America/New_York", dt("2026-11-01T05:30")) == dt(
        "2026-11-02T06:30"
    )
    assert next_run("30 1 * * *", "America/New_York", dt("2026-11-01T06:00")) == dt(
        "2026-11-02T06:30"
    )


@pytest.mark.parametrize(
    "expression,zone",
    [("* * * * * *", "UTC"), ("0 0 31 2 *", "UTC"), ("@daily", "UTC"), ("* * * * *", "Fake/Zone")],
)
def test_schedule_rejects_invalid(expression: str, zone: str) -> None:
    with pytest.raises(ValueError):
        next_run(expression, zone, dt("2026-09-01"))


def test_precheck_data_flow() -> None:
    script = """import json

def should_trigger(ctx):
    total = 0
    for item in ctx.get("items", []):
        if item["enabled"]:
            total = total + item["count"]
    return {"trigger": total > 2, "prompt_appendix": "Count: " + str(total), "reason": "threshold"}
"""
    result = run_precheck(
        script, {"items": [{"enabled": True, "count": 3}, {"enabled": False, "count": 9}]}
    )
    assert result.trigger and result.prompt_appendix == "Count: 3"
    assert not run_precheck(script, {}).trigger
    assert run_precheck(None, {}).trigger


@pytest.mark.parametrize(
    "script",
    [
        'import os\ndef should_trigger(ctx):\n return {"trigger": True}',
        "def should_trigger(ctx):\n return ctx.__class__.__base__.__subclasses__()",
        'def should_trigger(ctx):\n return eval("1+1")',
        'def should_trigger(ctx):\n return open("/etc/passwd").read()',
        "def should_trigger(ctx):\n while True:\n  pass",
        'def should_trigger(ctx):\n return {"trigger": "yes"}',
        'def should_trigger(ctx):\n return {"trigger": True, "secret": "x"}',
        'def should_trigger(ctx):\n return {"trigger": True, "reason": "x" * 1000000000}',
        "def should_trigger(ctx):\n for x in range(1000000000):\n  pass",
        'def should_trigger(ctx):\n return {"trigger": 2 ** 1000000000 > 1}',
        'def should_trigger(ctx):\n return getattr(ctx, "__class__")',
        '@print("oops")\ndef should_trigger(ctx):\n return {"trigger": True}',
    ],
)
def test_precheck_fail_closed(script: str) -> None:
    with pytest.raises(PrecheckError):
        run_precheck(script, {})


def test_precheck_budget_and_no_context_leak() -> None:
    with pytest.raises(PrecheckError, match="budget"):
        run_precheck(
            "def should_trigger(ctx):\n for x in range(10000):\n  for y in range(10000):\n   pass",
            {},
        )
    with pytest.raises(PrecheckError) as error:
        run_precheck(
            'def should_trigger(ctx):\n return int(ctx["secret"])', {"secret": "never-print-this"}
        )
    assert "never-print-this" not in str(error.value)


@pytest.mark.parametrize("finish", [None, "length"])
async def test_partial_cron_reply_is_not_success(finish):
    from coreman.core.relay.sse import FinishEvent, TextDelta
    from coreman.runtime.worker.cron_handler import CronRunHandler

    async def stream():
        yield TextDelta("partial report")
        if finish:
            yield FinishEvent(finish)

    with pytest.raises(ValueError, match="incomplete_result"):
        await CronRunHandler()._consume(stream())


def test_precheck_rebinding_large_value_is_not_quadratic() -> None:
    # 旧实现对每个表达式结果都整体遍历 + 序列化：这段 18000 步的脚本要跑几十秒。
    script = (
        "def should_trigger(ctx):\n"
        " big = range(10000)\n"
        " for i in range(6000):\n"
        "  x = big\n"
        " return {'trigger': True}\n"
    )
    started = time.monotonic()
    assert run_precheck(script, {}).trigger
    assert time.monotonic() - started < 3


def test_precheck_new_containers_are_still_size_checked() -> None:
    base = "def should_trigger(ctx):\n s = json.dumps(range(9000))\n"
    with pytest.raises(PrecheckError, match="data size limit"):
        run_precheck(base + " pair = [s, s]\n return {'trigger': True}", {})
    with pytest.raises(PrecheckError, match="data size limit"):
        run_precheck(base + " return {'trigger': True, 'reason': json.dumps([s, s])}", {})
    with pytest.raises(PrecheckError, match="integer limit"):
        run_precheck(
            "def should_trigger(ctx):\n x = 3\n for i in range(20):\n  x = x * x\n"
            " return {'trigger': True}",
            {},
        )


async def test_precheck_thread_keeps_event_loop_responsive() -> None:
    script = (
        "def should_trigger(ctx):\n"
        " big = range(10000)\n"
        " for i in range(5000):\n"
        "  s = json.dumps(big)\n"
        " return {'trigger': True}\n"
    )
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    running = asyncio.create_task(ticker())
    try:
        with pytest.raises(PrecheckError, match="budget"):
            await run_precheck_in_thread(script, {}, 0.5)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    # 同步执行时这 0.5 秒里事件循环一跳都走不了。
    assert ticks >= 5


async def test_precheck_abandons_stuck_thread_up_to_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def stuck(script, ctx, timeout_seconds=30, *, stop=None):  # type: ignore[no-untyped-def]
        release.wait(5)
        return precheck.PrecheckResult(True)

    monkeypatch.setattr(precheck, "run_precheck", stuck)
    monkeypatch.setattr(precheck, "MAX_ABANDONED_THREADS", 1)
    monkeypatch.setattr(precheck, "ABANDON_GRACE_SECONDS", 0.05)
    script = "def should_trigger(ctx):\n return {'trigger': True}"
    try:
        with pytest.raises(PrecheckError, match="budget"):
            await run_precheck_in_thread(script, {}, 0.01)
        assert precheck.abandoned_threads() == 1
        with pytest.raises(PrecheckError, match="capacity"):
            await run_precheck_in_thread(script, {}, 0.01)
    finally:
        release.set()
    for _ in range(200):
        if precheck.abandoned_threads() == 0:
            break
        await asyncio.sleep(0.01)
    assert precheck.abandoned_threads() == 0


async def test_cron_stream_classification_keeps_relay_reason() -> None:
    from coreman.core.relay.client import IncompleteResultError, RelayError
    from coreman.core.relay.sse import RelayErrorEvent, TextDelta
    from coreman.runtime.worker.cron_handler import CronRunHandler, CronStreamError

    async def relay_error():
        yield TextDelta("部分")
        yield RelayErrorEvent("\n\n[codex error] boom")

    with pytest.raises(CronStreamError) as caught:
        await CronRunHandler()._consume(relay_error())
    assert caught.value.code == "x_relay_error"
    assert caught.value.detail == "[codex error] boom" and caught.value.partial == "部分"

    async def zero_events():
        if False:
            yield TextDelta("")
        raise IncompleteResultError("incomplete_result: SSE 未返回确认终态")

    with pytest.raises(CronStreamError, match="empty_stream"):
        await CronRunHandler()._consume(zero_events())

    async def transport():
        yield TextDelta("x")
        raise RelayError("连接失败: ConnectError")

    with pytest.raises(RelayError, match="连接失败"):
        await CronRunHandler()._consume(transport())
