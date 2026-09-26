"""GFM → 飞书卡片 markdown：规范化规则、顶层切块成组件、图片地址收集。"""

from __future__ import annotations

import re
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coreman.core.feishu_cards import style
from coreman.core.feishu_cards.context import RenderContext
from coreman.core.feishu_cards.markdown import (
    CODE_LANGUAGES,
    image_urls,
    normalize,
    render_markdown,
)

ELEMENT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,19}$")


def render(md: str, **kwargs: Any) -> list[dict[str, Any]]:
    return render_markdown(md, RenderContext(**kwargs))


def tags(components: list[dict[str, Any]]) -> list[str]:
    return [c["tag"] for c in components]


def table(rows: int, header: str = "| 名称 | 说明 |", delimiter: str = "|---|---|") -> str:
    body = "".join(f"| 项{i} | 文字{i} |\n" for i in range(rows))
    return f"{header}\n{delimiter}\n{body}"


# ---------------------------------------------------------------- normalize：列表


def test_bullets_become_dashes_and_ordered_parens_become_dots() -> None:
    assert normalize("* a\n* b") == "- a\n- b"
    assert normalize("+ a\n+ b") == "- a\n- b"
    assert normalize("1) 一\n2) 二") == "1. 一\n2. 二"


def test_nested_lists_are_reindented_to_four_spaces_per_level() -> None:
    src = "- a\n  - b\n    - c\n- d"
    assert normalize(src) == "- a\n    - b\n        - c\n- d"
    assert normalize("1. 一\n   - 子项\n2. 二") == "1. 一\n    - 子项\n2. 二"


def test_loose_list_keeps_blank_lines_and_continuation_paragraph() -> None:
    assert normalize("- a\n\n- b\n\n  续段\n") == "- a\n\n- b\n\n    续段"


def test_task_list_items_become_check_boxes() -> None:
    assert normalize("- [ ] 待办\n- [x] 完成\n* [X] 也完成") == ("- ☐ 待办\n- ☑ 完成\n\n- ☑ 也完成")


def test_ordered_numbers_are_kept() -> None:
    assert normalize("3. 三\n4. 四") == "3. 三\n4. 四"


# ---------------------------------------------------------------- normalize：缩进与代码块


def test_indented_text_outside_lists_loses_its_indent() -> None:
    assert normalize("说明：\n\n    这不是代码\n    第二行") == "说明：\n\n这不是代码\n第二行"


@pytest.mark.parametrize(
    ("alias", "lang"),
    [
        ("py", "python"),
        ("js", "javascript"),
        ("jsx", "javascript"),
        ("mjs", "javascript"),
        ("ts", "typescript"),
        ("tsx", "typescript"),
        ("sh", "bash"),
        ("zsh", "bash"),
        ("console", "bash"),
        ("shell-session", "bash"),
        ("yml", "yaml"),
        ("c++", "cpp"),
        ("cc", "cpp"),
        ("hpp", "cpp"),
        ("c#", "c_sharp"),
        ("cs", "c_sharp"),
        ("objc", "objective_c"),
        ("dockerfile", "docker_file"),
        ("golang", "go"),
        ("txt", "plain_text"),
        ("text", "plain_text"),
        ("md", "markdown"),
        ("kt", "kotlin"),
        ("rb", "ruby"),
        ("rs", "rust"),
        ("ps1", "powershell"),
        ("proto", "protobuf"),
        ("tf", "plain_text"),
        ("hcl", "plain_text"),
        ("Python", "python"),
        ("SQL", "sql"),
        ("python3.12", "python"),
        ("js {1,3}", "javascript"),
    ],
)
def test_code_language_aliases_map_to_feishu_names(alias: str, lang: str) -> None:
    assert lang in CODE_LANGUAGES
    assert normalize(f"```{alias}\nx\n```") == f"```{lang}\nx\n```"


def test_unknown_code_language_is_dropped() -> None:
    assert normalize("```brainfuck\n+++\n```") == "```\n+++\n```"
    assert normalize("```zig\nconst x = 1;\n```") == "```\nconst x = 1;\n```"


def test_feishu_knows_69_code_languages() -> None:
    assert len(CODE_LANGUAGES) == 69
    assert {"plain_text", "c_sharp", "docker_file", "objective_c", "shell"} <= CODE_LANGUAGES


