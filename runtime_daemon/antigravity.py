"""Antigravity CLI（命令 agy）的登录状态、账号与额度读取；守护进程与实例代理共用。"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

# CLI 的可执行文件名与后端名不同的：Antigravity 的命令是 agy。
CLI_BINARY = {"antigravity": "agy"}


def cli_binary(provider: str) -> str:
    return CLI_BINARY.get(provider, provider)


ANTIGRAVITY_USAGE_TTL = 300.0
_antigravity_usage: dict[str, tuple[float, str, dict]] = {}


def antigravity_usage(
    env: dict | None = None, *, max_age: float = ANTIGRAVITY_USAGE_TTL
) -> tuple[str, dict]:
    """Antigravity 的登录状态与额度：`agy -p "/usage" --output-format json`，不消耗对话轮次。

    返回 (login, data)：login 为 ready / required / unknown，data 是命令给出的额度分组。
    未登录时 CLI 会打印 Authentication required 并等人登录（实测 1.5.19），所以关掉 stdin、
    限时执行，并按这句话判断。结果按 HOME 缓存几分钟，心跳不会每次都起进程。"""
    env = dict(os.environ if env is None else env)
    key = env.get("HOME", "")
    cached = _antigravity_usage.get(key)
    if cached and time.monotonic() - cached[0] < max_age:
        return cached[1], cached[2]
    login, data, output = "unknown", {}, ""
    try:
        proc = subprocess.run(
            [cli_binary("antigravity"), "-p", "/usage", "--output-format", "json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            env={**env, "AGY_CLI_DISABLE_AUTO_UPDATE": "true"},
        )
        output = proc.stdout + proc.stderr
        body = json.loads(proc.stdout)
        command = body.get("command") if isinstance(body, dict) else None
        if body.get("status") == "SUCCESS" and isinstance(command, dict):
            login = "ready"
            data = command.get("data") if isinstance(command.get("data"), dict) else {}
    except subprocess.TimeoutExpired as exc:
        output = "".join(
            part.decode(errors="replace") if isinstance(part, bytes) else (part or "")
            for part in (exc.stdout, exc.stderr)
        )
    except (OSError, ValueError, AttributeError):
        pass
    if login != "ready" and "authentication required" in output.lower():
        login = "required"
    _antigravity_usage[key] = (time.monotonic(), login, data)
    return login, data


def antigravity_account(env: dict | None = None) -> str:
    """Antigravity 当前登录的 Google 账号（~/.gemini/google_accounts.json 的 active）。"""
    home = Path((os.environ if env is None else env).get("HOME") or Path.home())
    try:
        data = json.loads((home / ".gemini/google_accounts.json").read_text())
        return str(data.get("active") or "")[:255]
    except (OSError, ValueError, AttributeError):
        return ""


WINDOWS = {"5h": "five_hour", "weekly": "seven_day"}


def antigravity_limits(data: dict) -> dict:
    """把 /usage 的分组换成平台的两个窗口。

    Antigravity 按模型分组计额度（Gemini 一组，Claude 与 GPT 一组），每组各有 5 小时与每周
    两个窗口；平台每个实例只有一对窗口，取各组里用得最多的那个，免得一组用完了还显示有余量。
    没用过的窗口重置时间是 1970 年，当作没有。"""
    out: dict = {}
    groups = data.get("groups") if isinstance(data, dict) else None
    for group in groups if isinstance(groups, list) else []:
        buckets = group.get("buckets") if isinstance(group, dict) else None
        for bucket in buckets if isinstance(buckets, list) else []:
            name = WINDOWS.get(bucket.get("window")) if isinstance(bucket, dict) else None
            remaining = bucket.get("remaining_fraction") if name else None
            if not isinstance(remaining, int | float) or isinstance(remaining, bool):
                continue
            used = round(min(100.0, max(0.0, (1 - remaining) * 100)), 1)
            if name in out and out[name]["used_percentage"] >= used:
                continue
            resets_at = None
            try:
                moment = datetime.fromisoformat(
                    str(bucket.get("reset_time")).replace("Z", "+00:00")
                )
                if moment.year > 2000:
                    resets_at = moment.astimezone(UTC).isoformat()
            except ValueError:
                pass
            out[name] = {"used_percentage": used, "resets_at": resets_at}
    return out
