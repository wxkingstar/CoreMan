from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coreman.api.routers.cron_jobs import CronIn


def body(**kwargs):
    return dict(
        bot_id="00000000-0000-0000-0000-000000000001", name="once", prompt="report", **kwargs
    )


def test_once_input_requires_offset_aware_instant():
    data = CronIn(**body(schedule_kind="once", run_at="2030-01-01T09:30:00+08:00"))
    assert data.run_at == datetime(2030, 1, 1, 1, 30, tzinfo=UTC)
    with pytest.raises(ValidationError):
        CronIn(**body(schedule_kind="once", run_at="2030-01-01T09:30:00"))


def test_delivery_summary_distinguishes_missing_partial_and_failed():
    from coreman.core.cron.delivery import delivery_summary

    assert delivery_summary(None, {}) == "no_run"
    assert delivery_summary({"outbox_ids": [1]}, {1: "sent"}) == "sent"
    assert delivery_summary({"outbox_ids": [1]}, {1: "queued"}) == "pending"
    assert delivery_summary({"outbox_ids": [1]}, {}) == "unknown"
    assert delivery_summary({"errors": {"user:x": "recipient_disabled"}}, {}) == "failed"
    assert delivery_summary({"outbox_ids": [1, 2]}, {1: "sent", 2: "failed"}) == "mixed"
    assert (
        delivery_summary(
            {"outbox_ids": [1], "errors": {"user:x": "recipient_unbound"}}, {1: "sent"}
        )
        == "mixed"
    )
    assert delivery_summary({}, {}) == "unknown"
