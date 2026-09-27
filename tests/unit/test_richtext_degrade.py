"""richtext.degrade：块降级成普通 Markdown、飞书行内标记去标签。"""

import json
from typing import Any

import pytest

from coreman.core.richtext.degrade import block_to_markdown, strip_feishu_inline, to_markdown
from coreman.core.richtext.schema import validate_block


def md(kind: str, data: Any) -> str:
    return block_to_markdown(kind, validate_block(kind, data))


def card(kind: str, data: Any) -> str:
    return f"```card:{kind}\n{json.dumps(data, ensure_ascii=False)}\n```"


# ---- 每种块的降级快照 ----


def test_header() -> None:
    data = {"title": "周报", "subtitle": "9 月第 4 周", "tags": ["已完成", {"text": "关注"}]}
    assert md("header", data) == "**周报** 【已完成】【关注】\n9 月第 4 周"
    assert md("header", {"title": "周报"}) == "**周报**"


def test_kpi() -> None:
    data = {
        "items": [
            {"label": "浏览", "value": 1240, "delta": "+12%", "good": True},
            {"label": "下单", "value": "0", "delta": "-3", "good": False, "note": "较昨日"},
            # 方向看正负号，不看好坏：成本下降是好事，箭头仍朝下。
            {"label": "成本", "value": "80", "delta": "−5%", "good": True},
            {"label": "退款", "value": "2", "delta": "持平", "good": True},
            {"label": "毛利", "value": "3", "delta": "0.0%", "good": False},
            {"label": "库存", "value": "9", "delta": "↑2", "good": True},
            {"label": "访客", "value": "5", "delta": "+1"},
            {"label": "客单价", "value": 88.5, "note": "含税"},
        ]
    }
    assert md("kpi", data) == (
        "- **浏览**：1,240（↑+12%）\n"
        "- **下单**：0（↓-3，较昨日）\n"
        "- **成本**：80（↓−5%）\n"
        "- **退款**：2（持平）\n"
        "- **毛利**：3（0.0%）\n"
        "- **库存**：9（↑2）\n"
        "- **访客**：5（+1）\n"
        "- **客单价**：88.5（含税）"
    )


def test_chart_cartesian() -> None:
    data = {
        "chart": "combo",
        "title": "销量与转化",
        "x": ["周一", "周二"],
        "series": [
            {"name": "销量", "values": [1200, 1500.456]},
            {"name": "转化", "values": [0.125, None], "axis": "right"},
        ],
        "format": {"prefix": "$"},
        "format2": {"percent_input": "ratio"},
        "summary": "周二销量最高",
    }
    assert md("chart", data) == (
        "**销量与转化**\n\n"
        "|  | 销量 | 转化 |\n"
        "| --- | ---: | ---: |\n"
        "| 周一 | $1,200 | 12.5% |\n"
        "| 周二 | $1,500.46 | - |\n\n"
        "周二销量最高"
    )


def test_chart_series_names_default() -> None:
    one = {"chart": "line", "x": ["a"], "series": [{"values": [1]}]}
    assert md("chart", one).splitlines()[0] == "|  | 数值 |"
    two = {"chart": "bar", "x": ["a"], "series": [{"values": [1]}, {"values": [2]}]}
    assert md("chart", two).splitlines()[0] == "|  | 系列1 | 系列2 |"


def test_chart_itemized() -> None:
    data = {
        "chart": "donut",
        "items": [["华东", 820], {"label": "华南", "value": "1,300.5"}],
        "center": {"value": 2120, "caption": "合计"},
        "format": {"unit": "件", "decimals": 0},
    }
    assert md("chart", data) == (
        "合计：2,120\n\n| 名称 | 数值 |\n| --- | ---: |\n| 华东 | 820件 |\n| 华南 | 1,300件 |"
    )


def test_chart_scatter() -> None:
    data = {
        "chart": "scatter",
        "points": [{"x": 1, "y": 2, "size": 3, "group": "A"}, {"x": 1.5, "y": -0.004}],
    }
    assert md("chart", data) == (
        "| X | Y | 大小 | 分组 |\n"
        "| ---: | ---: | ---: | --- |\n"
        "| 1 | 2 | 3 | A |\n"
        "| 1.5 | 0 | - |  |"
    )


def test_chart_row_limit() -> None:
    data = {"chart": "hbar", "items": [{"name": f"第{i}名", "value": i} for i in range(1, 21)]}
    lines = md("chart", data).splitlines()
    assert lines[-3] == "| 第12名 | 12 |"
    assert lines[-2:] == ["", "…共 20 行"]


