"""Opt-in, bounded observations of existing same-user Chromium instances.

No browser launch, navigation, JavaScript evaluation or public listening socket.
Images remain transient; the authorized caller owns persistence and access control.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from websockets.sync.client import connect

MAX_IMAGE = 512 * 1024
MAX_RESPONSE = 2 * 1024 * 1024


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017 - standalone runtime supports Python 3.10


def supports_proc() -> bool:
    return Path("/proc/self/stat").exists()


def identity(pid: int) -> int:
    root = Path("/proc") / str(pid)
    if root.stat().st_uid != os.getuid():
        raise ValueError("foreign_user")
    return int((root / "stat").read_text().rsplit(")", 1)[1].split()[19])


def flag(args: list[str], name: str) -> str | None:
    for i, value in enumerate(args):
        if value.startswith(name + "="):
            return value[len(name) + 1 :]
        if value == name and i + 1 < len(args):
            return args[i + 1]
    return None


def owns_loopback_port(pid: int, port: int) -> bool:
    inodes = set()
    try:
        for fd in (Path("/proc") / str(pid) / "fd").iterdir():
            try:
                link = os.readlink(fd)
                if link.startswith("socket:["):
                    inodes.add(link[8:-1])
            except OSError:
                continue
        for table in ("tcp", "tcp6"):
            for line in Path("/proc/net", table).read_text().splitlines()[1:]:
                fields = line.split()
                address, number = fields[1].split(":")
                if (
                    fields[3] == "0A"
                    and int(number, 16) == port
                    and fields[9] in inodes
                    and address in ("0100007F", "00000000000000000000000001000000")
                ):
                    return True
    except (OSError, ValueError, IndexError):
        pass
    return False


def browsers() -> list[dict]:
    result = []
    for proc in islice(Path("/proc").iterdir(), 10000):
        if not proc.name.isdigit():
            continue
        try:
            pid = int(proc.name)
            started = identity(pid)
            executable = os.path.basename(os.readlink(proc / "exe")).removesuffix(" (deleted)")
            if executable not in (
                "chrome",
                "chromium",
                "chromium-browser",
                "google-chrome",
                "headless_shell",
                "chrome-headless-shell",
                "firefox",
            ):
                continue
            args = [os.fsdecode(v) for v in (proc / "cmdline").read_bytes().split(b"\0") if v]
            if flag(args, "--type") is not None or "-contentproc" in args:
                continue
            port = flag(args, "--remote-debugging-port")
            detail = (
                "debug_pipe_only" if "--remote-debugging-pipe" in args else "debug_endpoint_missing"
            )
            if executable == "firefox":
                port, detail = None, "browser_protocol_not_supported"
            if port == "0":
                directory = flag(args, "--user-data-dir")
                port = None
                detail = "dynamic_debug_port_unavailable"
                if directory:
                    try:
                        active = Path(directory) / "DevToolsActivePort"
                        descriptor = os.open(active, os.O_RDONLY | os.O_NOFOLLOW)
                        with os.fdopen(descriptor, "rb") as stream:
                            if os.fstat(stream.fileno()).st_uid == os.getuid():
                                port = stream.read(200).splitlines()[0].decode("ascii")
                    except (OSError, ValueError, IndexError):
                        pass
            number = int(port) if port and port.isdecimal() else None
            result.append(
                {
                    "id": f"{pid}-{started}",
                    "pid": pid,
                    "start_ticks": started,
                    "kind": executable,
                    "_port": number if number and 0 < number < 65536 else None,
                    "_detail": detail,
                }
            )
        except (OSError, ValueError, IndexError):
            continue
    return result


def cdp(port: int, target: str, method: str, params: dict | None = None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", target) or method not in (
        "Page.getLayoutMetrics",
        "Page.captureScreenshot",
    ):
        raise ValueError("unsupported_cdp_request")
    uri = f"ws://127.0.0.1:{port}/devtools/page/{target}"
    with connect(
        uri, proxy=None, compression=None, open_timeout=3, close_timeout=1, max_size=MAX_IMAGE * 2
    ) as connection:
        connection.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            response = json.loads(connection.recv(timeout=max(0.01, deadline - time.monotonic())))
            if response.get("id") == 1:
                if "error" in response:
                    raise ValueError("cdp_request_failed")
                return response.get("result") or {}
    raise TimeoutError("browser_response_timeout")


def screenshot(port: int, target: str) -> str:
    layout = cdp(port, target, "Page.getLayoutMetrics").get("cssVisualViewport") or {}
    width, height = layout.get("clientWidth"), layout.get("clientHeight")
    if (
        not isinstance(width, int | float)
        or not isinstance(height, int | float)
        or width <= 0
        or height <= 0
    ):
        raise ValueError("viewport_unavailable")
    result = cdp(
        port,
        target,
        "Page.captureScreenshot",
        {
            "format": "jpeg",
            "quality": 45,
            "captureBeyondViewport": False,
            "clip": {
                "x": 0,
                "y": 0,
                "width": min(width, 1920),
                "height": min(height, 1080),
                "scale": 1,
            },
        },
    )
    encoded = result.get("data")
    if not isinstance(encoded, str) or len(encoded) > MAX_IMAGE * 4 // 3 + 4:
        raise ValueError("screenshot_too_large")
    image = base64.b64decode(encoded, validate=True)
    if len(image) > MAX_IMAGE:
        raise ValueError("screenshot_too_large")
    if not image.startswith(b"\xff\xd8\xff"):
        raise ValueError("invalid_screenshot")
    return encoded


class BrowserMonitor:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self.lock = threading.Lock()
        self.cached: dict | None = None
        self.last_scan = -float("inf")
        self.captures: dict[tuple[str, str], dict] = {}

    def snapshot(self, capture: bool = False, event: bool = False) -> dict:
        if not self.enabled:
            return {"success": True, "state": "disabled", "observed_at": None, "items": []}
        if not supports_proc():
            return {
                "success": True,
                "state": "unsupported",
                "reason": "linux_proc_required",
                "items": [],
            }
        with self.lock:
            clock = time.monotonic()
            interval = 60 if event else 900
            if self.cached is not None and clock - self.last_scan < interval:
                return self._response(self.cached, capture)
            roots = browsers()
            result = {
                "success": True,
                "state": "ok",
                "observed_at": stamp(),
                "interval_seconds": 900,
                "screenshot_interval_seconds": 1800,
                "observed_count": len(roots),
                "truncated": len(roots) > 20,
                "items": [],
            }
            deadline, budget = clock + 25, 2
            retained = {}
            for raw in roots[:20]:
                browser = {k: v for k, v in raw.items() if not k.startswith("_")}
                browser.update(
                    state="unsupported",
                    detail=raw.get("_detail", "debug_endpoint_missing"),
                    pages=[],
                )
                result["items"].append(browser)
                port = raw["_port"]
                if not port:
                    continue
                if not owns_loopback_port(browser["pid"], port):
                    browser["detail"] = "endpoint_ownership_unconfirmed"
                    continue
                try:
                    if identity(browser["pid"]) != browser["start_ticks"]:
                        raise ValueError("process_changed")
                    with httpx.Client(trust_env=False, timeout=3, follow_redirects=False) as client:
                        with client.stream("GET", f"http://127.0.0.1:{port}/json/list") as response:
                            response.raise_for_status()
                            body = bytearray()
                            for chunk in response.iter_bytes():
                                body.extend(chunk)
                                if len(body) > 256 * 1024:
                                    raise ValueError("inventory_too_large")
                    targets = json.loads(body)
                    if not isinstance(targets, list):
                        raise ValueError("invalid_inventory")
                    browser.update(state="connected", detail="", page_count=len(targets))
                    for target in targets[:10]:
                        if not isinstance(target, dict) or target.get("type") != "page":
                            continue
                        identity_key = target.get("id")
                        if not isinstance(identity_key, str) or not re.fullmatch(
                            r"[A-Za-z0-9_-]{1,100}", identity_key
                        ):
                            continue
                        url = urlsplit(str(target.get("url") or ""))
                        origin = (
                            f"{url.scheme}://{url.hostname}"
                            if url.scheme in ("https", "http") and url.hostname
                            else ""
                        )
                        page = {
                            "id": identity_key,
                            "url": origin,
                            "title": "".join(
                                c for c in str(target.get("title") or "") if c.isprintable()
                            )[:200],
                        }
                        key = (browser["id"], identity_key)
                        previous = self.captures.get(key)
                        if previous:
                            page.update(previous["page"])
                            retained[key] = previous
                        if (
                            capture
                            and origin
                            and budget
                            and time.monotonic() < deadline
                            and (not previous or clock - previous["clock"] >= 1800)
                        ):
                            try:
                                if identity(browser["pid"]) != browser["start_ticks"]:
                                    raise ValueError("process_changed")
                                encoded = screenshot(port, identity_key)
                                if identity(browser["pid"]) != browser["start_ticks"]:
                                    raise ValueError("process_changed")
                                page.update(screenshot_at=stamp(), image=encoded)
                                retained[key] = {
                                    "clock": clock,
                                    "page": {k: page[k] for k in ("screenshot_at", "image")},
                                }
                            except Exception as error:
                                page["screenshot_error"] = browser_error(error)
                            budget -= 1
                        browser["pages"].append(page)
                except Exception as error:
                    browser.update(state="unknown", detail=browser_error(error))
                if time.monotonic() >= deadline:
                    break
            self.captures = dict(
                sorted(retained.items(), key=lambda pair: pair[1]["clock"], reverse=True)[:2]
            )
            for browser in result["items"]:
                for page in browser["pages"]:
                    if (browser["id"], page["id"]) not in self.captures:
                        page.pop("image", None)
            self.cached, self.last_scan = result, clock
            return self._response(result, capture)

    @staticmethod
    def _response(value: dict, capture: bool) -> dict:
        result = copy.deepcopy(value)
        if not capture:
            for browser in result.get("items", []):
                for page in browser.get("pages", []):
                    page.pop("image", None)
        if len(json.dumps(result).encode()) > MAX_RESPONSE:
            raise ValueError("browser_snapshot_too_large")
        return result


def browser_error(error: Exception) -> str:
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, TimeoutError | httpx.TimeoutException):
        return "browser_response_timeout"
    if isinstance(error, ValueError) and str(error) in {
        "process_changed",
        "inventory_too_large",
        "invalid_inventory",
        "viewport_unavailable",
        "screenshot_too_large",
        "invalid_screenshot",
        "cdp_request_failed",
        "unsupported_cdp_request",
    }:
        return str(error)
    return "browser_connection_failed"
