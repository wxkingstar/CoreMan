"""A large synthetic back-office description shaped like a real operations console.

78 modules, a long guide, near-duplicate module names (carrier options vs carrier management,
product import vs sync-job control), "market" mentioned in unrelated modules, summaries mixing
requirement numbers and languages, and refund operations of several risk levels. Used to measure
catalog response sizes and search ranking; `metadata=True` adds the optional contract fields
(groups, display names, aliases, module limits) so both states can be compared.
"""

from __future__ import annotations

from typing import Any

GUIDE = (
    "运营后台 Agent API。所有时间为 UTC ISO 8601；金额为整数分（cents），币种见 currency 字段。"
    '响应统一包裹为 {"code":0,"data":...}，错误时 code 非 0、message 为原因。'
    "列表接口用 page、page_size 分页，page_size 最大 100，默认 20。"
    "资金相关的执行操作（退款执行、退款重试、打款、调账）仅网页端可用，Agent 只能查询。"
    "取消订单、售后判责等需要人工复核的操作也只在网页端执行。"
    "商品导入为异步作业：提交后用作业 ID 轮询进度；同步作业由调度器管理，中止后需在网页端重新排期。"
    "Market 是销售站点（站点 + 币种 + 语言），大部分配置按 market_id 隔离。"
    "承运商分两类接口：Carriers 只返回可选项供下拉使用，Ops Carriers 是承运商主数据维护。"
    "历史原因，部分模块名为 kebab-case（如 product-import），与 CamelCase 模块并存，含义不同。"
    "权限码前缀 ops:，详见 getMe 返回的 permissions。"
    "批量接口单次最多 200 条。导出接口返回异步任务 ID，结果文件 24 小时内有效。"
    "所有写操作写入审计日志，请在 reason 字段说明原因。"
) * 2

# (group, display) -> [(tag, display_name, description, aliases, [(op_id, method, summary, risk)])]
Op = tuple[str, str, str, str]
Module = tuple[str, str, str, list[str], list[Op]]

HAND: dict[tuple[str, str], list[Module]] = {
    ("config", "基础配置"): [
        (
            "Market",
            "市场（站点）",
            "Market 管理：销售站点、币种、语言与上下线（REQ-2231，决策 D-17 后拆分）",
            ["市场", "站点", "markets"],
            [
                ("listMarkets", "get", "List markets 列出全部 Market 及其状态", "read"),
                ("getMarket", "get", "查询单个 Market 的详细配置", "read"),
                ("updateMarket", "put", "Update market 修改 Market 名称、币种、语言", "write"),
                ("toggleMarket", "post", "上线或下线一个 Market（REQ-2231）", "destructive"),
            ],
        ),
        (
            "HomepageConfig",
            "首页配置",
            "首页楼层与 banner 配置，按 market 分别配置，实现走 CMS 缓存 v2",
            ["首页", "homepage"],
            [
                ("listHomepageSlots", "get", "按 market 查询首页楼层配置", "read"),
                ("updateHomepageSlot", "put", "修改首页楼层内容（各 market 独立）", "write"),
            ],
        ),
        (
            "Currencies",
            "币种",
            "币种与汇率查询",
            ["币种", "currency", "汇率"],
            [("listCurrencies", "get", "列出支持的币种及当日汇率", "read")],
        ),
    ],
    ("product", "商品"): [
        (
            "ProductImport",
            "商品导入",
            "商品导入：上传表格创建或更新商品，异步处理，查询导入结果（REQ-1880）",
            ["导入", "商品导入", "import"],
            [
                ("createProductImport", "post", "提交一个商品导入任务（上传文件 ID）", "write"),
                ("getProductImport", "get", "查询商品导入任务进度与失败行", "read"),
                ("listProductImports", "get", "分页列出最近的商品导入任务", "read"),
            ],
        ),
        (
            "product-import",
            "同步作业控制",
            "product sync jobs：调度器同步作业的查询、暂停、中止（legacy kebab 模块，D-09）",
            ["同步作业", "sync job", "同步"],
            [
                ("listSyncJobs", "get", "列出商品同步作业及调度状态", "read"),
                ("pauseSyncJob", "post", "暂停一个同步作业", "write"),
                (
                    "abortSyncJob",
                    "post",
                    "中止一个正在运行的同步作业，中止后需网页端重新排期",
                    "destructive",
                ),
            ],
        ),
        (
            "Pricing",
            "定价",
            "商品定价规则，按 market 与币种设置价格（REQ-2002）",
            ["价格", "定价", "price"],
            [
                ("listPriceRules", "get", "按 market 查询定价规则", "read"),
                ("updatePriceRule", "put", "修改定价规则，影响 market 前台价格", "write"),
            ],
        ),
    ],
    ("user", "用户"): [
        (
            "Subscriptions",
            "订阅",
            "会员订阅计划，各 market 可售计划不同",
            ["订阅", "会员", "subscription"],
            [
                ("listSubscriptionPlans", "get", "按 market 列出订阅计划", "read"),
                ("cancelSubscription", "post", "取消一个用户的订阅（立即生效）", "destructive"),
            ],
        ),
    ],
    ("aftersale", "售后"): [
        (
            "Refunds",
            "退款",
            "退款单查询；退款重试、取消、判责仅网页端可用（D-31，实现见 refund-worker）",
            ["退款", "refund", "退货退款"],
            [
                ("listRefunds", "get", "按状态、时间分页查询退款单", "read"),
                ("getRefund", "get", "查询一张退款单的明细与处理记录", "read"),
                ("retryRefund", "post", "重试一笔失败的退款（REQ-3010）", "financial"),
                ("cancelRefund", "post", "取消一笔待处理的退款", "financial"),
                ("judgeRefundLiability", "post", "售后判责：判定退款责任方", "destructive"),
            ],
        ),
    ],
    ("logistics", "物流"): [
        (
            "Carriers",
            "承运商选项",
            "承运商下拉选项（只读），供其他模块选择承运商",
            ["承运商", "carrier", "物流商"],
            [("listCarrierOptions", "get", "查询承运商选项列表（编码 + 名称）", "read")],
        ),
        (
            "Ops Carriers",
            "承运商管理",
            "承运商主数据维护：新增、修改、停用承运商及其服务线路",
            ["承运商", "carrier", "物流商"],
            [
                ("listCarriers", "get", "分页查询承运商主数据（含停用）", "read"),
                ("createCarrier", "post", "新增一个承运商", "write"),
                ("updateCarrier", "put", "修改承运商资料与服务线路", "write"),
                ("disableCarrier", "post", "停用一个承运商", "destructive"),
            ],
        ),
    ],
}

