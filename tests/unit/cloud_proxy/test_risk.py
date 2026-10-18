from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from coreman.core.cloud_proxy import risk
from coreman.core.cloud_proxy.risk import FALLBACK_REASON, Assessment
from coreman.core.cloud_proxy.risk_policy import CloudProxyPolicy

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)
PROD_NS = "00000000-0000-4000-8000-000000000003"
TEST_NS = "00000000-0000-4000-8000-000000000001"
STAGING_NS = "00000000-0000-4000-8000-000000000002"
TEST_ACL_V4 = "acl-example-test"
TRUSTED_IP = "1.2.3.5"  # 规则只对公网地址查名单，保留地址本来就算私网
INSTANCE = "mse-example-instance"
POLICY = CloudProxyPolicy.model_validate(
    {
        "alidns": {
            "managed_zones": ["example.com"],
            "trusted_cname_suffixes": [".example.com", ".example.net"],
            "trusted_ips": [TRUSTED_IP],
        },
        "mse": {
            "endpoint": "mse.cn-hangzhou.aliyuncs.com",
            "instance_id": INSTANCE,
            "namespaces": {TEST_NS: "test", STAGING_NS: "staging", PROD_NS: "prod"},
            "low_risk_envs": ["test", "staging"],
        },
        "slb": {"acls": {TEST_ACL_V4: "test", "acl-example-staging": "staging"}},
    }
)


async def assess(
    product: str,
    action: str,
    params: dict[str, str],
    reader: Any,
    *,
    now: datetime,
    policy: CloudProxyPolicy | None = POLICY,
) -> Assessment:
    return await risk.assess(product, action, params, reader, now=now, policy=policy)


class FakeReader:
    def __init__(
        self, responses: dict[str, Any] | None = None, fail: set[str] | None = None
    ) -> None:
        self.responses = responses or {}
        self.fail = fail or set()
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def call(self, product: str, action: str, params: dict[str, str]) -> dict[str, Any]:
        self.calls.append((product, action, params))
        if action in self.fail:
            raise RuntimeError("boom")
        resp = self.responses.get(action, {})
        assert isinstance(resp, dict)
        return resp


def _record(**over: str) -> dict[str, str]:
    rec = {
        "DomainName": "example.com",
        "RecordId": "1",
        "RR": "app",
        "Type": "A",
        "Value": "10.0.0.1",
        "Line": "default",
        "Status": "ENABLE",
    }
    rec.update(over)
    return rec


def _logs(*entries: tuple[int, str]) -> dict[str, Any]:
    return {
        "RecordLogs": {
            "RecordLog": [{"ActionTimestamp": ts, "Message": msg} for ts, msg in entries]
        }
    }


def _dns_reader(
    record: dict[str, str] | None = None, logs: dict[str, Any] | None = None
) -> FakeReader:
    return FakeReader(
        {
            "DescribeDomainRecordInfo": record or _record(),
            "DescribeRecordLogs": logs or _logs(),
        }
    )


def _add(**over: str) -> dict[str, str]:
    p = {"DomainName": "example.com", "RR": "app", "Type": "A", "Value": "10.0.0.1"}
    p.update(over)
    return p


async def _dns(action: str, params: dict[str, str], reader: FakeReader | None = None) -> Assessment:
    return await assess("alidns", action, params, reader or _dns_reader(), now=NOW)


# ── 通用 ─────────────────────────────────────────────────────────


async def test_unknown_product_is_high() -> None:
    a = await assess("ecs", "RunInstances", {}, FakeReader(), now=NOW)
    assert a.high and "ecs" in a.reasons[0]


async def test_read_action_passed_to_assess_is_high() -> None:
    a = await assess("alidns", "DescribeDomainRecords", {}, FakeReader(), now=NOW)
    assert a.high and "DescribeDomainRecords" in a.reasons[0]


async def test_domain_product_any_write_is_high() -> None:
    a = await assess("domain", "SaveSingleTaskForUpdatingContactInfo", {}, FakeReader(), now=NOW)
    assert a.high and "域名注册" in a.reasons[0]


