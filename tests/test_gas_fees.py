from web3 import Web3

from mevbot.chains.eth.main import BASE_PRIORITY_FEE_WEI, _compute_fees


def test_falls_back_to_base_priority_fee_when_profit_share_is_tiny():
    max_fee, priority_fee = _compute_fees(
        base_fee=Web3.to_wei(10, "gwei"), max_gas_gwei=50, expected_profit_wei=1, profit_share_bps=9000
    )
    assert priority_fee == BASE_PRIORITY_FEE_WEI


def test_tip_scales_with_profit():
    _, small_tip = _compute_fees(
        base_fee=Web3.to_wei(10, "gwei"), max_gas_gwei=1000, expected_profit_wei=Web3.to_wei(0.01, "ether"), profit_share_bps=9000
    )
    _, big_tip = _compute_fees(
        base_fee=Web3.to_wei(10, "gwei"), max_gas_gwei=1000, expected_profit_wei=Web3.to_wei(1, "ether"), profit_share_bps=9000
    )
    assert big_tip > small_tip


def test_max_fee_never_exceeds_operator_ceiling():
    max_fee, _ = _compute_fees(
        base_fee=Web3.to_wei(10, "gwei"), max_gas_gwei=50, expected_profit_wei=Web3.to_wei(10, "ether"), profit_share_bps=9000
    )
    assert max_fee <= Web3.to_wei(50, "gwei")


if __name__ == "__main__":
    test_falls_back_to_base_priority_fee_when_profit_share_is_tiny()
    test_tip_scales_with_profit()
    test_max_fee_never_exceeds_operator_ceiling()
    print("ok")