def test_table() -> None:
    data = {
        "title": "明细",
        "columns": [
            {"key": "name", "title": "名称"},
            {"key": "amt", "type": "money", "prefix": "¥"},
            {"key": "rate", "title": "占比", "type": "percent", "decimals": 1},
            {"key": "memo", "title": "备注", "align": "center"},
            "flag",
        ],
        "rows": [
            {"name": "a|b", "amt": -1234.5, "rate": 12.34, "memo": "第一行\n第二行", "flag": True},
            ["c", 7, None, ["x", "y"], {"k": 1}],
        ],
    }
    assert md("table", data) == (
        "**明细**\n\n"
        "| 名称 | amt | 占比 | 备注 | flag |\n"
        "| --- | ---: | ---: | :---: | --- |\n"
        "| a\\|b | -¥1,234.5 | 12.3% | 第一行 第二行 | 是 |\n"
        '| c | ¥7 |  | x、y | {"k":1} |'
    )


def test_table_row_limit() -> None:
    data = {"columns": ["n"], "rows": [[i] for i in range(45)]}
    out = md("table", data)
    assert out.count("\n| ") == 1 + 30  # 分隔行 + 30 行数据
    assert out.endswith("| 29 |\n\n…共 45 行")


def test_callout() -> None:
    data = {"level": "warning", "title": "库存告急", "text": "A 仓剩 3 件\n\n请尽快补货"}
    assert md("callout", data) == "> ⚠️ **库存告急**\n> A 仓剩 3 件\n>\n> 请尽快补货"
    assert md("callout", {"level": "success", "text": "已同步"}) == "> ✅ 已同步"
    assert md("callout", {"title": "提示"}) == "> ℹ️ **提示**"
    assert md("callout", {"level": "danger", "text": "失败"}) == "> ❗ 失败"


def test_item() -> None:
    data = {
        "title": "羊绒围巾",
        "eyebrow": "商品",
        "image": "https://example.com/a.png",
        "meta": ["¥1,280", "库存 3"],
        "code": "SKU-001",
        "highlight": {"text": "低库存", "level": "danger"},
        "tags": ["新品"],
        "link": "https://example.com/p/1",
    }
    assert md("item", data) == (
        "![羊绒围巾](https://example.com/a.png)\n"
        "**商品 · 羊绒围巾** 【新品】\n"
        "¥1,280 · 库存 3\n"
        "`SKU-001` · ❗ 低库存\n"
        "[查看](https://example.com/p/1)"
    )


def test_item_minimal_and_unsafe_urls() -> None:
    data = {"title": "订单 [A]", "image": "file:///etc/x.png", "link": "javascript:alert(1)"}
    assert md("item", data) == "**订单 [A]**"
    assert md("item", {"title": "t", "meta": "单行", "code": "a`b"}) == "**t**\n单行\n`` a`b ``"


def test_timeline() -> None:
    data = [
        {"time": "09:00", "text": "下单", "status": "done"},
        {"text": "发货", "status": "current"},
        {"text": "延迟", "status": "warning"},
        {"text": "异常", "status": "error"},
        {"text": "签收", "status": "pending"},
    ]
    assert md("timeline", data) == ("- ✅ 09:00  下单\n- 🔵 发货\n- ⚠️ 延迟\n- ❌ 异常\n- ⚪ 签收")


def test_columns() -> None:
    data = {
        "columns": [
            [{"type": "markdown", "text": "左栏"}, {"type": "note", "text": "口径 A"}],
            ["右栏文字", {"type": "kpi", "items": [{"label": "x", "value": 1}]}],
        ]
    }
    assert md("columns", data) == "左栏\n\n_注：口径 A_\n\n右栏文字\n\n- **x**：1"


def test_people() -> None:
    data = {"title": "负责人", "users": ["alice@example.com", "bob"]}
    assert md("people", data) == "👥 负责人：alice@example.com、bob"
    assert md("people", {"users": "carol"}) == "👥 carol"


def test_note() -> None:
    assert md("note", "口径：已支付订单\n不含退款") == "_注：口径：已支付订单 不含退款_"


def test_actions() -> None:
    data = [
        {"text": "查看详情", "url": "https://example.com/x"},
        {"text": "再查一次", "reply": "帮我再查一次"},
        {"text": "本地", "url": "ftp://example.com/y"},
    ]
    assert md("actions", data) == "[查看详情](https://example.com/x) · 「再查一次」 · 「本地」"


def test_raw() -> None:
    assert md("raw", {"elements": [{"tag": "hr"}], "fallback": "原生组件"}) == "原生组件"
    assert md("raw", {"elements": [{"tag": "hr"}]}) == ""


