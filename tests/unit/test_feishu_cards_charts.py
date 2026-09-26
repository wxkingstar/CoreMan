"""图表 DSL → VChart spec：深浅双主题安全规则、数据预处理、体积预算、组件外形。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest

from coreman.core.feishu_cards import style
from coreman.core.feishu_cards.charts import (
    SPEC_BUDGET,
    TRACK_FILL,
    chart_spec,
    render_chart,
    spec_size,
)
from coreman.core.feishu_cards.context import MAX_CHARTS, RenderContext
from coreman.core.richtext.schema import ChartBlock

MONTHS = [f"{i}月" for i in range(1, 13)]
REGIONS = ["华东", "华南", "华北", "西南", "西北", "东北", "华中", "港澳"]
COLOR_TEXT = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\(.*\)|[a-z]+)$")
# 数据色以外，允许出现写死颜色的位置（路径用 / 连接，数字下标换成 *）。
ALLOWED_COLOR_PATHS = (
    re.compile(r"^color/\*$"),  # 图表级数据色
    re.compile(r".*area/style/fill/stops/\*/color$"),  # 面积渐变
    re.compile(r"^track/style/fill$"),  # 条形进度底轨
    re.compile(r"^series/\*/point/style/fill$"),  # 高亮点
    re.compile(r"^series/\*/label/style/fill$"),  # 高亮点的标签
)


def block(**data: Any) -> ChartBlock:
    return ChartBlock.model_validate(data)


def spec_of(half: bool = False, **data: Any) -> dict[str, Any]:
    return chart_spec(block(**data), half=half)


def walk(node: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    """深度优先遍历，产出 (路径, 值)；列表下标记成 *。"""
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, (*path, str(key)))
    elif isinstance(node, list):
        for value in node:
            yield from walk(value, (*path, "*"))


def data_rows(spec: dict[str, Any], index: int = 0) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = spec["data"][index]["values"]
    return rows


def assert_theme_safe(spec: dict[str, Any]) -> None:
    """真机验证过的双主题规则，逐条检查。"""
    assert spec["background"] == "transparent"
    assert spec["hover"] == {"enable": False}
    assert spec["select"] == {"enable": False}
    text = json.dumps(spec, ensure_ascii=False)
    for banned in ("fontFamily", "conical", "texture", "colorScheme"):
        assert banned not in text
    data_colors = set(spec.get("color", []))
    for path, value in walk(spec):
        assert not callable(value)
        joined = "/".join(path)
        if path and path[0] in {"data", "theme"}:
            continue
        key = path[-1] if path else ""
        if key in {"fill", "stroke", "color"} or (len(path) >= 2 and path[-2] == "color"):
            if isinstance(value, str) and COLOR_TEXT.match(value):
                assert any(p.match(joined) for p in ALLOWED_COLOR_PATHS), joined
                if joined == "track/style/fill":
                    assert value == TRACK_FILL
                elif joined.endswith(("point/style/fill", "label/style/fill")):
                    assert value in {style.CHART_HIGHLIGHT, *data_colors}, joined
        # 标签和点一律去掉描边（坐标轴的 label 是轴文字，不在此列）。
        if key in {"label", "point", "transformLabel", "outerLabel"} and "axes" not in path:
            assert isinstance(value, dict)
            assert value.get("style", {}).get("lineWidth") == 0, joined
        if key == "barBackground":
            assert set(value.get("style", {})) == {"cornerRadius"}
        if key == "gradient":
            assert value == "linear"
    # 标签颜色写在 theme 里，用语义色解析。
    series_theme = spec["theme"]["series"]
    assert series_theme
    for entry in series_theme.values():
        assert entry["label"]["style"]["fill"] == style.LABEL_PRIMARY
    assert spec_size(spec) <= SPEC_BUDGET < 6 * 1024


def _series(count: int, points: int, base: float = 100.0) -> list[dict[str, Any]]:
    return [
        {
            "name": REGIONS[k % len(REGIONS)] + f"区域{k}",
            "values": [base + i * 7 + k for i in range(points)],
        }
        for k in range(count)
    ]


def _items(count: int, name: str = "一个比较长的类目名称") -> list[dict[str, Any]]:
    return [{"name": f"{name}{i}", "value": 1000 + i * 37} for i in range(count)]


_DAYS = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(62)]
# 每种图表一个常规样例 + 一个上限数据量样例（名称取长，逼近体积预算）。
SAMPLES: dict[str, dict[str, Any]] = {
    "line": {"chart": "line", "x": MONTHS, "series": _series(2, 12)},
    "line_max": {"chart": "line", "x": _DAYS, "series": _series(8, 62, 1e5), "labels": "all"},
    "area": {"chart": "area", "x": MONTHS, "series": _series(1, 12), "highlight": "max"},
    "area_stack": {"chart": "area", "x": MONTHS, "series": _series(3, 12), "stack": "percent"},
    "bar": {"chart": "bar", "x": MONTHS[:4], "series": _series(2, 4)},
    "bar_stack": {"chart": "bar", "x": MONTHS, "series": _series(3, 12), "stack": "stack"},
    "bar_max": {"chart": "bar", "x": _DAYS, "series": _series(8, 62, 1e5), "labels": "all"},
    "hbar": {"chart": "hbar", "items": _items(5), "highlight": ["一个比较长的类目名称2"]},
    "hbar_max": {"chart": "hbar", "items": _items(500), "top_n": 50},
    "pie": {"chart": "pie", "items": _items(4)},
    "pie_max": {"chart": "pie", "items": _items(500), "top_n": 50},
    "donut": {
        "chart": "donut",
        "items": _items(3),
        "center": {"value": "3,111", "caption": "总计"},
    },
    "funnel": {"chart": "funnel", "items": [["曝光", 5676], ["点击", 1668], ["下单", 320]]},
    "funnel_max": {"chart": "funnel", "items": _items(500), "top_n": 50},
    "combo": {
        "chart": "combo",
        "x": MONTHS[:6],
        "series": [
            {"name": "销售额", "values": [120, 150, 130, 170, 160, 190]},
            {"name": "转化率", "values": [0.12, 0.18, 0.15, 0.2, 0.17, 0.22], "kind": "line"},
        ],
        "format2": {"percent_input": "ratio"},
    },
    "combo_max": {"chart": "combo", "x": _DAYS, "series": _series(8, 62, 1e5), "labels": "all"},
    "progress": {"chart": "progress", "items": [["华东", 0.795], ["华南", 0.25]]},
    "progress_max": {"chart": "progress", "items": _items(500), "top_n": 50},
    "ring": {"chart": "ring", "items": [["完成率", 0.72]]},
    "radar": {
        "chart": "radar",
        "x": ["价格", "品质", "服务", "速度", "口碑"],
        "series": [
            {"name": "本品", "values": [80, 70, 90, 60, 75]},
            {"name": "竞品", "values": [65, 80, 70, 85, 60]},
        ],
    },
    "radar_max": {"chart": "radar", "x": _DAYS, "series": _series(8, 62), "labels": "all"},
    "scatter": {"chart": "scatter", "points": [{"x": 24.5, "y": 0.32}, {"x": 33, "y": 0.18}]},
    "scatter_max": {
        "chart": "scatter",
        "points": [
            {"x": i * 1.37, "y": i * 911.5, "size": i % 17, "group": f"分组名称{i % 8}"}
            for i in range(500)
        ],
        "labels": "all",
    },
}


@pytest.mark.parametrize("half", [False, True], ids=["full", "half"])
@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_every_chart_is_theme_safe_and_within_budget(name: str, half: bool) -> None:
    spec = spec_of(half=half, **SAMPLES[name])
    assert_theme_safe(spec)
    assert json.loads(json.dumps(spec)) == spec


def test_every_chart_kind_has_a_sample() -> None:
    kinds = {sample["chart"] for sample in SAMPLES.values()}
    assert kinds == {
        "line", "area", "bar", "hbar", "pie", "donut", "funnel",
        "combo", "progress", "ring", "radar", "scatter",
    }  # fmt: skip


# ---------------------------------------------------------------- 数据预处理


def test_pie_top_n_merges_the_rest_into_other_at_the_end() -> None:
    items = [[f"品类{i}", 100 - i] for i in range(9)] + [["其他", 5]]
    rows = data_rows(spec_of(chart="pie", items=items))
    assert [r["name"] for r in rows] == [*(f"品类{i}" for i in range(6)), "其他"]
    # 品类6–8 并入已有的「其他」：94 + 93 + 92 + 5。
    assert rows[-1]["value"] == 284


def test_hbar_sorts_descending_keeps_top_ten_and_leaves_room_for_labels() -> None:
    items = [[f"地区{i}", v] for i, v in enumerate([3, 9, 1, 7, 5, 11, 2, 8, 4, 6, 10, 12])]
    spec = spec_of(chart="hbar", items=items)
    rows = data_rows(spec)
    assert [r["value"] for r in rows] == [12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 3]
    assert rows[-1]["name"] == "其他"  # 2 + 1
    assert spec["color"][-1] == style.CHART_OTHER
    assert spec["axes"][1]["max"] == pytest.approx(12 * 1.2)
    assert spec["barBackground"] == {"visible": True, "style": {"cornerRadius": 6}}


def test_sort_asc_and_custom_other_name() -> None:
    items = [["甲", 3], ["乙", 1], ["丙", 2], ["丁", 0.5]]
    rows = data_rows(spec_of(chart="hbar", items=items, sort="asc", top_n=2, other_name="其余"))
    assert [r["name"] for r in rows] == ["丙", "甲", "其余"]
    assert rows[-1]["value"] == 1.5


def test_line_downsamples_evenly_and_keeps_both_ends() -> None:
    x = [f"D{i}" for i in range(100)]
    series = [{"name": "访问", "values": list(range(100))}]
    rows = data_rows(spec_of(chart="line", x=x, series=series))
    assert len(rows) == 60
    assert rows[0]["x"] == "D0" and rows[-1]["x"] == "D99"
    assert len(data_rows(spec_of(half=True, chart="line", x=x, series=series))) == 30


def test_bar_truncates_to_24_categories() -> None:
    x = [f"D{i}" for i in range(30)]
    series = [{"name": "访问", "values": list(range(30))}]
    rows = data_rows(spec_of(chart="bar", x=x, series=series))
    # 没排序多半是时间序列：保留最近的 24 个。
    assert [r["x"] for r in rows] == x[6:]
    ranked = data_rows(spec_of(chart="bar", x=x, series=series, sort="desc"))
    assert [r["x"] for r in ranked][:2] == ["D29", "D28"] and len(ranked) == 24


def test_null_values_stay_as_gaps() -> None:
    series = [{"name": "访问", "values": [1, None, 3]}]
    rows = data_rows(spec_of(chart="line", x=["1月", "2月", "3月"], series=series))
    assert [r["y"] for r in rows] == [1, None, 3]
    assert "null" in json.dumps(rows)


def test_auto_scale_uses_wan_and_yi() -> None:
    wan = spec_of(
        chart="bar",
        x=["华东", "华南"],
        series=[{"name": "销售额", "values": [123456, 234567]}],
        format={"scale": "auto", "unit": "元"},
    )
    assert [r["y"] for r in data_rows(wan)] == [12.35, 23.46]
    assert [r["t"] for r in data_rows(wan)] == ["12.3万元", "23.5万元"]
    assert wan["axes"][1]["label"]["formatter"] == "{label:,.0f}万元"
    yi = spec_of(
        chart="line",
        x=["1月", "2月"],
        series=[{"name": "GMV", "values": [1.5e8, 3.2e8]}],
        format={"scale": "auto", "prefix": "¥"},
    )
    assert [r["y"] for r in data_rows(yi)] == [1.5, 3.2]
    assert data_rows(yi, 1)[0]["t"] == "¥3.2亿"
    assert yi["axes"][1]["label"]["formatter"] == "¥{label:,.1f}亿"
    plain = spec_of(chart="bar", x=["a"], series=[{"values": [99999]}], format={"scale": "auto"})
    assert data_rows(plain)[0]["y"] == 99999


def test_percent_input_ratio_and_percent_render_the_same() -> None:
    for values, kind in (([0.25, 0.5], "ratio"), ([25, 50], "percent")):
        spec = spec_of(
            chart="bar",
            x=["华东", "华南"],
            series=[{"name": "占比", "values": values}],
            format={"percent_input": kind},
        )
        assert [r["y"] for r in data_rows(spec)] == [25, 50]
        assert [r["t"] for r in data_rows(spec)] == ["25%", "50%"]
        assert spec["axes"][1]["label"]["formatter"] == "{label:,.0f}%"


def test_decimals_are_respected_in_label_text() -> None:
    spec = spec_of(chart="hbar", items=[["华东", 1234.5]], format={"decimals": 2, "unit": "件"})
    assert data_rows(spec)[0]["t"] == "1,234.50件"


# ---------------------------------------------------------------- 标签、高亮、图例、颜色


def test_line_labels_only_the_last_point_by_default() -> None:
    series = [{"name": "访问", "values": [1, 2, 3, None]}]
    spec = spec_of(chart="line", x=["1月", "2月", "3月", "4月"], series=series)
    assert spec["type"] == "common"
    assert spec["series"][0]["label"]["visible"] is False
    marks = data_rows(spec, 1)
    assert marks == [{"x": "3月", "y": 3, "s": "访问", "t": "3"}]  # 最后一个非空值
    assert spec["legends"] == {"visible": False}
    quiet = spec_of(chart="line", x=["a", "b"], series=[{"values": [1, 2]}], labels="none")
    assert len(quiet["data"]) == 1 and len(quiet["series"]) == 1


def test_highlight_last_point_is_orange_like_the_verified_demo() -> None:
    spec = spec_of(
        chart="area", x=["1月", "2月"], series=[{"name": "GMV", "values": [1, 2]}], highlight="last"
    )
    mark = spec["series"][1]
    assert mark["type"] == "scatter"
    assert mark["point"]["style"]["fill"] == style.CHART_HIGHLIGHT
    assert mark["label"]["style"]["fill"] == style.CHART_HIGHLIGHT
    stops = spec["series"][0]["area"]["style"]["fill"]["stops"]
    assert [s["color"] for s in stops] == ["rgba(51,112,235,0.32)", "rgba(51,112,235,0.02)"]


def test_bar_labels_auto_up_to_eight_bars() -> None:
    few = spec_of(chart="bar", x=MONTHS[:8], series=[{"name": "单量", "values": list(range(8))}])
    assert few["label"]["visible"] is True and data_rows(few)[3]["t"] == "3"
    many = spec_of(chart="bar", x=MONTHS[:9], series=[{"name": "单量", "values": list(range(9))}])
    assert many["label"]["visible"] is False and "t" not in data_rows(many)[0]
    top = spec_of(chart="bar", x=MONTHS, series=[{"values": list(range(12))}], labels="max")
    assert [r["t"] for r in data_rows(top)].count("") == 11 and data_rows(top)[-1]["t"] == "11"


def test_bar_highlight_colors_single_categories() -> None:
    spec = spec_of(
        chart="bar",
        x=["华东", "华南", "华北"],
        series=[{"name": "销售额", "values": [3, 5, 4]}],
        highlight=["华南"],
    )
    assert spec["seriesField"] == "x"
    assert spec["color"] == [style.CHART_PALETTE[0], style.CHART_HIGHLIGHT, style.CHART_PALETTE[0]]


def test_bar_group_stack_and_percent_modes() -> None:
    series = _series(2, 3)
    grouped = spec_of(chart="bar", x=MONTHS[:3], series=series)
    assert grouped["xField"] == ["x", "s"] and "stack" not in grouped
    assert grouped["legends"] == {"visible": True, "orient": "bottom"}
    stacked = spec_of(chart="bar", x=MONTHS[:3], series=series, stack="stack")
    assert stacked["xField"] == "x" and stacked["stack"] is True
    percent = spec_of(chart="bar", x=MONTHS[:3], series=series, stack="percent", labels="all")
    assert percent["percent"] is True
    assert percent["axes"][1]["label"]["formatter"] == "{label:.0%}"
    assert data_rows(percent)[0]["t"].endswith("%")


def test_colors_semantic_hex_and_series_override() -> None:
    spec = spec_of(
        chart="line",
        x=["1月", "2月"],
        series=[
            {"name": "甲", "values": [1, 2]},
            {"name": "乙", "values": [2, 1], "color": "#123456"},
            {"name": "丙", "values": [3, 3]},
        ],
        colors=["teal", "red", "不认识"],
    )
    assert spec["color"] == [style.CHART_SEMANTIC["teal"], "#123456", style.CHART_PALETTE[2]]
    pie = spec_of(chart="pie", items=[["甲", 3], ["乙", 2], ["其他", 1]])
    assert pie["color"] == [style.CHART_RAMP[0], style.CHART_RAMP[1], style.CHART_OTHER]


def test_legend_modes() -> None:
    one = [{"name": "访问", "values": [1, 2]}]
    assert spec_of(chart="bar", x=["a", "b"], series=one, legend="bottom")["legends"]["visible"]
    two = _series(2, 2)
    assert spec_of(chart="bar", x=["a", "b"], series=two, legend="none")["legends"] == {
        "visible": False
    }


def test_smooth_and_y_zero() -> None:
    series = [{"name": "访问", "values": [10, 12]}]
    spec = spec_of(chart="line", x=["a", "b"], series=series, smooth=False, y_zero=False)
    assert "curveType" not in spec["series"][0]["line"]["style"]
    assert spec["axes"][1]["zero"] is False
    assert (
        spec_of(chart="line", x=["a", "b"], series=series)["series"][0]["line"]["style"][
            "curveType"
        ]
        == "monotone"
    )


# ---------------------------------------------------------------- 各模板要点


def test_pie_labels_full_width_legend_half_width() -> None:
    items = [["线上", 560], ["线下", 440]]
    full = spec_of(chart="pie", items=items)
    assert full["label"]["visible"] is True and full["legends"] == {"visible": False}
    assert [r["t"] for r in data_rows(full)] == ["线上 56%", "线下 44%"]
    half = spec_of(half=True, chart="pie", items=items)
    assert half["label"]["visible"] is False and half["legends"]["visible"] is True


def test_donut_center_indicator() -> None:
    spec = spec_of(chart="donut", items=[["线上", 560], ["线下", 440]], center={"value": "1,000万"})
    assert spec["innerRadius"] < spec["outerRadius"]
    indicator = spec["indicator"]
    assert indicator["title"]["style"]["text"] == "1,000万" and "content" not in indicator
    total = spec_of(chart="donut", items=[["线上", 560], ["线下", 440]])["indicator"]
    assert total["title"]["style"]["text"] == "1,000"
    assert total["content"][0]["style"]["text"] == "合计"


def test_funnel_transform_label_uses_secondary_color() -> None:
    spec = spec_of(chart="funnel", items=[["点击", 1668], ["曝光", 5676]])
    assert [r["name"] for r in data_rows(spec)] == ["曝光", "点击"]  # 从宽到窄
    assert spec["isTransform"] is True and spec["transformLabel"]["visible"] is True
    fill = spec["theme"]["series"]["funnel"]["transformLabel"]["style"]["fill"]
    assert fill == style.LABEL_SECONDARY


def test_combo_puts_lines_on_a_synced_right_axis() -> None:
    spec = spec_of(**SAMPLES["combo"])
    kinds = [s["type"] for s in spec["series"]]
    assert kinds == ["bar", "line"]
    left, right = spec["axes"][1], spec["axes"][2]
    assert left["seriesId"] == [spec["series"][0]["id"]]
    assert right["seriesId"] == [spec["series"][1]["id"]]
    assert right["sync"] == {"axisId": "yL", "tickAlign": True}
    assert right["label"]["formatter"] == "{label:,.0f}%"
    assert data_rows(spec, 1)[-1]["t"] == "22%"  # 线只标末点


def test_progress_track_and_percent_in_category() -> None:
    spec = spec_of(chart="progress", items=[["华东", 79.5], ["华南", 25]])
    assert spec["type"] == "linearProgress"
    assert spec["track"] == {"style": {"fill": TRACK_FILL}}
    assert data_rows(spec) == [
        {"type": "华东 79.5%", "value": 0.795},
        {"type": "华南 25%", "value": 0.25},
    ]
    capped = spec_of(chart="progress", items=[["华东", 120]], max=100)
    assert data_rows(capped)[0] == {"type": "华东 120%", "value": 1}


def test_ring_indicator_defaults_to_percent_and_name() -> None:
    spec = spec_of(chart="ring", items=[["完成率", 0.72]])
    assert spec["type"] == "circularProgress" and spec["maxValue"] == 1
    assert spec["indicator"]["title"]["style"]["text"] == "72%"
    assert spec["indicator"]["content"][0]["style"]["text"] == "完成率"
    assert "track" not in spec  # 默认是系列色 20% 透明度，深浅自适应


def test_radar_and_scatter_axes() -> None:
    radar = spec_of(**SAMPLES["radar"])
    assert radar["axes"][0]["orient"] == "radius" and radar["axes"][0]["max"] == 100
    assert radar["legends"]["visible"] is True
    scatter = spec_of(
        chart="scatter",
        points=[{"x": 1, "y": 2, "size": 3, "group": "A类"}, {"x": 2, "y": 3, "group": "B类"}],
    )
    assert scatter["seriesField"] == "s" and scatter["sizeField"] == "z"
    assert all(axis.get("zero") is False for axis in scatter["axes"])


# ---------------------------------------------------------------- 组件


def test_render_chart_component_shape() -> None:
    ctx = RenderContext()
    out = render_chart(
        block(chart="hbar", items=_items(3), title="区域排行", summary="华东领先"), ctx
    )
    title, chart, summary = out
    assert title["tag"] == "markdown" and title["content"] == "**区域排行**"
    assert title["text_size"] == style.TEXT_BODY
    assert chart["tag"] == "chart" and chart["element_id"].startswith("chart_")
    assert chart["height"] == f"{32 * 3 + 24}px" and chart["preview"] is True
    assert summary["content"] == "<font color='grey'>华东领先</font>"
    assert summary["text_size"] == style.TEXT_NOTE
    assert len({e["element_id"] for e in out}) == 3


@pytest.mark.parametrize(
    ("data", "half", "height"),
    [
        (SAMPLES["line"], False, 240),
        (SAMPLES["pie"], False, 220),
        (SAMPLES["radar"], False, 220),
        (SAMPLES["line"], True, style.CHART_HALF_HEIGHT),
        ({**SAMPLES["pie"], "height": "lg"}, False, style.CHART_HEIGHT["lg"]),
        (SAMPLES["hbar_max"], False, 360),
    ],
)
def test_chart_heights(data: dict[str, Any], half: bool, height: int) -> None:
    (chart,) = render_chart(block(**data), RenderContext(), half=half)
    assert chart["height"] == f"{height}px"


def test_ring_and_progress_disable_preview() -> None:
    for name in ("ring", "progress"):
        (chart,) = render_chart(block(**SAMPLES[name]), RenderContext())
        assert chart["preview"] is False


def test_chart_quota_exhausted_returns_nothing() -> None:
    ctx = RenderContext()
    sample = block(**SAMPLES["bar"])
    for _ in range(MAX_CHARTS):
        assert render_chart(sample, ctx)
    assert render_chart(sample, ctx) == []
    assert ctx.charts == MAX_CHARTS