# Bulk modules to reach the real console's size; names and summaries are generic.
BULK: dict[tuple[str, str], list[tuple[str, str, str]]] = {
    ("config", "基础配置"): [
        ("Settings", "系统参数", "system settings 全局参数"),
        ("Regions", "地区", "国家与地区"),
        ("Languages", "语言", "多语言文案"),
        ("Dictionaries", "字典", "数据字典"),
        ("Roles", "角色", "后台角色"),
        ("AuditLogs", "审计日志", "操作审计"),
    ],
    ("product", "商品"): [
        ("Products", "商品", "商品主数据"),
        ("Categories", "类目", "商品类目树"),
        ("Brands", "品牌", "品牌库"),
        ("Attributes", "属性", "商品属性模板"),
        ("ProductReview", "商品审核", "上架审核"),
        ("Bundles", "组合商品", "组合与套装"),
        ("product-tags", "商品标签", "运营标签"),
    ],
    ("sourcing", "货源"): [
        ("Suppliers", "供应商", "供应商档案"),
        ("PurchaseOrders", "采购单", "采购下单"),
        ("Consignments", "寄卖", "寄卖货源"),
        ("Appraisals", "鉴定", "鉴定流程"),
        ("SupplierSettlements", "供应商结算", "供应商对账"),
        ("Quotes", "报价", "货源报价"),
    ],
    ("inventory", "库存"): [
        ("Warehouses", "仓库", "仓库档案"),
        ("Stocks", "库存", "库存查询"),
        ("StockMoves", "库存移动", "调拨与移库"),
        ("Stocktakes", "盘点", "盘点任务"),
        ("Inbounds", "入库", "入库单"),
        ("Outbounds", "出库", "出库单"),
    ],
    ("content", "内容"): [
        ("Articles", "文章", "内容文章"),
        ("Banners", "横幅", "广告位"),
        ("Pages", "专题页", "活动专题页"),
        ("Media", "素材", "图片视频素材"),
        ("SeoRules", "SEO", "SEO 规则"),
    ],
    ("marketing", "营销"): [
        ("Coupons", "优惠券", "优惠券模板与发放"),
        ("Promotions", "促销", "满减折扣活动"),
        ("FlashSales", "限时购", "限时购场次"),
        ("Campaigns", "营销活动", "站外活动"),
        ("Affiliates", "联盟", "联盟推广"),
        ("Referrals", "邀请", "邀请有礼"),
        ("Memberships", "会员等级", "会员等级权益"),
        ("PushMessages", "推送", "消息推送"),
    ],
    ("user", "用户"): [
        ("Users", "用户", "前台用户"),
        ("UserTags", "用户标签", "用户分群"),
        ("Points", "积分", "积分账户"),
        ("Addresses", "地址", "收货地址"),
        ("Reviews", "评价", "商品评价"),
        ("Tickets", "工单", "客服工单"),
    ],
    ("order", "订单"): [
        ("Orders", "订单", "订单查询"),
        ("OrderNotes", "订单备注", "备注与标记"),
        ("OrderExports", "订单导出", "订单导出任务"),
        ("Invoices", "发票", "发票"),
        ("FraudChecks", "风控", "订单风控"),
        ("Carts", "购物车", "购物车"),
    ],
    ("aftersale", "售后"): [
        ("Returns", "退货", "退货单"),
        ("Exchanges", "换货", "换货单"),
        ("Disputes", "纠纷", "支付纠纷"),
        ("Compensations", "补偿", "补偿券与积分"),
    ],
    ("logistics", "物流"): [
        ("Shipments", "发货", "发货单"),
        ("Tracking", "物流跟踪", "轨迹查询"),
        ("ShippingRates", "运费", "运费模板"),
        ("Labels", "面单", "面单打印"),
    ],
    ("payment", "支付"): [
        ("Payments", "支付单", "支付记录"),
        ("PaymentMethods", "支付方式", "支付渠道配置"),
        ("Payouts", "打款", "打款记录"),
        ("Reconciliations", "对账", "渠道对账"),
    ],
    ("data", "数据"): [
        ("Dashboards", "看板", "经营看板"),
        ("SalesReports", "销售报表", "销售统计"),
        ("TrafficReports", "流量报表", "流量统计"),
        ("ExportJobs", "导出任务", "通用导出"),
        ("Metrics", "指标", "指标口径"),
        ("Funnels", "转化漏斗", "转化分析"),
    ],
}

