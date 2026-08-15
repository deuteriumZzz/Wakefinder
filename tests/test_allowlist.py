import pytest

from wakefinder.common.allowlist import validate_not_denylisted, validate_token_allowlist


def test_empty_allowlist_skips_check():
    validate_token_allowlist({"0xAAA"}, frozenset())  # не должно бросать


def test_allowed_tokens_pass():
    validate_token_allowlist({"0xAAA", "0xBBB"}, frozenset({"0xaaa", "0xbbb"}))  # регистронезависимо


def test_unknown_token_rejected():
    with pytest.raises(ValueError, match="0xccc"):
        validate_token_allowlist({"0xAAA", "0xCCC"}, frozenset({"0xaaa"}))


def test_empty_denylist_skips_check():
    validate_not_denylisted({"0xAAA"}, frozenset())  # не должно бросать


def test_denylisted_token_rejected():
    with pytest.raises(ValueError, match="0xbbb"):
        validate_not_denylisted({"0xAAA", "0xBBB"}, frozenset({"0xbbb"}))  # регистронезависимо


def test_non_denylisted_tokens_pass():
    validate_not_denylisted({"0xAAA", "0xCCC"}, frozenset({"0xbbb"}))  # не должно бросать


if __name__ == "__main__":
    test_empty_allowlist_skips_check()
    test_allowed_tokens_pass()
    test_unknown_token_rejected()
    test_empty_denylist_skips_check()
    test_denylisted_token_rejected()
    test_non_denylisted_tokens_pass()
    print("ok")
