"""个人助理的两个设置项：开通范围与可选的 AI；管理接口校验，默认值随设置一起返回。"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import Setting


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"assistant_eligibility": {"mode": "all"}}, 200),
        (
            {
                "assistant_eligibility": {
                    "mode": "listed",
                    "department_ids": [str(uuid.uuid4()).upper()],
                    "user_ids": [str(uuid.uuid4())],
                }
            },
            200,
        ),
        ({"assistant_eligibility": {"mode": "some"}}, 422),
        ({"assistant_eligibility": {"mode": "listed", "user_ids": ["李四"]}}, 422),
        ({"assistant_eligibility": None}, 422),
        ({"assistant_backends": ["codex"]}, 200),
        ({"assistant_backends": []}, 422),
        ({"assistant_backends": ["opencode"]}, 422),
    ],
)
async def test_assistant_settings_are_validated(
    admin_client: httpx.AsyncClient, db_session: AsyncSession, body: dict, status: int
) -> None:
    response = await admin_client.put("/api/admin/settings", json=body)
    assert response.status_code == status, response.text
    if status == 200:
        key = next(iter(body))
        stored = await db_session.scalar(select(Setting.value).where(Setting.key == key))
        if key == "assistant_eligibility":
            assert stored["mode"] == body[key]["mode"]
            assert all(v == v.lower() for v in stored["department_ids"])
        else:
            assert stored == body[key]


async def test_settings_defaults_include_the_assistant_keys(
    admin_client: httpx.AsyncClient,
) -> None:
    data = (await admin_client.get("/api/admin/settings")).json()["data"]
    assert data["assistant_eligibility"] == {"mode": "all", "department_ids": [], "user_ids": []}
    assert data["assistant_backends"] == ["claude", "codex"]
