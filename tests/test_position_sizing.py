from wakefinder.common.position_sizing import win_rate_size_multiplier


def test_insufficient_sample_returns_neutral():
    assert win_rate_size_multiplier(win_rate=1.0, sample_size=2, min_trades=5) == 1.0


def test_fifty_percent_win_rate_is_neutral():
    assert win_rate_size_multiplier(win_rate=0.5, sample_size=10) == 1.0


def test_high_win_rate_scales_up_toward_max():
    m = win_rate_size_multiplier(win_rate=1.0, sample_size=10, max_multiplier=1.5)
    assert abs(m - 1.5) < 1e-9


def test_low_win_rate_scales_down_toward_min():
    m = win_rate_size_multiplier(win_rate=0.0, sample_size=10, min_multiplier=0.25)
    assert abs(m - 0.25) < 1e-9


def test_intermediate_win_rate_interpolates():
    # win_rate=0.75 -> midway между 1.0 и max_multiplier
    m = win_rate_size_multiplier(win_rate=0.75, sample_size=10, max_multiplier=1.5)
    assert abs(m - 1.25) < 1e-9


if __name__ == "__main__":
    test_insufficient_sample_returns_neutral()
    test_fifty_percent_win_rate_is_neutral()
    test_high_win_rate_scales_up_toward_max()
    test_low_win_rate_scales_down_toward_min()
    test_intermediate_win_rate_interpolates()
    print("ok")
