from pathlib import Path

from scripts import hybrid_v3_atomic_packets_v7 as v7


def test_v6_dependency_is_byte_pinned() -> None:
    v7.assert_base_builder_frozen()
    assert v7.BUILDER_VERSION == "hybrid-v3-atomic-packets-v7"
    assert Path(v7._BASE_FILE).name == "hybrid_v3_atomic_packets_v6.py"


def test_relation_comparison_ratios_are_raw_and_unlabelled() -> None:
    baseline = {
        "absolute_price_change": 4.0,
        "bar_count": 4,
        "slope_pct_per_bar": 2.0,
        "mean_true_range_pct": 1.0,
        "realized_close_volatility_pct": 0.5,
        "average_volume": 100.0,
    }
    current = {
        "absolute_price_change": 2.0,
        "bar_count": 8,
        "slope_pct_per_bar": 1.0,
        "mean_true_range_pct": 0.75,
        "realized_close_volatility_pct": 0.25,
        "average_volume": 150.0,
    }
    result = v7._comparison_ratios(baseline, current)
    assert result == {
        "current_to_baseline_abs_amplitude_ratio": 0.5,
        "current_to_baseline_bar_count_ratio": 2.0,
        "current_to_baseline_abs_slope_ratio": 0.5,
        "current_to_baseline_true_range_ratio": 0.75,
        "current_to_baseline_realized_volatility_ratio": 0.5,
        "current_to_baseline_average_volume_ratio": 1.5,
        "threshold_or_pass_fail_precomputed": False,
    }
    assert not any(key.lower() in {"pass", "fail", "trade"} for key in result)
