"""每个进程入口先导入的模块不同，循环导入只在某种顺序下才炸：逐个在新进程里单独导入。"""

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "coreman.core.runtime_nodes.transport",
        "coreman.runtime.scheduler.service",
        "coreman.runtime.worker.service",
        "coreman.core.relay.models",
    ],
)
def test_module_imports_on_its_own(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
