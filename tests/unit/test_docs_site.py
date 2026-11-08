import re

from coreman import docs_site

BASE = "https://coreman.example.com"


def test_every_page_has_a_source_and_fills_in_the_address() -> None:
    site = docs_site.DocsSite(BASE + "/")
    for page in docs_site.PAGES:
        md = site.markdown(page.slug)
        assert md.startswith("# "), page.slug
        assert docs_site.PLACEHOLDER not in md
        assert docs_site.PLACEHOLDER not in site.html(page.slug)
    assert f"{BASE}/api/systems/mcp?token_env=SSO_TOKEN" in site.markdown("systems-catalog-mcp")
    assert f'BASE = "{BASE}"' in site.markdown("api-tasks")


def test_links_between_pages() -> None:
    site = docs_site.DocsSite(BASE)
    md = site.markdown("index")
    assert f"]({BASE}/docs/api-tasks.md)" in md
    assert "](api-tasks.md)" not in md
    page = site.html("index")
    assert 'href="/docs/api-tasks"' in page
    assert 'href="api-tasks.md"' not in page
    assert 'target="_blank" rel="noopener noreferrer"' in page  # 外链新开


def test_html_has_outline_search_index_and_markdown_link() -> None:
    page = docs_site.DocsSite(BASE).html("systems-catalog-mcp")
    assert '<h2 id="3-配置客户端">' in page
    assert 'href="#3-配置客户端"' in page
    assert '<link rel="alternate" type="text/markdown" href="/docs/systems-catalog-mcp.md"' in page
    assert "aria-current=page" in page
    pattern = r'<script id="search-index" type="application/json">(.*?)</script>'
    index = re.search(pattern, page, re.S)
    assert index and "通过 API 驱动 AI 员工" in index.group(1)


def test_raw_html_in_sources_is_escaped() -> None:
    body, _ = docs_site._render("<script>alert(1)</script>\n\n## a <b>\n")
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_llms_txt_lists_every_page() -> None:
    site = docs_site.DocsSite(BASE)
    text = site.llms_txt()
    assert text.startswith("# CoreMan 文档\n\n> ")
    for page in docs_site.PAGES:
        assert f"- [{page.title}]({BASE}/docs/{page.slug}.md): {page.summary}" in text
    full = site.llms_full()
    for page in docs_site.PAGES:
        assert site.markdown(page.slug).strip() in full
