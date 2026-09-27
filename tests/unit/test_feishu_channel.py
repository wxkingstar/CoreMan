import asyncio

import pytest

from coreman.runtime.gateway_feishu.channel import DurableChannel


async def test_ack_waits_for_committed_callback_and_errors_request_redelivery():
    entered, committed = asyncio.Event(), asyncio.Event()

    async def accept(raw):
        assert raw == {"header": {"event_id": "event1"}}
        entered.set()
        await committed.wait()

    channel = DurableChannel(accept=accept, app_id="cli_test", app_secret="synthetic")
    job = asyncio.create_task(
        asyncio.to_thread(channel._on_p2_im_message_receive_v1, {"header": {"event_id": "event1"}})
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert not job.done()
    committed.set()
    await job

    async def fail(raw):
        raise ValueError("synthetic secret SQL payload")

    channel._accept = fail
    with pytest.raises(RuntimeError, match="redelivery") as exc:
        await asyncio.to_thread(channel._on_p2_im_message_receive_v1, {})
    assert "synthetic" not in str(exc.value)


async def test_ack_timeout_cancels_uncommitted_work():
    cancelled = asyncio.Event()

    async def accept(raw):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    channel = DurableChannel(
        accept=accept, app_id="cli_test", app_secret="synthetic", ack_timeout=0.05
    )
    with pytest.raises(RuntimeError, match="timed out"):
        await asyncio.to_thread(channel._on_p2_im_message_receive_v1, {})
    await asyncio.wait_for(cancelled.wait(), timeout=1)


async def test_actual_sdk_dispatcher_preserves_header_and_waits_for_handler():
    import json

    from tests.unit.test_feishu_inbound import RAW

    accepted = []

    async def accept(raw):
        accepted.append(raw)

    channel = DurableChannel(accept=accept, app_id="cli_a", app_secret="synthetic")
    payload = json.dumps({"schema": "2.0", **RAW}).encode()
    await asyncio.to_thread(channel.dispatcher._do_without_validation, payload)
    assert accepted[0]["header"]["app_id"] == "cli_a"
    assert accepted[0]["event"]["message"]["message_id"] == "om1"


async def test_card_toasts_report_what_happened_to_the_click():
    results = iter(["queued", "duplicate", "not_requester", None, "unexpected"])

    async def accept(raw):
        return next(results)

    channel = DurableChannel(accept=accept, app_id="cli_test", app_secret="synthetic")
    toasts = [
        (await asyncio.to_thread(channel._on_p2_card_action_trigger, {})).toast for _ in range(5)
    ]
    # 已收下不等于已发送：worker 还可能拒绝（停用、白名单），所以说「已收到」。
    assert [(t.type, t.content) for t in toasts] == [
        ("success", "已收到"),
        ("info", "已选过"),
        ("warning", "只有提问人可以选择"),
        ("info", "✓"),
        ("info", "✓"),
    ]