async def test_reader_failure_is_fallback_high() -> None:
    reader = FakeReader(fail={"DescribeDomainRecordInfo"})
    a = await _dns(
        "UpdateDomainRecord",
        {"RecordId": "1", "RR": "app", "Type": "A", "Value": "10.0.0.2"},
        reader,
    )
    assert a == Assessment(high=True, reasons=(FALLBACK_REASON,))


async def test_missing_required_param_is_fallback_high() -> None:
    a = await _dns("AddDomainRecord", {"DomainName": "example.com", "RR": "app"})
    assert a == Assessment(high=True, reasons=(FALLBACK_REASON,))


async def test_unexpected_response_shape_is_fallback_high() -> None:
    reader = _dns_reader(logs={"RecordLogs": "nope"})
    a = await _dns("AddDomainRecord", _add(), reader)
    assert a == Assessment(high=True, reasons=(FALLBACK_REASON,))


# ── alidns ───────────────────────────────────────────────────────


async def test_dns_plain_add_is_low() -> None:
    reader = _dns_reader()
    a = await _dns("AddDomainRecord", _add(), reader)
    assert a == Assessment(high=False, reasons=())
    assert reader.calls[0][1] == "DescribeRecordLogs"
    assert reader.calls[0][2]["StartDate"] == "2026-10-19"


async def test_dns_remark_is_low_without_history() -> None:
    reader = _dns_reader()
    a = await _dns("UpdateDomainRecordRemark", {"RecordId": "1", "Remark": "x"}, reader)
    assert not a.high
    assert [c[1] for c in reader.calls] == ["DescribeDomainRecordInfo"]


def test_rule_managed_zone() -> None:
    assert risk._rule_managed_zone("example.org", POLICY.alidns)
    assert risk._rule_managed_zone("example.com", POLICY.alidns) is None


async def test_dns_remark_outside_managed_zone_is_high() -> None:
    reader = _dns_reader(record=_record(DomainName="example.org"))
    a = await _dns("UpdateDomainRecordRemark", {"RecordId": "1", "Remark": "x"}, reader)
    assert a.high and "example.org" in a.reasons[0]


def test_rule_protected_rr() -> None:
    assert risk._rule_protected_rr("_acme-challenge.test", None)
    assert risk._rule_protected_rr("app", _record(RR="_acme-challenge"))
    assert risk._rule_protected_rr("app", _record()) is None


def test_rule_writable_type() -> None:
    assert risk._rule_writable_type("NS")
    assert risk._rule_writable_type("A") is None


async def test_dns_add_ns_delegation_is_high() -> None:
    a = await _dns("AddDomainRecord", _add(Type="NS", Value="ns1.evil.example"))
    assert a.high and any("NS" in r for r in a.reasons)


def test_rule_root_or_wildcard() -> None:
    assert risk._rule_root_or_wildcard("@", None)
    assert risk._rule_root_or_wildcard("*.test", None)
    assert risk._rule_root_or_wildcard("app", _record(RR="*"))
    assert risk._rule_root_or_wildcard("app", _record()) is None


def test_rule_disable_live() -> None:
    assert risk._rule_disable_live("disable", _record())
    assert risk._rule_disable_live("disable", _record(Status="DISABLE")) is None
    assert risk._rule_disable_live("enable", _record()) is None


async def test_dns_disable_live_record_is_high() -> None:
    a = await _dns("SetDomainRecordStatus", {"RecordId": "1", "Status": "Disable"})
    assert a.high and "停用正在生效的记录" in a.reasons[0]


async def test_dns_status_unknown_value_is_fallback() -> None:
    a = await _dns("SetDomainRecordStatus", {"RecordId": "1", "Status": "Pause"})
    assert a == Assessment(high=True, reasons=(FALLBACK_REASON,))


def test_rule_update_live() -> None:
    after = {"RR": "app", "Type": "A", "Value": "10.0.0.2", "Line": "default"}
    reason = risk._rule_update_live("update", _record(), after)
    assert reason and "Value" in reason
    same = {"RR": "APP", "Type": "a", "Value": "10.0.0.1", "Line": "default"}
    assert risk._rule_update_live("update", _record(), same) is None
    assert risk._rule_update_live("update", _record(Status="DISABLE"), after) is None


