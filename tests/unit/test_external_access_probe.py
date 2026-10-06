import httpx
import pytest

from coreman.api.routers.infra_system_test import fetch, judge_external


@pytest.mark.parametrize(
    "baseline,result,success",
    [
        (401, 200, True),
        (403, 204, True),
        (200, 200, False),
        (302, 200, False),
        (401, 302, False),
        (401, 403, False),
    ],
)
def test_external_probe_requires_protected_api(baseline, result, success):
    assert judge_external("human", (baseline, None), (result, "/private"))[0] is success


async def test_external_probe_uses_bearer_not_cookie():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        client.cookies.set("private", "baseline-cookie")
        await fetch(client, httpx.URL("https://erp.example/api/me"), "synthetic-token", "bearer")
    assert seen[0].headers["authorization"] == "Bearer synthetic-token"
    assert "cookie" not in seen[0].headers


@pytest.mark.parametrize(
    "target",
    [
        None,
        "https://other.example/api",
        "http://erp.example/api",
        "https://erp.example:444/api",
        "https://user:secret@erp.example/api",
        "https://erp.example/api#fragment",
    ],
)
def test_external_probe_rejects_missing_or_unsafe_target(target):
    from coreman.api.routers.infra_system_test import probe_url
    from coreman.core.db.models import BusinessSystem

    system = BusinessSystem(
        key="erp",
        name="ERP",
        token_provider="issuer",
        base_url="https://erp.example",
        access_test_url=target,
    )
    with pytest.raises(ValueError):
        probe_url(system)


def test_external_probe_accepts_same_origin_and_default_port():
    from coreman.api.routers.infra_system_test import probe_url
    from coreman.core.db.models import BusinessSystem

    system = BusinessSystem(
        key="erp",
        name="ERP",
        token_provider="issuer",
        base_url="https://erp.example",
        access_test_url="https://erp.example:443/api/me",
    )
    assert probe_url(system).path == "/api/me"
