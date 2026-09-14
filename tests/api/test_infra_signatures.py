import time

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.auth.signatures import sign_request
from tests.api.conftest import login_as


def signed(path: str, *, secret: str, timestamp: int | None = None) -> dict[str, str]:
    ts = str(timestamp or int(time.time()))
    return {
        "X-App-Key": "test-client",
        "X-Timestamp": ts,
        "X-Signature": sign_request("GET", path, {}, ts, "test-client", secret),
    }


async def test_org_requires_actual_path_signature_enabled_client_and_scope(
    client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await login_as(client, db_session, role="platform_admin")
    data = (
        await client.post(
            "/api/admin/api-clients",
            json={"app_key": "test-client", "name": "test", "scopes": ["org"]},
        )
    ).json()["data"]
    secret = data["secret"]
    paths = [
        "/api/infra/org/full",
        "/api/organization/full",
        "/api/infra/org/members",
        "/api/robot/organization/members",
        "/api/infra/org/tree",
        "/api/robot/organization/tree",
    ]
    for path in paths:
        assert (await client.get(path)).status_code == 401
        r = await client.get(path, headers=signed(path, secret=secret))
        assert r.status_code == 200, r.text
    assert (await client.get(paths[0], headers=signed(paths[1], secret=secret))).status_code == 401
    assert (
        await client.get(
            paths[0], headers=signed(paths[0], secret=secret, timestamp=int(time.time()) - 601)
        )
    ).status_code == 401
    assert (await client.get(paths[0], headers=signed(paths[0], secret="wrong"))).status_code == 401
    # last_used_at 不能抬升配置 version；原版本仍可用于权限更新。
    r = await client.put(
        "/api/admin/api-clients/test-client",
        json={"name": "test", "scopes": ["relay"]},
        headers={"If-Match": str(data["version"])},
    )
    assert r.status_code == 200, r.text
    assert (await client.get(paths[0], headers=signed(paths[0], secret=secret))).status_code == 403
    version = r.json()["data"]["version"]
    await client.put(
        "/api/admin/api-clients/test-client",
        json={"name": "test", "enabled": False, "scopes": ["org"]},
        headers={"If-Match": str(version)},
    )
    assert (await client.get(paths[0], headers=signed(paths[0], secret=secret))).status_code == 401
