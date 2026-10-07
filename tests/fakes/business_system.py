"""A business system that follows the integration contract, served through httpx.MockTransport.

It starts from the minimal example in the published contract (so the tests break if the example
stops compiling) and adds a few operations the tests need. Every request is recorded; requests
without a business token get 401, like a real system.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import jwt
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "web/public/integration/business-system-openapi-contract.md"
BASE_URL = "https://stock.example.com"
SPEC_URL = BASE_URL + "/openapi.yaml"


def contract_example() -> str:
    """The YAML block of section 9 ("最小示例") of the published contract."""
    text = CONTRACT.read_text(encoding="utf-8")
    section = text.split("## 9.", 1)[1]
    return section.split("```yaml\n", 1)[1].split("\n```", 1)[0] + "\n"


def extended_spec() -> dict[str, Any]:
    """The contract example plus a read list, a write, a hidden op and a financial op."""
    spec = yaml.safe_load(contract_example())
    spec["tags"].append({"name": "reports", "description": "库存报表：按仓库与日期统计"})
    obj = {"type": "object", "properties": {"data": {"type": "object"}}}
    ok = {"200": {"description": "OK", "content": {"application/json": {"schema": obj}}}}
    spec["paths"]["/api/stock/documents"] = {
        "get": {
            "operationId": "listDocuments",
            "tags": ["documents"],
            "summary": "按状态分页查询出入库单据",
            "x-permission": "stock:doc:read",
            "parameters": [
                {
                    "name": "status",
                    "in": "query",
                    "required": True,
                    "description": "单据状态",
                    "schema": {"type": "string", "enum": ["draft", "confirmed", "cancelled"]},
                },
                {
                    "name": "page",
                    "in": "query",
                    "description": "页码",
                    "schema": {"type": "integer", "minimum": 1},
                },
            ],
            "responses": ok,
        },
        "post": {
            "operationId": "createDraft",
            "tags": ["documents"],
            "summary": "创建一张出入库单据草稿",
            "x-permission": "stock:doc:write",
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["sku", "quantity"],
                            "additionalProperties": False,
                            "properties": {
                                "sku": {"type": "string", "description": "商品编码"},
                                "quantity": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": "数量",
                                },
                            },
                        },
                        "example": {"sku": "SKU-1", "quantity": 2},
                    }
                },
            },
            "responses": ok,
        },
    }
    spec["paths"]["/api/stock/reports/daily"] = {
        "post": {
            "operationId": "dailyReport",
            "tags": ["reports"],
            "summary": "按仓库统计每日出入库数量",
            "x-permission": ["stock:report:read"],
            "x-agent": {"risk": "read", "hint": "日期为 UTC"},
            "requestBody": {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"day": {"type": "string", "description": "日期"}},
                        }
                    }
                }
            },
            "responses": ok,
        }
    }
    spec["paths"]["/api/stock/refunds"] = {
        "post": {
            "operationId": "refundSupplier",
            "tags": ["reports"],
            "summary": "向供应商退款",
            "x-permission": "stock:finance",
            "x-agent": {"risk": "financial"},
            "responses": ok,
        }
    }
    spec["paths"]["/api/stock/internal/rebuild"] = {
        "post": {
            "operationId": "rebuildIndex",
            "tags": ["reports"],
            "summary": "重建内部索引",
            "x-permission": "none",
            "x-agent": {"hidden": True},
            "responses": ok,
        }
    }
    return spec


class FakeBusinessSystem:
    def __init__(
        self,
        spec: dict[str, Any] | str | None = None,
        *,
        permissions: list[str] | None = None,
    ) -> None:
        self.spec: dict[str, Any] | str = spec if spec is not None else extended_spec()
        # None: the permission lookup fails with 500.
        self.permissions = permissions if permissions is not None else ["*"]
        self.requests: list[httpx.Request] = []
        self.spec_override: httpx.Response | None = None
        self.documents: dict[str, dict[str, Any]] = {"D1": {"id": "D1", "status": "confirmed"}}
        # Extra handlers a test adds, keyed by (method, path).
        self.routes: dict[tuple[str, str], Any] = {}

    def body(self) -> bytes:
        if isinstance(self.spec, str):
            return self.spec.encode()
        dumped = yaml.safe_dump(copy.deepcopy(self.spec), allow_unicode=True, sort_keys=False)
        return dumped.encode()

    @staticmethod
    def token(request: httpx.Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        cookie = request.headers.get("cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "bot_token" and value:
                return value
        return None

    def subject(self, request: httpx.Request) -> str | None:
        token = self.token(request)
        if token is None:
            return None
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            return token
        return str(claims.get("sub"))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.token(request) is None:
            return httpx.Response(401, json={"code": 401, "message": "login required"})
        path, method = request.url.path, request.method
        if (method, path) in self.routes:
            return self.routes[(method, path)](request)  # type: ignore[no-any-return]
        if path == "/openapi.yaml" and method == "GET":
            if self.spec_override is not None:
                return self.spec_override
            body = self.body()
            etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
            if request.headers.get("if-none-match") == etag:
                return httpx.Response(304, headers={"ETag": etag})
            return httpx.Response(
                200, content=body, headers={"ETag": etag, "Content-Type": "application/yaml"}
            )
        if path == "/api/stock/me" and method == "GET":
            if self.permissions is None:
                return httpx.Response(500, json={"code": 500})
            return httpx.Response(200, json={"code": 0, "data": {"permissions": self.permissions}})
        if path == "/api/stock/documents" and method == "GET":
            status = request.url.params.get("status")
            items = [d for d in self.documents.values() if d["status"] == status]
            return httpx.Response(200, json={"code": 0, "data": {"items": items}})
        if path == "/api/stock/documents" and method == "POST":
            body = json.loads(request.content)
            doc = {"id": f"D{len(self.documents) + 1}", "status": "draft", **body}
            self.documents[doc["id"]] = doc
            return httpx.Response(200, json={"code": 0, "data": doc})
        if path == "/api/stock/reports/daily" and method == "POST":
            rows = [{"warehouse": f"W{i}", "in": i, "out": i} for i in range(5000)]
            return httpx.Response(200, json={"code": 0, "data": rows})
        return httpx.Response(404, json={"code": 404})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def install(monkeypatch: Any, fake: FakeBusinessSystem) -> None:
    """Route the catalog fetcher (and proxy calls) for stock.example.com to `fake`."""
    from coreman.core.systems_catalog import fetch

    def factory(url: httpx.URL) -> httpx.AsyncBaseTransport:
        if url.host != "stock.example.com":
            raise ValueError("unexpected host")
        return fake.transport()

    monkeypatch.setattr(fetch, "transport_factory", factory)
