from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.parse

import pytest

from coreman.core.cloud_proxy.aliyun import (
    PRODUCTS,
    RESERVED_PARAMS,
    classify,
    endpoint_for,
    sign_v1,
)
from coreman.core.cloud_proxy.risk_policy import CloudProxyPolicy
from coreman.core.config import CLOUD_PROXY_PRODUCTS


def _pe(s: str) -> str:
    return urllib.parse.quote(str(s), safe="~")


def _legacy_sign(
    action: str,
    params: dict[str, object],
    *,
    version: str,
    ak: str,
    sk: str,
    nonce: str,
    timestamp: str,
) -> str:
    """oracle：原样复制自 Skill 原有的阿里云 RPC 客户端签名段。"""
    body = {
        "Format": "JSON",
        "Version": version,
        "AccessKeyId": ak,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": nonce,
        "Timestamp": timestamp,
        "Action": action,
    }
    body.update({k: str(v) for k, v in (params or {}).items() if v is not None})
    canon = "&".join(f"{_pe(k)}={_pe(v)}" for k, v in sorted(body.items()))
    to_sign = "POST&%2F&" + _pe(canon)
    body["Signature"] = base64.b64encode(
        hmac.new((sk + "&").encode(), to_sign.encode(), hashlib.sha1).digest()
    ).decode()
    return urllib.parse.urlencode(body)


CASES = [
    {"DomainName": "example.com", "PageNumber": "1", "PageSize": "500"},
    {"RR": "测试 主机", "Value": "a b"},
    {"Value": "v=spf1 ~all *", "Remark": "含 ~ 与 * 与 /+="},
    {"Value": "中文~空格 星*号&等=号"},
]


@pytest.mark.parametrize("extra", CASES)
def test_sign_v1_matches_legacy_bytes(extra: dict[str, str]) -> None:
    kw = {"version": "2015-01-09", "nonce": "abc123", "timestamp": "2026-10-20T01:02:03Z"}
    want = _legacy_sign("AddDomainRecord", extra, ak="AKID", sk="s3cret", **kw)
    got = sign_v1(
        {"Action": "AddDomainRecord", **extra},
        version=kw["version"],
        access_key_id="AKID",
        secret="s3cret",
        nonce=kw["nonce"],
        timestamp=kw["timestamp"],
    )
    assert got == want


def test_percent_encoding_specials_in_canonical_form() -> None:
    got = sign_v1(
        {"Action": "X", "V": "中 ~*"},
        version="v",
        access_key_id="a",
        secret="s",
        nonce="n",
        timestamp="t",
    )
    assert "V=%E4%B8%AD+~%2A" in got


def test_products_match_config_tuple() -> None:
    assert tuple(PRODUCTS) == CLOUD_PROXY_PRODUCTS
    assert all(p.key == k for k, p in PRODUCTS.items())


def test_product_endpoints() -> None:
    assert PRODUCTS["mse"].endpoint is None
    assert endpoint_for(PRODUCTS["mse"], None) is None
    assert endpoint_for(PRODUCTS["mse"], CloudProxyPolicy()) is None
    policy = CloudProxyPolicy.model_validate({"mse": {"endpoint": "mse.cn-hangzhou.aliyuncs.com"}})
    assert endpoint_for(PRODUCTS["mse"], policy) == "mse.cn-hangzhou.aliyuncs.com"
    assert endpoint_for(PRODUCTS["slb"], None) == "slb.aliyuncs.com"
    assert PRODUCTS["slb"].version == "2014-05-15"


def test_classify() -> None:
    alidns = PRODUCTS["alidns"]
    assert classify(alidns, "DescribeDomainRecords") == "read"
    assert classify(alidns, "AddDomainRecord") == "write"
    assert classify(alidns, "DeleteDomainRecord") is None
    assert classify(PRODUCTS["domain"], "QueryDomainList") == "read"
    assert PRODUCTS["domain"].write_actions == frozenset()


def test_no_action_is_both_read_and_write() -> None:
    for p in PRODUCTS.values():
        assert not (p.read_actions & p.write_actions)


def test_reserved_params() -> None:
    assert "Signature" in RESERVED_PARAMS and "Action" not in RESERVED_PARAMS
    assert len(RESERVED_PARAMS) == 8
