from datetime import UTC, datetime

import pytest

from coreman.core.cron.precheck import PrecheckError, run_precheck
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
