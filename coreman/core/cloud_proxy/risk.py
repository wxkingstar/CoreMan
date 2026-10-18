"""写动作的服务端风险判定：高风险要 Tower 审批，低风险直接执行。

规则移植自调用代理的 Skill 原有的本地判定：
- alidns：解析记录写入前的风险评估与拒绝条件
- mse：Nacos 配置更新的强确认判定、新建前的存在性检查
- slb：访问控制列表加白的校验
Skill 里「拒绝执行」的条件在这里一律记为高风险：代理不替人拒绝，交给审批人看。
判定所需的任何信息读不到、参数缺失或响应形状不对，都按高风险处理。
因部署而异的参数（域名、可信地址、实例、命名空间、ACL）来自 CLOUD_PROXY_POLICY，
缺哪一项，依赖它的写操作就按高风险处理。
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import yaml

from coreman.core.cloud_proxy.aliyun import PRODUCTS
from coreman.core.cloud_proxy.risk_policy import (
    AlidnsPolicy,
    CloudProxyPolicy,
    MsePolicy,
    SlbPolicy,
)

FALLBACK_REASON = "无法判定风险，按高风险处理"


def _unconfigured(item: str) -> str:
    return f"未配置 CLOUD_PROXY_POLICY.{item}，按高风险处理"


class AliyunReader(Protocol):
    async def call(self, product: str, action: str, params: dict[str, str]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Assessment:
    high: bool
    reasons: tuple[str, ...]


class _Undecidable(Exception):
    pass


async def assess(
    product: str,
    action: str,
    params: dict[str, str],
    reader: AliyunReader,
    *,
    now: datetime,
    policy: CloudProxyPolicy | None,
) -> Assessment:
    # 安全边界：任何异常（读失败、解析失败、乃至本模块的 bug）都只能导向高风险
    try:
        reasons = await _assess(product, action, params, reader, now, policy or CloudProxyPolicy())
    except Exception:
        return Assessment(high=True, reasons=(FALLBACK_REASON,))
    return Assessment(high=bool(reasons), reasons=tuple(reasons))


async def _assess(
    product: str,
    action: str,
    params: dict[str, str],
    reader: AliyunReader,
    now: datetime,
    policy: CloudProxyPolicy,
) -> list[str]:
    if product == "domain":
        return ["域名注册信息的任何写操作都按高风险处理"]
    p = PRODUCTS.get(product)
    if p is None:
        return [f"未登记的产品 {product}"]
    if action not in p.write_actions:
        return [f"{product} 的 {action} 不是登记的写操作"]
    # 规则只看得到登记的参数；其余参数照样会被签名发出，判不了就只能按高风险
    unknown = _rule_param_keys(action, params)
    if unknown:
        return unknown
    if product == "alidns":
        return await _assess_alidns(action, params, reader, now, policy.alidns)
    if product == "mse":
        return await _assess_mse(action, params, reader, policy.mse)
    if product == "slb":
        return _assess_slb(params, policy.slb)
    return [f"{product} 没有风险规则"]


# 每个写动作允许的参数名，取自 Skill 实际发出的参数；Action 由代理层携带
ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    "AddDomainRecord": frozenset({"DomainName", "RR", "Type", "Value", "TTL", "Line", "Priority"}),
    "UpdateDomainRecord": frozenset({"RecordId", "RR", "Type", "Value", "TTL", "Line", "Priority"}),
    "SetDomainRecordStatus": frozenset({"RecordId", "Status"}),
    "UpdateDomainRecordRemark": frozenset({"RecordId", "Remark"}),
    "UpdateNacosConfig": frozenset(
        {"InstanceId", "NamespaceId", "Group", "DataId", "Content", "Type", "AppName", "Desc"}
    ),
    "CreateNacosConfig": frozenset(
        {"InstanceId", "NamespaceId", "Group", "DataId", "Content", "Type"}
    ),
    "AddAccessControlListEntry": frozenset({"RegionId", "AclId", "AclEntrys"}),
}


def _rule_param_keys(action: str, params: dict[str, str]) -> list[str]:
    allowed = ALLOWED_PARAMS.get(action, frozenset()) | {"Action"}
    reasons = [f"包含未登记的参数：{k}" for k in sorted(params) if k not in allowed]
    # 阿里云对参数名大小写的处理未经验证，大小写变体可能让规则看到的和实际生效的不是同一个值
    folded: dict[str, list[str]] = {}
    for k in params:
        folded.setdefault(k.casefold(), []).append(k)
    reasons += [
        f"参数名仅大小写不同：{'、'.join(sorted(ks))}" for ks in folded.values() if len(ks) > 1
    ]
    return reasons


def _need(params: dict[str, str], *keys: str) -> list[str]:
    vals = [params.get(k) for k in keys]
    if any(not v for v in vals):
        raise _Undecidable
    return [v for v in vals if v]


def _str_field(d: dict[str, Any], key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str):
        raise _Undecidable
    return v


# ── alidns ────────────────────────────────────────────────────────

PROTECTED_RR_PREFIX = "_acme-challenge"
WRITABLE_TYPES = ("A", "AAAA", "CNAME", "TXT", "MX", "CAA", "SRV")
SENSITIVE_TYPES = ("MX", "CAA", "SRV")
RECENT_WINDOW_S, RECENT_LIMIT = 3600, 3
# 阿里云 UpdateDomainRecord 不传 Line 时按 default 写入，比较时必须按它算
DEFAULT_LINE = "default"


def _rule_managed_zone(zone: str, policy: AlidnsPolicy) -> str | None:
    if not policy.managed_zones:
        return _unconfigured("alidns.managed_zones")
    if zone.lower() not in policy.managed_zones:
        return f"{zone} 不在允许修改的域名 {', '.join(policy.managed_zones)} 内"
    return None


def _is_protected(rr: str) -> bool:
    rr = rr.lower()
    return rr == PROTECTED_RR_PREFIX or rr.startswith(PROTECTED_RR_PREFIX + ".")


def _rule_protected_rr(target_rr: str, before: dict[str, str] | None) -> str | None:
    if _is_protected(target_rr) or (before is not None and _is_protected(before.get("RR", ""))):
        return "_acme-challenge 记录由 cert-manager 自动维护，手工改会让证书签发失败"
    return None


def _rule_writable_type(rtype: str) -> str | None:
    if rtype.upper() not in WRITABLE_TYPES:
        return f"不支持写 {rtype} 记录（只允许 {', '.join(WRITABLE_TYPES)}）"
    return None


def _rule_root_or_wildcard(rr: str, before: dict[str, str] | None) -> str | None:
    before_rr = (before or {}).get("RR", "")
    if rr in ("@", "*") or rr.startswith("*.") or before_rr == "@" or before_rr.startswith("*"):
        return "根域名或泛解析，影响所有未单独配置的名称"
    return None


def _live(before: dict[str, str] | None) -> bool:
    return before is not None and str(before.get("Status", "")).upper() == "ENABLE"


def _rule_disable_live(action: str, before: dict[str, str] | None) -> str | None:
    if action == "disable" and _live(before):
        return "停用正在生效的记录，依赖它的服务会立即无法解析"
    return None


def _norm(v: object) -> str:
    return str(v).rstrip(".").lower()


def _rule_update_live(
    action: str, before: dict[str, str] | None, after: dict[str, str]
) -> str | None:
    if action != "update" or before is None or not _live(before):
        return None
    changed = [
        k
        for k in ("RR", "Type", "Value", "Line")
        if _norm(after.get(k, "")) != _norm(before.get(k, ""))
    ]
    if changed:
        return f"修改正在生效的记录（{', '.join(changed)}），会改变现有流量的去向"
    return None


def _rule_sensitive_type(rtype: str) -> str | None:
    if rtype in SENSITIVE_TYPES:
        return f"{rtype} 记录涉及邮件或证书签发授权"
    return None


def _rule_txt_verification(rtype: str, rr: str) -> str | None:
    if rtype == "TXT" and (rr == "@" or rr.startswith("_")):
        return "根域名或下划线开头的 TXT 常用于域名所有权验证、SPF/DMARC"
    return None


def _rule_untrusted_target(action: str, rtype: str, value: str, policy: AlidnsPolicy) -> str | None:
    if action not in ("add", "update", "enable") or not value:
        return None
    v = value.rstrip(".").lower()
    if rtype == "CNAME":
        if v.endswith(policy.trusted_cname_suffixes) or v in policy.managed_zones:
            return None
        return f"CNAME 指向未登记的外部目标 {v}"
    if rtype in ("A", "AAAA"):
        try:
            ip = ipaddress.ip_address(v)
        except ValueError:
            return f"无法识别的地址 {v}"
        if v in policy.trusted_ips or ip.is_private:
            return None
        return f"指向不在已知列表里的公网 IP {v}"
    return None


def _rule_recent_changes(logs: dict[str, Any], now: datetime) -> str | None:
    box = logs.get("RecordLogs")
    if not isinstance(box, dict) or not isinstance(box.get("RecordLog"), list):
        raise _Undecidable
    cutoff = (now.timestamp() - RECENT_WINDOW_S) * 1000
    # cert-manager 续签证书时会频繁改 _acme-challenge，不算人工变更
    count = 0
    for r in box["RecordLog"]:
        if not isinstance(r, dict):
            raise _Undecidable
        if int(r.get("ActionTimestamp") or 0) >= cutoff and "_acme-challenge" not in str(
            r.get("Message", "")
        ):
            count += 1
    if count >= RECENT_LIMIT:
        return f"最近 1 小时内已有 {RECENT_LIMIT} 次以上人工变更，需要确认不是批量误操作或破坏"
    return None


async def _read_record(reader: AliyunReader, record_id: str) -> dict[str, str]:
    r = await reader.call("alidns", "DescribeDomainRecordInfo", {"RecordId": record_id})
    rec = {k: _str_field(r, k) for k in ("DomainName", "RR", "Type", "Value", "Status")}
    line = r.get("Line", DEFAULT_LINE)
    if not isinstance(line, str):
        raise _Undecidable
    rec["Line"] = line
    return rec


async def _assess_alidns(
    action: str,
    params: dict[str, str],
    reader: AliyunReader,
    now: datetime,
    policy: AlidnsPolicy,
) -> list[str]:
    before: dict[str, str] | None
    if action == "AddDomainRecord":
        zone, rr, rtype, value = _need(params, "DomainName", "RR", "Type", "Value")
        kind, before = "add", None
        after = {
            "RR": rr,
            "Type": rtype,
            "Value": value,
            "Line": params.get("Line") or DEFAULT_LINE,
        }
    elif action == "UpdateDomainRecord":
        record_id, rr, rtype, value = _need(params, "RecordId", "RR", "Type", "Value")
        before = await _read_record(reader, record_id)
        zone, kind = before["DomainName"], "update"
        after = {
            "RR": rr,
            "Type": rtype,
            "Value": value,
            "Line": params.get("Line") or DEFAULT_LINE,
        }
    elif action == "SetDomainRecordStatus":
        record_id, status = _need(params, "RecordId", "Status")
        if status.lower() not in ("enable", "disable"):
            raise _Undecidable
        before = await _read_record(reader, record_id)
        zone, kind = before["DomainName"], status.lower()
        after = {"Status": status.upper()}
    elif action == "UpdateDomainRecordRemark":
        (record_id,) = _need(params, "RecordId")
        before = await _read_record(reader, record_id)
        zone, kind, after = before["DomainName"], "remark", {}
    else:
        return [f"alidns 的 {action} 没有风险规则"]

    # 改了主机记录时按新名称判断，与 Skill 预览里的目标名一致
    target_rr = (after.get("RR") or (before or {}).get("RR", "")).strip().lower()
    reasons = [
        r
        for r in (
            _rule_managed_zone(zone, policy),
            _rule_protected_rr(target_rr, before),
        )
        if r
    ]
    if kind in ("add", "update"):
        types = {after["Type"].upper()} | ({before["Type"].upper()} if before else set())
        reasons += [r for r in (_rule_writable_type(t) for t in sorted(types)) if r]
    # Skill 对备注不做风险判定，也不看变更历史
    if kind == "remark":
        return reasons

    rtype = (after.get("Type") or (before or {}).get("Type") or "").upper()
    value = after.get("Value") or (before or {}).get("Value") or ""
    reasons += [
        r
        for r in (
            _rule_root_or_wildcard(target_rr, before),
            _rule_disable_live(kind, before),
            _rule_update_live(kind, before, after),
            _rule_sensitive_type(rtype),
            _rule_txt_verification(rtype, target_rr),
            _rule_untrusted_target(kind, rtype, value, policy),
        )
        if r
    ]
    start = (now.date() - timedelta(days=1)).isoformat()
    logs = await reader.call(
        "alidns", "DescribeRecordLogs", {"DomainName": zone, "PageSize": "100", "StartDate": start}
    )
    recent = _rule_recent_changes(logs, now)
    if recent:
        reasons.append(recent)
    return reasons


# ── mse ───────────────────────────────────────────────────────────


def _rule_known_instance(instance_id: str | None, policy: MsePolicy) -> str | None:
    if policy.instance_id is None:
        return _unconfigured("mse.instance_id")
    if instance_id and instance_id != policy.instance_id:
        return f"MSE 实例 {instance_id} 不是登记的 Nacos 实例"
    return None


def _rule_known_namespace(env: str | None, namespace_id: str, policy: MsePolicy) -> str | None:
    if not policy.namespaces:
        return _unconfigured("mse.namespaces")
    if env is None:
        return f"命名空间 {namespace_id} 不是登记的环境（{' / '.join(policy.namespaces.values())}）"
    return None


def _rule_protected_update(env: str | None, policy: MsePolicy) -> str | None:
    if env is not None and env not in policy.low_risk_envs:
        return f"更新 {env} 环境的配置（不在 mse.low_risk_envs 内，需要审批）"
    return None


# YAML 1.2 类型解析（与读取方 Go yaml.v3 一致）、拒绝重复键
class _YamlAlias(_Undecidable):
    pass


# Skill 拒绝超过 100KB 的内容
NACOS_MAX_BYTES = 100 * 1024


class _Yaml12Loader(yaml.SafeLoader):
    # 别名会让小文本展开成指数级的对象树（alias bomb），拖垮遍历键路径；Skill 的配置用不到锚点和别名
    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()  # type: ignore[no-untyped-call]
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise _YamlAlias
        return super().compose_node(parent, index)


_DROP = {
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:timestamp",
}
_Yaml12Loader.yaml_implicit_resolvers = {
    k: [r for r in v if r[0] not in _DROP]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)
_Yaml12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"),
    list("-+0123456789"),
)
_Yaml12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+\.[0-9]*|[0-9]+(?:\.[0-9]*)?[eE][-+]?[0-9]+)"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
    ),
    list("-+0123456789."),
)


def _construct_int(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> int:
    v = str(loader.construct_scalar(node))
    if v.startswith("0o"):
        return int(v[2:], 8)
    if v.startswith("0x"):
        return int(v[2:], 16)
    return int(v, 10)


def _construct_bool(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> bool:
    return str(loader.construct_scalar(node)).lower() == "true"


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    out: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in out:
            raise _Undecidable
        out[key] = loader.construct_object(value_node, deep=True)
    return out


_Yaml12Loader.add_constructor("tag:yaml.org,2002:int", _construct_int)
_Yaml12Loader.add_constructor("tag:yaml.org,2002:bool", _construct_bool)
_Yaml12Loader.add_constructor("tag:yaml.org,2002:map", _construct_mapping)


def _no_dup_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise _Undecidable
        out[k] = v
    return out


def _nacos_format(type_: str | None, data_id: str) -> str:
    # 与 Skill 同口径：只有 json 和 yaml 两种解析
    if (type_ or "").lower() == "json" or data_id.endswith(".json"):
        return "json"
    return "yaml"


def _parse_nacos(text: str, fmt: str) -> Any:
    if fmt == "json":
        return json.loads(text, object_pairs_hook=_no_dup_pairs)
    docs = list(yaml.load_all(text, Loader=_Yaml12Loader))
    if len(docs) != 1:
        raise _Undecidable
    return docs[0]


class _Item:
    """路径里代表「列表元素」的标记，不会和任何真实的键相等。"""

    def __repr__(self) -> str:
        return "[]"


_ITEM = _Item()


def _key_paths(obj: Any, prefix: tuple[Any, ...] = ()) -> set[tuple[Any, ...]]:
    # 列表元素统一记成 []：删列表元素在 Skill 里算更新，删元素里的键算删除。
    # 用元组保留键的原始值和类型，避免「a.b」这种键名或 1 与 "1" 互相顶替
    out: set[tuple[Any, ...]] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = (*prefix, k)
            out.add(p)
            out |= _key_paths(v, p)
    elif isinstance(obj, list):
        for v in obj:
            out |= _key_paths(v, (*prefix, _ITEM))
    return out


def _show_path(path: tuple[Any, ...]) -> str:
    return ".".join(repr(k) if k is _ITEM else str(k) for k in path).replace(".[]", "[]")


def _rule_update_content(old_text: str, old_fmt: str, new_text: str, new_fmt: str) -> str | None:
    old_obj = _parse_nacos(old_text, old_fmt)
    if not new_text.strip():
        return "新内容为空，会清空整份配置"
    if len(new_text.encode("utf-8")) > NACOS_MAX_BYTES:
        return f"新内容超过 {NACOS_MAX_BYTES // 1024}KB 上限"
    try:
        new_obj = _parse_nacos(new_text, new_fmt)
    except _YamlAlias:
        return "配置内容包含 YAML 别名，无法安全比对"
    except Exception:
        return f"新内容无法按 {new_fmt} 解析（或有重复的键）"
    if not isinstance(new_obj, dict):
        return "新内容顶层不是对象"
    dropped = sorted(_show_path(p) for p in _key_paths(old_obj) - _key_paths(new_obj))
    if dropped:
        more = "…" if len(dropped) > 10 else ""
        return f"删除了现有的键：{'、'.join(dropped[:10])}{more}"
    return None


def _rule_create_existing(resp: dict[str, Any]) -> str | None:
    conf = resp.get("Configuration")
    if conf is None:
        return None
    if not isinstance(conf, dict):
        raise _Undecidable
    # 与 Skill 读取同口径：没有 Content 视为文档不存在
    if conf.get("Content") is None:
        return None
    return "目标环境已有这份文档，新建会覆盖现有内容（按更新处理）"


async def _assess_mse(
    action: str, params: dict[str, str], reader: AliyunReader, policy: MsePolicy
) -> list[str]:
    (namespace_id,) = _need(params, "NamespaceId")
    env = policy.namespaces.get(namespace_id)
    reasons = [
        r
        for r in (
            _rule_known_instance(params.get("InstanceId"), policy),
            _rule_known_namespace(env, namespace_id, policy),
        )
        if r
    ]
    if action == "UpdateNacosConfig":
        protected = _rule_protected_update(env, policy)
        if protected:
            reasons.append(protected)
        # Skill 拒绝删键和校验不过的内容；低风险环境经代理无人确认，必须在这里拦
        if env is not None and env in policy.low_risk_envs:
            group, data_id = _need(params, "Group", "DataId")
            new_text = params.get("Content")
            if new_text is None:
                raise _Undecidable
            query = {"NamespaceId": namespace_id, "Group": group, "DataId": data_id}
            if params.get("InstanceId"):
                query["InstanceId"] = params["InstanceId"]
            conf = (await reader.call("mse", "GetNacosConfig", query)).get("Configuration")
            if conf is not None and not isinstance(conf, dict):
                raise _Undecidable
            if conf is None or conf.get("Content") is None:
                reasons.append("目标环境没有这份文档，更新等于新建")
                return reasons
            old_text = _str_field(conf, "Content")
            old_type = conf.get("Type")
            old_fmt = _nacos_format(old_type if isinstance(old_type, str) else None, data_id)
            new_fmt = _nacos_format(params.get("Type") or None, data_id)
            content = _rule_update_content(old_text, old_fmt, new_text, new_fmt)
            if content:
                reasons.append(content)
        return reasons
    if action == "CreateNacosConfig":
        group, data_id = _need(params, "Group", "DataId")
        query = {"NamespaceId": namespace_id, "Group": group, "DataId": data_id}
        if params.get("InstanceId"):
            query["InstanceId"] = params["InstanceId"]
        existing = _rule_create_existing(await reader.call("mse", "GetNacosConfig", query))
        if existing:
            reasons.append(existing)
        return reasons
    return [f"mse 的 {action} 没有风险规则"]


# ── slb ───────────────────────────────────────────────────────────

MIN_PREFIX = 24
V6_HOST_PREFIX = 64
MIN_PREFIX_V6 = 48
KEEP_PREFIX = "keep-"

_Net = ipaddress.IPv4Network | ipaddress.IPv6Network


def _rule_known_acl(acl_id: str, policy: SlbPolicy) -> str | None:
    if not policy.acls:
        return _unconfigured("slb.acls")
    if acl_id not in policy.acls:
        return f"访问控制列表 {acl_id} 不是登记的白名单"
    return None


def _rule_too_wide(net: _Net) -> str | None:
    min_prefix = MIN_PREFIX_V6 if net.version == 6 else MIN_PREFIX
    if net.prefixlen < min_prefix:
        return f"网段 {net} 太宽：最宽只允许 /{min_prefix}"
    return None


def _rule_not_global(net: _Net) -> str | None:
    addr = net.network_address
    if not addr.is_global or addr.is_multicast:
        return f"{net} 不是公网地址（私网、保留、回环、组播段都不收）"
    return None


def _rule_not_host(net: _Net) -> str | None:
    if net.prefixlen >= (V6_HOST_PREFIX if net.version == 6 else 32):
        return None
    size = (
        f"{net.num_addresses} 个 IP"
        if net.version == 4
        else f"{2 ** (V6_HOST_PREFIX - net.prefixlen)} 个 /64"
    )
    return f"网段 /{net.prefixlen}（{size}）"


def _rule_keep_entry(comment: str) -> str | None:
    if comment.startswith(KEEP_PREFIX):
        return "keep- 条目不会被自动清理"
    return None


def _assess_slb(params: dict[str, str], policy: SlbPolicy) -> list[str]:
    acl_id, raw = _need(params, "AclId", "AclEntrys")
    # 原文会原样签名发给阿里云：重复键时两边解析器可能取不同的值，只能判不了
    entries = json.loads(raw, object_pairs_hook=_no_dup_pairs)
    # Skill 每次只加一条 {entry, comment}；多条、空列表或多余的键不是 Skill 会发出的请求
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise _Undecidable
    if set(entries[0]) - {"entry", "comment"}:
        raise _Undecidable
    entry = _str_field(entries[0], "entry").strip()
    comment = entries[0].get("comment", "")
    if not isinstance(comment, str):
        raise _Undecidable
    net = ipaddress.ip_network(
        entry if "/" in entry else f"{entry}/{32 if ':' not in entry else 128}", strict=False
    )
    return [
        r
        for r in (
            _rule_known_acl(acl_id, policy),
            _rule_too_wide(net),
            _rule_not_global(net),
            _rule_not_host(net),
            _rule_keep_entry(comment),
        )
        if r
    ]
