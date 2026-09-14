import base64
import io
import zipfile

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from coreman.core.wecom.media import (
    MAX_BYTES,
    Media,
    MediaError,
    MediaFetcher,
    data_uri,
    decrypt_media,
    derive_key,
    guess_filename,
    mime_for,
    sniff_image_mime,
)
from tests.fakes.fake_media import FakeMedia, encrypt_media

KEY32 = "0123456789abcdef0123456789abcdef"
KEY64 = "ab" * 32
KEY43 = base64.b64encode(b"\x07" * 32).decode().rstrip("=")


def test_derive_key_by_shape() -> None:
    assert derive_key(KEY32) == KEY32.encode()
    assert derive_key(KEY64) == bytes.fromhex(KEY64)
    assert derive_key(KEY43) == b"\x07" * 32
    # 43 位那一档看的是 base64 字母表："g" 本身合法（"g" * 43 解出来正好 32 字节），
    # 所以坏例子得挑一个不在字母表里的字符。
    for bad in ("", "short", "zz" * 32, "g" * 42 + "!"):
        with pytest.raises(MediaError) as ei:
            derive_key(bad)
        assert ei.value.reason == "invalid_key"


@settings(max_examples=60, deadline=None)
@given(st.binary(min_size=0, max_size=200), st.sampled_from([KEY32, KEY64, KEY43]))
def test_decrypt_roundtrip(plain: bytes, key: str) -> None:
    assert decrypt_media(encrypt_media(plain, key), key) == plain


def test_decrypt_rejects_bad_padding_and_length() -> None:
    with pytest.raises(MediaError) as ei:
        decrypt_media(b"\x00" * 15, KEY32)
    assert ei.value.reason == "decrypt_failed"
    # 合法密文改坏最后一块 → 填充值越界。
    blob = bytearray(encrypt_media(b"hello", KEY32))
    blob[-1] ^= 0xFF
    with pytest.raises(MediaError):
        decrypt_media(bytes(blob), KEY32)


def test_sniff_image_mime() -> None:
    assert sniff_image_mime(b"\x89PNG\r\n\x1a\n...") == "image/png"
    assert sniff_image_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert sniff_image_mime(b"GIF89a") == "image/gif"
    assert sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image_mime(b"whatever") == "image/jpeg"


def _zip_with(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, "x")
    return buf.getvalue()


def test_guess_filename_three_tiers() -> None:
    assert guess_filename(given="报表.xlsx", content_disposition=None, data=b"") == "报表.xlsx"
    cd = "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.pdf"
    assert guess_filename(given=None, content_disposition=cd, data=b"") == "报告.pdf"
    cd2 = 'inline; filename="a b.txt"'
    assert guess_filename(given=None, content_disposition=cd2, data=b"") == "a b.txt"
    assert guess_filename(given=None, content_disposition=None, data=b"%PDF-1.4") == "file.pdf"
    xls = b"\xd0\xcf\x11\xe0\xa1\xb1"
    assert guess_filename(given=None, content_disposition=None, data=xls) == "file.xls"
    zips = {
        "xl/workbook.xml": "file.xlsx",
        "word/document.xml": "file.docx",
        "ppt/slides/s1.xml": "file.pptx",
        "other.txt": "file.zip",
    }
    for entry, expected in zips.items():
        got = guess_filename(given=None, content_disposition=None, data=_zip_with(entry))
        assert got == expected
    png = b"\x89PNG\r\n\x1a\n"
    assert guess_filename(given=None, content_disposition=None, data=png) == "file.png"
    assert guess_filename(given=None, content_disposition=None, data=b"\xff\xd8\xff") == "file.jpg"
    text = "纯文本".encode()
    assert guess_filename(given=None, content_disposition=None, data=text) == "file.txt"
    assert (
        guess_filename(given=None, content_disposition=None, data=b"\x00\xff\xfe\x01") == "file.bin"
    )


def _corrupt_zip(entry: str = "xl/workbook.xml") -> bytes:
    """PK 魔数齐全、中央目录被改坏的字节：CPython 3.12 会抛 NotImplementedError。

    这类异常既不是 BadZipFile 也不是 OSError，曾经会直接穿过 guess_filename / fetch_file，
    把 MediaError 的封闭 reason 集合撕开一个口子。
    """
    raw = bytearray(_zip_with(entry))
    j = raw.rfind(b"PK\x01\x02")  # 中央目录头
    assert j > 0, "没找到中央目录，样本构造失效"
    raw[j + 6] = 0xFF  # extract_version 远超 MAX_EXTRACT_VERSION
    return bytes(raw)


