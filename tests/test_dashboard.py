from wakefinder.dashboard import _usd_estimate


def test_usd_estimate_eth():
    # 1.5 ETH * $2000 = $3000
    result = _usd_estimate(int(1.5 * 10**18), "eth", {"eth": 2000.0})
    assert "3,000.00" in result


def test_usd_estimate_sol():
    result = _usd_estimate(int(2 * 10**9), "sol", {"sol": 150.0})
    assert "300.00" in result


def test_usd_estimate_missing_price_returns_empty():
    assert _usd_estimate(10**18, "eth", {}) == ""


def test_usd_estimate_unknown_chain_returns_empty():
    assert _usd_estimate(10**18, "unknown", {"eth": 2000.0}) == ""


if __name__ == "__main__":
    test_usd_estimate_eth()
    test_usd_estimate_sol()
    test_usd_estimate_missing_price_returns_empty()
    test_usd_estimate_unknown_chain_returns_empty()
    print("ok")
