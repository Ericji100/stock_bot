import pytest

from scripts.v2_core_upper_space_evidence_r11 import build_upper_space_evidence_asof


def _high(identifier, pivot, confirmed, price, scale="LARGE"):
    return {
        "candidate_id": identifier, "pivot_date": pivot, "confirmed_on": confirmed,
        "price": price, "scale": scale,
    }


def _build(highs):
    return build_upper_space_evidence_asof(
        as_of="2023-05-04", signal_close=95.0,
        frozen_episode_stop=88.0, confirmed_highs=highs,
    )


def test_old_high_is_not_cut_off_by_a_120_day_window():
    result = _build([_high("OLD", "2021-04-09", "2021-04-12", 120.0)])
    assert result["confirmed_above_high_candidates"][0]["candidate_id"] == "OLD"
    assert result["upper_space_status"] == "CONFIRMED_HIGH_CANDIDATES_AVAILABLE"
    assert result["close_to_stop_risk_pct"] == round(7 / 95 * 100, 8)
    assert not result["fixed_rr_veto_applied"]
    assert not result["course_relevance_or_target_selected_by_program"]


def test_no_confirmed_high_above_is_unknown_not_unlimited_or_zero():
    result = _build([_high("BELOW", "2023-04-25", "2023-04-28", 94.5)])
    assert result["upper_space_status"] == "NO_CONFIRMED_ABOVE_HIGH_SPACE_UNKNOWN"
    assert result["confirmed_above_high_candidates"] == []
    assert not result["trade_permission_granted"]


def test_unconfirmed_future_high_or_invalid_stop_is_rejected():
    with pytest.raises(ValueError):
        _build([_high("FUTURE", "2023-05-03", "2023-05-08", 100.0)])
    with pytest.raises(ValueError):
        build_upper_space_evidence_asof(
            as_of="2023-05-04", signal_close=95.0,
            frozen_episode_stop=95.0, confirmed_highs=[],
        )
