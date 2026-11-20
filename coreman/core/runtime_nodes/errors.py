"""Local envelope validation is independent of runtime connectivity."""

import httpx


class RuntimeRequestRejected(httpx.RequestError):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        request: httpx.Request,
        request_bytes: int | None = None,
        limit_bytes: int | None = None,
    ) -> None:
        super().__init__(detail, request=request)
        self.code = code
        self.request_bytes = request_bytes
        self.limit_bytes = limit_bytes
