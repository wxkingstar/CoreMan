import base64
import contextlib
import json

import pytest

from runtime_daemon import browser_monitor as browser


@pytest.fixture(autouse=True)
def proc_supported(monkeypatch):
    monkeypatch.setattr(browser, "supports_proc", lambda: True)


def root(port=None):
    return {
        "id": "101-77",
        "pid": 101,
        "start_ticks": 77,
        "kind": "chrome",
        "_port": port,
        "_detail": "debug_pipe_only",
    }


def test_disabled_never_scans_or_connects(monkeypatch):
    monkeypatch.setattr(browser, "browsers", lambda: pytest.fail("must not scan"))
    assert browser.BrowserMonitor().snapshot(True)["state"] == "disabled"


@pytest.mark.parametrize(
    "raw",
    [
        b"/usr/bin/chrome\0--remote-debugging-port=0\0--user-data-dir=/tmp/profile space\0",
        b'/usr/bin/chrome --remote-debugging-port=0 --user-data-dir="/tmp/profile space"\0',
    ],
)
def test_flattened_and_normal_browser_arguments_keep_debug_flags(raw):
    args = browser.command_arguments(raw)
    assert browser.flag(args, "--remote-debugging-port") == "0"
    assert browser.flag(args, "--user-data-dir") == "/tmp/profile space"


def test_flattened_child_type_and_malformed_titles():
    assert (
        browser.flag(browser.command_arguments(b"chrome --type=renderer\0"), "--type") == "renderer"
    )
    assert browser.command_arguments(b'chrome --user-data-dir="unterminated\0') == []


def test_no_endpoint_is_an_observation_not_a_missing_browser(monkeypatch):
    monkeypatch.setattr(browser, "browsers", lambda: [root()])
    result = browser.BrowserMonitor(True).snapshot(True)
    assert result["observed_count"] == 1
    assert result["items"][0]["detail"] == "debug_pipe_only"
    assert "_port" not in result["items"][0]


def test_unowned_port_is_not_probed(monkeypatch):
    monkeypatch.setattr(browser, "browsers", lambda: [root(9001)])
    monkeypatch.setattr(browser, "owns_loopback_port", lambda *_: False)
    monkeypatch.setattr(browser.httpx, "Client", lambda **_: pytest.fail("must not connect"))
    result = browser.BrowserMonitor(True).snapshot(True)
    assert result["items"][0]["detail"] == "endpoint_ownership_unconfirmed"


def test_sampling_cache_keeps_original_time_and_limits_events(monkeypatch):
    count = []
    clock = [100.0]
    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(browser, "browsers", lambda: count.append(1) or [])
    monitor = browser.BrowserMonitor(True)
    first = monitor.snapshot()
    clock[0] += 30
    assert monitor.snapshot(True, event=True)["observed_at"] == first["observed_at"]
    assert len(count) == 1
    clock[0] += 31
    monitor.snapshot(True, event=True)
    assert len(count) == 2
    clock[0] += 800
    monitor.snapshot()
    assert len(count) == 2


@pytest.mark.parametrize("method", ["Runtime.evaluate", "Page.navigate", "Browser.close"])
def test_cdp_cannot_execute_javascript_or_change_the_browser(method):
    with pytest.raises(ValueError, match="unsupported_cdp_request"):
        browser.cdp(9001, "target", method)


def test_cdp_connects_directly_to_loopback_with_bounded_frames(monkeypatch):
    calls = []

    class Connection:
        def send(self, data):
            calls.append(data)

        def recv(self, **_):
            return '{"id":1,"result":{"ok":true}}'

    @contextlib.contextmanager
    def connect(uri, **kwargs):
        assert uri == "ws://127.0.0.1:9001/devtools/page/target"
        assert kwargs["proxy"] is None and kwargs["compression"] is None
        assert kwargs["max_size"] <= 1024 * 1024
        yield Connection()

    monkeypatch.setattr(browser, "connect", connect)
    assert browser.cdp(9001, "target", "Page.getLayoutMetrics") == {"ok": True}
    assert len(calls) == 1


def test_oversized_image_is_rejected(monkeypatch):
    def result(_, __, method, *args):
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"clientWidth": 100, "clientHeight": 100}}
        return {"data": base64.b64encode(b"x" * (browser.MAX_IMAGE + 1)).decode()}

    monkeypatch.setattr(browser, "cdp", result)
    with pytest.raises(ValueError, match="screenshot_too_large"):
        browser.screenshot(9001, "target")


def test_captures_are_bounded_cached_and_keep_their_original_time(monkeypatch):
    clock = [100.0]
    shots = []
    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(browser, "browsers", lambda: [root(9001)])
    monkeypatch.setattr(browser, "identity", lambda _: 77)
    monkeypatch.setattr(browser, "owns_loopback_port", lambda *_: True)
    monkeypatch.setattr(browser, "screenshot", lambda *args: shots.append(args) or "fixture-image")

    class Response:
        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield json.dumps(
                [
                    {
                        "id": str(i),
                        "type": "page",
                        "url": "https://example.test/private?token=secret",
                    }
                    for i in range(5)
                ]
            ).encode()

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        @contextlib.contextmanager
        def stream(self, method, url):
            assert method == "GET" and url == "http://127.0.0.1:9001/json/list"
            yield Response()

    monkeypatch.setattr(browser.httpx, "Client", Client)
    monitor = browser.BrowserMonitor(True)
    first = monitor.snapshot(True)
    assert len(shots) == 2 and len(monitor.captures) == 2
    pages = first["items"][0]["pages"]
    assert all(page["url"] == "https://example.test" for page in pages)
    assert sum("image" in page for page in pages) == 2
    clock[0] += 20
    cached = monitor.snapshot(True)
    assert cached == first and len(shots) == 2
    assert all("image" not in page for page in monitor.snapshot(False)["items"][0]["pages"])
    assert len(monitor.captures) == 2