def test_tilde_fences_become_backticks_and_fence_lines_lose_spaces() -> None:
    assert normalize("~~~python\nx = 1\n~~~") == "```python\nx = 1\n```"
    assert normalize("  ```py  \n  x = 1\n  ```  ") == "```python\nx = 1\n```"


def test_fence_grows_when_code_contains_backtick_fences() -> None:
    assert normalize("~~~~md\n```\ninner\n```\n~~~~") == "````markdown\n```\ninner\n```\n````"


def test_code_block_content_is_never_rewritten() -> None:
    code = (
        "* not a list\n    indented\n<b>tag</b> a<b List<T> <at id=all></at>\n"
        "_x_ **a****b** https://example.com [t](mailto:a@example.com) \\* ~~~\n"
        "line with two spaces  \n\ttab"
    )
    assert normalize(f"```\n{code}\n```") == f"```\n{code}\n```"


def test_inline_code_is_never_rewritten() -> None:
    src = "用 `List<T>`、`a_b_c_`、`https://example.com`、`<at id=all>` 和 ``a ` b``"
    assert normalize(src) == src


def test_unclosed_fence_is_closed() -> None:
    assert normalize("```py\nprint(1)") == "```python\nprint(1)\n```"


def test_fence_inside_list_follows_the_list_indent() -> None:
    src = "1. 安装：\n   ```sh\n   pip install x\n   ```\n2. 运行"
    assert normalize(src) == "1. 安装：\n    ```bash\n    pip install x\n    ```\n2. 运行"


# ---------------------------------------------------------------- normalize：HTML


def test_tags_outside_the_whitelist_and_bare_lt_are_escaped() -> None:
    assert normalize("<b>粗</b> <span>x</span>") == "&#60;b>粗&#60;/b> &#60;span>x&#60;/span>"
    assert normalize("List<T> 和 a<b 以及 x < y") == "List&#60;T> 和 a&#60;b 以及 x &#60; y"
    assert normalize('<img src="https://example.com/a.png">') == (
        '&#60;img src="https://example.com/a.png">'
    )


def test_html_blocks_are_escaped_into_text() -> None:
    assert normalize("<div>\n<p>段落</p>\n</div>") == "&#60;div>\n&#60;p>段落&#60;/p>\n&#60;/div>"


@pytest.mark.parametrize(
    "tag",
    [
        "<br>",
        "<br/>",
        "<text_tag color='blue'>标签</text_tag>",
        "<a href='https://example.com'>链接</a>",
        "<at id=ou_1></at>",
        "<at email=demo@example.com></at>",
        "<person id='ou_1' show_name=true></person>",
        "<local_datetime millisecond='1700000000000' format_type='date'></local_datetime>",
        "<link icon='chat_outlined' url='https://example.com'>跳转</link>",
        "<font color='red'>红</font>",
        "<font color=green>绿</font>",
    ],
)
def test_whitelisted_tags_are_kept(tag: str) -> None:
    assert normalize(f"前 {tag} 后") == f"前 {tag} 后"


def test_hr_tag_is_kept_on_its_own_line() -> None:
    assert normalize("a\n\n<hr>\n\nb") == "a\n\n<hr>\n\nb"


@pytest.mark.parametrize(
    "at_all",
    ["<at id=all></at>", '<at id="all"></at>', "<at id='all'></at>", "<AT ID=ALL>所有人</AT>"],
)
def test_at_all_becomes_plain_text(at_all: str) -> None:
    assert normalize(f"请 {at_all} 查看") == "请 @所有人 查看"


def test_font_colors_outside_the_palette_are_unwrapped() -> None:
    assert normalize("<font color='#ff0000'>红</font> <font color=pink>粉</font>") == "红 粉"
    assert normalize("<font color='rgba(0,0,0,1)'>黑</font>") == "黑"
    for color in style.FONT_COLORS:
        assert normalize(f"<font color='{color}'>x</font>") == f"<font color='{color}'>x</font>"


def test_anchor_tags_without_web_href_are_unwrapped() -> None:
    assert normalize("<a href='mailto:a@example.com'>邮件</a>") == "邮件"
    assert normalize("<a href='javascript:alert(1)'>点</a>") == "点"


# ---------------------------------------------------------------- normalize：分割线与标题


@pytest.mark.parametrize("rule", ["***", "___", "---", "- - -", "* * *", "_____"])
def test_thematic_breaks_become_hr_tags_with_blank_lines(rule: str) -> None:
    assert normalize(f"上\n\n{rule}\n\n下") == "上\n\n<hr>\n\n下"


