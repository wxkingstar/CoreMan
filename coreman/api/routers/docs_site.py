"""/docs 文档站与 /integration/ 下的公开说明：不需要登录，人看网页、AI 读 Markdown。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from coreman import docs_site
from coreman.api.errors import not_found

router = APIRouter(include_in_schema=False)

CACHE = {"Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"}
HTML_HEADERS = {
    **CACHE,
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "Vary": "Accept",
}


def _site(request: Request) -> docs_site.DocsSite:
    return docs_site.site(request.app.state.settings.public_base_url)


def _markdown(text: str, filename: str) -> Response:
    return Response(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={**CACHE, "Content-Disposition": f'inline; filename="{filename}"'},
    )


def _text(text: str) -> Response:
    return Response(text, media_type="text/plain; charset=utf-8", headers=CACHE)


def _wants_markdown(request: Request) -> bool:
    if request.query_params.get("for") == "ai":
        return True
    accept = request.headers.get("accept", "")
    return "text/markdown" in accept and "text/html" not in accept


def _page(request: Request, slug: str) -> Response:
    site = _site(request)
    if _wants_markdown(request):
        return _markdown(site.markdown(slug), f"{slug}.md")
    return HTMLResponse(site.html(slug), headers=HTML_HEADERS)


@router.get("/docs")
@router.get("/docs/")
async def docs_index(request: Request) -> Response:
    return _page(request, "index")


@router.get("/docs/llms.txt")
async def llms_txt(request: Request) -> Response:
    return _text(_site(request).llms_txt())


@router.get("/docs/llms-full.txt")
async def llms_full(request: Request) -> Response:
    return _text(_site(request).llms_full())


@router.get("/docs/{name}")
async def docs_page(name: str, request: Request) -> Response:
    slug = name.removesuffix(".md")
    if slug not in docs_site.BY_SLUG:
        raise not_found("文档不存在")
    if name.endswith(".md"):
        return _markdown(_site(request).markdown(slug), name)
    return _page(request, slug)


def _integration(name: str, slug: str) -> None:
    async def handler(request: Request) -> Response:
        return _markdown(_site(request).markdown(slug), name)

    # 只登记这两份；同目录的规则集等仍是管理台静态文件，由 SPA 路由提供。
    router.add_api_route(f"/integration/{name}", handler, methods=["GET"])


for _name, _slug in docs_site.INTEGRATION_ALIASES.items():
    _integration(_name, _slug)