async def test_dns_update_missing_line_counts_as_default() -> None:
    reader = _dns_reader(record=_record(Line="telecom"))
    params = {"RecordId": "1", "RR": "app", "Type": "A", "Value": "10.0.0.1"}
    a = await _dns("UpdateDomainRecord", params, reader)
    assert a.high and "Line" in a.reasons[0]


def test_rule_sensitive_type() -> None:
    assert risk._rule_sensitive_type("MX")
    assert risk._rule_sensitive_type("A") is None


def test_rule_txt_verification() -> None:
    assert risk._rule_txt_verification("TXT", "_dmarc")
    assert risk._rule_txt_verification("TXT", "@")
    assert risk._rule_txt_verification("TXT", "app") is None


def test_rule_untrusted_target() -> None:
    p = POLICY.alidns
    assert risk._rule_untrusted_target("add", "CNAME", "evil.example.org", p)
    assert risk._rule_untrusted_target("add", "CNAME", "x.example.net.", p) is None
    assert risk._rule_untrusted_target("add", "CNAME", "example.com", p) is None
    assert risk._rule_untrusted_target("add", "CNAME", "evilexample.net", p)
    assert risk._rule_untrusted_target("add", "A", "1.2.3.4", p)
    assert risk._rule_untrusted_target("add", "A", TRUSTED_IP, p) is None
    assert risk._rule_untrusted_target("add", "A", "not-ip", p)
    assert risk._rule_untrusted_target("disable", "A", "1.2.3.4", p) is None


async def test_dns_enable_uses_existing_value_for_target_check() -> None:
    reader = _dns_reader(record=_record(Value="1.2.3.4", Status="DISABLE"))
    a = await _dns("SetDomainRecordStatus", {"RecordId": "1", "Status": "Enable"}, reader)
    assert a.high and "1.2.3.4" in a.reasons[0]


def test_rule_recent_changes() -> None:
    cutoff = NOW_MS - 3600 * 1000
    recent = _logs((NOW_MS, "a"), (NOW_MS, "b"), (NOW_MS, "c"))
    assert risk._rule_recent_changes(recent, NOW)
    ignored = _logs((NOW_MS, "a"), (NOW_MS, "b"), (NOW_MS, "_acme-challenge c"), (cutoff - 1, "d"))
    assert risk._rule_recent_changes(ignored, NOW) is None


async def test_dns_recent_burst_makes_add_high() -> None:
    logs = _logs(*[(NOW_MS - i, "x") for i in range(3)])
    a = await _dns("AddDomainRecord", _add(), _dns_reader(logs=logs))
    assert a.high and "最近 1 小时" in a.reasons[0]


async def test_dns_history_read_failure_is_fallback() -> None:
    reader = FakeReader(fail={"DescribeRecordLogs"})
    a = await _dns("AddDomainRecord", _add(), reader)
    assert a == Assessment(high=True, reasons=(FALLBACK_REASON,))


# ── mse ──────────────────────────────────────────────────────────


def _nacos(ns: str, **over: str) -> dict[str, str]:
    p = {"NamespaceId": ns, "Group": "g", "DataId": "d.yaml", "Content": "a: 1"}
    p.update(over)
    return p


def test_rule_protected_update() -> None:
    assert risk._rule_protected_update("prod", POLICY.mse)
    assert risk._rule_protected_update("staging", POLICY.mse) is None
    assert risk._rule_protected_update(None, POLICY.mse) is None


async def test_mse_update_prod_is_high() -> None:
    a = await assess("mse", "UpdateNacosConfig", _nacos(PROD_NS), FakeReader(), now=NOW)
    assert a.high and "prod" in a.reasons[0]


def _cur(content: str, type_: str = "yaml") -> FakeReader:
    return FakeReader({"GetNacosConfig": {"Configuration": {"Content": content, "Type": type_}}})


async def test_mse_update_test_is_low() -> None:
    reader = _cur("a: 0")
    a = await assess("mse", "UpdateNacosConfig", _nacos(TEST_NS), reader, now=NOW)
    assert a == Assessment(high=False, reasons=())
    assert reader.calls[0][1] == "GetNacosConfig"


