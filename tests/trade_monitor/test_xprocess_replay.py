from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trade_monitor.pivot_replay import Candle
from trade_monitor.cclass_replay import ReplayLeg
from trade_monitor.xprocess_replay import causal_lens_distribution, opening_evidence, select_primary_lens


TAIPEI = ZoneInfo("Asia/Taipei")


def _candle(at: datetime, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(at=at, open=open_, high=high, low=low, close=close)


def test_first_dh_break_is_classified_causally_and_not_from_final_high() -> None:
    start = datetime(2026, 9, 2, 8, 45, tzinfo=TAIPEI)
    rows = [
        _candle(start + timedelta(minutes=index), 100 + index, 102 + index, 99 + index, 101 + index)
        for index in range(15)
    ]
    initial_high = max(item.high for item in rows)
    rows.extend(
        [
            _candle(start + timedelta(minutes=15), initial_high - 1, initial_high + 3, initial_high - 2, initial_high + 1),
            _candle(start + timedelta(minutes=16), initial_high + 1, initial_high + 4, initial_high, initial_high + 2),
            _candle(start + timedelta(minutes=17), initial_high + 2, initial_high + 5, initial_high + 1, initial_high + 3),
            _candle(start + timedelta(minutes=18), initial_high + 3, initial_high + 20, initial_high + 2, initial_high + 10),
        ]
    )
    prior = _candle(start - timedelta(minutes=1), 99, 101, 98, 100)

    evidence = opening_evidence(rows, prior=prior)

    assert evidence["cash_gap_source"] == "UNAVAILABLE"
    assert evidence["futures_gap_source"] == "FUTURES_PROXY"
    assert evidence["first_endpoint_side"] == "DH"
    assert evidence["first_endpoint_style"] == "TRUE_LIKE"
    assert evidence["endpoint_first_seen_at"] == rows[15].at.isoformat()
    assert evidence["endpoint_confirmed_at"] == rows[17].at.isoformat()


def test_first_endpoint_return_within_two_closed_bars_is_false_like() -> None:
    start = datetime(2026, 9, 2, 8, 45, tzinfo=TAIPEI)
    rows = [_candle(start + timedelta(minutes=index), 100, 102, 98, 100) for index in range(15)]
    rows.extend(
        [
            _candle(start + timedelta(minutes=15), 101, 104, 101, 103),
            _candle(start + timedelta(minutes=16), 103, 104, 99, 101),
        ]
    )

    evidence = opening_evidence(rows, prior=None)

    assert evidence["first_endpoint_style"] == "FALSE_LIKE"
    assert evidence["endpoint_confirmed_at"] == rows[16].at.isoformat()


def test_lens_distribution_uses_only_legs_known_by_each_bar() -> None:
    legs = [
        ReplayLeg("BULL", 0, 2, 2, 20, 2, 10, 0.8, 120),
        ReplayLeg("BEAR", 2, 3, 3, 8, 1, 8, 0.8, 112),
        ReplayLeg("BULL", 3, 5, 5, 22, 2, 11, 0.8, 134),
        ReplayLeg("BEAR", 5, 6, 6, 30, 1, 30, 0.5, 104),
    ]
    result = causal_lens_distribution(8, legs)

    assert result["OPENING_EVIDENCE_ONLY"] == 3
    assert sum(result.values()) == 8
    assert result["TAIJI_PRIMARY"] >= 1
    assert result["QUADRANT_PRIMARY"] >= 1


def test_two_leg_clarity_can_choose_either_taiji_or_quadrant() -> None:
    taiji_legs = [
        ReplayLeg("BULL", 0, 4, 4, 40, 4, 10, 0.8, 140),
        ReplayLeg("BEAR", 4, 6, 6, 18, 2, 9, 0.8, 122),
    ]
    quadrant_legs = [
        ReplayLeg("BULL", 0, 4, 4, 20, 4, 5, 0.6, 120),
        ReplayLeg("BEAR", 4, 6, 6, 45, 2, 22.5, 0.9, 75),
    ]

    assert select_primary_lens(taiji_legs) == "TAIJI_PRIMARY"
    assert select_primary_lens(quadrant_legs) == "QUADRANT_PRIMARY"
