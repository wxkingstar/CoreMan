import json

import httpx
import pytest

from coreman.core.chat.decider import DECISIONS_URL, OpenAIDecider
from coreman.core.feishu_apps import manifest


def _decider(handler):
    return OpenAIDecider(
        "sk-test", "gpt-6-luna", http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


@pytest.mark.asyncio
async def test_decision_reads_probability_and_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "answers": [{"type": "predicate", "name": "interject", "probability": 0.82}],
                "usage": {"input_tokens": 1291},
            },
        )

    decision = await _decider(handler).should_interject({"bot": {}, "recent_messages": []})
    assert seen["url"] == DECISIONS_URL
    assert seen["body"]["model"] == "gpt-6-luna"
    assert seen["body"]["questions"][0]["type"] == "predicate"
    assert decision.probability == 0.82 and decision.input_tokens == 1291
    assert decision.error is None and not decision.refused


@pytest.mark.asyncio
async def test_refusal_and_failures_never_raise():
    refusal = _decider(
        lambda r: httpx.Response(200, json={"answers": [{"type": "refusal", "name": "interject"}]})
    )
    assert (await refusal.should_interject({})).refused
    failing = _decider(lambda r: httpx.Response(429, json={}))
    assert (await failing.should_interject({})).error == "http_429"

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    result = await _decider(broken).should_interject({})
    assert result.probability is None and result.error == "ConnectError"


def test_observe_scope_only_for_observing_bots():
    scope = "im:message.group_msg.include_bot:read"
    assert scope not in manifest.addons()["scopes"]["tenant"]
    assert scope in manifest.addons(observe=True)["scopes"]["tenant"]
    granted = list(manifest.TENANT_SCOPES)
    assert manifest.missing_scopes(granted, kind="tenant") == []
    assert manifest.missing_scopes(granted, kind="tenant", observe=True) == [scope]