def test_rule_known_namespace() -> None:
    assert risk._rule_known_namespace(None, "ns-x", POLICY.mse)
    assert risk._rule_known_namespace("test", TEST_NS, POLICY.mse) is None


async def test_mse_unknown_namespace_is_high() -> None:
    a = await assess("mse", "UpdateNacosConfig", _nacos("ns-x"), FakeReader(), now=NOW)
    assert a.high and "ns-x" in a.reasons[0]


def test_rule_known_instance() -> None:
    assert risk._rule_known_instance("other", POLICY.mse)
    assert risk._rule_known_instance(INSTANCE, POLICY.mse) is None
    assert risk._rule_known_instance(None, POLICY.mse) is None


def test_rule_create_existing() -> None:
    assert risk._rule_create_existing({"Configuration": {"Content": "a: 1"}})
    assert risk._rule_create_existing({"Configuration": None}) is None
    assert risk._rule_create_existing({}) is None
    assert risk._rule_create_existing({"Configuration": {"Content": None}}) is None


async def test_mse_create_new_doc_is_low_even_in_prod() -> None:
    reader = FakeReader({"GetNacosConfig": {}})
    a = await assess("mse", "CreateNacosConfig", _nacos(PROD_NS), reader, now=NOW)
    assert not a.high
    assert reader.calls[0][2] == {"NamespaceId": PROD_NS, "Group": "g", "DataId": "d.yaml"}


async def test_mse_create_existing_doc_is_high() -> None:
    reader = FakeReader({"GetNacosConfig": {"Configuration": {"Content": "a: 1"}}})
    a = await assess("mse", "CreateNacosConfig", _nacos(TEST_NS), reader, now=NOW)
    assert a.high and "已有" in a.reasons[0]


async def test_mse_create_bad_shape_is_fallback() -> None:
    reader = FakeReader({"GetNacosConfig": {"Configuration": "x"}})
    a = await assess("mse", "CreateNacosConfig", _nacos(TEST_NS), reader, now=NOW)
    assert a == Assessment(high=True, reasons=(FALLBACK_REASON,))


# ── slb ──────────────────────────────────────────────────────────


def _acl(entry: str, comment: str = "张三-家里", acl: str = TEST_ACL_V4) -> dict[str, str]:
    return {
        "RegionId": "cn-hangzhou",
        "AclId": acl,
        "AclEntrys": json.dumps([{"entry": entry, "comment": comment}], ensure_ascii=False),
    }


async def _slb(params: dict[str, str]) -> Assessment:
    return await assess("slb", "AddAccessControlListEntry", params, FakeReader(), now=NOW)


async def test_slb_single_ip_is_low() -> None:
    assert await _slb(_acl("1.2.3.4/32")) == Assessment(high=False, reasons=())
    assert not (await _slb(_acl("2001:4860:4860::/64"))).high


def test_rule_known_acl() -> None:
    assert risk._rule_known_acl("acl-other", POLICY.slb)
    assert risk._rule_known_acl(TEST_ACL_V4, POLICY.slb) is None


async def test_slb_unknown_acl_is_high() -> None:
    a = await _slb(_acl("1.2.3.4/32", acl="acl-other"))
    assert a.high and "acl-other" in a.reasons[0]


def test_rule_not_host() -> None:
    import ipaddress

    reason = risk._rule_not_host(ipaddress.ip_network("1.2.3.0/28"))
    assert reason == "网段 /28（16 个 IP）"
    v6 = risk._rule_not_host(ipaddress.ip_network("2001:4860::/48"))
    assert v6 == "网段 /48（65536 个 /64）"
    assert risk._rule_not_host(ipaddress.ip_network("1.2.3.4/32")) is None


def test_rule_keep_entry() -> None:
    assert risk._rule_keep_entry("keep-办公室-出口") == "keep- 条目不会被自动清理"
    assert risk._rule_keep_entry("张三-家里") is None


