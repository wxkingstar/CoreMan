import pytest

from coreman.core.contacts.feishu_source import fetch_feishu_directory, fetch_feishu_user
from coreman.core.platforms.feishu import FeishuClient, FeishuError


async def test_individual_scope_never_reads_root_department(monkeypatch):
    client = FeishuClient("test", "synthetic")
    calls = []

    async def call(method, path, **kwargs):
        calls.append(path)
        if path.endswith("/scopes"):
            return {"data": {"department_ids": [], "user_ids": ["u1"], "has_more": False}}
        assert path.endswith("/users/u1")
        return {"data": {"user": {"user_id": "u1", "name": "Tester", "department_ids": ["0"]}}}

    monkeypatch.setattr(client, "call", call)
    try:
        directory = await fetch_feishu_directory(client)
        assert [u.platform_user_id for u in directory.users] == ["u1"]
        assert len(calls) == 2
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "data",
    [
        {"user_ids": [], "has_more": True, "page_token": ""},
        {"user_ids": [], "has_more": False, "group_ids": ["g1"]},
        {"user_ids": ["../x"], "has_more": False},
    ],
)
async def test_incomplete_or_unsupported_scope_fails_closed(monkeypatch, data):
    client = FeishuClient("test", "synthetic")

    async def call(*args, **kwargs):
        return {"data": data}

    monkeypatch.setattr(client, "call", call)
    try:
        with pytest.raises(FeishuError):
            await fetch_feishu_directory(client)
    finally:
        await client.aclose()


async def test_scoped_department_collects_descendants_and_members(monkeypatch):
    client = FeishuClient("test", "synthetic")

    async def call(method, path, **kwargs):
        if path.endswith("/scopes"):
            return {"data": {"department_ids": ["d1"], "user_ids": [], "has_more": False}}
        assert path.endswith("/departments/d1")
        return {"data": {"department": {"department_id": "d1", "name": "Team"}}}

    async def pages(path, params):
        if path.endswith("/children"):
            assert "/d1/" in path
            return [{"department_id": "d2", "name": "Child", "parent_department_id": "d1"}]
        assert params["department_id"] in {"d1", "d2"}
        return [{"user_id": "u1", "name": "Tester"}]

    monkeypatch.setattr(client, "call", call)
    monkeypatch.setattr(client, "pages", pages)
    try:
        directory = await fetch_feishu_directory(client)
        assert len(directory.departments) == 2
        assert len(directory.users) == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"user_id": "u1", "name": "Tester", "department_ids": ["d1"]}, "u1"),
        ({"user_id": "u1", "name": "Tester", "status": {"is_resigned": True}}, None),
    ],
)
async def test_fetch_single_user(monkeypatch, row, expected):
    client = FeishuClient("test", "synthetic")
    calls = []

    async def call(method, path, **kwargs):
        calls.append((path, kwargs["params"]["user_id_type"]))
        return {"data": {"user": row}}

    monkeypatch.setattr(client, "call", call)
    try:
        member = await fetch_feishu_user(client, "u1")
        assert (member.platform_user_id if member else None) == expected
        assert calls == [("/open-apis/contact/v3/users/u1", "user_id")]
    finally:
        await client.aclose()


@pytest.mark.parametrize(("user_id", "returned"), [("../x", "../x"), ("u1", "u2")])
async def test_fetch_single_user_fails_closed(monkeypatch, user_id, returned):
    client = FeishuClient("test", "synthetic")

    async def call(method, path, **kwargs):
        assert "/" not in user_id
        return {"data": {"user": {"user_id": returned}}}

    monkeypatch.setattr(client, "call", call)
    try:
        with pytest.raises(FeishuError):
            await fetch_feishu_user(client, user_id)
    finally:
        await client.aclose()
