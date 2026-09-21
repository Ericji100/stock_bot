import pandas as pd

from scripts.course_entry_variant_backtest import _decision_target, _entry_quality, _raw_level


def test_raw_level_inverts_economic_basis():
    assert _raw_level(52.0, (2.0, 2.0)) == 25.0


def test_actual_risk_gate_uses_fill_not_armed_close():
    accepted, reason, metrics = _entry_quality(
        economic_entry=103.0, defense=91.0, atr=5.0, measured_target=130.0,
        risk_check=True, rr_min=None,
    )
    assert not accepted
    assert reason.startswith("ACTUAL_RISK_TOO_WIDE")
    assert metrics["risk_pct"] > 10.0


def test_rr2_gate_is_pre_entry_arithmetic():
    accepted, reason, metrics = _entry_quality(
        economic_entry=100.0, defense=95.0, atr=3.0, measured_target=109.0,
        risk_check=True, rr_min=2.0,
    )
    assert not accepted
    assert reason.startswith("HEADROOM_BELOW_2R")
    assert metrics["reward_risk_proxy"] == 1.8


def test_entry_quality_accepts_bounded_risk_and_two_r_headroom():
    accepted, reason, metrics = _entry_quality(
        economic_entry=100.0, defense=95.0, atr=3.0, measured_target=110.0,
        risk_check=True, rr_min=2.0,
    )
    assert accepted
    assert reason is None
    assert metrics["reward_risk_proxy"] == 2.0


def test_decision_target_uses_only_rows_through_signal():
    frame = pd.DataFrame({"high": [10.0, 11.0, 99.0], "low": [8.0, 9.0, 1.0],
                          "close": [9.0, 10.5, 50.0]})
    assert _decision_target(frame, 1, 10.0) == 13.5
