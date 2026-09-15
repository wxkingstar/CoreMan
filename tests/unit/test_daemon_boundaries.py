"""Failures found in the daemon review must remain failures, never success."""

import asyncio
import json
import uuid
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from coreman.core.i18n.messages import msg
from coreman.core.relay.client import (
    ChatRequest,
    IncompleteResultError,
    RelayBusyError,
    RelayClient,
    RelayError,
)
from coreman.core.runtime_nodes.transport import TOTAL_TIMEOUT_EXTENSION, RuntimeQueueTimeout
from coreman.runtime.gateway_wecom.ws_client import (
    DeliveryRejected,
    DeliveryUncertain,
    WeComWsClient,
)
from coreman.runtime.worker.background import BackgroundPusher
from coreman.runtime.worker.chat_handler import ChatTaskHandler, Outcome, Verdict


async def test_eof_after_partial_output_fails():
    raw = "data: " + json.dumps({"choices": [{"delta": {"content": "partial"}}]}) + "\n\n"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=raw)),
        base_url="http://fake",
    ) as http:
        client = RelayClient("http://fake", http=http)
        with pytest.raises(RelayError, match="incomplete_result"):
            [
                e
                async for e in client.chat_stream(
                    ChatRequest("m", "s", "u", "/tmp", "s", "claude"), total_timeout=1
                )
            ]


@pytest.mark.parametrize("reason", [None, "length", "error"])
def test_unconfirmed_terminal_is_not_success(reason):
    pre = NS(writer=NS(pending_text="partial"), session_url="http://fake/s", relay=NS(name="test"))
    verdict = ChatTaskHandler()._classify(
        NS(locale="zh"), pre, Outcome(text_events=1, finish_reason=reason)
    )
    assert verdict.task_status == "failed"


def _classify(text: str, out: Outcome) -> Verdict:
    pre = NS(writer=NS(pending_text=text), session_url="http://fake/s", relay=NS(name="test"))
    return ChatTaskHandler()._classify(NS(locale="zh"), pre, out)


def test_relay_error_wins_over_unconfirmed_terminal() -> None:
    verdict = _classify(
        "[codex error] original session retained",
        Outcome(relay_error=True, error=IncompleteResultError("incomplete_result")),
    )
    assert verdict.error_code == "x_relay_error"
    assert verdict.final_text == "[codex error] original session retained"


def test_zero_event_unconfirmed_stream_is_empty_stream() -> None:
    verdict = _classify("", Outcome(error=IncompleteResultError("incomplete_result")))
    assert verdict.error_code == "empty_stream" and "http://fake/s" in verdict.final_text


def test_unconfirmed_partial_output_keeps_streamed_text() -> None:
    verdict = _classify(
        "half answer", Outcome(text_events=1, error=IncompleteResultError("incomplete_result"))
    )
    assert verdict.error_code == "incomplete_result" and verdict.task_status == "failed"
    assert verdict.final_text.startswith("half answer\n\n")


async def test_queue_timeout_surfaces_as_busy_and_declares_total_timeout() -> None:
    seen: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get(TOTAL_TIMEOUT_EXTENSION))
        raise RuntimeQueueTimeout("运行时繁忙：排队 120 秒仍未开始执行", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://fake"
    ) as http:
        client = RelayClient("http://fake", http=http)
        with pytest.raises(RelayBusyError, match="排队"):
            [
                e
                async for e in client.chat_stream(
                    ChatRequest("m", "s", "u", "/tmp", "s", "claude"), total_timeout=5400
                )
            ]
    # 反向通道按这个总时限排队并计算调用期限
    assert seen == [5400]


def test_busy_runtime_is_classified_apart_from_connection_errors() -> None:
    verdict = _classify("", Outcome(error=RelayBusyError("运行时繁忙：排队 120 秒仍未开始执行")))
    assert verdict.error_code == "runtime_busy" and verdict.task_status == "failed"
    assert verdict.final_text == msg("runtime_busy", "zh", relay="test")
    assert "排队" in (verdict.error_message or "")


def test_transport_error_stays_generic() -> None:
    verdict = _classify("", Outcome(error=RelayError("连接失败: ConnectError")))
    assert verdict.error_code == "RelayError" and verdict.task_status == "failed"


