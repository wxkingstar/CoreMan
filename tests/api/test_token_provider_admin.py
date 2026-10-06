import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.api.routers.infrastructure_admin import SystemIn
from coreman.core.db.models import AuditLog
from tests.api.conftest import login_as


def test_provider_fields_and_blank_normalization():
    body = SystemIn(name="ERP", token_provider="issuer", token_audience="  ", access_test_url="")
    assert body.token_provider == "issuer"
    assert body.token_audience is None
    assert body.access_test_url is None


async def test_provider_selection_preservation_validation_and_audit(
    client: httpx.AsyncClient, db_session: AsyncSession, app
):
    from types import SimpleNamespace

    app.state.settings.business_token_providers = {
        "issuer": SimpleNamespace(max_token_ttl_seconds=120)
    }
    await login_as(client, db_session, role="platform_admin")
    metadata = await client.get("/api/admin/token-providers")
    assert metadata.json()["data"] == [
        {"id": "builtin", "max_token_ttl_seconds": None},
        {"id": "issuer", "max_token_ttl_seconds": 120},
    ]
    body = {
        "key": "erp",
        "name": "ERP",
        "base_url": "https://erp.example",
        "token_provider": "issuer",
        "token_audience": "erp-api",
        "access_test_url": "https://erp.example/api/me",
    }
    result = await client.post("/api/admin/systems", json=body)
    assert result.status_code == 201, result.text
    row = result.json()["data"]
    app.state.settings.business_token_providers = {}
    result = await client.put(
        "/api/admin/systems/erp",
        json={"name": "Renamed", "base_url": body["base_url"]},
        headers={"If-Match": str(row["version"])},
    )
    assert result.status_code == 200, result.text
    row = result.json()["data"]
    assert row["token_provider"] == "issuer" and row["token_audience"] == "erp-api"
    for changes in (
        {"token_provider": "missing"},
        {"access_test_url": "https://other.example/api/me"},
        {"base_url": "https://other.example"},
    ):
        result = await client.put(
            "/api/admin/systems/erp",
            json={"name": "ERP", "base_url": body["base_url"], **changes},
            headers={"If-Match": str(row["version"])},
        )
        assert result.status_code == 422, result.text
    result = await client.put(
        "/api/admin/systems/erp",
        json={
            "name": "ERP",
            "base_url": body["base_url"],
            "token_provider": "builtin",
            "token_audience": "",
            "access_test_url": "",
        },
        headers={"If-Match": str(row["version"])},
    )
    assert result.status_code == 200, result.text
    logs = list(
        (
            await db_session.execute(select(AuditLog).where(AuditLog.action == "system.update"))
        ).scalars()
    )
    assert any(log.diff and log.diff.get("token_provider") == ["issuer", "builtin"] for log in logs)
    assert (
        await client.post(
            "/api/admin/systems",
            json={"key": "unknown", "name": "Unknown", "token_provider": "missing"},
        )
    ).status_code == 422
    await login_as(client, db_session, role="team_lead")
    assert (await client.get("/api/admin/token-providers")).status_code == 403


def test_origin_and_removed_provider_validation_without_database():
    from types import SimpleNamespace

    import pytest

    from coreman.api.errors import ApiError
    from coreman.api.routers.infrastructure_admin import validate_provider_settings

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(settings=SimpleNamespace(business_token_providers={}))
        )
    )
    values = {
        "token_provider": "removed",
        "base_url": "https://erp.example",
        "access_test_url": "https://erp.example:443/api/me",
    }
    validate_provider_settings(request, values, "removed")
    with pytest.raises(ApiError):
        validate_provider_settings(request, values)
    for url in (
        "http://erp.example/api/me",
        "https://erp.example:444/api/me",
        "https://other.example/api/me",
        "https://erp.example/api/me#fragment",
    ):
        with pytest.raises(ApiError):
            validate_provider_settings(request, {**values, "access_test_url": url}, "removed")


async def test_external_access_probe_issues_for_current_user_and_hides_secrets(
    client, db_session, app, monkeypatch
):
    from coreman.api.routers import infra_system_test
    from coreman.core.auth.provider_config import HTTPTokenProviderConfig
    from coreman.core.auth.token_providers import IssuedToken, TokenProviderError

    app.state.settings.business_token_providers = {
        "issuer": HTTPTokenProviderConfig(
            token_url="https://issuer.example/token",
            client_id="client",
            client_secret="synthetic-secret",
        )
    }
    await login_as(client, db_session, role="platform_admin", login_name="real-human")
    body = {
        "key": "external",
        "name": "External",
        "base_url": "https://erp.example",
        "token_provider": "issuer",
        "access_test_url": "https://erp.example/api/me",
    }
    assert (await client.post("/api/admin/systems", json=body)).status_code == 201
    issued_requests = []

    async def issue(session, cipher, **kwargs):
        issued_requests.append(kwargs)
        return IssuedToken("synthetic-bearer-token", 60, 9999999999, "bearer")

    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == body["access_test_url"]
        assert "cookie" not in request.headers
        return httpx.Response(
            200 if "authorization" in request.headers else 401, text="synthetic-private-body"
        )

    monkeypatch.setattr(infra_system_test, "issue_system_token", issue)
    monkeypatch.setattr(
        infra_system_test,
        "make_http",
        lambda url: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    path = "/api/admin/systems/test-access"
    response = await client.post(
        path, json={"system_key": "external", "email_prefix": "someone-else"}
    )
    assert response.status_code == 403 and not issued_requests
    response = await client.post(path, json={"system_key": "external"})
    assert response.status_code == 200 and response.json()["data"]["success"]
    assert issued_requests[0]["subject"] == "real-human" and issued_requests[0]["ttl_seconds"] == 60
    assert requests[1].headers["authorization"] == "Bearer synthetic-bearer-token"
    assert "synthetic" not in response.text

    async def fail_issue(*args, **kwargs):
        raise TokenProviderError("issuer_unavailable")

    monkeypatch.setattr(infra_system_test, "issue_system_token", fail_issue)
    response = await client.post(path, json={"system_key": "external"})
    assert response.status_code == 422 and "synthetic" not in response.text
    assert len(requests) == 2


async def test_reserved_audience_cannot_mint_platform_credentials(client, db_session):
    await login_as(client, db_session, role="platform_admin")
    result = await client.post(
        "/api/admin/systems", json={"key": "erp", "name": "ERP", "token_audience": "coreman"}
    )
    assert result.status_code == 422, result.text
    result = await client.post("/api/admin/systems", json={"key": "erp", "name": "ERP"})
    row = result.json()["data"]
    result = await client.put(
        "/api/admin/systems/erp",
        json={"name": "ERP", "token_audience": "coreman"},
        headers={"If-Match": str(row["version"])},
    )
    assert result.status_code == 422, result.text