def test_rule_too_wide() -> None:
    import ipaddress

    assert risk._rule_too_wide(ipaddress.ip_network("1.2.0.0/16"))
    assert risk._rule_too_wide(ipaddress.ip_network("2001:4860::/32"))
    assert risk._rule_too_wide(ipaddress.ip_network("1.2.3.0/24")) is None


def test_rule_not_global() -> None:
    import ipaddress

    assert risk._rule_not_global(ipaddress.ip_network("10.0.0.1/32"))
    assert risk._rule_not_global(ipaddress.ip_network("1.2.3.4/32")) is None


async def test_slb_range_and_keep_reasons_in_order() -> None:
    a = await _slb(_acl("1.2.3.0/28", comment="keep-办公室-出口"))
    assert a.reasons == ("网段 /28（16 个 IP）", "keep- 条目不会被自动清理")


@pytest.mark.parametrize(
    "entrys",
    [
        "not json",
        "[]",
        json.dumps([{"entry": "1.2.3.4"}, {"entry": "5.6.7.8"}]),
        json.dumps([{"comment": "x"}]),
        json.dumps([{"entry": "nope", "comment": "x"}]),
    ],
)
async def test_slb_bad_entrys_is_fallback(entrys: str) -> None:
    params = _acl("1.2.3.4/32")
    params["AclEntrys"] = entrys
    assert await _slb(params) == Assessment(high=True, reasons=(FALLBACK_REASON,))


async def test_recent_cutoff_uses_now() -> None:
    old = _logs(*[(NOW_MS - 3600 * 1000 - i - 1, "x") for i in range(5)])
    a = await _dns("AddDomainRecord", _add(), _dns_reader(logs=old))
    assert not a.high


# ── Fix round 1 ─────────────────────────────────────────────────


async def test_slb_duplicate_entry_key_is_fallback() -> None:
    params = _acl("1.2.3.4/32")
    params["AclEntrys"] = '[{"entry":"0.0.0.0/0","entry":"1.2.3.4/32","comment":"x"}]'
    assert await _slb(params) == Assessment(high=True, reasons=(FALLBACK_REASON,))


async def test_slb_extra_entry_key_is_fallback() -> None:
    params = _acl("1.2.3.4/32")
    params["AclEntrys"] = json.dumps(
        [{"entry": "1.2.3.4/32", "comment": "x", "Entry": "0.0.0.0/0"}]
    )
    assert await _slb(params) == Assessment(high=True, reasons=(FALLBACK_REASON,))


def test_rule_param_keys() -> None:
    assert risk._rule_param_keys("AddDomainRecord", {**_add(), "Lang": "en"}) == [
        "包含未登记的参数：Lang"
    ]
    assert risk._rule_param_keys("AddDomainRecord", {**_add(), "Action": "AddDomainRecord"}) == []


async def test_unregistered_param_is_high() -> None:
    a = await _dns("AddDomainRecord", {**_add(), "Foo": "1"})
    assert a == Assessment(high=True, reasons=("包含未登记的参数：Foo",))


async def test_case_variant_param_is_high() -> None:
    a = await _dns("AddDomainRecord", {**_add(RR="x"), "rr": "@"})
    assert a.high
    assert "包含未登记的参数：rr" in a.reasons
    assert "参数名仅大小写不同：RR、rr" in a.reasons


async def test_case_variant_namespace_is_high() -> None:
    params = {**_nacos(TEST_NS), "namespaceId": PROD_NS}
    a = await assess("mse", "UpdateNacosConfig", params, _cur("a: 0"), now=NOW)
    assert a.high and any("大小写" in r for r in a.reasons)


