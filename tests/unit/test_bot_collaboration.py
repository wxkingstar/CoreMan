import pytest

from coreman.core.chat.bot_collaboration import event_matches, issue_capability, read_capability
from coreman.core.crypto import Cipher


def test_capability_is_bound_expiring_and_authenticated():
    cipher = Cipher(b"x" * 32)
    token = issue_capability(cipher, task_id=12, user_id="actor", now=100)
    assert read_capability(cipher, token, now=101) == (12, "actor")
    with pytest.raises(ValueError):
        read_capability(cipher, token, now=4000)
    with pytest.raises(ValueError):
        read_capability(Cipher(b"y" * 32), token, now=101)


@pytest.mark.parametrize("message_type", ["text", "post"])
def test_platform_event_must_match_all_boundaries(message_type):
    raw = {
        "header": {"tenant_key": "tenant"},
        "event": {
            "sender": {"sender_type": "bot", "sender_id": {"union_id": "peer"}},
            "message": {
                "message_id": "issued",
                "chat_id": "group",
                "chat_type": "group",
                "message_type": message_type,
                "parent_id": "request",
            },
        },
    }
    kwargs = dict(
        tenant="tenant", chat="group", sender_union="peer", message_id="issued", parent_id="request"
    )
    assert event_matches(raw, **kwargs)
    for key in kwargs:
        assert not event_matches(raw, **{**kwargs, key: "wrong"})
    raw["event"]["sender"]["sender_type"] = "user"
    assert not event_matches(raw, **kwargs)


def test_bot_normalization_preserves_bot_sender_and_never_fakes_user():
    import copy

    from coreman.runtime.gateway_feishu.inbound import normalize_event
    from tests.unit.test_feishu_inbound import KW, RAW

    raw = copy.deepcopy(RAW)
    raw["event"]["sender"]["sender_type"] = "bot"
    # Even if a callback carries an ID-shaped field, bots cannot become people.
    assert normalize_event(raw, **KW) is None
    normalized = normalize_event(raw, **KW, allow_bot=True)
    assert normalized.sender.sender_type == "bot"
    assert normalized.sender.platform_user_id == ""
    raw["event"]["message"]["mentions"] = []
    assert normalize_event(raw, **KW, allow_bot=True) is None


async def test_collaboration_transport_formats_markdown_and_retains_real_mention():
    import json
    import uuid

    from coreman.core.db.models import OutboxItem
    from coreman.runtime.gateway_feishu.transport import FeishuTransport
    from tests.integration.test_feishu_transport import FakeAPI

    api = FakeAPI()
    transport = FeishuTransport(None, api, bot_id=uuid.uuid4(), instance_id="test", generation=1)
    from unittest.mock import AsyncMock

    transport.fence = AsyncMock()
    markdown = "**库存**\n\n| 可用 | 数量 |\n| --- | --- |\n| A | 77 |"
    item = OutboxItem(
        id=uuid.uuid4(),
        target={"chat_id": "group", "message_id": "origin"},
        payload={
            "_collaboration_id": "collab",
            "markdown": markdown,
            "_mention_open_id": "ou_peer",
        },
    )
    await transport._send_item(item)
    _, path, body = api.calls[-1]
    assert path.endswith("/origin/reply")
    assert body["msg_type"] == "post"
    content = json.loads(body["content"])["zh_cn"]["content"]
    assert content == [[{"tag": "at", "user_id": "ou_peer"}], [{"tag": "md", "text": markdown}]]
    assert item.payload["_feishu_message_id"] == "om_1"


@pytest.mark.parametrize("legacy", [True, False])
def test_rich_bot_event_prefers_original_markdown_without_duplicate_text(legacy):
    import copy
    import json

    from coreman.runtime.gateway_feishu.inbound import normalize_event
    from tests.unit.test_feishu_inbound import KW, RAW

    raw = copy.deepcopy(RAW)
    raw["event"]["sender"]["sender_type"] = "bot"
    raw["event"]["message"]["message_type"] = "post"
    raw["event"]["message"]["content"] = json.dumps(
        {
            "content": [[{"tag": "text", "text": "legacy flattened"}]],
            "content_v2": [
                [{"tag": "at", "user_id": "@_user_1"}],
                [{"tag": "md", "text": "**反馈**\n\n| 数量 |\n| --- |\n| 77 |"}],
            ],
        }
    )
    if not legacy:
        content = json.loads(raw["event"]["message"]["content"])
        del content["content"]
        raw["event"]["message"]["content"] = json.dumps(content)
    normalized = normalize_event(raw, **KW, allow_bot=True)
    assert normalized is not None and normalized.mentions_bot
    assert normalized.sender.sender_type == "bot"
    assert len(normalized.parts) == 1
    assert normalized.parts[0].text.startswith("**反馈**")


@pytest.mark.parametrize(
    "text",
    [
        "正在等待60秒",
        '{"status":"blocked","answer":"数据缺失"}',
        '{"status":"completed","answer":""}',
        "[]",
    ],
)
def test_incomplete_helper_result_cannot_resume_source(text):
    from coreman.runtime.worker.chat.collaboration import read_helper_result

    with pytest.raises(ValueError):
        read_helper_result(text)


def test_completed_helper_result_preserves_markdown():
    from coreman.runtime.worker.chat.collaboration import read_helper_result

    assert read_helper_result('{"status":"completed","answer":"**可用78**"}') == "**可用78**"


def test_helper_contract_uses_final_tool_segment_and_rejects_progress_tail():
    from coreman.runtime.worker.chat.collaboration import read_helper_result

    progress = "我去读取数据。"
    final = '{"status":"completed","answer":"**可用78**"}'
    assert read_helper_result(progress + final, [len(progress)]) == "**可用78**"
    with pytest.raises(ValueError):
        read_helper_result(final + "还在等待", [len(final)])


def test_current_turn_context_preserves_multimodal_attachments():
    from coreman.runtime.worker.chat.collaboration import with_turn_context

    parts = [
        {"type": "text", "text": "please inspect this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
    ]
    result = with_turn_context(parts, "current authorized peers")
    assert result[1:] == parts
    assert len(parts) == 2
    assert result[0]["type"] == "text"
    assert "current authorized peers" in result[0]["text"]
    assert with_turn_context(parts, "") is parts
