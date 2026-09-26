import copy
import json
import uuid
from unittest.mock import AsyncMock

import pytest

from coreman.core.chat import human_collaboration as human
from coreman.core.db.models import OutboxItem


class RecordingAPI:
    def __init__(self):
        self.calls = []

    async def call(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return {"data": {"message_id": "om_ask"}}


def transport(api):
    from coreman.runtime.gateway_feishu.transport import FeishuTransport

    sender = FeishuTransport(None, api, bot_id=uuid.uuid4(), instance_id="t", generation=1)
    sender.fence = AsyncMock()
    return sender


async def test_direct_ask_is_sent_to_the_colleague_by_user_id():
    api = RecordingAPI()
    item = OutboxItem(
        id=1,
        target={"user_id": "expert-id"},
        payload={"_human_collaboration_id": "x", "_human_phase": "ask", "markdown": "口径？"},
    )
    await transport(api)._send_item(item)
    _, path, kwargs = api.calls[-1]
    assert path == "/open-apis/im/v1/messages"
    assert kwargs["params"] == {"receive_id_type": "user_id"}
    assert kwargs["json"]["receive_id"] == "expert-id"
    content = json.loads(kwargs["json"]["content"])["zh_cn"]["content"]
    assert content == [[{"tag": "md", "text": "口径？"}]]
    assert item.payload["_feishu_message_id"] == "om_ask"


async def test_group_ask_replies_to_origin_with_platform_at_node():
    api = RecordingAPI()
    item = OutboxItem(
        id=2,
        target={"chat_id": "group", "message_id": "origin"},
        payload={
            "_human_collaboration_id": "x",
            "_human_phase": "ask",
            "_mention_user_id": "expert-id",
            "markdown": "口径？",
        },
    )
    await transport(api)._send_item(item)
    _, path, kwargs = api.calls[-1]
    assert path.endswith("/origin/reply")
    content = json.loads(kwargs["json"]["content"])["zh_cn"]["content"]
    assert content[0] == [{"tag": "at", "user_id": "expert-id"}]


def test_reply_threading_keeps_root_id():
    from coreman.runtime.gateway_feishu.inbound import normalize_event
    from tests.unit.test_feishu_inbound import KW, RAW

    raw = copy.deepcopy(RAW)
    raw["event"]["message"]["parent_id"] = "om_parent"
    raw["event"]["message"]["root_id"] = "om_root"
    normalized = normalize_event(raw, **KW)
    assert normalized is not None
    assert normalized.reply_context["parent_id"] == "om_parent"
    assert normalized.reply_context["root_id"] == "om_root"


@pytest.mark.parametrize(
    ("key", "valid"),
    [("human:" + str(uuid.UUID(int=1)), True), ("human:nope", False), ("bot_key", False)],
)
def test_partner_keys_are_namespaced(key, valid):
    assert (human.parse_key(key) is not None) is valid


def test_model_markup_cannot_mention_anyone():
    assert "<at" not in human.sanitize('<at user_id="all"></at>问题')
    assert "问题" in human.sanitize('<at user_id="all"></at>问题')
