"""Catalog loader, compiler, contract checks and search: everything that needs no database."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
import yaml

from coreman.core.systems_catalog import lint as contract
from coreman.core.systems_catalog.compiler import compile_spec, server_url
from coreman.core.systems_catalog.loader import MAX_BYTES, SpecError, load
from coreman.core.systems_catalog.search import SearchIndex, tokens
from coreman.core.systems_catalog.service import Held, resolve_pointer
from tests.fakes.business_system import BASE_URL, ROOT, SPEC_URL, contract_example, extended_spec

RULESET = ROOT / "web/public/integration/business-system-contract.spectral.yaml"


def compiled(spec: dict[str, Any]) -> Any:
    return compile_spec(spec, spec_url=SPEC_URL, base_url=BASE_URL)


def minimal(**operations: dict[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {"title": "库存", "version": "1"},
        "tags": [{"name": "documents", "description": "单据"}],
        "paths": {},
    }
    for path, op in operations.items():
        spec["paths"]["/" + path] = op
    return spec


def op(op_id: str | None = "getThing", method: str = "get", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "tags": ["documents"],
        "summary": "取一个东西",
        "x-permission": "none",
        "responses": {"200": {"description": "OK"}},
        **extra,
    }
    if op_id is not None:
        body["operationId"] = op_id
    return {method: body}


# Loader -----------------------------------------------------------------------------------------


def test_contract_example_loads_compiles_and_passes_every_check() -> None:
    result = compiled(load(contract_example().encode()))
    assert result.lint == []
    assert (result.operation_count, result.hidden_count, result.module_count) == (2, 0, 1)
    assert result.compiled["permissions"] == {
        "operation_id": "getMe",
        "pointer": "/data/permissions",
        "method": "GET",
        "path": "/api/stock/me",
    }
    cancel = result.compiled["operations"]["cancelDocument"]
    assert cancel["risk"] == "destructive" and cancel["permission"] == ["stock:doc:confirm"]
    assert cancel["detail"]["body"]["schema"]["required"] == ["reason"]


def test_yaml_alias_expansion_is_rejected_before_construction() -> None:
    lines = ["a: &a [x, x, x, x, x, x, x, x, x, x]"]
    for index in range(1, 9):
        name, prev = chr(97 + index), chr(96 + index)
        lines.append(f"{name}: &{name} [" + ", ".join([f"*{prev}"] * 10) + "]")
    with pytest.raises(SpecError) as exc:
        load("\n".join(lines).encode())
    assert exc.value.code == "too_many_nodes"
    # A few aliases are fine: they count at their expanded size, well below the cap.
    assert load(b"a: &a [1, 2]\nb: *a\n") == {"a": [1, 2], "b": [1, 2]}


def test_yaml_uses_the_safe_loader_and_json_types() -> None:
    with pytest.raises(SpecError):
        load(b"a: !!python/object/apply:os.system ['true']\n")
    data = load(b"openapi: 3.0.3\non: off\nday: 2026-01-01\nresponses:\n  200: {}\nn: .nan\n")
    # YAML 1.1 booleans and dates stay strings; numeric keys such as status codes become strings.
    assert data == {
        "openapi": "3.0.3",
        "on": "off",
        "day": "2026-01-01",
        "responses": {"200": {}},
        "n": "nan",
    }


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b'{"a": NaN}', "invalid_document"),
        (b"[1, 2]", "invalid_document"),
        (b"\xff\xfe", "not_utf8"),
        (b"[" * 5000 + b"]" * 5000, "too_deep"),
        (b"x" * (MAX_BYTES + 1), "too_large"),
    ],
)
def test_loader_errors_are_codes(raw: bytes, code: str) -> None:
    with pytest.raises(SpecError) as exc:
        load(raw)
    assert exc.value.code == code


# Compiler ---------------------------------------------------------------------------------------


def test_missing_and_duplicate_operation_ids_are_skipped_and_reported() -> None:
    spec = minimal(a=op(None), b=op("dup"), c=op("dup"), d=op("keep"))
    result = compiled(spec)
    assert list(result.compiled["operations"]) == ["keep"]
    rules = {item["rule"] for item in result.lint}
    assert {"operation-operationId", "operation-operationId-unique"} <= rules


def test_hidden_operations_and_risk_defaults() -> None:
    spec = minimal(
        a=op("readIt"),
        b=op("writeIt", "post"),
        c=op("queryIt", "post", **{"x-agent": {"risk": "read"}}),
        d=op("secret", "post", **{"x-agent": {"hidden": True}}),
        e=op("headIt", "head"),
    )
    result = compiled(spec)
    ops = result.compiled["operations"]
    assert "secret" not in ops and result.hidden_count == 1
    assert {k: v["risk"] for k, v in ops.items()} == {
        "readIt": "read",
        "writeIt": "write",
        "queryIt": "read",
        "headIt": "read",
    }


def test_permission_normalization() -> None:
    spec = minimal(
        a=op("one", **{"x-permission": "a:b"}),
        b=op("many", **{"x-permission": ["a:b", "c:d"]}),
        c=op("anyone", **{"x-permission": "none"}),
    )
    ops = compiled(spec).compiled["operations"]
    assert ops["one"]["permission"] == ["a:b"]
    assert ops["many"]["permission"] == ["a:b", "c:d"]
    assert ops["anyone"]["permission"] == []


def test_cyclic_refs_are_cut_and_external_refs_never_followed() -> None:
    spec = minimal(
        a=op(
            "tree",
            "post",
            requestBody={
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}
            },
        )
    )
    spec["components"] = {
        "schemas": {
            "Node": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "children": {"type": "array", "items": {"$ref": "#/components/schemas/Node"}},
                    "remote": {"$ref": "https://evil.example/schema.json"},
                },
            }
        }
    }
    result = compiled(spec)
    schema = result.compiled["operations"]["tree"]["detail"]["body"]["schema"]
    assert schema["properties"]["children"]["items"] == {
        "$ref": "#/components/schemas/Node",
        "recursive": True,
    }
    assert schema["properties"]["remote"] == {
        "$ref": "https://evil.example/schema.json",
        "external": True,
    }
    assert any(item["rule"] == "contract-local-refs" for item in result.lint)


def test_depth_width_and_enum_limits() -> None:
    deep: dict[str, Any] = {"type": "string"}
    for level in range(6):
        deep = {"type": "object", "properties": {f"l{level}": deep}}
    wide = {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(55)}}
    spec = minimal(
        a=op(
            "shape",
            "post",
            parameters=[
                {
                    "name": "brand",
                    "in": "query",
                    "description": "品牌",
                    "schema": {"type": "string", "enum": [f"b{i}" for i in range(60)]},
                    "x-agent-options": "listBrands",
                }
            ],
            requestBody={
                "content": {
                    "application/json": {
                        "schema": {"type": "object", "properties": {"deep": deep, "wide": wide}}
                    }
                }
            },
        )
    )
    result = compiled(spec)
    detail = result.compiled["operations"]["shape"]["detail"]
    brand = detail["parameters"][0]
    assert len(brand["enum"]) == 20 and brand["enum_total"] == 60
    assert brand["options"] == "listBrands"
    body = detail["body"]["schema"]["properties"]
    assert body["wide"]["omitted_properties"] == 15 and len(body["wide"]["properties"]) == 40
    level = body["deep"]
    for _ in range(3):
        level = next(iter(level["properties"].values()))
    assert level.get("truncated") is True and "properties" not in level
    assert any(item["rule"] == "contract-enum-size" for item in result.lint)


def test_all_of_is_merged_and_examples_are_clipped() -> None:
    spec = minimal(
        a=op(
            "merge",
            "post",
            requestBody={
                "content": {
                    "application/json": {
                        "schema": {
                            "allOf": [
                                {"$ref": "#/components/schemas/Base"},
                                {"type": "object", "required": ["b"], "properties": {"b": {}}},
                            ]
                        },
                        "example": {"text": "x" * 3000},
                    }
                }
            },
        )
    )
    spec["components"] = {
        "schemas": {"Base": {"type": "object", "required": ["a"], "properties": {"a": {}}}}
    }
    detail = compiled(spec).compiled["operations"]["merge"]["detail"]
    assert detail["body"]["schema"]["required"] == ["a", "b"]
    assert set(detail["body"]["schema"]["properties"]) == {"a", "b"}
    assert len(detail["example"]) == 1000


def test_server_url_stays_on_the_system_origin() -> None:
    assert server_url({}, SPEC_URL, BASE_URL) == BASE_URL
    assert server_url({"servers": [{"url": "/v1"}]}, SPEC_URL, BASE_URL) == BASE_URL + "/v1"
    other = {"servers": [{"url": "https://evil.example/api"}]}
    assert server_url(other, SPEC_URL, BASE_URL) == BASE_URL
    templated = {"servers": [{"url": "/{ver}", "variables": {"ver": {"default": "v2"}}}]}
    assert server_url(templated, SPEC_URL, BASE_URL) == BASE_URL + "/v2"


# Contract checks --------------------------------------------------------------------------------


def test_rules_match_the_published_spectral_ruleset() -> None:
    ruleset = yaml.safe_load(RULESET.read_text(encoding="utf-8"))
    published = {
        name: (rule if isinstance(rule, str) else rule["severity"])
        for name, rule in ruleset["rules"].items()
    }
    assert published == contract.RULES


def broken_spec() -> dict[str, Any]:
    spec = copy.deepcopy(extended_spec())
    spec["x-agent"] = {"guide": "g", "extra": 1}
    spec["tags"].append({"name": "undocumented"})
    paths = spec["paths"]
    paths["/api/x/{id}"] = {
        "get": {
            "operationId": "9bad",
            "tags": ["ghost"],
            "summary": "s" * 81,
            "x-agent": {"risk": "maybe"},
            "parameters": [{"name": "other", "in": "path", "schema": {"type": "string"}}],
            "responses": {
                "200": {"description": "OK", "content": {"application/json": {}}},
                "201": {
                    "description": "OK",
                    "content": {"application/json": {"schema": {"type": "object"}}},
                },
            },
        },
        "put": {"tags": [], "responses": {}, "x-permission": ""},
    }
    return spec


def test_each_rule_reports_its_own_problem() -> None:
    found = contract.lint(broken_spec())
    rules = {item["rule"] for item in found}
    assert rules >= {
        "path-params",
        "operation-operationId",
        "operation-tags",
        "operation-tag-defined",
        "contract-operation-id-pattern",
        "contract-summary",
        "contract-permission",
        "contract-operation-x-agent",
        "contract-root-x-agent",
        "contract-root-permissions",
        "contract-tag-description",
        "contract-parameter-description",
        "contract-success-schema",
        "contract-success-not-free-form",
    }
    severities = [item["severity"] for item in found]
    assert severities == sorted(severities, key=lambda s: s != "error")
    assert contract.lint({"openapi": "2.0", "paths": []})[0]["rule"] == "oas3-schema"


def test_findings_are_capped() -> None:
    spec = minimal(**{f"p{i}": op(None) for i in range(300)})
    assert len(contract.lint(spec)) == contract.MAX_FINDINGS


# Search and permissions -------------------------------------------------------------------------


def test_search_matches_chinese_bigrams_and_split_identifiers() -> None:
    assert {"cancel", "document", "canceldocument"} <= set(tokens("cancelDocument"))
    assert tokens("作废单据") == ["作废", "废单", "单据"]
    index = SearchIndex(compiled(extended_spec()).compiled)
    assert index.search("作废单据")[0][0] == "cancelDocument"
    assert index.search("daily report")[0][0] == "dailyReport"
    assert index.search("nothing-like-this") == []


def test_permission_matching_follows_the_contract() -> None:
    held = Held(["stock:doc:*", "stock:report:read"])
    assert held.allows(["stock:doc:confirm", "stock:report:read"])
    assert not held.allows(["stock:doc"]) and not held.allows(["stock:finance"])
    assert Held(["*"]).allows(["anything:at:all"])
    assert Held([]).allows([])


def test_json_pointer() -> None:
    document = json.loads('{"data": {"a/b": [1, {"c~d": "x"}]}}')
    assert resolve_pointer(document, "/data/a~1b/1/c~0d") == "x"
    assert resolve_pointer(document, "/data/missing") is None
