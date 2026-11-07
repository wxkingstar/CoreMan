import httpx


async def test_docs_pages_are_public(client: httpx.AsyncClient) -> None:
    client.cookies.clear()
    r = await client.get("/docs")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert "<title>CoreMan 文档</title>" in r.text
    assert (await client.get("/docs/")).status_code == 200
    r = await client.get("/docs/api-tasks")
    assert r.status_code == 200 and "通过 API 驱动 AI 员工" in r.text


async def test_markdown_for_agents(client: httpx.AsyncClient) -> None:
    r = await client.get("/docs/systems-catalog-mcp.md")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/markdown; charset=utf-8"
    assert "http://testserver/api/systems/mcp?token_env=SSO_TOKEN" in r.text
    assert (await client.get("/docs/systems-catalog-mcp?for=ai")).text == r.text
    negotiated = await client.get("/docs/systems-catalog-mcp", headers={"Accept": "text/markdown"})
    assert negotiated.text == r.text
    index = await client.get("/docs/llms.txt")
    assert index.headers["content-type"] == "text/plain; charset=utf-8"
    assert "(http://testserver/docs/systems-catalog-mcp.md)" in index.text
    assert r.text.strip() in (await client.get("/docs/llms-full.txt")).text


async def test_integration_addresses_keep_working(client: httpx.AsyncClient) -> None:
    guide = await client.get("/integration/api-tasks-agent-guide.md")
    assert guide.status_code == 200
    assert guide.text == (await client.get("/docs/api-tasks.md")).text
    assert "{{COREMAN_BASE_URL}}" not in guide.text
    contract = await client.get("/integration/business-system-openapi-contract.md")
    assert contract.text.startswith("# 业务系统 OpenAPI 接入规范")


async def test_unknown_page_is_404(client: httpx.AsyncClient) -> None:
    for path in ("/docs/nope", "/docs/nope.md", "/docs/../pyproject.toml"):
        assert (await client.get(path)).status_code == 404, path
