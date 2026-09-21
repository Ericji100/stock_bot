from pathlib import Path

import pandas as pd

from scripts import hybrid_v3_atomic_packets_v5 as v5


def _pivot(scale: str, side: str, day: str, price: float, suffix: str) -> dict:
    return {
        "scale": scale,
        "side": side,
        "source_date": day,
        "confirmation_date": day,
        "price": price,
        "ref": f"PIVOT:{scale}:{side}:{day}:{suffix}",
    }


def test_v4_dependency_is_byte_pinned() -> None:
    v5.assert_base_builder_frozen()
    assert v5.BUILDER_VERSION == "hybrid-v3-atomic-packets-v5"
    assert Path(v5._BASE_FILE).name == "hybrid_v3_atomic_packets_v4.py"


def test_same_day_pivot_leg_is_not_a_comparison_segment() -> None:
    visible = pd.DataFrame(
        [
            {"date": pd.Timestamp("2023-01-02"), "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100},
            {"date": pd.Timestamp("2023-01-03"), "open": 10, "high": 12, "low": 10, "close": 11, "volume": 100},
            {"date": pd.Timestamp("2023-01-04"), "open": 11, "high": 13, "low": 10, "close": 12, "volume": 100},
            {"date": pd.Timestamp("2023-01-05"), "open": 12, "high": 14, "low": 11, "close": 13, "volume": 100},
            {"date": pd.Timestamp("2023-01-09"), "open": 12, "high": 13, "low": 11, "close": 12, "volume": 100},
        ]
    )
    pivots = [
        _pivot("LARGE", "LOW", "2023-01-02", 9, "a"),
        _pivot("LARGE", "HIGH", "2023-01-05", 14, "b"),
        _pivot("LARGE", "LOW", "2023-01-09", 11, "c"),
        _pivot("LARGE", "HIGH", "2023-01-09", 13, "d"),
    ]
    result = v5.build_working_comparison_windows(
        objective={"controls": {"large": {"state": "UP_CONTROL"}}},
        confirmed_pivots=pivots,
        visible=visible,
        as_of="2023-01-09",
    )
    assert result["LARGE"] is None
    assert result["SMALL"] is None
