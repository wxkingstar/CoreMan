"""知识库路径的规范化：同一页的各种写法归到同一个键，不像路径的一律拒绝。"""

from __future__ import annotations

import pytest

from coreman.core.assistants import notes


@pytest.mark.parametrize(
    "written",
    [
        "People/Boss.md",
        "/People/Boss.md",
        "People/Boss.md/",
        "//People///Boss.md//",
        " People / Boss.md ",
        "People\\Boss.md",
        "Ｐeople/Boss.md",
    ],
)
def test_spellings_of_one_page_share_a_display_form(written: str) -> None:
    assert notes.normalize(written) == ("People/Boss.md", "people/boss.md")


def test_case_only_differs_in_the_display_form() -> None:
    assert notes.normalize("PEOPLE/BOSS.MD") == ("PEOPLE/BOSS.MD", "people/boss.md")
    assert notes.normalize("项目/Q4 报价.md") == ("项目/Q4 报价.md", "项目/q4 报价.md")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "///",
        "a/../b",
        "./a",
        "a/./b",
        "a\nb",
        "a/\x00b",
        "a​b",
        "x" * (notes.MAX_PATH_CHARS + 1),
        "/".join(["a"] * (notes.MAX_PATH_SEGMENTS + 1)),
    ],
)
def test_things_that_are_not_paths_are_refused(bad: str) -> None:
    with pytest.raises(notes.NotesError) as caught:
        notes.normalize(bad)
    assert caught.value.code == "invalid_path"
    assert caught.value.payload() == {"error": "invalid_path"}


def test_longest_allowed_path_passes() -> None:
    deep = "/".join(["a"] * notes.MAX_PATH_SEGMENTS)
    assert notes.normalize(deep) == (deep, deep)
