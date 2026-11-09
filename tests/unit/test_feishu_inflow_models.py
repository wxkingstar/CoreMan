from coreman.core.db.models import FeishuAuthWait, FeishuGroupAction
from coreman.core.db.models.interactions import INTERACTION_KINDS


def test_tables_and_kind_are_registered():
    assert FeishuAuthWait.__tablename__ == "feishu_auth_waits"
    assert FeishuGroupAction.__tablename__ == "feishu_group_actions"
    assert "feishu_group_write" in INTERACTION_KINDS
