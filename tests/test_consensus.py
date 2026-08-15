from wakefinder.common.consensus import ConsensusTracker


def test_single_wallet_not_enough_for_threshold_two():
    c = ConsensusTracker(min_wallets=2, window_seconds=60)
    assert not c.record_buy("0xTOKEN", "0xWALLET_A", now=1000.0)


def test_two_different_wallets_reach_consensus():
    c = ConsensusTracker(min_wallets=2, window_seconds=60)
    assert not c.record_buy("0xTOKEN", "0xWALLET_A", now=1000.0)
    assert c.record_buy("0xTOKEN", "0xWALLET_B", now=1010.0)


def test_same_wallet_twice_does_not_reach_consensus():
    c = ConsensusTracker(min_wallets=2, window_seconds=60)
    assert not c.record_buy("0xTOKEN", "0xWALLET_A", now=1000.0)
    assert not c.record_buy("0xTOKEN", "0xWALLET_A", now=1010.0)  # тот же кошелёк повторно


def test_signal_outside_window_expires():
    c = ConsensusTracker(min_wallets=2, window_seconds=60)
    assert not c.record_buy("0xTOKEN", "0xWALLET_A", now=1000.0)
    # WALLET_B пришёл спустя 120с — сигнал WALLET_A уже вне окна
    assert not c.record_buy("0xTOKEN", "0xWALLET_B", now=1120.0)


def test_different_tokens_tracked_independently():
    c = ConsensusTracker(min_wallets=2, window_seconds=60)
    assert not c.record_buy("0xTOKEN_A", "0xWALLET_A", now=1000.0)
    assert not c.record_buy("0xTOKEN_B", "0xWALLET_B", now=1000.0)


def test_clear_resets_token():
    c = ConsensusTracker(min_wallets=2, window_seconds=60)
    c.record_buy("0xTOKEN", "0xWALLET_A", now=1000.0)
    c.clear("0xTOKEN")
    assert not c.record_buy("0xTOKEN", "0xWALLET_B", now=1001.0)


if __name__ == "__main__":
    test_single_wallet_not_enough_for_threshold_two()
    test_two_different_wallets_reach_consensus()
    test_same_wallet_twice_does_not_reach_consensus()
    test_signal_outside_window_expires()
    test_different_tokens_tracked_independently()
    test_clear_resets_token()
    print("ok")
