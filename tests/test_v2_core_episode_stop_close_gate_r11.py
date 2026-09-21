import pytest

from scripts.v2_core_episode_stop_close_gate_r11 import evaluate_stop_close_asof
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR, load_json


REVIEW_ID = "FP-a956dbdf5f936dfdb64d45ec"


def _daily():
    packet = load_json(ARTIFACT_DIR / "legacy_anchor_alignment_input_packets_candidate_r1" / f"{REVIEW_ID}.json")
    return packet["daily_context_to_as_of"]


def test_intraday_low_does_not_close_out_selected_39_5_stop():
    result = evaluate_stop_close_asof(
        _daily(), stop_price=39.5,
        stop_confirmation_date="2023-08-09", signal_date="2023-08-31",
    )
    assert result["status"] == "ACTIVE_BY_DAILY_CLOSE"
    assert "2023-08-28" in result["prior_intraday_breach_without_close_dates"]
    assert "2023-08-29" in result["prior_intraday_breach_without_close_dates"]
    assert not result["trade_permission_granted"]


def test_legacy_40_25_stop_had_prior_closing_breach():
    result = evaluate_stop_close_asof(
        _daily(), stop_price=40.25,
        stop_confirmation_date="2023-08-21", signal_date="2023-08-31",
    )
    assert result["status"] == "PRIOR_CLOSE_BREACH_REQUIRES_NEW_EPISODE"
    assert result["first_prior_close_breach"] == {"date": "2023-08-28", "close": 39.65}


def test_rejects_future_or_unordered_packet():
    with pytest.raises(ValueError):
        evaluate_stop_close_asof(
            [{"date": "2023-08-31", "close": 43.35, "low": 41.65},
             {"date": "2023-08-29", "close": 40.65, "low": 38.05}],
            stop_price=39.5, stop_confirmation_date="2023-08-21", signal_date="2023-08-31",
        )
