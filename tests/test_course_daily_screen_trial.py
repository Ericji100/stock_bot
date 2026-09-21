from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

import scripts.course_daily_screen_trial as trial
from trade_monitor.pivot_replay import LocalPivot


def _leg(
    *,
    direction: str,
    start: float,
    end: float,
    amplitude_atr: float,
    duration: int,
    efficiency: float,
    confirmed: bool = True,
) -> dict:
    amplitude = abs(end - start)
    return {
        "direction": direction,
        "start_price": start,
        "end_price": end,
        "amplitude": amplitude,
        "amplitude_atr": amplitude_atr,
        "duration_bars": duration,
        "slope_atr_per_bar": amplitude_atr / duration,
        "efficiency": efficiency,
        "confirmed": confirmed,
    }


def _frame(last_close: float = 125.0) -> pd.DataFrame:
    start = datetime(2026, 1, 1)
    closes = [100.0] * 145
    closes += [90.0, 94.0, 98.0, 102.0, 106.0, 110.0]
    closes += [108.0, 106.0, 104.0, 103.0, 102.0]
    remaining = 170 - len(closes)
    step = (last_close - closes[-1]) / remaining
    closes += [closes[-1] + step * (index + 1) for index in range(remaining)]
    rows = [
        {
            "date": start + timedelta(days=index),
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1_000_000,
        }
        for index, close in enumerate(closes)
    ]
    return trial._add_indicators(pd.DataFrame(rows))


def _pivot(kind: str, index: int, price: float) -> LocalPivot:
    at = datetime(2026, 1, 1) + timedelta(days=index)
    return LocalPivot(
        pivot_id=f"P-{kind}-{index}",
        kind=kind,
        bar_index=index,
        confirmation_index=index + 2,
        bar_time=at.isoformat(),
        confirmation_time=(at + timedelta(days=2)).isoformat(),
        price=price,
        delay_bars=2,
    )


def test_taiji_copy_quality_uses_more_than_absolute_points() -> None:
    parent = _leg(direction="BULL", start=100, end=110, amplitude_atr=2.0, duration=10, efficiency=0.70)
    faster_child = _leg(direction="BULL", start=104, end=113, amplitude_atr=2.0, duration=5, efficiency=0.80)
    failed_child = _leg(direction="BULL", start=104, end=109, amplitude_atr=1.0, duration=8, efficiency=0.60)

    strong = trial._taiji_copy_quality(faster_child, parent, trial.THRESHOLDS)
    failed = trial._taiji_copy_quality(failed_child, parent, trial.THRESHOLDS)

    assert strong["quality"] == "ACCEPTABLE"
    assert strong["broke_parent_extreme"] is True
    assert strong["slope_ratio"] > 1
    assert failed["quality"] == "FAILED"
    assert failed["broke_parent_extreme"] is False


def test_taiji_correction_quality_distinguishes_healthy_and_failed() -> None:
    parent = _leg(direction="BULL", start=100, end=120, amplitude_atr=3.0, duration=10, efficiency=0.80)
    healthy = _leg(direction="BEAR", start=120, end=113, amplitude_atr=1.0, duration=5, efficiency=0.60)
    failed = _leg(direction="BEAR", start=120, end=98, amplitude_atr=3.5, duration=8, efficiency=0.70)

    healthy_result = trial._taiji_correction_quality(healthy, parent, trial.THRESHOLDS)
    failed_result = trial._taiji_correction_quality(failed, parent, trial.THRESHOLDS)

    assert healthy_result["quality"] == "STRONG"
    assert healthy_result["holds_parent_origin"] is True
    assert failed_result["quality"] == "FAILED"
    assert failed_result["holds_parent_origin"] is False


def test_taiji_builds_anchor_correction_copy_state(monkeypatch) -> None:
    frame = _frame(last_close=125.0)
    pivots = [_pivot("LOW", 145, 89.5), _pivot("HIGH", 150, 110.5), _pivot("LOW", 155, 101.5)]
    monkeypatch.setattr(trial, "detect_local_pivots", lambda candles, n: (pivots, {}))
    monkeypatch.setattr(trial, "pair_pivots", lambda rows: (rows, 0, 0))

    result = trial._taiji(
        frame,
        {"large": "BULL", "small": "BULL", "relation": "多方共振"},
        trial.THRESHOLDS,
    )

    assert result["sequence"] == "LEG_3"
    assert result["state"] == "COPY_CONFIRMED"
    assert result["entry_support"] is True
    assert [item["role"] for item in result["legs"]] == ["ANCHOR", "CORRECTION", "COPY"]


def test_elson_alignment_requires_plane_line_and_point() -> None:
    frame = _frame(last_close=125.0)
    direction = {"large": "BULL", "small": "BULL", "relation": "多方共振"}
    setup = {"confirmed": True}
    sstv = {"quality": "合格"}

    result = trial._elson_context(frame, direction, setup, sstv)

    assert result["alignment"] == "ALIGNED"
    assert result["stock_type"] == "UNCLASSIFIED"
