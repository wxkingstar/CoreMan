async def test_internal_metrics_exposes_operational_counts(client):
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "coreman_tasks_queued" in response.text
    assert "version=0.0.4" in response.headers["content-type"]


async def test_failed_metrics_collection_never_exposes_exception(client, monkeypatch):
    from coreman.api.routers import metrics

    async def fail(*args):
        raise RuntimeError("private SQL configuration")

    monkeypatch.setattr(metrics, "render", fail)
    response = await client.get("/metrics")
    assert response.status_code == 503
    assert response.text == "coreman_metrics_collection_success 0\n"
