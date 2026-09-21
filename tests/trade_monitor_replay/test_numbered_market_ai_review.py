from __future__ import annotations

from trade_monitor_replay.deterministic_state import (
    _apply_program_course_entry_quality_gate,
    _derive_numbered_market_state,
)


def _bull_q4_anchor_state() -> dict:
    return {
        "background_anchor": {"direction": "BULL", "status": "ACTIVE"},
        "quadrant_context": {
            "working_primary": "Q4",
            "working_candidates": ["Q4"],
            "working_trend_dynamics": "INCREASING",
        },
        "taiji_context": {"program_state": "CORRECTION_HELD"},
    }


def _two_plus_bull_methods() -> dict:
    return {
        "cclass_mode": "TAIJI_ORDERED",
        "numbered_market": {
            "status": "NUMBERED",
            "number": 2,
            "direction": "BULL",
            "restriction": "HIGH_NOISE_AI_REVIEW",
        },
    }


def test_two_plus_numbered_market_is_high_noise_ai_review_not_hard_ban() -> None:
    state = _derive_numbered_market_state(
        {},
        previous={
            "status": "NUMBERED",
            "number": 2,
            "direction": "BULL",
            "first_direction": "BULL",
        },
        as_of="2026-08-11T11:22:00+08:00",
    )

    assert state["restriction"] == "HIGH_NOISE_AI_REVIEW"


def test_two_plus_allows_non_false_break_when_other_course_gates_pass() -> None:
    candidate = {
        "setup_key": "q4-after-two-flips",
        "direction": "LONG",
        "candidate_source": "CONFIRMED_PULLBACK_ENDPOINT_N2",
    }

    qualified, audit = _apply_program_course_entry_quality_gate(
        candidate,
        anchor_state=_bull_q4_anchor_state(),
        course_method_state=_two_plus_bull_methods(),
        structure_events=[],
    )

    assert qualified is not None
    assert qualified["setup_key"] == candidate["setup_key"]
    assert audit["status"] == "EXECUTABLE"
    assert audit["reason_codes"] == ["ALL_PROGRAM_COURSE_ENTRY_GATES_PASSED"]
    assert audit["risk_flags"] == [
        "NUMBERED_MARKET_2_PLUS_HIGH_NOISE_AI_REVIEW"
    ]
    assert audit["requires_ai_quality_review"] is True
    assert audit["numbered_market"] == {
        "status": "NUMBERED",
        "number": 2,
        "direction": "BULL",
        "restriction": "HIGH_NOISE_AI_REVIEW",
        "risk_flags": ["NUMBERED_MARKET_2_PLUS_HIGH_NOISE_AI_REVIEW"],
        "requires_ai_quality_review": True,
    }


def test_two_plus_risk_flag_does_not_hide_an_independent_hard_rejection() -> None:
    candidate = {
        "setup_key": "bear-against-bull-controller",
        "direction": "SHORT",
        "candidate_source": "CONFIRMED_PULLBACK_ENDPOINT_N2",
    }

    qualified, audit = _apply_program_course_entry_quality_gate(
        candidate,
        anchor_state=_bull_q4_anchor_state(),
        course_method_state=_two_plus_bull_methods(),
        structure_events=[],
    )

    assert qualified is None
    assert "COUNTERTREND_TO_HIGHEST_ACTIVE_ANCHOR" in audit["reason_codes"]
    assert "NUMBERED_MARKET_DIRECTION_MISMATCH" in audit["reason_codes"]
    assert "NUMBERED_MARKET_2_PLUS_STRATEGY_RESTRICTED" not in audit["reason_codes"]
    assert audit["risk_flags"] == [
        "NUMBERED_MARKET_2_PLUS_HIGH_NOISE_AI_REVIEW"
    ]
