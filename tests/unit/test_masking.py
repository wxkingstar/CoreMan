from coreman.core.masking import MASK, is_masked, mask_secret


def test_mask_secret_keeps_two_ends() -> None:
    assert mask_secret("abcdefgh") == "ab" + MASK + "gh"
    assert mask_secret("abcd") == MASK
    assert mask_secret("") == MASK
    assert mask_secret(None) is None


def test_is_masked() -> None:
    assert is_masked("ab" + MASK + "gh") and is_masked(MASK)
    assert not is_masked("plain") and not is_masked(None)