def test_hr_right_after_text_gets_blank_lines() -> None:
    assert normalize("上\n***\n下") == "上\n\n<hr>\n\n下"


def test_setext_headings_become_atx() -> None:
    assert normalize("大标题\n===\n正文") == "# 大标题\n\n正文"
    assert normalize("小标题\n---\n正文") == "## 小标题\n\n正文"


def test_atx_heading_levels_are_kept() -> None:
    assert normalize("# 一\n## 二\n###### 六 ##") == "# 一\n\n## 二\n\n###### 六"


# ---------------------------------------------------------------- normalize：链接


def test_http_links_are_kept_and_others_become_text() -> None:
    assert normalize("[官网](https://example.com/a?b=1)") == "[官网](https://example.com/a?b=1)"
    assert normalize("[旧站](http://example.com)") == "[旧站](http://example.com)"
    assert normalize("[文档](./docs/a.md) [脚本](javascript:alert(1))") == "文档 脚本"
    assert normalize("[联系](mailto:demo@example.com)") == "联系（demo@example.com）"
    assert normalize("[demo@example.com](mailto:demo@example.com)") == "demo@example.com"


def test_link_titles_are_dropped() -> None:
    assert normalize('[t](https://example.com "标题")') == "[t](https://example.com)"


def test_tel_links_are_kept() -> None:
    assert normalize("[拨打](tel://10086)") == "[拨打](tel://10086)"
    assert normalize("[拨打](tel:10086)") == "[拨打](tel://10086)"


def test_bare_urls_are_wrapped_but_not_inside_links_or_code() -> None:
    assert normalize("见 https://example.com/a，谢谢") == (
        "见 [https://example.com/a](https://example.com/a)，谢谢"
    )
    assert normalize("(https://example.com/b) 和 https://example.com/c.") == (
        "([https://example.com/b](https://example.com/b)) 和 "
        "[https://example.com/c](https://example.com/c)."
    )
    assert normalize("**https://example.com/d**") == (
        "**[https://example.com/d](https://example.com/d)**"
    )
    assert normalize("[https://example.com](https://example.com)") == (
        "[https://example.com](https://example.com)"
    )
    assert normalize("`https://example.com`") == "`https://example.com`"


def test_bare_url_keeps_balanced_parentheses_and_underscores() -> None:
    url = "https://example.com/wiki/A_(b)_c"
    assert normalize(url) == f"[{url}]({url})"


def test_angle_autolinks_become_links_or_text() -> None:
    assert normalize("<https://example.com>") == "[https://example.com](https://example.com)"
    assert normalize("<mailto:demo@example.com> <demo@example.com>") == (
        "demo@example.com demo@example.com"
    )


def test_reference_links_are_expanded_and_definitions_removed() -> None:
    src = "看 [文档][doc]、[首页][] 和 [首页]。\n\n[doc]: https://example.com/doc\n[首页]: https://example.com"
    assert normalize(src) == (
        "看 [文档](https://example.com/doc)、[首页](https://example.com) 和 "
        "[首页](https://example.com)。"
    )


def test_unknown_reference_stays_literal() -> None:
    assert normalize("数组 a[0] 和 [未定义][x]") == "数组 a[0] 和 [未定义][x]"


# ---------------------------------------------------------------- normalize：行内细节


def test_hard_breaks_become_br() -> None:
    assert normalize("第一行  \n第二行\\\n第三行\n第四行") == "第一行<br>第二行<br>第三行\n第四行"


def test_backslash_escapes_become_entities() -> None:
    assert normalize(r"\*不是斜体\* 和 \_x\_ 以及 \# \[x\]") == (
        "&#42;不是斜体&#42; 和 &#95;x&#95; 以及 &#35; &#91;x&#93;"
    )


def test_underscore_emphasis_becomes_star_but_snake_case_is_kept() -> None:
    assert normalize("_斜体_ 和 snake_case_name 与 __粗体__") == (
        "*斜体* 和 snake_case_name 与 __粗体__"
    )


def test_four_stars_are_merged() -> None:
    assert normalize("**a****b**") == "**ab**"


