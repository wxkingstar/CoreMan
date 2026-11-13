"""导入 / 导出压缩包的格式与安全检查：每一条限制各有一例；导出的文件读回来与原样一致。"""

from __future__ import annotations

import io
import json
import stat
import zipfile
from datetime import date

import pytest

from coreman.core.assistants import archive
from coreman.core.assistants.archive import ArchiveError, FollowupItem, PageFile
from tests.fakes.archives import encrypted, pack, special


def code_of(data: bytes) -> str:
    with pytest.raises(ArchiveError) as refused:
        archive.parse(data)
    return refused.value.code


def test_markdown_files_become_pages_and_the_header_is_optional() -> None:
    parsed = archive.parse(
        pack(
            {
                "人/李四.md": (
                    "---\ntitle: 李四\nsummary: 采购负责人\npinned: true\ntags: [a, b]\n---\n"
                    "正文第一行\n"
                ),
                "规则/口吻.md": "没有头的一页\n",
                "日志/": "",
                "分隔线.md": "---\n只是一条分隔线\n---\n后面还有\n",
                "带签名.md": "﻿---\ntitle: 2026\n---\n正文",
                "空的头.md": "---\n---\n正文",
            }
        )
    )
    assert parsed.followups == []
    assert parsed.pages == [
        # 其余键（tags）忽略；收尾的 --- 之后全是正文。
        PageFile("人/李四", "李四", "采购负责人", True, "正文第一行\n"),
        PageFile("规则/口吻", None, None, None, "没有头的一页\n"),
        # 两条 --- 之间不是映射：整篇都是正文。
        PageFile("分隔线", None, None, None, "---\n只是一条分隔线\n---\n后面还有\n"),
        # 开头的 BOM 去掉；数字写的标题按字符串用。
        PageFile("带签名", "2026", None, None, "正文"),
        PageFile("空的头", None, None, None, "正文"),
    ]


@pytest.mark.parametrize(
    "header",
    [
        "title: [1, 2]",
        "pinned: yes please",
        "title: true",
        "summary: {a: b}",
        'title: "没收尾',
        # 十六进制写的超大整数：YAML 读得出来，转成字符串时超过位数上限。
        pytest.param("title: 0x" + "f" * 4000, id="huge-title"),
        pytest.param("summary: 0x" + "f" * 4000, id="huge-summary"),
        pytest.param("pinned: 0x" + "f" * 4000, id="huge-pinned"),
        "pinned: 2026-11-14",
        "summary: [[[[1]]]]",
        "title: &a [*a]",
    ],
)
def test_a_bad_header_marks_only_that_file(header: str) -> None:
    parsed = archive.parse(pack({"坏.md": f"---\n{header}\n---\n正文", "好.md": "正文"}))
    assert parsed.pages[0] == PageFile("坏", None, None, None, "", "invalid_front_matter")
    assert parsed.pages[1] == PageFile("好", None, None, None, "正文")


def test_odd_scalars_in_the_header_are_used_as_text() -> None:
    header = "title: 2026-11-14\nsummary: 2026-11-14 10:00:00\npinned: false"
    parsed = archive.parse(pack({"日.md": f"---\n{header}\n---\n正文"}))
    assert parsed.pages == [PageFile("日", "2026-11-14", "2026-11-14 10:00:00", False, "正文")]
    for value in (".inf", ".nan", "1e400", "0x" + "f" * 100):
        (page,) = archive.parse(pack({"数.md": f"---\ntitle: {value}\n---\n正文"})).pages
        assert page.problem is None and page.title


@pytest.mark.parametrize(
    "name",
    ["../外面.md", "a/../../b.md", "/etc/x.md", "a\\b.md", "a/./b.md", "a//b.md", "响\x07铃.md"],
)
def test_names_that_could_leave_the_archive_root_are_refused(name: str) -> None:
    assert code_of(pack({"好.md": "正文", name: "正文"})) == "unsafe_path"


def test_symlinks_and_other_special_files_are_refused() -> None:
    assert code_of(special("链接.md", stat.S_IFLNK)) == "symlink_not_allowed"
    assert code_of(special("目录链接/", stat.S_IFLNK)) == "symlink_not_allowed"
    assert code_of(special("管道.md", stat.S_IFIFO)) == "unsupported_file"


def test_encrypted_entries_are_refused() -> None:
    assert code_of(encrypted("密.md")) == "encrypted_entry"


@pytest.mark.parametrize("name", ["里面.zip", "备份.tar.gz", "a/b.7z", "X.ZIP"])
def test_nested_archives_are_refused_by_name(name: str) -> None:
    assert code_of(pack({name: b"whatever"})) == "nested_archive"