# ---- 整条回复 ----


def test_plain_text_passes_through() -> None:
    assert to_markdown("# 标题\n\n正文 **粗体**\n") == "# 标题\n\n正文 **粗体**"
    assert to_markdown("") == ""


def test_mixed_reply() -> None:
    text = (
        "结论先行。\n\n"
        + card("kpi", {"items": [{"label": "浏览", "value": 10}]})
        + "\n\n"
        + card("raw", {"elements": [{"tag": "hr"}]})
        + "\n\n<font color='green'>完成</font>\n"
    )
    assert to_markdown(text) == "结论先行。\n\n- **浏览**：10\n\n完成"


def test_invalid_block_kept_as_json_code() -> None:
    text = '前\n```card:kpi\n{"items": 不对}\n```\n后'
    assert to_markdown(text) == '前\n\n```json\n{"items": 不对}\n```\n\n后'
    unknown = '```card:mystery\n{"a": "```x"}\n```'
    assert to_markdown(unknown) == '````json\n{"a": "```x"}\n````'


def test_pending_hidden_while_streaming() -> None:
    text = '先说结论。\n\n```card:kpi\n{"items":[{"label":"浏'
    assert to_markdown(text, final=False) == "先说结论。"
    assert to_markdown(text) == '先说结论。\n\n```json\n{"items":[{"label":"浏\n```'


def test_unclosed_but_complete_block_when_final() -> None:
    text = '```card:note\n{"text":"口径说明"}'
    assert to_markdown(text) == "_注：口径说明_"
    assert to_markdown(text, final=False) == ""


def test_inline_tags_stripped_inside_blocks() -> None:
    text = card("callout", {"text": "状态 <text_tag color='red'>异常</text_tag>"})
    assert to_markdown(text) == "> ℹ️ 状态 【异常】"
    # 单独降级一个块时保留标签：飞书的简化卡片还认它们。
    model = validate_block("note", {"text": "<font color='red'>x</font>"})
    assert block_to_markdown("note", model) == "_注：<font color='red'>x</font>_"


def test_every_prefix_is_safe() -> None:
    text = (
        "开头 <font color='red'>红</font>\n\n"
        + card("chart", {"chart": "pie", "items": [["a", 1], ["b", 2]]})
        + "\n"
        + card("table", {"columns": ["k"], "rows": [["v"]]})
        + "\n```python\nprint('<font>')\n```\n结尾"
    )
    for i in range(len(text) + 1):
        to_markdown(text[:i], final=False)
        to_markdown(text[:i])


# ---- 行内标记 ----


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("<font color='green'>好</font>", "好"),
        ('<font color="red">坏</font>', "坏"),
        ("<font color=grey>灰</font>", "灰"),
        ("<FONT COLOR='blue'>蓝</FONT>", "蓝"),
        ("<text_tag color='orange'>进行中</text_tag>", "【进行中】"),
        ("<text_tag>无色</text_tag>", "【无色】"),
        ("<font color='red'><font color='blue'>嵌套</font>尾</font>", "嵌套尾"),
        ("<font color='red'>跨\n行</font>", "跨\n行"),
        # 其他标签和没闭合的标签原样。
        ("<b>粗</b> <at id=all></at>", "<b>粗</b> <at id=all></at>"),
        ("<font color='red'>没闭合", "<font color='red'>没闭合"),
        ("a < b > c", "a < b > c"),
    ],
)
def test_strip_feishu_inline(src: str, expected: str) -> None:
    assert strip_feishu_inline(src) == expected


def test_strip_feishu_inline_leaves_code_alone() -> None:
    src = (
        "外 <font color='red'>红</font> 与 `<font color='red'>行内</font>`\n"
        "```html\n<font color='red'>代码块</font>\n```\n"
        "~~~\n<text_tag>波浪</text_tag>\n~~~\n"
        "``a ` <font>b</font>`` 后 <text_tag>t</text_tag>"
    )
    assert strip_feishu_inline(src) == (
        "外 红 与 `<font color='red'>行内</font>`\n"
        "```html\n<font color='red'>代码块</font>\n```\n"
        "~~~\n<text_tag>波浪</text_tag>\n~~~\n"
        "``a ` <font>b</font>`` 后 【t】"
    )


def test_strip_feishu_inline_unmatched_backtick_is_literal() -> None:
    assert strip_feishu_inline("单个 ` 反引号 <font color=red>x</font>") == "单个 ` 反引号 x"


def test_strip_feishu_inline_unclosed_fence_protects_rest() -> None:
    src = "```\n<font color=red>x</font>"
    assert strip_feishu_inline(src) == src
