import pandas as pd

from scripts.course_entry_structure_v2_backtest import (
    _next_open_entry,
    _taiji_audit,
    _unbroken_boundary,
)


def test_prior_failed_taiji_leg_requires_reanchor_even_when_active_leg_is_not_failed():
    snapshot = {
        "taiji": {
            "state": "COPY_ARMED",
            "sequence": "LEG_4",
            "late_generation": False,
            "anchor": {"start_date": "2026-01-01"},
            "legs": [
                {"sequence_number": 1, "confirmed": True, "quality": None},
                {"sequence_number": 2, "confirmed": True, "quality": "FAILED", "role": "CORRECTION"},
                {"sequence_number": 3, "confirmed": True, "quality": "ACCEPTABLE"},
                {"sequence_number": 4, "confirmed": False, "quality": "ACCEPTABLE"},
            ],
        }
    }
    audit = _taiji_audit(snapshot)
    assert audit["reanchor_required"]
    assert audit["failed_completed_legs"][0]["sequence_number"] == 2


def test_leg_five_is_late_cycle():
    snapshot = {
        "taiji": {
            "state": "COPY_WEAKENING", "sequence": "LEG_5", "late_generation": True,
            "anchor": {}, "legs": [],
        }
    }
    assert _taiji_audit(snapshot)["late_cycle"]


def test_boundary_is_stale_after_a_prior_close_above_it():
    frame = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]),
        "close": [99.0, 101.0, 100.0, 102.0],
    })
    boundary = {"price": 100.0, "bar_date": "2026-01-01", "confirmed_date": "2026-01-01"}
    assert not _unbroken_boundary(frame, boundary)


def test_boundary_remains_fresh_until_current_signal_close():
    frame = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]),
        "close": [99.0, 100.0, 101.0],
    })
    boundary = {"price": 100.0, "bar_date": "2026-01-01", "confirmed_date": "2026-01-01"}
    assert _unbroken_boundary(frame, boundary)


def test_confirmed_signal_enters_at_next_open_not_intraday_trigger():
    signal = {"signal_close": 100.0, "defense": 95.0, "signal_atr": 3.0}
    next_row = pd.Series({"date": pd.Timestamp("2026-01-06"), "open": 100.5, "high": 110.0})
    entry, reason = _next_open_entry(signal, next_row, next_atr=3.0, actions=[])
    assert reason.startswith("TRIGGERED")
    assert entry is not None
    assert entry["entry_price"] == 100.5


def test_next_open_chase_and_actual_risk_are_enforced():
    signal = {"signal_close": 100.0, "defense": 94.0, "signal_atr": 3.0}
    next_row = pd.Series({"date": pd.Timestamp("2026-01-06"), "open": 102.0, "high": 110.0})
    entry, reason = _next_open_entry(signal, next_row, next_atr=3.0, actions=[])
    assert entry is None
    assert reason.startswith("OPEN_ABOVE_CHASE_CAP")