def test_a_renamed_archive_is_refused_by_content() -> None:
    assert code_of(pack({"伪装.md": pack({"x.md": "正文"})})) == "nested_archive"


def test_system_clutter_is_ignored_and_counted() -> None:
    # 系统自带的压缩功能会带上这些文件：跳过并计数，不因为它们拒绝整个包。
    parsed = archive.parse(
        pack(
            {
                "好.md": "正文",
                ".DS_Store": b"\x00\x01Bud1",
                "子目录/.DS_Store": b"\x00",
                "__MACOSX/": "",
                # 资源分支文件的名字以 .md 结尾、内容是二进制：不能当成一页去读。
                "__MACOSX/._好.md": b"\x00\x05\x16\x07",
                "__MACOSX/子目录/照片.png": b"\x89PNG",
                "Thumbs.db": b"\xd0\xcf\x11\xe0",
                "子目录/thumbs.db": b"\xd0\xcf",
                "子目录/._笔记.md": b"\x00\x05",
            }
        )
    )
    assert [page.path for page in parsed.pages] == ["好"]
    assert parsed.ignored == 7
    assert archive.parse(pack({"好.md": "正文"})).ignored == 0
    # 忽略的只是这几种已知的系统文件：别的非 Markdown 文件照样让整个包被拒绝。
    assert code_of(pack({".DS_Store": b"x", "照片.png": b"x"})) == "unsupported_file"
    assert code_of(pack({"子目录/.gitignore": "x"})) == "unsupported_file"
    # 名字不安全的、是符号链接的，即使叫这些名字也拒绝。
    assert code_of(pack({"__MACOSX/../x.md": "x"})) == "unsafe_path"
    assert code_of(special(".DS_Store", stat.S_IFLNK)) == "symlink_not_allowed"


def test_ignored_files_are_never_read_and_do_not_count_as_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive, "MAX_PAGE_FILES", 1)
    monkeypatch.setattr(archive, "MAX_UNPACKED_BYTES", 10)
    # 不占页面文件的名额，也不占解包的字节数（根本不读）。
    parsed = archive.parse(pack({"a.md": "x", "__MACOSX/._a.md": b"y" * 500, ".DS_Store": b"z"}))
    assert len(parsed.pages) == 1 and parsed.ignored == 2
    # 但仍然算进条目总数的硬上限。
    monkeypatch.setattr(archive, "MAX_RAW_ENTRIES", 2)
    assert code_of(pack({"a.md": "x", "._a.md": b"y", ".DS_Store": b"z"})) == "too_many_entries"


@pytest.mark.parametrize("name", ["照片.png", "笔记.txt", "子目录/followups.json", ".gitignore"])
def test_anything_but_markdown_and_the_root_followups_file_is_refused(name: str) -> None:
    assert code_of(pack({name: "x"})) == "unsupported_file"


@pytest.mark.parametrize("content", ["中文".encode("gbk"), b"\xff\xfe\x00\x00", b"a\x00b"])
def test_text_that_is_not_utf8_is_refused(content: bytes) -> None:
    assert code_of(pack({"页.md": content})) == "not_utf8"


def test_the_number_of_page_files_is_capped() -> None:
    most = {f"p/{n}.md": "x" for n in range(archive.MAX_PAGE_FILES)}
    # 目录条目与 followups.json 不占页面文件的名额。
    full = pack({"p/": "", **most, "followups.json": json.dumps({"version": 1, "items": []})})
    assert len(archive.parse(full).pages) == archive.MAX_PAGE_FILES
    assert code_of(pack({**most, "多一个.md": "x"})) == "too_many_entries"


def test_the_raw_entry_count_is_capped_before_anything_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive, "MAX_RAW_ENTRIES", 3)
    assert code_of(pack({"a/": "", "b/": "", "c/": "", "d/": ""})) == "too_many_entries"


def test_a_zip_bomb_is_stopped_by_bytes_actually_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 高压缩比的条目：包很小、解开很大。上限按解出来的字节数算，读到上限就停，不会整个解开。
    bomb = pack({"炸弹.md": "0" * 5_000_000})
    assert len(bomb) < 10_000
    monkeypatch.setattr(archive, "MAX_UNPACKED_BYTES", 100_000)
    assert code_of(bomb) == "unpacked_too_large"
    # 中央目录里把大小改小来骗过预检的：实际读的时候照样拦下（读不超过上限 + 1 字节）。
    forged = bytearray(pack({"炸弹.md": "0" * 5_000_000}))
    at = forged.rfind(b"PK\x01\x02")
    forged[at + 24 : at + 28] = (10).to_bytes(4, "little")
    assert code_of(bytes(forged)) in ("unpacked_too_large", "invalid_archive")