def test_emoji_shortcodes_math_and_footnotes_are_kept() -> None:
    src = "完成 :smile: :white_check_mark: 公式 $x^2 + y_1$ 见[^1]\n\n[^1]: 脚注说明"
    out = normalize(src)
    assert out.startswith("完成 :smile: :white_check_mark: 公式 $x^2 + y_1$ 见[^1]")
    assert "脚注说明" in out


def test_chinese_and_emoji_survive() -> None:
    src = "# 周报 📊\n\n- **完成** 接口联调 ✅\n- 进行中：压测 🚧"
    assert normalize(src) == "# 周报 📊\n\n- **完成** 接口联调 ✅\n- 进行中：压测 🚧"


def test_blockquotes_are_kept_with_normalized_content() -> None:
    src = "> * a\n>   * b\n>\n> ```js\n> x\n> ```"
    assert normalize(src) == "> - a\n>     - b\n>\n> ```javascript\n> x\n> ```"


def test_markdown_tables_are_kept_with_normalized_cells() -> None:
    src = "| a | b |\n|:-|--:|\n| x \\| y | `p\\|q` <b> |"
    assert normalize(src) == "| a | b |\n| :--- | ---: |\n| x \\| y | `p\\|q` &#60;b> |"


def test_images_in_normalize_use_keys_or_become_links() -> None:
    src = "![图一](https://example.com/1.png) 和 ![](https://example.com/2.png) ![本地](a.png)"
    assert normalize(src, {"https://example.com/1.png": "img_v3_1"}) == (
        "![图一](img_v3_1) 和 [🖼️ 图片](https://example.com/2.png) 🖼️ 本地"
    )


def test_empty_and_blank_input() -> None:
    assert normalize("") == ""
    assert normalize("   \n\n\t\n") == ""


def test_crlf_input() -> None:
    assert normalize("* a\r\n* b\r\n") == "- a\n- b"


# ---------------------------------------------------------------- render_markdown：切块


def test_empty_input_renders_nothing() -> None:
    assert render("") == []
    assert render(" \n\n  \n") == []
    assert render("[x]: https://example.com") == []


def test_adjacent_text_blocks_merge_into_one_markdown_component() -> None:
    src = "# 标题\n\n段落 **粗体**\n\n* 列表\n\n> 引用\n\n```py\nx = 1\n```"
    out = render(src)
    assert out == [
        {
            "tag": "markdown",
            "element_id": "md_1",
            "content": "# 标题\n\n段落 **粗体**\n\n- 列表\n\n> 引用\n\n```python\nx = 1\n```",
        }
    ]


def test_thematic_break_becomes_hr_component() -> None:
    out = render("上\n\n***\n\n下\n\n<hr>\n\n尾")
    assert tags(out) == ["markdown", "hr", "markdown", "hr", "markdown"]
    assert out[1] == {"tag": "hr", "element_id": "hr_1"}
    assert [c.get("content") for c in out if c["tag"] == "markdown"] == ["上", "下", "尾"]


def test_text_is_not_lost_across_slices() -> None:
    words = [f"词{i}" for i in range(12)]
    src = (
        f"{words[0]}\n{words[1]}\n\n- {words[2]}\n  - {words[3]}\n\n---\n\n"
        f"{table(6)}\n{words[4]} [链接][r]\n\n![图](https://example.com/x.png)\n\n"
        f"> {words[5]}\n\n    {words[6]}\n\n[r]: https://example.com/r\n\n{words[7]}"
    )
    out = render(src)
    text = "\n".join(str(c) for c in out)
    for word in words[:8]:
        assert word in text
    assert "[链接](https://example.com/r)" in text


# ---------------------------------------------------------------- render_markdown：表格


def test_small_plain_table_stays_in_markdown() -> None:
    out = render(f"前言\n\n{table(3)}\n结尾")
    assert tags(out) == ["markdown"]
    assert "| 名称 | 说明 |\n| --- | --- |\n| 项0 | 文字0 |" in out[0]["content"]


def test_table_with_more_than_five_rows_becomes_table_component() -> None:
    ctx = RenderContext()
    out = render_markdown(f"前言\n\n{table(6)}\n结尾", ctx)
    assert tags(out) == ["markdown", "table", "markdown"]
    tbl = out[1]
    assert ctx.tables == 1
    assert tbl["element_id"] == "tbl_1"
    assert tbl["page_size"] == 6
    assert tbl["row_height"] == "auto"
    assert tbl["header_style"] == {
        "background_style": "grey",
        "text_color": "grey",
        "bold": True,
        "text_size": "normal",
    }
    assert tbl["columns"] == [
        {"name": "c0", "display_name": "名称", "data_type": "lark_md"},
        {"name": "c1", "display_name": "说明", "data_type": "lark_md"},
    ]
    assert tbl["rows"][0] == {"c0": "项0", "c1": "文字0"}
    assert len(tbl["rows"]) == 6


