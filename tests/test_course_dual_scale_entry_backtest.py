import pandas as pd

from scripts.course_dual_scale_entry_backtest import (
    _execute_next_session,
    _select_boundary,
)


def test_select_boundary_prefers_nearest_causal_large_level():
    selected = _select_boundary(
        platform_high=105.0,
        confirmed_large_highs=[
            {
                "price": 101.0,
                "source": "P2_HIGH（已確認大級樞紐高）",
                "bar_date": "2026-01-01",
                "confirmed_date": "2026-01-10",
            }
        ],
        close=100.0,
        atr=3.0,
    )
    assert selected is not None
    assert selected["price"] == 101.0
    assert selected["confirmed_date"] == "2026-01-10"


def test_select_boundary_rejects_far_resistance():
    assert _select_boundary(
        platform_high=110.0,
        confirmed_large_highs=[],
        close=100.0,
        atr=2.0,
    ) is None


def test_next_session_touch_uses_small_defense_and_actual_fill_risk():
    plan = {
        "armed_date": "2026-01-05",
        "trigger": 100.0,
        "chase_cap": 101.0,
        "defense": 95.0,
        "boundary": 100.0,
        "dual_resonance": True,
    }
    row = pd.Series({
        "date": pd.Timestamp("2026-01-06"),
        "open": 99.0,
        "high": 101.0,
    })
    proposal, reason = _execute_next_session(
        plan=plan, next_row=row, next_atr=3.0, actions=[]
    )
    assert reason.startswith("TRIGGERED")
    assert proposal is not None
    assert proposal["entry_price"] == 100.0
    assert proposal["initial_defense"] == 95.0
    assert proposal["actual_risk_pct"] == 5.0


def test_next_session_rejects_gap_that_makes_actual_risk_too_wide():
    plan = {
        "armed_date": "2026-01-05",
        "trigger": 100.0,
        "chase_cap": 112.0,
        "defense": 95.0,
        "boundary": 100.0,
        "dual_resonance": False,
    }
    row = pd.Series({
        "date": pd.Timestamp("2026-01-06"),
        "open": 110.0,
        "high": 111.0,
    })
    proposal, reason = _execute_next_session(
        plan=plan, next_row=row, next_atr=4.0, actions=[]
    )
    assert proposal is None
    assert reason.startswith("ACTUAL_RISK_TOO_WIDE")
