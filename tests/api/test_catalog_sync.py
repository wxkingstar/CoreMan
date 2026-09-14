import json

import pytest
from sqlalchemy import select

from coreman.core.db.models import Skill, SkillSource
from coreman.core.knowledge.catalog_sync import read_catalog, sync_catalog


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
    assert (await sync_catalog(db_session, source_key="tools", url=url, entries=entries))[
        "created"
    ] == 1
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
    assert (await sync_catalog(db_session, source_key="tools", url=url, entries=entries))[
        "updated"
    ] == 1
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
        )
    await db_session.rollback()
    assert await db_session.scalar(select(Skill).where(Skill.name == "new")) is None


def test_catalog_rejects_escape_and_ignores_instructions(tmp_path):
    make_catalog(tmp_path)
    manifest = tmp_path / ".claude-plugin/marketplace.json"
    manifest.write_text(json.dumps({"plugins": [{"name": "query", "source": "../outside"}]}))
    with pytest.raises(ValueError):
        read_catalog(tmp_path)