def test_table_page_size_is_capped_at_ten() -> None:
    (tbl,) = render(table(23))
    assert tbl["page_size"] == 10
    assert len(tbl["rows"]) == 23


def test_five_rows_is_not_enough() -> None:
    assert tags(render(table(5))) == ["markdown"]


@pytest.mark.parametrize(
    ("delimiter", "aligns"),
    [
        ("|:--|---|", ["left", None]),
        ("|---|--:|", [None, "right"]),
        ("|:-:|:-:|", ["center", "center"]),
    ],
)
def test_column_alignment_maps_to_horizontal_align(
    delimiter: str, aligns: list[str | None]
) -> None:
    (tbl,) = render(table(1, delimiter=delimiter))
    assert tbl["tag"] == "table"
    assert [c.get("horizontal_align") for c in tbl["columns"]] == aligns


def test_numeric_column_turns_table_into_component_and_aligns_right() -> None:
    src = "| 名称 | 金额 | 占比 | 备注 |\n|---|---|---|---|\n"
    src += "| A | ¥1,234.50 | 12% | 一 |\n| B | -3 | **0.5%** | - |\n| C | - | 7 | 三 |\n"
    (tbl,) = render(src)
    assert tbl["tag"] == "table"
    assert [c.get("horizontal_align") for c in tbl["columns"]] == [None, "right", "right", None]
    assert tbl["rows"][0] == {"c0": "A", "c1": "¥1,234.50", "c2": "12%", "c3": "一"}


def test_explicit_alignment_beats_numeric_default() -> None:
    (tbl,) = render("| k | v |\n|---|:-:|\n| a | 1 |")
    assert tbl["columns"][1]["horizontal_align"] == "center"


def test_mixed_column_is_not_numeric() -> None:
    assert tags(render("| k | v |\n|---|---|\n| a | 1 |\n| b | 两个 |")) == ["markdown"]


def test_table_cells_keep_inline_markdown_as_lark_md() -> None:
    src = table(6) + (
        "| **粗** [链](https://example.com) | <font color='red'>红</font> a<b <br> 换行 |\n"
        "| ![图](https://example.com/p.png) | [相对](./a.md) `code` |\n"
    )
    (tbl,) = render(src, images={"https://example.com/p.png": "img_v3_p"})
    assert tbl["rows"][6] == {
        "c0": "**粗** [链](https://example.com)",
        "c1": "<font color='red'>红</font> a&#60;b\n换行",
    }
    # lark_md 不支持图片：即使有 key 也退成链接。
    assert tbl["rows"][7] == {"c0": "[🖼️ 图](https://example.com/p.png)", "c1": "相对 `code`"}


def test_header_display_names_are_plain_text() -> None:
    (tbl,) = render(table(6, header="| **名称** | [说明](https://example.com) |"))
    assert [c["display_name"] for c in tbl["columns"]] == ["名称", "说明"]


def test_fifth_table_in_one_markdown_component_becomes_table_component() -> None:
    src = "\n".join(table(2) for _ in range(6))
    out = render(src)
    assert tags(out) == ["markdown", "table", "markdown"]
    assert out[0]["content"].count("| 名称 | 说明 |") == 4
    assert out[2]["content"].count("| 名称 | 说明 |") == 1


def test_table_budget_exhausted_keeps_tables_in_markdown_split_by_four() -> None:
    ctx = RenderContext(tables=5)
    out = render_markdown("\n".join(table(8) for _ in range(6)), ctx)
    assert tags(out) == ["markdown", "markdown"]
    assert out[0]["content"].count("| 名称 | 说明 |") == 4
    assert out[1]["content"].count("| 名称 | 说明 |") == 2
    assert ctx.tables == 5


def test_table_budget_is_shared_through_context() -> None:
    ctx = RenderContext()
    out = render_markdown("\n".join(table(8) for _ in range(7)), ctx)
    assert tags(out).count("table") == 5
    assert ctx.tables == 5


