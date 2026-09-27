"""样例回复：覆盖全部块类型，给真机预览命令和测试共用。数据都是虚构的。"""

from __future__ import annotations

# 预览命令会把这个地址换成一张本地生成的示意图。
SAMPLE_IMAGE_URL = "https://example.com/bag.png"

DAILY_REPORT = """```card:header
{"title": "经营日报 · 9 月 25 日", "subtitle": "数据截至 23:59", "color": "turquoise",
 "tags": [{"text": "达标", "color": "green"}]}
```

GMV **$286,400**，比上周同日 <font color='green'>↑12.6%</font>，订单 97 单。

```card:kpi
{"items": [
  {"label": "GMV", "value": "$286,400", "delta": "+12.6%", "good": true},
  {"label": "订单", "value": 97, "delta": "+8", "good": true},
  {"label": "客单价", "value": "$2,953", "delta": "+4.1%", "good": true},
  {"label": "退款率", "value": "2.1%", "delta": "+0.4pt", "good": false}
]}
```

```card:chart
{"chart": "hbar", "title": "品类占比", "size": "half",
 "items": [["手袋", 34], ["腕表", 26], ["珠宝", 17], ["配饰", 11], ["其他", 12]],
 "format": {"unit": "%"}}
```

```card:chart
{"chart": "area", "title": "近 7 天 GMV（千美元）", "size": "half",
 "x": ["9/19", "9/20", "9/21", "9/22", "9/23", "9/24", "9/25"],
 "series": [{"name": "GMV", "values": [214, 231, 298, 252, 233, 268, 286]}],
 "labels": "last", "highlight": "last"}
```

💡 增长主要来自手袋品类，两个爆款成交最多。
"""

ITEM_DIAGNOSIS = """这件商品流量不差，问题在价格：

```card:item
{"eyebrow": "示例品牌", "title": "经典托特包 · 黑色", "image": "https://example.com/bag.png",
 "meta": ["2024 款", "成色 优", "售价 $4,621"],
 "highlight": {"text": "已上架 72 天", "level": "warning"},
 "code": "SKU-1024"}
```

```card:kpi
{"items": [{"label": "浏览", "value": 1240}, {"label": "收藏", "value": 38},
 {"label": "加购", "value": 6}, {"label": "下单", "value": 0, "good": false}]}
```

- 站内同款同成色在售 5 件，这件**最贵**
- 近 90 天同款成交中位价 **$4,050**，平均 18 天卖出

```card:callout
{"level": "success", "title": "建议调到 $4,099", "text": "预计两周内成交。"}
```
"""

REPRICING = """找到 **14 件**上架超 60 天、比同款成交价高 10% 以上：

| 商品编号 | 款式 | 现价 | 建议价 |
|---|---|---:|---:|
| `SKU-1024` | 经典托特包 | $4,621 | <font color='green'>$4,099</font> |
| `SKU-2048` | 迷你手提包 | $9,980 | <font color='green'>$8,850</font> |
| `SKU-4096` | 斜挎包 | $3,420 | <font color='green'>$2,990</font> |
| `SKU-8192` | 购物袋 | $3,150 | <font color='green'>$2,780</font> |
| `SKU-1638` | 链条包 | $2,860 | <font color='green'>$2,540</font> |
| `SKU-3276` | 水桶包 | $2,310 | <font color='green'>$2,050</font> |

```card:callout
{"level": "warning", "title": "即将在商品后台修改 14 件商品的售价", "text": "确认后开始执行。"}
```

```card:actions
{"buttons": [{"text": "查看调价明细", "url": "https://example.com/pricing", "style": "primary"},
 {"text": "打开商品后台", "url": "https://example.com/admin"},
 {"text": "只列出手袋品类的调价商品", "reply": true}]}
```
"""

LOGISTICS = """```card:timeline
{"steps": [
  {"time": "9/17", "text": "下单支付 · 经典翻盖包", "status": "done"},
  {"time": "9/18", "text": "鉴定通过", "status": "done"},
  {"time": "9/19", "text": "海外仓出库", "status": "done"},
  {"time": "9/20", "text": "航班抵达", "status": "done"},
  {"time": "9/20 起", "text": "清关中 · **已停留 6 天**", "status": "warning"}
]}
```

同一航班还有 **11 单**卡在清关，建议统一找清关代理加急。

**给客户的回复草稿：**

> Hi, thank you for your patience! Your bag has passed authentication and is now clearing
> customs. We expect delivery by Oct 1 and will send tracking updates as it moves.

```card:people
{"title": "跟进人", "users": ["ops@example.com", "customs@example.com"]}
```

```card:note
{"text": "物流数据每 30 分钟同步一次。"}
```
"""

INSPECTION = """```card:header
{"title": "☀️ 每日经营巡检 · 9 月 27 日", "color": "turquoise"}
```

```card:table
{"columns": [{"key": "m", "title": "指标"}, {"key": "y", "title": "昨日"},
  {"key": "c", "title": "对比"}, {"key": "s", "title": "状态", "type": "tag"}],
 "rows": [
  {"m": "日活", "y": "48,210", "c": "7 日均值 −3%", "s": "正常"},
  {"m": "下单转化", "y": "1.2%", "c": "7 日均值 1.6%", "s": "偏低"},
  {"m": "鉴定超 48 小时", "y": "17 单", "c": "集中在手袋", "s": "待处理"}
 ]}
```

转化下降主要在 **iOS 结算页**：支付失败率升到 8%（平时 2%）。

```card:columns
{"columns": [
  [{"type": "chart", "chart": "donut", "title": "支付失败原因",
    "items": [["超时", 52], ["风控", 28], ["余额不足", 20]],
    "center": {"value": "8%", "caption": "失败率"}}],
  [{"type": "chart", "chart": "line", "title": "近 7 天转化率",
    "x": ["9/21", "9/22", "9/23", "9/24", "9/25", "9/26", "9/27"],
    "series": [{"name": "iOS", "values": [1.7, 1.6, 1.6, 1.5, 1.4, 1.3, 1.2]},
               {"name": "Android", "values": [1.6, 1.6, 1.7, 1.6, 1.6, 1.7, 1.6]}],
    "format": {"unit": "%", "decimals": 1}}]
]}
```
"""

SAMPLES: dict[str, str] = {
    "daily": DAILY_REPORT,
    "item": ITEM_DIAGNOSIS,
    "repricing": REPRICING,
    "logistics": LOGISTICS,
    "inspection": INSPECTION,
}