def test_rule_update_content() -> None:
    assert risk._rule_update_content("a: 1\nb: 2", "yaml", "a: 1\nb: 3", "yaml") is None
    assert risk._rule_update_content("a: 1", "yaml", "", "yaml")
    assert risk._rule_update_content("a: 1", "yaml", "a: [", "yaml")
    assert risk._rule_update_content("a: 1", "yaml", "a: 1\na: 2", "yaml")
    assert risk._rule_update_content("a: 1", "yaml", "- a", "yaml")
    dropped = risk._rule_update_content("a: {x: 1, y: 2}", "yaml", "a: {x: 1}", "yaml")
    assert dropped and "a.y" in dropped
    # 删列表元素在 Skill 里是更新，不是删除
    assert risk._rule_update_content("l: [1, 2]", "yaml", "l: [1]", "yaml") is None
    assert risk._rule_update_content('{"a": 1}', "json", '{"a": 1, "b": 2}', "json") is None
    assert risk._rule_update_content('{"a": 1}', "json", '{"b": 2}', "json")
    # 键名带点或类型变化不能顶替原来的键
    assert risk._rule_update_content('{"a": {"b": 1}}', "json", '{"a.b": 1, "a": 2}', "json")
    assert risk._rule_update_content("1: x", "yaml", "'1': x", "yaml")
    inner = risk._rule_update_content("l: [{k: 1, j: 2}]", "yaml", "l: [{k: 1}]", "yaml")
    assert inner and "l[].j" in inner


@pytest.mark.parametrize(
    ("content", "needle"),
    [
        ("", "为空"),
        ("a: [", "无法"),
        ("b: 1", "删除了现有的键：a"),
    ],
)
async def test_mse_update_staging_destructive_is_high(content: str, needle: str) -> None:
    params = _nacos(STAGING_NS, Content=content)
    a = await assess("mse", "UpdateNacosConfig", params, _cur("a: 1"), now=NOW)
    assert a.high and needle in a.reasons[0]


async def test_mse_update_json_by_type() -> None:
    params = _nacos(TEST_NS, DataId="d", Type="json", Content='{"a": 2}')
    reader = _cur('{"a": 1}', "json")
    assert not (await assess("mse", "UpdateNacosConfig", params, reader, now=NOW)).high


async def test_mse_update_missing_doc_is_high() -> None:
    reader = FakeReader({"GetNacosConfig": {}})
    a = await assess("mse", "UpdateNacosConfig", _nacos(TEST_NS), reader, now=NOW)
    assert a.high and "没有这份文档" in a.reasons[0]


@pytest.mark.parametrize(
    "reader",
    [
        FakeReader(fail={"GetNacosConfig"}),
        _cur("a: ["),
        _cur("a: 1\na: 2"),
        FakeReader({"GetNacosConfig": {"Configuration": "x"}}),
    ],
)
async def test_mse_update_old_unreadable_is_fallback(reader: FakeReader) -> None:
    a = await assess("mse", "UpdateNacosConfig", _nacos(TEST_NS), reader, now=NOW)
    assert a == Assessment(high=True, reasons=(FALLBACK_REASON,))


@pytest.mark.parametrize(
    ("action", "params", "record", "needle"),
    [
        ("AddDomainRecord", _add(Type="mx", Value="mx.example.com", Priority="10"), None, "MX"),
        ("AddDomainRecord", _add(Type="cname", Value="x.example.org"), None, "CNAME"),
        ("AddDomainRecord", _add(RR=" @ "), None, "根域名"),
        (
            "UpdateDomainRecord",
            {"RecordId": "1", "RR": "@", "Type": "A", "Value": "10.0.0.1"},
            _record(Status="DISABLE"),
            "根域名",
        ),
        (
            "UpdateDomainRecord",
            {"RecordId": "1", "RR": "app", "Type": "A", "Value": "10.0.0.1"},
            _record(Type="NS", Status="DISABLE"),
            "NS",
        ),
        ("AddDomainRecord", _add(Type="AAAA", Value="::ffff:8.8.8.8"), None, "8.8.8.8"),
    ],
)
async def test_dns_bypass_classes_are_high(
    action: str, params: dict[str, str], record: dict[str, str] | None, needle: str
) -> None:
    a = await _dns(action, params, _dns_reader(record=record))
    assert a.high and any(needle in r for r in a.reasons), a.reasons


@pytest.mark.parametrize("entry", ["0.0.0.0/0", "::/0"])
async def test_slb_any_address_is_high(entry: str) -> None:
    a = await _slb(_acl(entry))
    assert a.high and any("太宽" in r for r in a.reasons)


# ── Fix round 2 ─────────────────────────────────────────────────