def test_table_wider_than_fifty_columns_stays_in_markdown() -> None:
    header = "|" + "|".join(f"h{i}" for i in range(51)) + "|"
    delimiter = "|" + "|".join("---" for _ in range(51)) + "|"
    rows = "".join("|" + "|".join("x" for _ in range(51)) + "|\n" for _ in range(8))
    ctx = RenderContext()
    out = render_markdown(f"{header}\n{delimiter}\n{rows}", ctx)
    assert tags(out) == ["markdown"]
    assert ctx.tables == 0


# ---------------------------------------------------------------- render_markdown：图片


def images_md(count: int) -> str:
    return " ".join(f"![图{i}](https://example.com/{i}.png)" for i in range(count))


KEYS = {f"https://example.com/{i}.png": f"img_v3_{i}" for i in range(20)}


def test_standalone_image_with_key_becomes_img() -> None:
    out = render("说明\n\n![趋势图](https://example.com/0.png)\n\n结尾", images=KEYS)
    assert tags(out) == ["markdown", "img", "markdown"]
    assert out[1] == {
        "tag": "img",
        "element_id": "img_1",
        "img_key": "img_v3_0",
        "alt": {"tag": "plain_text", "content": "趋势图"},
        "scale_type": "fit_horizontal",
        "corner_radius": style.RADIUS,
        "preview": True,
    }


def test_image_without_alt_still_has_alt_object() -> None:
    (img,) = render("![](https://example.com/0.png)", images=KEYS)
    assert img["alt"] == {"tag": "plain_text", "content": ""}


@pytest.mark.parametrize(
    ("count", "mode"),
    [(2, "double"), (3, "triple"), (4, "bisect"), (6, "bisect"), (7, "trisect"), (9, "trisect")],
)
def test_several_keyed_images_become_img_combination(count: int, mode: str) -> None:
    (combo,) = render(images_md(count), images=KEYS)
    assert combo["tag"] == "img_combination"
    assert combo["combination_mode"] == mode
    assert combo["corner_radius"] == style.RADIUS
    assert combo["img_list"] == [{"img_key": f"img_v3_{i}"} for i in range(count)]


def test_more_than_nine_images_are_split() -> None:
    out = render(images_md(11), images=KEYS)
    assert [(c["tag"], c.get("combination_mode")) for c in out] == [
        ("img_combination", "trisect"),
        ("img_combination", "double"),
    ]
    out = render(images_md(10), images=KEYS)
    assert tags(out) == ["img_combination", "img"]


def test_images_on_separate_lines_of_one_paragraph_combine() -> None:
    src = "![a](https://example.com/0.png)\n![b](https://example.com/1.png)"
    (combo,) = render(src, images=KEYS)
    assert combo["combination_mode"] == "double"


def test_images_without_key_become_links() -> None:
    out = render("![截图](https://example.com/0.png)")
    assert out == [
        {
            "tag": "markdown",
            "element_id": "md_1",
            "content": "[🖼️ 截图](https://example.com/0.png)",
        }
    ]


def test_mixed_keyed_and_unkeyed_images_keep_order() -> None:
    src = "![a](https://example.com/0.png) ![b](https://example.com/x.png) ![c](https://example.com/1.png)"
    out = render(src, images=KEYS)
    assert tags(out) == ["img", "markdown", "img"]
    assert out[1]["content"] == "[🖼️ b](https://example.com/x.png)"
    assert [out[0]["img_key"], out[2]["img_key"]] == ["img_v3_0", "img_v3_1"]


def test_unkeyed_standalone_image_joins_neighbouring_text() -> None:
    out = render("前文\n\n![图](https://example.com/x.png)\n\n后文")
    assert out == [
        {
            "tag": "markdown",
            "element_id": "md_1",
            "content": "前文\n\n[🖼️ 图](https://example.com/x.png)\n\n后文",
        }
    ]


def test_inline_images_stay_in_markdown() -> None:
    src = "看图 ![a](https://example.com/0.png) 和 ![b](https://example.com/x.png) 对比"
    (md,) = render(src, images=KEYS)
    assert md["tag"] == "markdown"
    assert md["content"] == "看图 ![a](img_v3_0) 和 [🖼️ b](https://example.com/x.png) 对比"


def test_linked_image_is_not_standalone() -> None:
    (md,) = render("[![徽章](https://example.com/0.png)](https://example.com)", images=KEYS)
    assert md == {"tag": "markdown", "element_id": "md_1", "content": "[徽章](https://example.com)"}


