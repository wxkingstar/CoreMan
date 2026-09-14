"""运行时进程（worker / scheduler / 网关）集成测试的环境：让 `get_settings()` 指向测试库。

这些进程不像 api 那样由测试注入 `Settings`，它们在 `Service.__init__` 里自己调
`get_settings()`，所以只能靠环境变量 + 清缓存来换掉配置。`MASTER_KEY` 与
`tests.integration.worker_helpers.MASTER` 同源，进程解出来的凭证才和用例造的密文对得上。
"""

from __future__ import annotations

import base64
from collections.abc import Iterator

import pytest

from coreman.core.config import reset_settings_cache
from tests.integration.worker_helpers import MASTER


@pytest.fixture
def runtime_settings(migrated_database: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", migrated_database)
    monkeypatch.setenv("MASTER_KEY", base64.b64encode(MASTER).decode())
    monkeypatch.setenv("SESSION_SECRET", "integration-test-session-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost")
    reset_settings_cache()
    yield
    reset_settings_cache()
