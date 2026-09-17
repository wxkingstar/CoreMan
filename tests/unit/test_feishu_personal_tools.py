from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coreman.core.feishu_personal import service, tools


@pytest.fixture
def upstream(monkeypatch):
    call = AsyncMock(return_value={"items": [], "has_more": False})
    monkeypatch.setattr(service, "api_request", call)
    return call


async def invoke(name, args):
    scope = SimpleNamespace(event=SimpleNamespace(sender_open_id="ou_self"))
    return await tools.dispatch(None, None, scope, name, args)


@pytest.mark.parametrize(
    "name,args",
    [
        ("feishu_send_message", {}),
        ("feishu_search_messages", {"actor": "foreign"}),
        ("feishu_search_messages", {"limit": 21}),
        ("feishu_search_messages", {"limit": True}),
        ("feishu_search_messages", {"start_time": "2026-09-16T00:00:00.123Z"}),
        ("feishu_search_messages", {"start_time": "2026-09-16T00:00:00"}),
        ("feishu_search_messages", {"start_time": "2026-02-31T00:00:00Z"}),
        ("feishu_search_meetings", {"participant_ids": ["foreign"]}),
        ("feishu_search_minutes", {"owner_ids": ["foreign"]}),
        ("feishu_read_minutes", {"minute_token": "../secret"}),
        ("feishu_read_document", {"document_id": "https://example.com"}),
        ("feishu_chat_history", {"chat_id": "oc_1", "start_time": 1700000000000}),
        ("feishu_read_messages", {"message_ids": ["om_1"] * 21}),
    ],
)
async def test_invalid_arguments_never_contact_service(upstream, name, args):
    assert await invoke(name, args) == {"error": "invalid_tool_or_arguments"}
    upstream.assert_not_called()


async def test_search_preserves_seconds_timezone_and_allows_recent_empty_query(upstream):
    await invoke(
        "feishu_search_messages",
        {
            "start_time": "2026-09-16T00:00:00+08:00",
            "end_time": "2026-09-17T00:00:00+08:00",
            "limit": 2,
        },
    )
    kwargs = upstream.call_args.kwargs
    assert kwargs["json"]["query"] == ""
    assert kwargs["json"]["filter"]["time_range"]["start_time"] == "2026-09-16T00:00:00+08:00"
    assert kwargs["params"]["page_size"] == 2


@pytest.mark.parametrize(
    "name,extra,key",
    [
        ("feishu_search_minutes", {}, "participant_ids"),
        ("feishu_search_minutes", {"relationship": "owner"}, "owner_ids"),
        ("feishu_search_meetings", {}, "participant_ids"),
    ],
)
async def test_meetings_and_minutes_bound_to_verified_human(upstream, name, extra, key):
    await invoke(name, extra)
    body = upstream.call_args.kwargs["json"]
    filters = body["meeting_filter" if name.endswith("meetings") else "filter"]
    assert filters[key] == ["ou_self"]
    if name.endswith("minutes"):
        assert body["sorter"] == "create_time_desc"


async def test_results_bounded_and_pagination_explicit(upstream):
    upstream.return_value = {"items": [{"id": str(i)} for i in range(8)], "page_token": "next"}
    result = await invoke("feishu_search_minutes", {"limit": 2, "page_token": "previous"})
    assert len(result["items"]) == 2
    assert result["has_more"] is True and result["page_token"] == "next"
    assert upstream.call_args.kwargs["params"] == {"page_size": 2, "page_token": "previous"}


async def test_search_enriches_only_valid_bounded_message_ids(upstream):
    upstream.side_effect = [
        {
            "items": [
                {"meta_data": {"message_id": "om_1"}},
                {"meta_data": {"message_id": "../bad"}},
            ],
            "has_more": True,
            "page_token": "next",
        },
        {"items": [{"body": {"content": "hello"}, "access_token": "secret"}]},
    ]
    result = await invoke("feishu_search_messages", {"limit": 2})
    assert upstream.call_args.kwargs["params"] == {"message_ids": ["om_1"]}
    assert result["messages"] == [{"body": {"content": "hello"}}]
    assert result["has_more"] is True and result["page_token"] == "next"
    assert result["content_trust"] == "external_untrusted_data"


def test_tool_catalog_is_closed_and_strict():
    catalog = tools.definitions()
    assert len(catalog) == 12
    assert all(item["inputSchema"]["additionalProperties"] is False for item in catalog)
    assert not any("send" in item["name"] or "proxy" in item["name"] for item in catalog)


@pytest.mark.parametrize(
    "name,args",
    [
        ("feishu_read_document", {"document_id": "doc_1", "text_offset": -1}),
        ("feishu_read_minutes", {"minute_token": "m1", "text_limit": 30001}),
    ],
)
async def test_text_page_bounds_rejected_before_http(upstream, name, args):
    assert await invoke(name, args) == {"error": "invalid_tool_or_arguments"}
    upstream.assert_not_called()


async def test_document_text_can_be_read_in_complete_pages(upstream):
    upstream.return_value = {"content": "abcdefghij"}
    first = await invoke("feishu_read_document", {"document_id": "doc_1", "text_limit": 4})
    assert (first["content"], first["total_chars"], first["next_offset"], first["truncated"]) == (
        "abcd",
        10,
        4,
        True,
    )
    last = await invoke(
        "feishu_read_document", {"document_id": "doc_1", "text_offset": 4, "text_limit": 8}
    )
    assert (last["content"], last["next_offset"], last["truncated"]) == ("efghij", None, False)


async def test_minutes_transcript_is_paged_without_dropping_metadata(upstream):
    upstream.side_effect = [
        {"minute": {"title": "meeting"}},
        {"transcript": "abcdefgh", "summary": "summary"},
    ]
    out = await invoke(
        "feishu_read_minutes", {"minute_token": "m1", "text_offset": 2, "text_limit": 3}
    )
    assert out["artifacts"] == {"transcript": "cde", "summary": "summary"}
    assert out["minute"]["title"] == "meeting"
    assert (out["total_chars"], out["next_offset"], out["truncated"]) == (8, 5, True)


async def test_large_message_output_is_explicit_error_not_silent_truncation(upstream):
    upstream.return_value = {"items": [{"body": {"content": "x" * 210000}}]}
    out = await invoke("feishu_read_messages", {"message_ids": ["om_1"]})
    assert out["error"] == "response_too_large"
    assert "smaller" in out["hint"]
    assert len(str(out)) < 200000
