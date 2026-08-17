from wakefinder.common.wallet_lock import WalletAlreadyRunningError, acquire_wallet_lock


def test_acquire_lock_succeeds_when_free(tmp_path):
    f = acquire_wallet_lock(str(tmp_path), "0xABC", "eth_arb")
    assert f is not None
    f.close()


def test_second_acquire_same_address_raises(tmp_path):
    f1 = acquire_wallet_lock(str(tmp_path), "0xABC", "eth_arb")
    try:
        acquire_wallet_lock(str(tmp_path), "0xABC", "eth_copytrade")
        assert False, "second lock should have raised"
    except WalletAlreadyRunningError as exc:
        assert "eth_arb" in str(exc)
        assert "eth_copytrade" in str(exc)
    finally:
        f1.close()


def test_different_address_does_not_conflict(tmp_path):
    f1 = acquire_wallet_lock(str(tmp_path), "0xABC", "eth_arb")
    f2 = acquire_wallet_lock(str(tmp_path), "0xDEF", "solana_arb")
    f1.close()
    f2.close()


def test_lock_released_after_close_allows_reacquire(tmp_path):
    f1 = acquire_wallet_lock(str(tmp_path), "0xABC", "eth_arb")
    f1.close()
    f2 = acquire_wallet_lock(str(tmp_path), "0xABC", "eth_snipe")
    f2.close()


def test_address_case_normalized(tmp_path):
    f1 = acquire_wallet_lock(str(tmp_path), "0xAbC", "eth_arb")
    try:
        acquire_wallet_lock(str(tmp_path), "0xabc", "eth_copytrade")
        assert False, "different case of same address should still conflict"
    except WalletAlreadyRunningError:
        pass
    finally:
        f1.close()


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    tests = [
        test_acquire_lock_succeeds_when_free,
        test_second_acquire_same_address_raises,
        test_different_address_does_not_conflict,
        test_lock_released_after_close_allows_reacquire,
        test_address_case_normalized,
    ]
    for test_fn in tests:
        with tempfile.TemporaryDirectory() as d:
            test_fn(Path(d))
    print("ok")