# ---------------------------------------------------------------- render_markdown：element_id


def test_element_ids_are_unique_and_valid() -> None:
    ctx = RenderContext(images=KEYS)
    src = "\n\n".join(
        [
            "# 标题",
            images_md(1),
            "---",
            table(7),
            "段落",
            images_md(3),
            "***",
            table(1, delimiter="|:-|-:|"),
            "尾声 🎉",
        ]
    )
    out = render_markdown(src, ctx)
    ids = [c["element_id"] for c in out]
    assert len(ids) == len(set(ids))
    assert all(ELEMENT_ID.match(i) for i in ids)
    assert tags(out) == [
        "markdown",
        "img",
        "hr",
        "table",
        "markdown",
        "img_combination",
        "hr",
        "table",
        "markdown",
    ]
    # 继续用同一个 ctx 渲染，id 也不会撞。
    more = render_markdown(src, ctx)
    assert not {c["element_id"] for c in more} & set(ids)


# ---------------------------------------------------------------- image_urls


def test_image_urls_are_deduplicated_in_order() -> None:
    src = (
        "![a](https://example.com/b.png) ![b](https://example.com/a.png)\n\n"
        "![again](https://example.com/b.png) ![ref][pic] ![local](./c.png)"
        " ![data](data:image/png;base64,xx)\n\n"
        "| k | v |\n|---|---|\n| ![t](https://example.com/t.png) | x |\n\n"
        "`![code](https://example.com/code.png)`\n\n"
        "```\n![fence](https://example.com/fence.png)\n```\n\n"
        "[![badge](https://example.com/badge.png)](https://example.com)\n\n"
        "[pic]: https://example.com/ref.png"
    )
    assert image_urls(src) == [
        "https://example.com/b.png",
        "https://example.com/a.png",
        "https://example.com/ref.png",
        "https://example.com/t.png",
    ]


def test_image_urls_of_empty_input() -> None:
    assert image_urls("") == []
    assert image_urls("没有图片 https://example.com/x.png") == []


def test_image_urls_match_render_lookups() -> None:
    src = "![a](https://example.com/中文.png)\n\n![b][r]\n\n[r]: <https://example.com/a b.png>"
    urls = image_urls(src)
    assert len(urls) == 2
    out = render(src, images={u: f"img_v3_{i}" for i, u in enumerate(urls)})
    assert tags(out) == ["img", "img"]


# ---------------------------------------------------------------- 性质：任意输入都不崩


FRAGMENTS = [
    "# 标题",
    "段落 **粗** _斜_ `code` [链](https://example.com)",
    "- a\n  - b\n    1) c",
    "> 引用\n> - 列表",
    "```py\nx = '<b>'\n```",
    "~~~\n未闭合",
    "| a | b |\n|--:|---|\n| 1 | 2 |",
    "| a |\n|---|\n" + "| x |\n" * 7,
    "![图](https://example.com/0.png)",
    "![图](https://example.com/0.png) ![图](https://example.com/1.png)",
    "***",
    "<hr>",
    "<at id=all></at> <font color=red>红",
    "文字\n---",
    "    缩进",
    "[r]: https://example.com",
    "[^1]: 脚注",
    "<div>\n块",
    "中文 🎉 https://example.com/x_(y)",
    "\\",
    "`",
    "[",
    "![",
    "<",
]


@settings(max_examples=150, deadline=None)
@given(
    parts=st.lists(
        st.one_of(st.sampled_from(FRAGMENTS), st.text(max_size=40)), min_size=0, max_size=8
    ),
    sep=st.sampled_from(["\n", "\n\n", " "]),
)
def test_any_input_renders_valid_components(parts: list[str], sep: str) -> None:
    src = sep.join(parts)
    ctx = RenderContext(images=KEYS)
    out = render_markdown(src, ctx)
    ids = [c["element_id"] for c in out]
    assert len(ids) == len(set(ids))
    assert all(ELEMENT_ID.match(i) for i in ids)
    assert set(tags(out)) <= {"markdown", "table", "img", "img_combination", "hr"}
    assert ctx.tables <= 5
    for c in out:
        if c["tag"] == "markdown":
            assert c["content"].strip()
            if "`" not in c["content"]:  # 代码里的内容原样保留
                assert "<at id=all" not in c["content"].lower()
    normalize(src)
    image_urls(src)
