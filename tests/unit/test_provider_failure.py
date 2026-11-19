import pytest

from coreman.core.relay.observations import provider_failure


@pytest.mark.parametrize(
    "message",
    [
        "You've hit your limit · resets 6pm (Asia/Shanghai)",
        "You’ve hit your limit · resets Nov 23 at 1pm",
        "You have reached your usage limit. Try again later.",
        "You've reached your limit",
        '{"error":{"type":"insufficient_quota"}}',
        "Your credit balance is too low to access the Anthropic API.",
        "insufficient credits",
        "quota_exhausted",
        "本周额度已用尽",
    ],
)
def test_exhausted_subscription_is_distinct_from_model_failure(message):
    assert provider_failure(message) == ("down", "quota_limited", "已触发订阅额度限制")


@pytest.mark.parametrize(
    "message,code",
    [
        ("Rate limit exceeded, retry after 60 seconds", "rate_limited"),
        ("429 Too many requests", "rate_limited"),
        ("Your account is on hold. Usage limit unavailable.", "account_restricted"),
        ("Your Claude session has expired", "session_expired"),
        ("Invalid API key", "authentication_error"),
        ("Connection reset by peer", "model_request_failed"),
        ("Please limit your response to 100 words", "model_request_failed"),
    ],
)
def test_other_failures_are_not_relabelled_as_exhaustion(message, code):
    assert provider_failure(message)[1] == code
