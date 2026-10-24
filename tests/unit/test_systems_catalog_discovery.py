"""Catalog discovery on a large console-sized catalog: layered browsing, paging, ranking, call
marks and detail trimming. The tools run against compiled catalogs directly; storage, refresh
and permission lookups are covered by tests/api/test_systems_catalog.py."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from coreman.core.db.models import BusinessSystem
from coreman.core.systems_catalog import context, service, tools
from coreman.core.systems_catalog.compiler import compile_spec
from coreman.core.systems_catalog.lint import lint
from coreman.core.systems_catalog.search import SearchIndex
from tests.fakes.ops_catalog import ops_spec

BASE = "https://ops.example.com"


def loaded_for(spec: dict[str, Any], sha: str = "sha-1") -> service.Loaded:
    compiled = compile_spec(spec, spec_url=BASE + "/openapi.json", base_url=BASE).compiled
    return service.Loaded("ops", "ok", sha, compiled, SearchIndex(compiled))


def ops_system(delivery: str = "env") -> BusinessSystem:
    return BusinessSystem(
        key="ops",
        name="运营后台",
        description="",
        base_url=BASE,
        openapi_url=BASE + "/openapi.json",
        token_delivery=delivery,
        token_provider="builtin",
    )


class Catalogs:
    """Serves one compiled catalog to the tools; `held` maps a context to its permission codes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, loaded: service.Loaded) -> None:
        self.loaded = loaded
        self.held: dict[int, service.Held | None] = {}

        async def catalog(ctx: context.Context, system: BusinessSystem) -> service.Loaded:
            return self.loaded

        async def held(
            ctx: context.Context, system: BusinessSystem, loaded: service.Loaded
        ) -> service.Held | None:
            return self.held.get(id(ctx))

        async def load(session: Any, key: str) -> service.Loaded:
            return self.loaded

        monkeypatch.setattr(context, "catalog", catalog)
        monkeypatch.setattr(context, "held", held)
        monkeypatch.setattr(service, "load", load)

    def ctx(
        self,
        *,
        delivery: str = "env",
        codes: list[str] | None = None,
        writable: frozenset[str] = frozenset(),
    ) -> context.Context:
        ctx = context.Context(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            [ops_system(delivery)],
            writable=writable,
        )
        self.held[id(ctx)] = service.Held(codes) if codes is not None else None
        return ctx


@pytest.fixture
def plain(monkeypatch: pytest.MonkeyPatch) -> Catalogs:
    """The console as it is today: no groups, display names or aliases."""
    return Catalogs(monkeypatch, loaded_for(ops_spec(metadata=False)))


@pytest.fixture
def described(monkeypatch: pytest.MonkeyPatch) -> Catalogs:
    """The same console with the optional contract fields filled in."""
    return Catalogs(monkeypatch, loaded_for(ops_spec(metadata=True)))


