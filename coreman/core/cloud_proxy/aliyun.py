"""阿里云 RPC 签名 v1 与产品登记。

动作清单逐个来自调用代理的 Skill 实际用到的接口；不在清单内的动作
一律分类为 None，由调用方拒绝，避免代理被当成通用阿里云出口。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.parse
from dataclasses import dataclass
from typing import Literal

from coreman.core.cloud_proxy.risk_policy import CloudProxyPolicy


@dataclass(frozen=True)
class Product:
    key: str
    # None 表示地域服务，endpoint 由部署配置给出（见 endpoint_for）
    endpoint: str | None
    version: str
    read_actions: frozenset[str]
    write_actions: frozenset[str]


PRODUCTS: dict[str, Product] = {
    "alidns": Product(
        key="alidns",
        endpoint="alidns.aliyuncs.com",
        version="2015-01-09",
        read_actions=frozenset(
            {
                "DescribeDomainRecords",
                "DescribeDomainRecordInfo",
                "DescribeRecordLogs",
                "DescribeDomainInfo",
                "DescribeDomains",
            }
        ),
        write_actions=frozenset(
            {
                "AddDomainRecord",
                "UpdateDomainRecord",
                "SetDomainRecordStatus",
                "UpdateDomainRecordRemark",
            }
        ),
    ),
    "domain": Product(
        key="domain",
        endpoint="domain.aliyuncs.com",
        version="2018-01-29",
        read_actions=frozenset({"QueryDomainList", "QueryDomainByDomainName"}),
        write_actions=frozenset(),
    ),
    "mse": Product(
        key="mse",
        endpoint=None,
        version="2019-05-31",
        read_actions=frozenset(
            {
                "GetNacosConfig",
                "ListNacosConfigs",
                "ListNacosHistoryConfigs",
                "GetNacosHistoryConfig",
                "ListListenersByConfig",
            }
        ),
        write_actions=frozenset({"UpdateNacosConfig", "CreateNacosConfig"}),
    ),
    "slb": Product(
        key="slb",
        endpoint="slb.aliyuncs.com",
        version="2014-05-15",
        read_actions=frozenset({"DescribeAccessControlListAttribute"}),
        write_actions=frozenset({"AddAccessControlListEntry"}),
    ),
}

# 这些参数由代理自己填；调用方带了就是想篡改签名材料或换身份，必须拒绝。
RESERVED_PARAMS = frozenset(
    {
        "AccessKeyId",
        "Signature",
        "SignatureMethod",
        "SignatureVersion",
        "SignatureNonce",
        "Timestamp",
        "Version",
        "Format",
    }
)


def endpoint_for(product: Product, policy: CloudProxyPolicy | None) -> str | None:
    """产品实际要连的主机；地域服务没配置时为 None，调用方应拒绝。"""
    if product.endpoint is not None:
        return product.endpoint
    if product.key == "mse" and policy is not None:
        return policy.mse.endpoint
    return None


def classify(product: Product, action: str) -> Literal["read", "write"] | None:
    if action in product.write_actions:
        return "write"
    if action in product.read_actions:
        return "read"
    return None


def _pe(s: str) -> str:
    # 阿里云规则：只有 ~ 不编码；quote 默认会放过 /，safe 必须显式收窄，且 * 要编码
    return urllib.parse.quote(str(s), safe="~")


def sign_v1(
    params: dict[str, str],
    *,
    version: str,
    access_key_id: str,
    secret: str,
    nonce: str,
    timestamp: str,
) -> str:
    """返回已签名的 form 体。params 须含 Action，且不得含保留参数。"""
    body = {
        "Format": "JSON",
        "Version": version,
        "AccessKeyId": access_key_id,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": nonce,
        "Timestamp": timestamp,
    }
    body.update(params)
    canon = "&".join(f"{_pe(k)}={_pe(v)}" for k, v in sorted(body.items()))
    to_sign = "POST&%2F&" + _pe(canon)
    body["Signature"] = base64.b64encode(
        hmac.new((secret + "&").encode(), to_sign.encode(), hashlib.sha1).digest()
    ).decode()
    return urllib.parse.urlencode(body)