@pytest.mark.parametrize("offset", [0, 10, 500])
async def test_takeover_error_keeps_full_reason(offset):
    pusher = BackgroundPusher(
        task_id=1,
        bot_id=uuid.uuid4(),
        platform="wecom",
        chat_id="c",
        verbosity_level=1,
        session_url="http://fake/s",
    )
    push = AsyncMock()
    pre = NS(supervisor=NS(pusher=pusher, push=push))
    handler = ChatTaskHandler()
    handler._queue_card = AsyncMock()
    await handler._push_if_proactive(
        NS(log=Mock(), task=NS(id=1)),
        pre,
        Verdict("error", "failed", "network", "网络失败", "连接失败，请稍后重试"),
        NS(delivery_mode="proactive", offset=offset, version=2),
    )
    assert "连接失败，请稍后重试" in push.call_args.args[0]


async def test_supersede_does_not_continue_while_victim_runs(monkeypatch):
    from coreman.runtime.worker import chat_handler

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    active = AsyncMock(return_value=[NS(id=7), NS(id=9), NS(id=12)])
    monkeypatch.setattr(chat_handler.tasks, "active_for_session", active)
    handler = ChatTaskHandler()
    handler.SUPERSEDE_WAIT_SECONDS = 0
    handler.SUPERSEDE_POLL_SECONDS = 0

    def ctx(cancelled: bool = False):  # type: ignore[no-untyped-def]
        event = asyncio.Event()
        if cancelled:
            event.set()
        return NS(session_factory=Session, log=Mock(), task=NS(id=9), cancel_event=event)

    # 更早的 7 还活着：等满上限就交给调用方重排；更晚的 12 会反过来替代本任务，不等它。
    with pytest.raises(TimeoutError, match="session_busy"):
        await handler._wait_superseded(ctx(), uuid.uuid4(), "s")
    assert await handler._wait_superseded(ctx(cancelled=True), uuid.uuid4(), "s") is False
    active.return_value = [NS(id=9), NS(id=12)]
    assert await handler._wait_superseded(ctx(), uuid.uuid4(), "s") is True


def ws_client():
    return WeComWsClient(bot_id="b", bot_key="b", secret="fake", on_frame=AsyncMock())


@pytest.mark.parametrize("code", [0, 846607, 846608])
async def test_ack_before_send_returns_is_not_lost(code):
    ws = ws_client()
    late_handler = AsyncMock()
    ws.on_errcode = late_handler

    async def send(frame):
        await ws._handle_ack({"headers": frame["headers"], "errcode": code, "errmsg": "test"})

    ws.send = send
    frame = {"headers": {"req_id": "attempt-1"}}
    if code:
        with pytest.raises(DeliveryRejected) as caught:
            await ws.send_confirmed(frame)
        assert caught.value.errcode == code
    else:
        await ws.send_confirmed(frame)
    late_handler.assert_not_called()
    assert not ws._acks


async def test_missing_ack_is_unknown_not_success():
    ws = ws_client()
    ws.send = AsyncMock()
    with pytest.raises(DeliveryUncertain):
        await ws.send_confirmed({"headers": {"req_id": "a"}}, timeout=0.01)
    assert not ws._acks


async def test_late_old_ack_cannot_complete_new_attempt():
    ws = ws_client()
    ws.send = AsyncMock()
    pending = asyncio.create_task(ws.send_confirmed({"headers": {"req_id": "new"}}, timeout=1))
    await asyncio.sleep(0)
    await ws._handle_ack({"headers": {"req_id": "old"}, "errcode": 0})
    assert not pending.done()
    await ws._handle_ack({"headers": {"req_id": "new"}, "errcode": 0})
    await pending


def test_completed_background_handoff_still_limits_stream_to_owned_prefix():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from coreman.runtime.gateway_wecom.pusher import _view_up_to

    row = SimpleNamespace(
        thinking_md="",
        pending_text="prefix-tail",
        final_text="prefix-tail",
        is_complete=True,
        running_since=datetime.now(UTC),
        session_url=None,
    )
    view = _view_up_to(row, 6)
    assert view.pending_text == "prefix"
    assert view.final_text is None and not view.is_complete
    assert _view_up_to(row, 0).pending_text == ""
