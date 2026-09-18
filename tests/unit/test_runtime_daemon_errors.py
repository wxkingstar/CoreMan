"""Failed Agent operations: fixed OperationError prompts reach the platform, nothing else does."""

import ast
import base64
import json
import logging
from pathlib import Path

import pytest

import runtime_daemon
from runtime_daemon import agent as agent_module
from runtime_daemon.daemon import Daemon


@pytest.fixture
def node(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"workspace_root": str(root), "api_url": "http://localhost", "node_id": "n1"})
    )
    daemon = Daemon(path)
    # Same whitelist as the node in the 2026-09-18 incident.
    daemon.agents["claude"] = agent_module.Agent(
        root=root,
        home=tmp_path / "home",
        api_url="http://localhost",
        token="synthetic-test-token-123456789",
        relay_id="relay-1",
        git_hosts=("github.com",),
    )
    return daemon


async def run(daemon, body, path="/"):
    sent = []

    async def send_frames(identity, frames):
        assert identity == "call-1"
        sent.extend(frames)

    daemon.send_frames = send_frames
    await daemon.execute(
        {
            "id": "call-1",
            "provider": "claude",
            "method": "POST",
            "path": path,
            "body": base64.b64encode(json.dumps(body).encode()).decode(),
        }
    )
    assert sent and sent[-1]["done"] is True
    return sent


def body_of(frames):
    return json.loads(b"".join(base64.b64decode(frame["data"]) for frame in frames))


async def test_whitelist_rejection_is_returned_and_logged(node, caplog):
    (node.agents["claude"].root / "bot-1").mkdir()
    caplog.set_level(logging.WARNING, logger="coreman-runtime")
    frames = await run(
        node,
        {
            "type": "install-skill",
            "project_dir": "bot-1",
            "install_type": "skill",
            "skill_name": "query",
            "git_url": "https://git.example.org/ai/skills.git",
        },
    )
    assert frames[0]["status_code"] == 200
    assert not any(frame.get("error") for frame in frames)
    assert body_of(frames) == {
        "success": False,
        "code": "operation_failed",
        "message": "Git 来源不在白名单内",
    }
    assert "Operation install-skill failed: Git 来源不在白名单内" in caplog.text


async def test_unexpected_errors_stay_type_only(node, caplog):
    def dispatch(data):
        raise RuntimeError("synthetic-secret from /home/user/.git-credentials")

    node.agents["claude"].dispatch = dispatch
    caplog.set_level(logging.WARNING, logger="coreman-runtime")
    frames = await run(node, {"type": "install-skill"})
    assert frames[-1]["error"] == "execution_failed"
    assert not any("status_code" in frame for frame in frames)
    assert "Request failed: install-skill: RuntimeError" in caplog.text
    assert "synthetic-secret" not in caplog.text


async def test_chat_request_operation_error_is_logged_but_not_answered(node, caplog):
    caplog.set_level(logging.WARNING, logger="coreman-runtime")
    frames = await run(node, {"working_dir": "../outside"}, path="/v1/chat/completions")
    assert frames[-1]["error"] == "execution_failed"
    assert "Request failed: OperationError: 工作目录不在允许范围内" in caplog.text


# Values an f-string message may interpolate: numbers only, never output or paths.
ALLOWED_VALUES = {"rc", "response.status_code", "count", "failed"}


def fixed_message(arg: ast.expr, constants: set[str]) -> bool:
    if isinstance(arg, ast.JoinedStr):
        return all(
            isinstance(part, ast.Constant)
            or (isinstance(part, ast.FormattedValue) and ast.unparse(part.value) in ALLOWED_VALUES)
            for part in arg.values
        )
    if isinstance(arg, ast.Name):
        return arg.id in constants
    return isinstance(arg, ast.Constant) and isinstance(arg.value, str)


def test_operation_error_messages_are_fixed_prompts():
    # The daemon returns these messages to the platform, which shows them to bot admins.
    checked, problems = 0, []
    for path in sorted(Path(runtime_daemon.__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text())
        constants = {
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            for target in node.targets
            if isinstance(target, ast.Name) and isinstance(node.value.value, str)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = ast.unparse(node.func)
            if func != "OperationError" and (path.name, func) != ("workspace.py", "self.error"):
                continue
            checked += 1
            if node.keywords or len(node.args) != 1 or not fixed_message(node.args[0], constants):
                problems.append(f"{path.name}:{node.lineno}: {ast.unparse(node)}")
    assert checked > 50
    assert problems == []
