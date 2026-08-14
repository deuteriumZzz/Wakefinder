import pytest

from wakefinder.common.allowlist import validate_token_allowlist


def test_empty_allowlist_skips_check():
    validate_token_allowlist({"0xAAA"}, frozenset())  # не должно бросать


def test_allowed_tokens_pass():
    validate_token_allowlist({"0xAAA", "0xBBB"}, frozenset({"0xaaa", "0xbbb"}))  # регистронезависимо


def test_unknown_token_rejected():
    with pytest.raises(ValueError, match="0xccc"):
        validate_token_allowlist({"0xAAA", "0xCCC"}, frozenset({"0xaaa"}))


if __name__ == "__main__":
    test_empty_allowlist_skips_check()
    test_allowed_tokens_pass()
    test_unknown_token_rejected()
    print("ok")