TEMPLATES = (
    ("list{E}", "get", "分页查询{d}（REQ-{n}）", "read"),
    ("get{E}", "get", "Get {d} detail 查询单个{d}详情", "read"),
    ("create{E}", "post", "新增{d}，实现见 {e}-service v{v}", "write"),
    ("update{E}", "put", "修改{d}", "write"),
    ("delete{E}", "delete", "删除{d}（D-{v}）", "destructive"),
)


def _entity(tag: str) -> str:
    words = tag.replace("-", " ").split()
    name = "".join(w[:1].upper() + w[1:] for w in words)
    return name[:-1] if name.endswith("s") and not name.endswith("ss") else name


def _operation(op_id: str, method: str, tag: str, summary: str, risk: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "operationId": op_id,
        "tags": [tag],
        "summary": summary,
        "description": f"{summary}。详细说明：参见运营手册；与 market 无关的全局数据。",
        "x-permission": f"ops:{tag.lower()}:{'read' if risk == 'read' else 'write'}",
        "responses": {
            "200": {
                "description": "OK",
                "content": {
                    "application/json": {
                        "schema": {"type": "object", "properties": {"data": {"type": "object"}}}
                    }
                },
            }
        },
    }
    if risk != ("read" if method == "get" else "write"):
        body["x-agent"] = {"risk": risk}
    if method != "get":
        body["parameters"] = [
            {
                "name": "id",
                "in": "query",
                "description": "目标 ID",
                "schema": {"type": "string"},
            }
        ]
    return body


def ops_spec(*, metadata: bool) -> dict[str, Any]:
    tags: list[dict[str, Any]] = []
    paths: dict[str, dict[str, Any]] = {}
    groups: dict[str, dict[str, Any]] = {}

    def add_module(group: tuple[str, str], tag: str, display: str, desc: str, aliases: list[str]):
        entry: dict[str, Any] = {"name": tag, "description": desc}
        agent: dict[str, Any] = {"aliases": aliases}
        if metadata:
            entry["x-displayName"] = display
            entry["x-agent"] = agent
            if tag == "Refunds":
                agent["limits"] = "退款重试、取消、判责仅网页端可用；Agent 只能查询退款单"
        tags.append(entry)
        groups.setdefault(group[1], {"name": group[1], "tags": []})["tags"].append(tag)

    n = 1000
    for group, modules in HAND.items():
        for tag, display, desc, aliases, ops in modules:
            add_module(group, tag, display, desc, aliases)
            for op_id, method, summary, risk in ops:
                paths[f"/api/ops/{tag.lower().replace(' ', '-')}/{op_id}"] = {
                    method: _operation(op_id, method, tag, summary, risk)
                }
    for group, modules in BULK.items():
        for tag, display, desc in modules:
            add_module(group, tag, display, f"{display}：{desc}，按 market 区分（REQ-{n}）", [])
            entity = _entity(tag)
            for index, (pattern, method, summary, risk) in enumerate(TEMPLATES):
                n += 7
                op_id = pattern.format(E=entity + ("s" if pattern == "list{E}" else ""))
                text = summary.format(d=display, n=n, e=entity.lower(), v=index + 2)
                paths[f"/api/ops/{tag.lower()}/{op_id}"] = {
                    method: _operation(op_id, method, tag, text, risk)
                }
    paths["/api/ops/me"] = {
        "get": {
            "operationId": "getMe",
            "tags": ["Settings"],
            "summary": "当前调用者身份与权限码",
            "x-permission": "none",
            "responses": {"200": {"description": "OK"}},
        }
    }
    spec: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {"title": "运营后台", "version": "1"},
        "x-agent": {"guide": GUIDE[:2000]},
        "tags": tags,
        "paths": paths,
    }
    if metadata:
        spec["x-tagGroups"] = list(groups.values())
    return spec
