from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.course_tg_enlightenment_ai_v2_backtest import (
    DECISIONS_PATH,
    RESULT_PATH,
    _distribution,
    _major_pivots,
)


def test_distribution_bins_are_exclusive_and_complete() -> None:
    values = [-25, -20, -15, -10, -5, 0, 5, 10, 15, 20, 50, 100]
    result = _distribution(values)
    assert sum(result.values()) == len(values)
    assert result["≤-20%"] == 2
    assert result["≥100%"] == 1


def test_major_pivot_is_visible_only_after_right_side_confirmation() -> None:
    frame = pd.DataFrame({"low": [9, 8, 7, 8, 9, 10, 11]})
    pivots = _major_pivots(frame, radius=2)
    assert pivots.iloc[:4].isna().all()
    assert pivots.iloc[4] == 7


def test_saved_decisions_never_precede_monitor_date() -> None:
    if not DECISIONS_PATH.exists():
        return
    payload = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
    for events in payload["approved_events"].values():
        assert all(event["signal_date"] >= event["monitor_on"] for event in events)


def test_saved_backtest_reconciles_and_respects_tranche_limits() -> None:
    if not RESULT_PATH.exists():
        return
    payload = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    for key, variant in payload["variants"].items():
        episodes = [episode for stock in variant["stocks"] for episode in stock["episodes"]]
        assert round(sum(float(episode["net_pnl"]) for episode in episodes), 2) == variant["summary"]["net_pnl"]
        maximum = 3 if "PLUS_2" in key else 1
        assert all(1 <= len(episode["tranches"]) <= maximum for episode in episodes)