# 经典 billion laughs：9 层、每层 9 个引用，展开后约 3.9 亿个节点
_BOMB = "a0: &a0 [x, x, x, x, x, x, x, x, x]\n" + "".join(
    f"a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 9) + "]\n" for i in range(1, 10)
)


async def test_mse_update_alias_bomb_is_high_and_fast() -> None:
    import time

    assert len(_BOMB) < 2048
    start = time.monotonic()
    a = await assess(
        "mse", "UpdateNacosConfig", _nacos(TEST_NS, Content=_BOMB), _cur("a: 1"), now=NOW
    )
    assert time.monotonic() - start < 1
    assert a.high and a.reasons == ("配置内容包含 YAML 别名，无法安全比对",)


async def test_mse_update_old_doc_alias_is_fallback() -> None:
    a = await assess("mse", "UpdateNacosConfig", _nacos(TEST_NS), _cur("a: &x 1\nb: *x"), now=NOW)
    assert a == Assessment(high=True, reasons=(FALLBACK_REASON,))


def test_rule_update_content_size_cap() -> None:
    big = "a: '" + "x" * (100 * 1024) + "'"
    assert risk._rule_update_content("a: 1", "yaml", big, "yaml") == "新内容超过 100KB 上限"
    assert risk._rule_update_content("a: 1", "yaml", "a: &x 1", "yaml")


# ── 部署参数缺失 ─────────────────────────────────────────────────


@pytest.mark.parametrize("policy", [None, CloudProxyPolicy()])
@pytest.mark.parametrize(
    ("product", "action", "params", "reader", "item"),
    [
        ("alidns", "AddDomainRecord", _add(), _dns_reader(), "alidns.managed_zones"),
        ("mse", "UpdateNacosConfig", _nacos(TEST_NS), _cur("a: 0"), "mse.namespaces"),
        (
            "mse",
            "CreateNacosConfig",
            _nacos(TEST_NS),
            FakeReader({"GetNacosConfig": {}}),
            "mse.namespaces",
        ),
        ("slb", "AddAccessControlListEntry", _acl("1.2.3.4/32"), FakeReader(), "slb.acls"),
    ],
)
async def test_missing_policy_is_high(
    policy: CloudProxyPolicy | None,
    product: str,
    action: str,
    params: dict[str, str],
    reader: FakeReader,
    item: str,
) -> None:
    a = await assess(product, action, params, reader, now=NOW, policy=policy)
    assert a.high
    assert f"未配置 CLOUD_PROXY_POLICY.{item}，按高风险处理" in a.reasons


async def test_missing_instance_is_high() -> None:
    policy = POLICY.model_copy(update={"mse": POLICY.mse.model_copy(update={"instance_id": None})})
    reader = _cur("a: 0")
    a = await assess("mse", "UpdateNacosConfig", _nacos(TEST_NS), reader, now=NOW, policy=policy)
    assert a.reasons == ("未配置 CLOUD_PROXY_POLICY.mse.instance_id，按高风险处理",)


async def test_no_low_risk_envs_means_every_update_is_high() -> None:
    policy = POLICY.model_copy(update={"mse": POLICY.mse.model_copy(update={"low_risk_envs": ()})})
    reader = _cur("a: 0")
    a = await assess("mse", "UpdateNacosConfig", _nacos(TEST_NS), reader, now=NOW, policy=policy)
    assert a.high and "test" in a.reasons[0]
    assert reader.calls == []


async def test_no_trusted_targets_means_public_targets_are_high() -> None:
    alidns = POLICY.alidns.model_copy(update={"trusted_cname_suffixes": (), "trusted_ips": ()})
    policy = POLICY.model_copy(update={"alidns": alidns})

    async def run(params: dict[str, str]) -> Assessment:
        return await assess(
            "alidns", "AddDomainRecord", params, _dns_reader(), now=NOW, policy=policy
        )

    a = await run(_add(Type="CNAME", Value="x.example.net"))
    assert a.high and "x.example.net" in a.reasons[0]
    a = await run(_add(Value=TRUSTED_IP))
    assert a.high and TRUSTED_IP in a.reasons[0]
    assert not (await run(_add())).high
