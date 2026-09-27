"""richtext.blocks：切块、围栏识别、宽松 JSON、流式截断。"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coreman.core.richtext.blocks import Segment, has_blocks, parse_json, split
from coreman.core.richtext.schema import KpiBlock

KPI = '{"items":[{"label":"浏览","value":"1,240"}]}'
CALLOUT = '{"level":"warning","text":"库存告急"}'


def kinds(segments: list[Segment]) -> list[tuple[str, str | None]]:
    return [(s.kind, s.block) for s in segments]


# ---- 基本切分 ----


def test_plain_text_is_one_markdown_segment() -> None:
    assert split("你好\n\n- 一\n- 二\n") == [Segment("markdown", "你好\n\n- 一\n- 二")]


def test_empty_and_blank_text() -> None:
    assert split("") == []
    assert split("\n  \n\t\n") == []


def test_blocks_and_markdown_keep_order() -> None:
    text = f"开头\n\n```card:kpi\n{KPI}\n```\n\n中间\n```card:callout\n{CALLOUT}\n```\n结尾\n"
    segs = split(text)
    assert kinds(segs) == [
        ("markdown", None),
        ("block", "kpi"),
        ("markdown", None),
        ("block", "callout"),
        ("markdown", None),
    ]
    assert [s.text for s in segs] == ["开头", KPI, "中间", CALLOUT, "结尾"]
    assert isinstance(segs[1].model, KpiBlock)
    assert segs[1].model.items[0].value == "1,240"


def test_adjacent_blocks_have_no_blank_markdown_between() -> None:
    text = f"```card:kpi\n{KPI}\n```\n\n\n```card:callout\n{CALLOUT}\n```"
    assert kinds(split(text)) == [("block", "kpi"), ("block", "callout")]


def test_markdown_edges_strip_blank_lines_but_keep_indent() -> None:
    text = f"```card:kpi\n{KPI}\n```\n\n\n    缩进代码\n  继续\n\n\n"
    assert split(text)[1] == Segment("markdown", "    缩进代码\n  继续")


def test_crlf_line_endings() -> None:
    text = f"前\r\n```card:kpi\r\n{KPI}\r\n```\r\n后"
    segs = split(text)
    assert kinds(segs) == [("markdown", None), ("block", "kpi"), ("markdown", None)]
    assert segs[0].text == "前"


# ---- 围栏识别 ----


def test_card_inside_regular_code_block_stays_markdown() -> None:
    demo = f"块的写法：\n\n```markdown\n```card:kpi\n{KPI}\n```\n\n之后"
    segs = split(demo)
    # 内层 ``` 关掉了 markdown 代码块；之后的内容仍是普通文字，不会把示例当块。
    assert kinds(segs) == [("markdown", None)]
    assert "```card:kpi" in segs[0].text


def test_card_inside_longer_fence_is_not_a_block() -> None:
    demo = f"````markdown\n```card:kpi\n{KPI}\n```\n````\n"
    assert kinds(split(demo)) == [("markdown", None)]
    assert not has_blocks(demo)


def test_card_inside_tilde_code_block_is_not_a_block() -> None:
    demo = f"~~~\n```card:kpi\n{KPI}\n```\n~~~\n\n```card:callout\n{CALLOUT}\n```"
    assert kinds(split(demo)) == [("markdown", None), ("block", "callout")]


def test_tilde_card_fence() -> None:
    segs = split(f"~~~card:kpi\n{KPI}\n~~~")
    assert kinds(segs) == [("block", "kpi")]


def test_close_needs_same_char_and_enough_length() -> None:
    # ~~~ 开的块不能被 ``` 关；```` 开的块不能被 ``` 关。
    tilde = f"~~~card:kpi\n{KPI}\n```\n~~~"
    assert kinds(split(tilde)) == [("invalid", "kpi")]
    assert kinds(split(tilde.replace("```\n", ""))) == [("block", "kpi")]
    long = f"````card:kpi\n{KPI}\n```\n`````\n"
    assert kinds(split(long)) == [("invalid", "kpi")]
    assert kinds(split(long.replace("```\n", "", 1))) == [("block", "kpi")]


def test_close_fence_allows_only_trailing_whitespace() -> None:
    text = f"```card:kpi\n{KPI}\n``` 不是结束\n```  \n后"
    segs = split(text)
    assert kinds(segs) == [("invalid", "kpi"), ("markdown", None)]
    segs = split(f"```card:kpi\n{KPI}\n```  \t\n后")
    assert kinds(segs) == [("block", "kpi"), ("markdown", None)]


def test_indentation_up_to_three_spaces() -> None:
    assert kinds(split(f"   ```card:kpi\n{KPI}\n   ```")) == [("block", "kpi")]
    four = f"    ```card:kpi\n    {KPI}\n    ```"
    assert kinds(split(four)) == [("markdown", None)]
    assert kinds(split(f"\t```card:kpi\n{KPI}\n```")) == [("markdown", None)]


def test_info_string_is_case_insensitive_and_tolerates_spaces() -> None:
    assert kinds(split(f"```CARD:KPI\n{KPI}\n```")) == [("block", "kpi")]
    assert kinds(split(f"```  card : kpi  \n{KPI}\n```")) == [("block", "kpi")]


def test_info_string_must_be_exactly_card_kind() -> None:
    for info in ("card:kpi extra", "cards:kpi", "json card:kpi", "card:"):
        segs = split(f"```{info}\n{KPI}\n```")
        assert kinds(segs) == [("markdown", None)], info


def test_backtick_in_info_is_not_a_fence() -> None:
    assert kinds(split("```card:kpi```\n正文")) == [("markdown", None)]


def test_blockquoted_fence_is_not_a_block() -> None:
    assert kinds(split(f"> ```card:kpi\n> {KPI}\n> ```")) == [("markdown", None)]


# ---- 未闭合的块 ----


def test_unclosed_block_while_streaming_is_pending() -> None:
    text = '说明\n\n```card:kpi\n{"items":[{"label":"浏'
    segs = split(text, final=False)
    assert kinds(segs) == [("markdown", None), ("pending", "kpi")]
    assert segs[1].text == '{"items":[{"label":"浏'


def test_unclosed_block_at_end_is_parsed_when_final() -> None:
    assert kinds(split(f"```card:kpi\n{KPI}\n")) == [("block", "kpi")]
    assert kinds(split(f"```card:kpi\n{KPI}", final=False)) == [("pending", "kpi")]
    broken = split('```card:kpi\n{"items":[{"lab')
    assert kinds(broken) == [("invalid", "kpi")]
    assert broken[0].error


def test_empty_unclosed_fence() -> None:
    assert split("```card:kpi", final=False) == [Segment("pending", "", block="kpi")]
    assert kinds(split("```card:kpi")) == [("invalid", "kpi")]


@pytest.mark.parametrize(
    "tail", ["`", "``", "```", "```c", "```Car", "```card", "``` card :", "~~~"]
)
def test_streaming_withholds_a_line_that_may_become_a_card_fence(tail: str) -> None:
    # 半行先不显示：否则正文先带着这几个字符，下一帧块出现时又被吃回去。
    assert split(f"正文\n{tail}", final=False) == [Segment("markdown", "正文")]
    assert split(f"正文\n{tail}", final=True)[0].text == f"正文\n{tail}"


@pytest.mark.parametrize("tail", ["`x", "```py", "````json", "```card:kpi```"])
def test_streaming_shows_a_line_that_cannot_become_a_card_fence(tail: str) -> None:
    assert split(f"正文\n{tail}", final=False) == [Segment("markdown", f"正文\n{tail}")]


def test_streaming_partial_kind_is_pending() -> None:
    assert split("```card:ch", final=False) == [Segment("pending", "", block="ch")]


def test_streaming_inside_regular_code_block_is_not_withheld() -> None:
    assert split("```python\nx = 1\n``", final=False) == [
        Segment("markdown", "```python\nx = 1\n``")
    ]


# ---- 坏块 ----


def test_unknown_kind_is_invalid() -> None:
    [seg] = split('```card:mystery\n{"a":1}\n```')
    assert (seg.kind, seg.block, seg.text) == ("invalid", "mystery", '{"a":1}')
    assert seg.error is not None and "mystery" in seg.error


def test_bad_json_is_invalid() -> None:
    [seg] = split("```card:kpi\n不是 JSON\n```")
    assert seg.kind == "invalid"
    assert seg.error is not None and "JSON" in seg.error


def test_schema_violation_is_invalid() -> None:
    [seg] = split('```card:kpi\n{"items":[]}\n```')
    assert seg.kind == "invalid"
    assert seg.error is not None and seg.error.startswith("kpi.")
    assert seg.model is None


def test_unexpected_validator_error_is_invalid() -> None:
    # 列定义写成数字时，按位置对行的校验器会对 int 取 .get；不能让它冒出去。
    [seg] = split('```card:table\n{"columns":[1,2],"rows":[[1,2]]}\n```')
    assert seg.kind == "invalid"


def test_lenient_json_in_block() -> None:
    [seg] = split("```card:kpi\n{“items”:[{“label”：“浏览”,“value”:1240,},],}\n```")
    assert seg.kind == "block"
    assert isinstance(seg.model, KpiBlock)
    assert seg.model.items[0].label == "浏览"


# ---- 宽松 JSON ----


def test_parse_json_strict_first() -> None:
    assert parse_json('{"a": [1, 2]}') == {"a": [1, 2]}
    # 严格 JSON 能解析时，字符串里的中文引号和 // 都是正文。
    assert parse_json('{"t": "他说“好”，见 http://x//y"}') == {"t": "他说“好”，见 http://x//y"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1,}', {"a": 1}),
        ("[1, 2, ]", [1, 2]),
        ('{"a": [1,\n  2,\n],\n}', {"a": [1, 2]}),
        ("{“a”: “b”}", {"a": "b"}),
        ("{‘a’: ‘b’}", {"a": "b"}),
        ('{"a": 1, // 注释\n "b": 2}', {"a": 1, "b": 2}),
        ('{"a": 1, // 尾逗号后面跟注释\n}', {"a": 1}),
        ('{"a"：1，"b"：2}', {"a": 1, "b": 2}),
        # 修补路径里，半角字符串中的中文引号仍是正文。
        ('{"t": "他说“好”",}', {"t": "他说“好”"}),
        # 开合混写。
        ('{"name”: "浏览"}', {"name": "浏览"}),
        ('{“name": "浏览"}', {"name": "浏览"}),
        # 中文单引号括起来的字符串里的半角双引号是正文，要转义。
        ('{‘t’: ‘a"b’}', {"t": 'a"b'}),
        # 字符串里的真换行。
        ('{"t": "一\n二",}', {"t": "一\n二"}),
        ('{"url": "http://example.com/a",}', {"url": "http://example.com/a"}),
    ],
)
def test_parse_json_repairs(raw: str, expected: object) -> None:
    assert parse_json(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "{", "不是 JSON", '{"a": }', "[" * 100_000])
def test_parse_json_failure_is_value_error(raw: str) -> None:
    with pytest.raises(ValueError, match="JSON 解析失败"):
        parse_json(raw)


# ---- has_blocks ----


def test_has_blocks() -> None:
    assert has_blocks(f"前\n```card:kpi\n{KPI}\n```")
    assert has_blocks("```card:kpi\n{")  # 未闭合也算
    assert has_blocks("~~~ Card:Mystery\n{}\n~~~")  # 未知类型也算
    assert not has_blocks("普通文字，提到 card:kpi")
    assert not has_blocks("```python\nprint(1)\n```")
    assert not has_blocks(f"```markdown\n```card:kpi\n{KPI}\n```\n")
    assert not has_blocks(f"    ```card:kpi\n    {KPI}\n    ```")


# ---- 流式截断 ----

SAMPLE = (
    "## 本周概况\n\n销量**上涨**，详见下表。\n\n"
    f"```card:kpi\n{KPI}\n```\n\n"
    "示例写法：\n\n````markdown\n```card:chart\n{}\n```\n````\n\n"
    '~~~card:chart\n{"chart":"bar","x":["一","二"],"series":[{"name":"销量","values":[1,2]}],}\n~~~\n'
    "```python\nprint('```card:kpi')\n```\n"
    f"```card:callout\n{CALLOUT}\n```\n"
    "结论：继续观察。\n"
)


def test_every_prefix_is_safe() -> None:
    for i in range(len(SAMPLE) + 1):
        for final in (True, False):
            split(SAMPLE[:i], final=final)


def test_sample_final_shape() -> None:
    assert kinds(split(SAMPLE)) == [
        ("markdown", None),
        ("block", "kpi"),
        ("markdown", None),
        ("block", "chart"),
        ("markdown", None),
        ("block", "callout"),
        ("markdown", None),
    ]


def test_streaming_prefixes_only_grow() -> None:
    """流式时已出现的片段不再改变，只有最后一个片段在长：markdown 文字是定稿的前缀，
    pending 最终成为块。这是打字机效果能用的前提。"""
    full = split(SAMPLE)
    for i in range(len(SAMPLE) + 1):
        segs = split(SAMPLE[:i], final=False)
        assert len(segs) <= len(full)
        for k, seg in enumerate(segs[:-1]):
            assert seg == full[k], (i, k)
        if not segs:
            continue
        last, target = segs[-1], full[len(segs) - 1]
        if last.kind == "markdown":
            assert target.kind == "markdown" and target.text.startswith(last.text), i
        elif last.kind == "pending":
            assert target.kind in {"block", "invalid"}, i
            assert target.block is not None and last.block is not None
            assert target.block.startswith(last.block), i
        else:
            assert last == target, i


_FENCY = st.text(
    alphabet=st.sampled_from(list('`~ \n\r\t{}[]":,card:kpiCARD“”，：/x中')),
    max_size=200,
)


@settings(max_examples=400, deadline=None)
@given(_FENCY, st.booleans())
def test_random_text_never_raises(text: str, final: bool) -> None:
    segs = split(text, final=final)
    for seg in segs:
        assert seg.kind != "markdown" or seg.text.strip()
        assert seg.kind != "pending" or not final
    has_blocks(text)


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=300))
def test_random_unicode_never_raises(text: str) -> None:
    for final in (True, False):
        split(text, final=final)
    try:
        parse_json(text)
    except ValueError:
        pass
