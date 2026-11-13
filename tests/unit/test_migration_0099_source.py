"""迁移 0099 只加列：不给已开通的助理补 `cold_start_done_at`（他们下次私聊时才开始冷启动）。"""

from __future__ import annotations

from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "migrations/versions/0099_assistant_cold_start.py"


def test_the_migration_backfills_nothing() -> None:
    text = SOURCE.read_text(encoding="utf-8")
    assert 'revision = "0099"' in text and 'down_revision = "0098"' in text
    assert "UPDATE" not in text.upper().replace("UPDATED_AT", "")
    upgrade = text.split("def upgrade")[1].split("def downgrade")[0]
    assert "cold_start_done_at" not in upgrade.replace("cold_start_done_at IS NULL", "")