def test_the_unpacked_size_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive, "MAX_UNPACKED_BYTES", 1000)
    assert code_of(pack({"a.md": "字" * 200, "b.md": "字" * 200})) == "unpacked_too_large"
    assert len(archive.parse(pack({"a.md": "x" * 600, "b.md": "y" * 400})).pages) == 2
    # 中央目录里写的大小不可信：实际读的时候按读到的字节数再核一次。
    data = pack({"a.md": "x" * 600})
    with zipfile.ZipFile(io.BytesIO(data)) as zf, pytest.raises(ArchiveError) as refused:
        archive._read(zf, zf.infolist()[0], 599)
    assert refused.value.code == "unpacked_too_large"


def test_the_upload_itself_is_capped_and_must_be_a_zip(monkeypatch: pytest.MonkeyPatch) -> None:
    assert code_of(b"this is not a zip file") == "invalid_archive"
    assert code_of(pack({"a.md": "x"})[:40]) == "invalid_archive"
    monkeypatch.setattr(archive, "MAX_ARCHIVE_BYTES", 100)
    assert code_of(b"x" * 101) == "archive_too_large"


def test_followups_are_read_item_by_item() -> None:
    items = [
        {"category": "decide", "title": " 续约 ", "notes": "明年一月", "due_on": "2026-12-01"},
        {"category": "waiting", "title": "报价", "review_on": "2026-11-20", "status": "snoozed"},
        {"category": "remind", "title": "体检", "notes": None, "due_on": None, "extra": 1},
        {"category": "someday", "title": "x"},
        {"category": "decide", "title": "   "},
        {"category": "decide", "title": "字" * 201},
        {"category": "decide", "title": "x", "notes": "字" * 4001},
        {"category": "decide", "title": "x", "due_on": "12/01/2026"},
        {"category": "decide", "title": "x", "due_on": "2026-13-40"},
        {"category": "decide", "title": "x", "status": "done"},
        "不是对象",
    ]
    parsed = archive.parse(
        pack({"followups.json": json.dumps({"version": 1, "items": items}, ensure_ascii=False)})
    )
    assert parsed.pages == []
    assert parsed.followups[:3] == [
        FollowupItem(1, "decide", "续约", "明年一月", None, date(2026, 12, 1), False),
        FollowupItem(2, "waiting", "报价", None, date(2026, 11, 20), None, True),
        FollowupItem(3, "remind", "体检", None, None, None, False),
    ]
    assert [(item.index, item.problem) for item in parsed.followups[3:]] == [
        (4, "invalid_category"),
        (5, "invalid_title"),
        (6, "invalid_title"),
        (7, "invalid_notes"),
        (8, "invalid_date"),
        (9, "invalid_date"),
        (10, "invalid_status"),
        (11, "invalid_item"),
    ]


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"version": 2, "items": []}),
        json.dumps({"version": True, "items": []}),
        json.dumps({"version": 1, "items": {}}),
        json.dumps({"version": 1, "items": [{}] * 501}),
    ],
)
def test_a_malformed_followups_file_refuses_the_archive(content: str) -> None:
    assert code_of(pack({"followups.json": content})) == "invalid_followups"


@pytest.mark.parametrize(
    ("title", "summary", "pinned", "body"),
    [
        ("李四", "采购负责人", True, "正文\n第二行\n"),
        ("2026", "true", False, "---\n正文以分隔线开头\n---\n"),
        ("带: 冒号 # 井号", "引号 \" 与 ' 都有", False, "行尾是回车换行\r\n第二行\r\n"),
        ("- 像列表", "{像映射}", True, "\n\n前面有空行"),
        ("长" * 120, "长" * 300, False, "x"),
    ],
)
def test_an_exported_page_reads_back_unchanged(
    title: str, summary: str, pinned: bool, body: str
) -> None:
    text = archive.page_text(title=title, summary=summary, pinned=pinned, body=body)
    parsed = archive.parse(archive.build([("目录/页", text)], None))
    assert parsed.pages == [PageFile("目录/页", title, summary, pinned, body)]


def test_exported_followups_read_back_unchanged() -> None:
    items = [
        {
            "category": "waiting",
            "title": "报价",
            "notes": "等李四",
            "review_on": "2026-11-20",
            "due_on": None,
            "status": "snoozed",
        }
    ]
    data = archive.build([], archive.followups_text(items))
    assert archive.parse(data).followups == [
        FollowupItem(1, "waiting", "报价", "等李四", date(2026, 11, 20), None, True)
    ]


def test_nothing_is_ever_extracted_to_disk() -> None:
    # 包只在内存里读：模块里不该出现把条目解到磁盘上的调用。
    source = archive.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert ".extract(" not in text and ".extractall(" not in text
