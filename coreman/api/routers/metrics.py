"""仅容器内部抓取；公网入口由 Caddy 明确阻断。"""

from fastapi import APIRouter, Request, Response

from coreman.core.observability.metrics import render

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    try:
        body = await render(request.app.state.session_factory)
    except Exception:
        return Response(
            "coreman_metrics_collection_success 0\n",
            status_code=503,
            media_type="text/plain; version=0.0.4",
        )
    return Response(body, media_type="text/plain; version=0.0.4")