def test_guess_filename_survives_corrupt_zip() -> None:
    garbage = b"PK\x03\x04" + bytes(range(60))  # 连本地头都读不出来
    assert guess_filename(given=None, content_disposition=None, data=garbage) == "file.zip"
    broken = _corrupt_zip()
    assert broken.startswith(b"PK\x03\x04")
    assert guess_filename(given=None, content_disposition=None, data=broken) == "file.zip"
    # 前两级回退仍然优先，坏字节不影响取名来源。
    assert guess_filename(given="x.xlsx", content_disposition=None, data=broken) == "x.xlsx"
    cd = 'attachment; filename="y.xlsx"'
    assert guess_filename(given=None, content_disposition=cd, data=broken) == "y.xlsx"


async def test_fetch_file_with_corrupt_zip_is_not_an_error() -> None:
    fake = FakeMedia()
    broken = _corrupt_zip("word/document.xml")
    url = fake.add("/broken", broken, aeskey=KEY64)
    fetcher = MediaFetcher(fake.client())
    media = await fetcher.fetch_file({"url": url, "aeskey": KEY64}, filename=None)
    assert isinstance(media, Media)
    assert media.data == broken
    assert media.filename == "file.zip"
    assert media.mime == "application/zip"
    await fetcher.aclose()


def test_mime_for_and_data_uri() -> None:
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert mime_for("a.xlsx") == xlsx
    assert mime_for("a.unknownext") == "application/octet-stream"
    assert data_uri(Media(b"ab", "image/png", None)) == "data:image/png;base64,YWI="


async def test_fetch_image_and_file_success() -> None:
    fake = FakeMedia()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    url = fake.add("/img1", png, aeskey=KEY64)
    fetcher = MediaFetcher(fake.client())
    media = await fetcher.fetch_image({"url": url, "aeskey": KEY64})
    assert media.data == png and media.mime == "image/png" and media.filename is None
    furl = fake.add(
        "/f1", b"%PDF-1.7 ...", aeskey=KEY32, content_disposition='attachment; filename="q.pdf"'
    )
    f = await fetcher.fetch_file({"url": furl, "aeskey": KEY32}, filename=None)
    assert f.filename == "q.pdf" and f.mime == "application/pdf" and f.data.startswith(b"%PDF")
    g = await fetcher.fetch_file({"url": furl, "aeskey": KEY32}, filename="给定.pdf")
    assert g.filename == "给定.pdf"
    assert fake.hits == ["/img1", "/f1", "/f1"]
    await fetcher.aclose()


async def test_fetch_errors_are_typed() -> None:
    fake = FakeMedia()
    fetcher = MediaFetcher(fake.client(), max_bytes=64)
    with pytest.raises(MediaError) as ei:
        await fetcher.fetch_image({"url": "", "aeskey": KEY64})
    assert ei.value.reason == "invalid_key" or ei.value.reason == "download_failed"
    url404 = fake.add("/gone", b"x", aeskey=KEY64, status=404)
    with pytest.raises(MediaError) as ei:
        await fetcher.fetch_image({"url": url404, "aeskey": KEY64})
    assert ei.value.reason == "download_failed"
    big = fake.add("/big", b"z" * 200, aeskey=KEY64)
    with pytest.raises(MediaError) as ei:
        await fetcher.fetch_file({"url": big, "aeskey": KEY64}, filename="b.bin")
    assert ei.value.reason == "too_large"
    # Content-Length 先声明超限：一个字节都不下载。
    declared = fake.add("/declared", b"z" * 10, aeskey=KEY64, content_length=MAX_BYTES + 1)
    fetcher2 = MediaFetcher(fake.client())
    with pytest.raises(MediaError) as ei:
        await fetcher2.fetch_file({"url": declared, "aeskey": KEY64}, filename=None)
    assert ei.value.reason == "too_large"
    slow = fake.add("/slow", b"x" * 16, aeskey=KEY64, delay=0.3)
    fetcher3 = MediaFetcher(fake.client(), image_timeout=0.05)
    with pytest.raises(MediaError) as ei:
        await fetcher3.fetch_image({"url": slow, "aeskey": KEY64})
    assert ei.value.reason == "timeout"
    wrong = fake.add("/wrongkey", b"x" * 16, aeskey=KEY64)
    with pytest.raises(MediaError) as ei:
        await fetcher2.fetch_image({"url": wrong, "aeskey": KEY32})
    assert ei.value.reason == "decrypt_failed"
    assert isinstance(fetcher2, MediaFetcher) and isinstance(httpx.AsyncClient, type)