async def run(ctx: context.Context, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return await tools.dispatch(ctx, name, args)


def ids(result: dict[str, Any]) -> list[str]:
    return [item["operation"].split()[0] for item in result["items"]]


# Compiler and contract -----------------------------------------------------------------------


def test_compiles_groups_display_names_aliases_and_limits() -> None:
    compiled = loaded_for(ops_spec(metadata=True)).compiled
    groups = {g["name"]: g["modules"] for g in compiled["groups"]}
    assert groups["物流"][:2] == ["Carriers", "Ops Carriers"]
    assert sum(len(m) for m in groups.values()) == len(compiled["modules"]) == 78
    refunds = next(m for m in compiled["modules"] if m["name"] == "Refunds")
    assert refunds["display_name"] == "退款" and "退款" in refunds["aliases"]
    assert refunds["limits"].startswith("退款重试、取消、判责仅网页端可用")
    assert loaded_for(ops_spec(metadata=False)).compiled["groups"] == []


def test_ungrouped_modules_and_repeated_tags() -> None:
    spec = ops_spec(metadata=True)
    spec["x-tagGroups"] = [
        {"name": "物流", "tags": ["Carriers", "Ops Carriers"]},
        {"name": "退款", "tags": ["Carriers", "Refunds", "NoSuchTag"]},
    ]
    groups = loaded_for(spec).compiled["groups"]
    # A module stays in its first group; unknown tags are dropped; the rest go to one group.
    assert [g["name"] for g in groups] == ["物流", "退款", "其他"]
    assert groups[1]["modules"] == ["Refunds"]
    assert len(groups[2]["modules"]) == 75


def test_contract_checks_for_the_new_fields() -> None:
    spec = ops_spec(metadata=True)
    spec["tags"][0]["x-displayName"] = "x" * 61
    spec["tags"][1]["x-agent"] = {"aliases": "市场", "other": 1}
    spec["x-tagGroups"].append({"name": "空", "tags": []})
    rules = {(f["rule"], f["path"]) for f in lint(spec)}
    assert ("contract-tag-extensions", "#/tags/0/x-displayName") in rules
    assert ("contract-tag-extensions", "#/tags/1/x-agent/aliases") in rules
    assert ("contract-tag-extensions", "#/tags/1/x-agent/other") in rules
    assert ("contract-tag-groups", "#/x-tagGroups/12") in rules
    plain = {f["rule"] for f in lint(ops_spec(metadata=True))}
    # The generated summaries carry ticket numbers on purpose; the new fields themselves pass.
    assert "contract-summary-plain" in plain
    assert not plain & {"contract-tag-extensions", "contract-tag-groups"}


# Layered browsing ---------------------------------------------------------------------------


async def test_system_overview_is_groups_when_declared(described: Catalogs) -> None:
    ctx = described.ctx()
    overview = await run(ctx, "systems_browse", {"system": "ops"})
    assert overview["view"] == "groups"
    assert overview["total_modules"] == 78 and "modules" not in overview
    assert sum(g["modules"] for g in overview["groups"]) == 78
    assert sum(g["operations"] for g in overview["groups"]) == overview["total_operations"]
    # The guide is long: an excerpt, its length, and how to read it in full.
    assert overview["guide_truncated"] is True and len(overview["guide"]) <= 201
    full = await run(ctx, "systems_browse", {"system": "ops", "view": "guide"})
    assert len(full["guide"]) == overview["guide_chars"]
    assert len(json.dumps(overview, ensure_ascii=False)) < 2000

    logistics = await run(ctx, "systems_browse", {"system": "ops", "group": "物流"})
    carriers = {m["name"]: m for m in logistics["modules"]}
    assert carriers["Carriers"]["display_name"] == "承运商选项"
    assert carriers["Ops Carriers"]["display_name"] == "承运商管理"
    assert carriers["Ops Carriers"]["risks"] == {"write": 2, "destructive": 1}
    missing = await run(ctx, "systems_browse", {"system": "ops", "group": "天气"})
    assert missing["error"] == "group_not_found" and "物流" in missing["groups"]


async def test_systems_without_groups_list_modules(plain: Catalogs) -> None:
    ctx = plain.ctx()
    first = await run(ctx, "systems_browse", {"system": "ops"})
    assert first["view"] == "modules" and len(first["modules"]) == tools.MODULE_PAGE
    assert first["total"] == 78 and first["has_more"] is True
    asked = await run(ctx, "systems_browse", {"system": "ops", "view": "groups"})
    assert asked["view"] == "modules" and "message" in asked


@pytest.mark.parametrize("fixture", ["plain", "described"])
async def test_module_pages_cover_everything_once(fixture: str, request: Any) -> None:
    catalogs: Catalogs = request.getfixturevalue(fixture)
    ctx = catalogs.ctx()
    names: list[str] = []
    cursor = None
    pages = 0
    while True:
        args: dict[str, Any] = {"system": "ops", "view": "modules"}
        if cursor:
            args["cursor"] = cursor
        page = await run(ctx, "systems_browse", args)
        pages += 1
        assert len(json.dumps(page, ensure_ascii=False)) <= tools.L1_MAX + 200
        names += [m["name"] for m in page["modules"]]
        assert page["has_more"] is (page["next_cursor"] is not None)
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert names == [m["name"] for m in catalogs.loaded.compiled["modules"]]
    assert pages == 4


async def test_search_pages_are_complete_and_stable(plain: Catalogs) -> None:
    ctx = plain.ctx()
    seen: list[str] = []
    cursor = None
    total = None
    while True:
        args: dict[str, Any] = {"query": "market"}
        if cursor:
            args["cursor"] = cursor
        page = await run(ctx, "systems_search", args)
        total = page["total"]
        seen += ids(page)
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == total
    # Same query, same order.
    again = await run(ctx, "systems_search", {"query": "market"})
    assert ids(again) == seen[:10]


async def test_cursor_from_another_catalog_version_expires(plain: Catalogs) -> None:
    ctx = plain.ctx()
    first = await run(ctx, "systems_search", {"query": "market"})
    page = await run(ctx, "systems_browse", {"system": "ops", "view": "modules"})
    plain.loaded = loaded_for(ops_spec(metadata=False), sha="sha-2")
    stale = await run(ctx, "systems_search", {"query": "market", "cursor": first["next_cursor"]})
    assert stale["error"] == "cursor_expired"
    stale = await run(
        ctx, "systems_browse", {"system": "ops", "view": "modules", "cursor": page["next_cursor"]}
    )
    assert stale["error"] == "cursor_expired"
    # A cursor from another query does not apply either; bare offsets still work.
    other = await run(ctx, "systems_search", {"query": "market"})
    mixed = await run(ctx, "systems_search", {"query": "refund", "cursor": other["next_cursor"]})
    assert mixed["error"] == "cursor_expired"
    legacy = await run(ctx, "systems_search", {"query": "market", "cursor": "10"})
    second = await run(ctx, "systems_search", {"query": "market", "cursor": other["next_cursor"]})
    assert ids(legacy) == ids(second)


async def test_module_by_display_name_or_unique_alias(described: Catalogs) -> None:
    ctx = described.ctx()
    by_name = await run(ctx, "systems_browse", {"system": "ops", "module": "同步作业控制"})
    assert by_name["module"] == "product-import" and by_name["total"] == 3
    # Both carrier modules have the alias 承运商: ambiguous, so not guessed.
    ambiguous = await run(ctx, "systems_browse", {"system": "ops", "module": "承运商"})
    assert ambiguous["error"] == "module_not_found"


# Search ranking -----------------------------------------------------------------------------

# (query, operations any of which should rank first, how many results may come before it)
TASKS = [
    ("列出当前 Market", {"listMarkets"}, 0),
    ("market", {"listMarkets"}, 0),
    ("listMarkets", {"listMarkets"}, 0),
    ("查询承运商", {"listCarrierOptions", "listCarriers"}, 0),
    ("carrier", {"listCarrierOptions", "listCarriers"}, 0),
    ("中止同步作业", {"abortSyncJob"}, 0),
    ("商品导入", {"createProductImport"}, 2),
    ("退款", {"listRefunds"}, 0),
    ("refund", {"listRefunds"}, 0),
]


@pytest.mark.parametrize("fixture", ["plain", "described"])
@pytest.mark.parametrize(("query", "targets", "before"), TASKS)
async def test_common_tasks_find_their_operation_on_the_first_page(
    fixture: str, query: str, targets: set[str], before: int, request: Any
) -> None:
    catalogs: Catalogs = request.getfixturevalue(fixture)
    found = ids(await run(catalogs.ctx(), "systems_search", {"query": query}))
    rank = next(i for i, op_id in enumerate(found) if op_id in targets)
    assert rank <= before, found


async def test_exact_matches_lead_without_hiding_others(plain: Catalogs) -> None:
    ctx = plain.ctx()
    found = await run(ctx, "systems_search", {"query": "listMarkets"})
    assert found["items"][0]["match"] == "operation_id"
    assert found["total"] > 1
    # Operations that only mention "market" in their description come after named ones.
    page = await run(ctx, "systems_search", {"query": "market"})
    matches = [item["match"] for item in page["items"]]
    assert matches == sorted(matches, key=lambda m: m == "description")


async def test_aliases_bridge_languages(plain: Catalogs) -> None:
    query = {"query": "市场列表"}
    # Without aliases a Chinese query cannot reach the English-only Market module.
    assert "listMarkets" not in ids(await run(plain.ctx(), "systems_search", query))
    plain.loaded = loaded_for(ops_spec(metadata=True))
    assert "listMarkets" in ids(await run(plain.ctx(), "systems_search", query))


async def test_manage_carriers_lands_in_carrier_management(described: Catalogs) -> None:
    found = await run(described.ctx(), "systems_search", {"query": "管理承运商"})
    top = found["items"][:3]
    assert {item["module"] for item in top} == {"Ops Carriers"}
    assert top[0]["module_name"] == "承运商管理"


async def test_module_and_risk_filters_and_no_results(described: Catalogs) -> None:
    ctx = described.ctx()
    in_module = await run(ctx, "systems_search", {"query": "承运商", "module": "Carriers"})
    assert ids(in_module) == ["listCarrierOptions"]
    by_display = await run(ctx, "systems_search", {"query": "承运商", "module": "承运商管理"})
    assert set(ids(by_display)) == {
        "listCarriers",
        "createCarrier",
        "updateCarrier",
        "disableCarrier",
    }
    reads = await run(ctx, "systems_search", {"query": "退款", "risk": "read"})
    assert set(ids(reads)) == {"listRefunds", "getRefund"}
    nothing = await run(ctx, "systems_search", {"query": "天气预报"})
    assert nothing["items"] == [] and nothing["total"] == 0 and "message" in nothing
    no_module = await run(ctx, "systems_search", {"query": "退款", "module": "天气"})
    assert no_module["items"] == [] and "没有这个模块" in no_module["message"]


# What may be called --------------------------------------------------------------------------


async def test_refunds_show_queries_and_what_needs_a_person(described: Catalogs) -> None:
    env = described.ctx()
    module = await run(env, "systems_browse", {"system": "ops", "module": "Refunds"})
    assert module["limits"].startswith("退款重试、取消、判责仅网页端可用")
    lines = dict(line.split("  ", 1) for line in module["operations"])
    assert lines["listRefunds"].endswith("按状态、时间分页查询退款单")
    assert lines["retryRefund"].endswith("[financial; 需确认]")

    proxied = described.ctx(delivery="proxy")
    module = await run(proxied, "systems_browse", {"system": "ops", "module": "Refunds"})
    lines = dict(line.split("  ", 1) for line in module["operations"])
    assert lines["retryRefund"].endswith("[financial; 不代理]")
    assert lines["judgeRefundLiability"].endswith("[destructive; 不代理]")
    overview = await run(proxied, "systems_browse", {"system": "ops"})
    assert "未开启" in overview["call_rules"]

    carriers = await run(proxied, "systems_browse", {"system": "ops", "module": "Ops Carriers"})
    assert any(line.endswith("[write; 未开启写入]") for line in carriers["operations"])
    writable = described.ctx(delivery="proxy", writable=frozenset({"ops"}))
    carriers = await run(writable, "systems_browse", {"system": "ops", "module": "Ops Carriers"})
    assert not any("未开启写入" in line for line in carriers["operations"])

    detail = await run(env, "systems_describe", {"system": "ops", "operation_id": "retryRefund"})
    assert detail["risk"] == "financial" and detail["module_limits"].startswith("退款重试")
    assert detail["permission_check"] == "not_declared"


async def test_permissions_never_leak_between_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = ops_spec(metadata=True)
    spec["x-agent"]["permissions"] = {"operationId": "getMe", "pointer": "/data/permissions"}
    catalogs = Catalogs(monkeypatch, loaded_for(spec))
    finance = catalogs.ctx(codes=["ops:*"])
    clerk = catalogs.ctx(codes=["ops:refunds:read"])
    assert "retryRefund" in ids(await run(finance, "systems_search", {"query": "退款"}))
    # Same catalog, same search index, different member: the write operations are gone.
    found = await run(clerk, "systems_search", {"query": "退款"})
    assert set(ids(found)) == {"listRefunds", "getRefund"} and found["total"] == 2
    overview = await run(clerk, "systems_browse", {"system": "ops"})
    # Refunds, plus getMe (x-permission: none) in Settings.
    assert overview["permission_check"] == "filtered" and overview["total_operations"] == 3
    hidden = await run(clerk, "systems_describe", {"system": "ops", "operation_id": "retryRefund"})
    assert hidden["error"] == "operation_not_found"
    assert "retryRefund" in ids(await run(finance, "systems_search", {"query": "退款"}))


# Detail --------------------------------------------------------------------------------------


async def test_describe_names_what_it_shortened_and_expands_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = ops_spec(metadata=False)
    op = spec["paths"]["/api/ops/refunds/listRefunds"]["get"]
    wide = {
        "type": "object",
        "properties": {
            f"field_{i}": {"type": "string", "description": "说明" * 40} for i in range(40)
        },
    }
    op["responses"]["200"]["content"]["application/json"]["schema"] = {
        "type": "object",
        "properties": {"data": {"type": "array", "items": wide}},
    }
    catalogs = Catalogs(monkeypatch, loaded_for(spec))
    ctx = catalogs.ctx()
    detail = await run(ctx, "systems_describe", {"system": "ops", "operation_id": "listRefunds"})
    assert len(json.dumps(detail, ensure_ascii=False)) <= tools.L3_MAX + 200
    assert "response" in detail["omitted"]
    assert detail["risk"] == "read" and detail["permission"] == ["ops:refunds:read"]
    assert detail["call"]["mode"] == "env"
    expanded = await run(
        ctx,
        "systems_describe",
        {"system": "ops", "operation_id": "listRefunds", "expand": "response"},
    )
    assert expanded["expand"] == "response" and expanded["risk"] == "read"
    full = catalogs.loaded.compiled["operations"]["listRefunds"]["detail"]["response"]
    assert expanded["response"] == full or expanded.get("truncated") is True
    assert len(json.dumps(expanded, ensure_ascii=False)) <= tools.EXPAND_MAX + 200


async def test_old_compiled_catalogs_still_browse(monkeypatch: pytest.MonkeyPatch) -> None:
    """A catalog compiled before groups and display names, served until its recheck."""
    compiled = copy.deepcopy(loaded_for(ops_spec(metadata=False)).compiled)
    compiled.pop("groups")
    for module in compiled["modules"]:
        for key in ("display_name", "aliases", "limits"):
            module.pop(key)
    for op in compiled["operations"].values():
        op.pop("aliases")
    old = service.Loaded("ops", "stale", "sha-0", compiled, SearchIndex(compiled))
    catalogs = Catalogs(monkeypatch, old)
    ctx = catalogs.ctx()
    overview = await run(ctx, "systems_browse", {"system": "ops"})
    assert overview["view"] == "modules" and overview["total"] == 78
    assert ids(await run(ctx, "systems_search", {"query": "listMarkets"}))[0] == "listMarkets"
