import httpx

from coreman import __version__


async def test_health_ok(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__, "db": "ok"}


async def test_unknown_api_path_is_json_404(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/admin/nope")
    assert r.status_code == 404
    assert r.json()["code"] == 404
    assert "message" in r.json()
