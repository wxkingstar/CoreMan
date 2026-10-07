"""Proxied calls without a database: schema validation, request building, response shaping."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from coreman.core.systems_catalog.compiler import compile_spec
from coreman.core.systems_catalog.proxy import RESULT_MAX, Call, build_request, fit_json, shape
from coreman.core.systems_catalog.validate import Validator, coerce
from tests.fakes.business_system import BASE_URL, SPEC_URL, extended_spec


def compiled() -> dict[str, Any]:
    return compile_spec(extended_spec(), spec_url=SPEC_URL, base_url=BASE_URL).compiled


def test_validator_core_rules() -> None:
    v = Validator({"#/components/schemas/Node": {"$ref": "#/components/schemas/Node"}})
    schema = {
        "type": "object",
        "required": ["name", "id"],
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string", "readOnly": True},
            "name": {"type": "string", "minLength": 2, "maxLength": 4},
            "count": {"type": "integer", "minimum": 1, "exclusiveMaximum": 10},
            "ratio": {"type": "number", "maximum": 1, "exclusiveMaximum": True},
            "tags": {"type": "array", "maxItems": 2, "items": {"enum": ["a", "b"]}},
            "note": {"type": "string", "nullable": True},
            "kind": {"type": ["string", "null"]},
            "loop": {"$ref": "#/components/schemas/Node"},
            "either": {"anyOf": [{"type": "integer"}, {"type": "boolean"}]},
        },
    }
    assert (
        v.errors({"name": "ok", "note": None, "kind": None, "loop": 1, "either": True}, schema)
        == []
    )
    errors = v.errors(
        {"name": "x", "count": 10, "ratio": 1, "tags": ["a", "c", "b"], "extra": 1, "either": "s"},
        schema,
    )
    assert "$.name: needs at least 2 characters" in errors
    assert len(errors) == 5  # capped
    assert v.errors({"name": "okay", "count": 1.5}, schema) == ["$.count: expected integer"]
    assert v.errors(True, {"type": "integer"}) == ["$: expected integer"]
    assert v.errors(3.0, {"type": "integer"}) == []


def test_coercion_of_path_and_query_strings() -> None:
    v = Validator({})
    assert coerce("5", {"type": "integer"}, v) == 5
    assert coerce("2.5", {"type": "number"}, v) == 2.5
    assert coerce("true", {"type": "boolean"}, v) is True
    assert coerce("abc", {"type": "integer"}, v) == "abc"
    assert coerce(["1", "2"], {"type": "array", "items": {"type": "integer"}}, v) == [1, 2]


def test_build_request_checks_every_parameter() -> None:
    catalog = compiled()
    ops, defs = catalog["operations"], catalog["defs"]
    path, pairs, body, problems = build_request(
        ops["listDocuments"],
        defs,
        Call(system="stock", operation_id="x", query={"status": "draft", "page": "2"}),
    )
    assert (path, pairs, body, problems) == (
        "/api/stock/documents",
        [("status", "draft"), ("page", "2")],
        None,
        [],
    )
    _, _, _, problems = build_request(
        ops["listDocuments"],
        defs,
        Call(system="stock", operation_id="x", query={"status": "lost", "page": 0, "other": 1}),
    )
    assert any("query.status: must be one of" in p for p in problems)
    assert "query.page: must be ≥ 1" in problems and "query.other: unknown parameter" in problems
    _, _, _, problems = build_request(
        ops["listDocuments"], defs, Call(system="s", operation_id="x")
    )
    assert problems == ["query.status: required"]
    _, _, _, problems = build_request(
        ops["createDraft"], defs, Call(system="s", operation_id="x", body={"sku": "A", "x": 1})
    )
    assert problems == ["body.quantity: required", "body.x: not allowed"]
    _, _, _, problems = build_request(ops["createDraft"], defs, Call(system="s", operation_id="x"))
    assert problems == ["body: required"]
    _, _, _, problems = build_request(
        ops["getMe"], defs, Call(system="s", operation_id="x", body={"a": 1})
    )
    assert problems == ["body: this operation takes no request body"]


def test_path_values_cannot_escape_their_segment() -> None:
    op = compiled()["operations"]["cancelDocument"]
    path, _, _, problems = build_request(
        op,
        {},
        Call(system="s", operation_id="x", path_params={"id": "../me?x=1#"}, body={"reason": "r"}),
    )
    assert problems == []
    assert path == "/api/stock/documents/..%2Fme%3Fx%3D1%23/cancel"
    _, _, _, problems = build_request(
        op, {}, Call(system="s", operation_id="x", path_params={"id": ""}, body={"reason": "r"})
    )
    assert problems == ["path_params.id: must not be empty"]


def test_large_json_is_trimmed_by_lists_first() -> None:
    data = {"code": 0, "data": [{"i": i, "name": "x" * 50} for i in range(5000)]}
    trimmed, omitted = fit_json(data, RESULT_MAX)
    assert len(json.dumps(trimmed, ensure_ascii=False)) <= RESULT_MAX
    assert trimmed["code"] == 0 and omitted == 5000 - len(trimmed["data"])
    assert fit_json({"a": 1}, RESULT_MAX) == ({"a": 1}, None)


def response(status: int, content: bytes, headers: dict[str, str]) -> httpx.Response:
    return httpx.Response(status, content=content, headers=headers)


def test_shape_redacts_echoed_tokens_and_keeps_files_out() -> None:
    echoed = response(200, b'{"auth": "Bearer tok-123456"}', {"Content-Type": "application/json"})
    result = shape(echoed, echoed.content, False, "tok-123456")
    assert result["data"] == {"auth": "Bearer [REDACTED]"}
    pdf = response(
        200,
        b"%PDF-1.7 binary",
        {"Content-Type": "application/pdf", "Content-Disposition": 'attachment; filename="a.pdf"'},
    )
    assert shape(pdf, pdf.content, False, "tok") == {
        "status": 200,
        "ok": True,
        "content_type": "application/pdf",
        "bytes": 15,
        "file": True,
    }
    moved = response(302, b"", {"Location": "https://elsewhere.example/?token=x"})
    assert "Location" not in json.dumps(shape(moved, b"", False, "tok"))
    error = response(
        403, b'{"message": "' + b"x" * 9000 + b'"}', {"Content-Type": "application/json"}
    )
    result = shape(error, error.content, False, "tok")
    assert result["ok"] is False and result["truncated"] is True


@pytest.mark.parametrize("cut_off", [True, False])
def test_shape_text(cut_off: bool) -> None:
    text = response(200, b"plain " * 30000, {"Content-Type": "text/plain"})
    result = shape(text, text.content, cut_off, "tok")
    assert len(result["text"]) == RESULT_MAX and result["truncated"] is True
