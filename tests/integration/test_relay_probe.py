import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coreman.core.db.models import ModelCatalog, RelayServer
from coreman.core.relay.client import RelayClient
from coreman.core.relay.probe import probe_relay


def _client(handler):  # type: ignore[no-untyped-def]
    return RelayClient(
        "http://r:1",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://r:1"),
    )


async def test_probe_writes_health_and_prefills_catalog(db_session: AsyncSession) -> None:
    relay = RelayServer(name="kimi01", host="10.0.0.9", clawrelay_port=50009, model_provider="kimi")
    db_session.add(relay)
    await db_session.commit()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health":
            return httpx.Response(
                200,
                json={"status": "healthy", "backend": "claude", "version": "2.2.0", "mode": "v1"},
            )
        return httpx.Response(
            200, json={"data": [{"id": "kimi/kimi-k2.5"}, {"id": "kimi/kimi-k3"}]}
        )

    version = relay.version
    health, added = await probe_relay(db_session, relay, _client(handler))
    await db_session.commit()
    assert health.status == "healthy" and added == ["kimi/kimi-k2.5", "kimi/kimi-k3"]
    await db_session.refresh(relay)
    assert (
        relay.health_status == "healthy"
        and relay.health_fail_count == 0
        and relay.relay_version == "2.2.0"
        and relay.relay_mode == "v1"
    )
    assert relay.version == version
    rows = (
        (
            await db_session.execute(
                select(ModelCatalog)
                .where(ModelCatalog.provider == "kimi")
                .order_by(ModelCatalog.model)
            )
        )
        .scalars()
        .all()
    )
    assert [(r.model, r.is_default) for r in rows] == [
        ("kimi/kimi-k2.5", True),
        ("kimi/kimi-k3", False),
    ]

    def down(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Claude CLI not available")

    health, added = await probe_relay(db_session, relay, _client(down))
    await db_session.commit()
    await db_session.refresh(relay)
    assert (
        health.status == "down"
        and added == []
        and relay.health_fail_count == 1
        and relay.health_detail is not None
    )
