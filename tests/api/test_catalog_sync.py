import json

import pytest
from sqlalchemy import select

from coreman.core.crypto import Cipher
from coreman.core.db.models import Skill, SkillSource
from coreman.core.knowledge.catalog_sync import read_catalog, sync_catalog
from coreman.core.knowledge.installation import MCP_AAD

CIPHER = Cipher(b"\x07" * 32)


def make_catalog(root):
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin/marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "query", "source": "./plugins/query"}]})
    )
    target = root / "plugins/query/.claude-plugin"
    target.mkdir(parents=True)
    (target / "plugin.json").write_text(
        json.dumps({"name": "query", "description": "data only", "version": "1.0"})
    )


async def test_catalog_preserves_security_and_is_atomic(db_session, tmp_path):
    make_catalog(tmp_path)
    entries = read_catalog(tmp_path)
    url = "https://github.com/example/tools.git"
    source = SkillSource(key="tools", label="Tools", git_url=url)
    db_session.add(source)
    await db_session.commit()
    assert (
        await sync_catalog(db_session, source_key="tools", url=url, entries=entries, cipher=CIPHER)
    )["created"] == 1
    await db_session.commit()
    skill = await db_session.scalar(select(Skill))
    assert not skill.enabled
    skill.enabled, skill.security_level, skill.security_prompt_template = (
        True,
        "internal",
        "approved policy",
    )
    await db_session.commit()
    entries[0]["version"] = "2.0"
    assert (
        await sync_catalog(db_session, source_key="tools", url=url, entries=entries, cipher=CIPHER)
    )["updated"] == 1
    await db_session.commit()
    assert (
        skill.enabled
        and skill.security_level == "internal"
        and skill.security_prompt_template == "approved policy"
    )
    other = SkillSource(key="other", label="Other", git_url=url)
    db_session.add(other)
    await db_session.commit()
    with pytest.raises(ValueError):
        await sync_catalog(
            db_session,
            source_key="other",
            url=url,
            entries=[{"name": "new", "description": "", "version": None}, entries[0]],
            cipher=CIPHER,
        )
    await db_session.rollback()
    assert await db_session.scalar(select(Skill).where(Skill.name == "new")) is None


def test_catalog_rejects_escape_and_ignores_instructions(tmp_path):
    make_catalog(tmp_path)
    manifest = tmp_path / ".claude-plugin/marketplace.json"
    manifest.write_text(json.dumps({"plugins": [{"name": "query", "source": "../outside"}]}))
    with pytest.raises(ValueError):
        read_catalog(tmp_path)


FIGMA = {"command": "npx", "args": ["-y", "figma-developer-mcp", "--stdio"]}


def add_mcp_plugin(root, plugin, mcp_file=None):
    manifest = root / ".claude-plugin/marketplace.json"
    data = json.loads(manifest.read_text())
    data["plugins"].append({"name": plugin["name"], "source": f"./plugins/{plugin['name']}"})
    manifest.write_text(json.dumps(data))
    target = root / f"plugins/{plugin['name']}"
    (target / ".claude-plugin").mkdir(parents=True)
    (target / ".claude-plugin/plugin.json").write_text(json.dumps(plugin))
    if mcp_file is not None:
        (target / ".mcp.json").write_text(json.dumps(mcp_file))


def test_catalog_reads_plugin_mcp_declarations(tmp_path):
    make_catalog(tmp_path)
    add_mcp_plugin(tmp_path, {"name": "figma"}, {"mcpServers": {"figma": FIGMA}})
    add_mcp_plugin(
        tmp_path,
        {
            "name": "inline",
            "mcpServers": {"remote": {"type": "http", "url": "https://mcp.example.com/mcp"}},
        },
    )
    add_mcp_plugin(tmp_path, {"name": "by-path", "mcpServers": "./config/servers.json"})
    (tmp_path / "plugins/by-path/config").mkdir()
    (tmp_path / "plugins/by-path/config/servers.json").write_text(
        json.dumps({"mcpServers": {"x": FIGMA}})
    )
    add_mcp_plugin(tmp_path, {"name": "both"}, {"mcpServers": {"figma": FIGMA}})
    (tmp_path / "plugins/both/skills/both").mkdir(parents=True)
    (tmp_path / "plugins/both/skills/both/SKILL.md").write_text("---\nname: both\n---\n")
    entries = {entry["name"]: entry for entry in read_catalog(tmp_path)}
    assert entries["query"]["mcp_config"] is None
    assert entries["both"]["mcp_config"] is None
    assert entries["figma"]["mcp_config"] == FIGMA
    assert entries["inline"]["mcp_config"]["url"] == "https://mcp.example.com/mcp"
    assert entries["by-path"]["mcp_config"] == FIGMA


@pytest.mark.parametrize(
    ("plugin", "mcp_file"),
    [
        ({"name": "two"}, {"mcpServers": {"a": FIGMA, "b": FIGMA}}),
        (
            {"name": "plain-http"},
            {"mcpServers": {"a": {"type": "http", "url": "http://mcp.example.com"}}},
        ),
        ({"name": "no-command"}, {"mcpServers": {"a": {"args": []}}}),
        ({"name": "escape", "mcpServers": "../../outside.json"}, None),
    ],
)
def test_catalog_rejects_invalid_mcp(tmp_path, plugin, mcp_file):
    make_catalog(tmp_path)
    (tmp_path / "outside.json").write_text(json.dumps({"mcpServers": {"a": FIGMA}}))
    add_mcp_plugin(tmp_path, plugin, mcp_file)
    with pytest.raises(ValueError):
        read_catalog(tmp_path)


async def test_sync_imports_and_updates_mcp_skills(db_session, tmp_path):
    make_catalog(tmp_path)
    add_mcp_plugin(tmp_path, {"name": "figma", "version": "1.0"}, {"mcpServers": {"figma": FIGMA}})
    url = "https://github.com/example/tools.git"
    db_session.add(SkillSource(key="tools", label="Tools", git_url=url))
    await db_session.commit()

    async def sync(entries):
        result = await sync_catalog(
            db_session, source_key="tools", url=url, entries=entries, cipher=CIPHER
        )
        await db_session.commit()
        return result

    entries = read_catalog(tmp_path)
    assert (await sync(entries))["created"] == 2
    figma = await db_session.scalar(select(Skill).where(Skill.name == "figma"))
    query = await db_session.scalar(select(Skill).where(Skill.name == "query"))
    assert (figma.install_type, query.install_type) == ("mcp", "git")
    assert not figma.enabled and query.mcp_config_enc is None
    assert json.loads(CIPHER.decrypt(figma.mcp_config_enc, MCP_AAD)) == FIGMA

    assert (await sync(entries))["unchanged"] == 2
    changed = {**FIGMA, "env": {"FIGMA_API_KEY": "${FIGMA_API_KEY}"}}
    for entry in entries:
        if entry["name"] == "figma":
            entry["mcp_config"] = changed
        else:
            entry["mcp_config"] = FIGMA
    assert (await sync(entries))["updated"] == 2
    await db_session.refresh(figma)
    await db_session.refresh(query)
    assert json.loads(CIPHER.decrypt(figma.mcp_config_enc, MCP_AAD)) == changed
    assert query.install_type == "mcp"
    for entry in entries:
        entry["mcp_config"] = None
    assert (await sync(entries))["updated"] == 2
    await db_session.refresh(figma)
    assert figma.install_type == "git" and figma.mcp_config_enc is None
