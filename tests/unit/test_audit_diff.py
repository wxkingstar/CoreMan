from coreman.core.audit import diff_dict


def test_diff_only_changed_and_masks_secrets() -> None:
    before = {"name": "a", "secret": "s1", "enabled": True, "extra": {"k": 1}}
    after = {"name": "b", "secret": "s2", "enabled": True, "extra": {"k": 2}}
    assert diff_dict(before, after, secret_keys=("secret",)) == {
        "name": ["a", "b"],
        "secret": ["***", "***"],
        "extra": [{"k": 1}, {"k": 2}],
    }
    assert diff_dict(before, before) == {}
