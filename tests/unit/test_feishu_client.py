import json

import httpx
import pytest

from coreman.core.contacts.feishu_source import parse_feishu_directory
from coreman.core.platforms.feishu import FeishuClient, FeishuError


async def test_oauth_uses_app_then_user_token_and_does_not_expose_error_body():
    calls = []

    def response(request):
        calls.append(request)
        if request.url.path.endswith("/app_access_token/internal"):
            assert json.loads(request.content) == {
                "app_id": "cli_test",
                "app_secret": "synthetic-secret",
            }
            return httpx.Response(
                200, json={"code": 0, "app_access_token": "app-token", "expire": 7200}
            )
        if request.url.path.endswith("/oidc/access_token"):
            assert request.headers["Authorization"] == "Bearer app-token"
            return httpx.Response(200, json={"code": 0, "data": {"access_token": "user-token"}})
        assert request.headers["Authorization"] == "Bearer user-token"
        return httpx.Response(200, json={"code": 0, "data": {"user_id": "employee1"}})

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn", transport=httpx.MockTransport(response)
    ) as http:
        client = FeishuClient("cli_test", "synthetic-secret", http=http)
        assert await client.user_info_by_code("code") == {"user_id": "employee1"}
        assert len(calls) == 3

    def denied(request):
        return httpx.Response(200, json={"code": 230013, "msg": "synthetic-secret"})

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn", transport=httpx.MockTransport(denied)
    ) as http:
        with pytest.raises(FeishuError) as exc:
            await FeishuClient("x", "y", http=http).get_token()
        assert "synthetic-secret" not in str(exc.value)


async def test_directory_pagination_rejects_incomplete_or_repeated_page(monkeypatch):
    client = FeishuClient("x", "y")
    pages = iter(
        [
            {"data": {"items": [{"user_id": "u"}], "has_more": True, "page_token": "same"}},
            {"data": {"items": [], "has_more": True, "page_token": "same"}},
        ]
    )

    async def call(*args, **kwargs):
        return next(pages)

    monkeypatch.setattr(client, "call", call)
    try:
        with pytest.raises(FeishuError, match="pagination"):
            await client.pages("/users", {})
    finally:
        await client.aclose()


def test_feishu_directory_requires_stable_user_id_and_preserves_identity():
    row = {
        "user_id": "u",
        "open_id": "ou",
        "union_id": "on",
        "name": "员工",
        "department_ids": ["1"],
        "email": "a@example.com",
    }
    directory = parse_feishu_directory([{"department_id": "1", "name": "团队"}], [row, row])
    assert len(directory.users) == 1
    assert directory.users[0].profile == {"open_id": "ou", "union_id": "on"}
    assert directory.users[0].active
    with pytest.raises(FeishuError, match="user_id"):
        parse_feishu_directory([], [{"open_id": "ou"}])
    with pytest.raises(FeishuError, match="inconsistent"):
        parse_feishu_directory([], [row, {**row, "email": "b@example.com"}])
