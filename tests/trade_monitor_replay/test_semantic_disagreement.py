from __future__ import annotations

from trade_monitor_replay.semantic_disagreement import (
    attach_forward_outcomes,
    build_semantic_comparison,
)


def _analysis(
    *,
    small_anchor: str | None,
    working_quadrant: str,
    event_ref: str | None,
    action: str = "NONE",
) -> dict[str, object]:
    return {
        "large_trend": {"classification": "盤整"},
        "current_trend": {"classification": "偏多"},
        "message_type": "OBSERVATION",
        "message_direction": "BULL",
        "course_reading": {
            "large_anchor_ref": None,
            "small_anchor_ref": small_anchor,
            "working_anchor_ref": "working-ai",
            "reverse_anchor_candidate_ref": None,
            "large_defense_ref": None,
            "small_defense_ref": "defense-ai",
            "structure_event_ref": event_ref,
            "controlling_grade": "SMALL",
            "grade_relation": "ONLY_SMALL",
            "background_quadrant": "TRANSITION",
            "working_quadrant": working_quadrant,
            "primary_quadrant_candidate": "Q4",
            "secondary_quadrant_candidate": "Q1",
            "background_trend_dynamics": "UNCLEAR",
            "background_volatility_dynamics": "UNSTABLE",
            "working_trend_dynamics": "INCREASING",
            "working_volatility_dynamics": "CONTRACTING",
            "cclass_mode": "TAIJI_ORDERED",
            "x_stage": "OPPORTUNITY_GRADING",
            "focus_methods": ["QUADRANT", "TAIJI"],
            "main_strategy": "多方Q4候選",
            "setup_stage": "FORMING",
        },
        "scenario": {
            "bull_probability": 50,
            "range_probability": 35,
            "bear_probability": 15,
        },
        "action": {
            "position_action": action,
            "entry_rejection_reason": "GRADE_CONFLICT" if action == "NONE" else "NONE",
            "setup_key": None,
        },
    }


def test_comparison_records_disagreement_without_choosing_winner() -> None:
    raw = _analysis(
        small_anchor=None,
        working_quadrant="Q4",
        event_ref=None,
    )
    validated = _analysis(
        small_anchor="child-program-candidate",
        working_quadrant="Q4",
        event_ref=None,
    )
    ledger = {
        "anchor_control": {
            "active_background_anchor_ref": None,
            "active_child_anchor_ref": "child-program-candidate",
            "working_leg_ref": "working-program-candidate",
            "reverse_anchor_candidate_ref": None,
            "large_defense_ref": None,
            "small_defense_ref": "defense-program-candidate",
        },
        "anchor_lifecycle": {
            "quadrant_context": {
                "authority": "EVIDENCE_ONLY",
                "working_primary": "Q1",
                "working_trend_dynamics": "INCREASING",
                "working_volatility_dynamics": "EXPANDING",
            },
            "dow_context": {"large_state": "UNDEFINED", "small_state": "BULL"},
        },
        "course_method_state": {
            "cclass_mode": "YIZHI_MOMENTUM",
            "x_stage": "ENTRY_EXECUTION",
        },
        "structure_events": [
            {
                "id": "event-upgrade",
                "event_type": "GRADE_UPGRADE",
                "direction": "BULL",
                "first_seen_at": "2026-08-11T10:28:00+08:00",
            }
        ],
    }

    comparison = build_semantic_comparison(
        raw_analysis=raw,
        validated_analysis=validated,
        ledger=ledger,
        evidence_events=[{"event_id": "event-upgrade"}],
        entry_gate={"status": "ENTRY_ELIGIBLE", "direction": "LONG"},
        stage="day",
        bar_time="2026-08-11T10:28:00+08:00",
        artifact_key="day-20260811-1028",
        analysis_source="codex_live",
    )

    assert comparison["status"] == "DISAGREEMENT"
    assert comparison["winner"] is None
    assert comparison["posthoc_outcome"] is None
    assert comparison["causal_cutoff"] == "2026-08-11T10:28:00+08:00"
    assert "small_anchor_ref" in comparison["validator_changed_semantic_fields"]
    kinds = {item["kind"] for item in comparison["differences"]}
    assert "AI_INTERPRETATION_DIFFERS_FROM_PROGRAM_BASELINE" in kinds
    assert "AI_DID_NOT_SELECT_NEW_PROGRAM_EVENT_CANDIDATE" in kinds
    assert "AI_REJECTED_ENTRY_ELIGIBLE_CANDIDATE" in kinds
    assert all(item["verdict"] == "UNRESOLVED" for item in comparison["differences"])


def test_posthoc_outcomes_use_only_bars_after_frozen_cutoff_and_do_not_pick_winner() -> None:
    comparison = {
        "comparison_id": "SC-example",
        "causal_cutoff": "2026-08-11T09:00:00+08:00",
        "winner": None,
        "posthoc_outcome": None,
    }
    bars = [
        {"bar_time": "2026-08-11T08:59:00+08:00", "high": 101, "low": 98, "close": 99},
        {"bar_time": "2026-08-11T09:00:00+08:00", "high": 102, "low": 99, "close": 100},
        {"bar_time": "2026-08-11T09:01:00+08:00", "high": 104, "low": 99, "close": 103},
        {"bar_time": "2026-08-11T09:02:00+08:00", "high": 106, "low": 101, "close": 105},
        {"bar_time": "2026-08-11T09:03:00+08:00", "high": 105, "low": 97, "close": 98},
    ]

    result = attach_forward_outcomes(comparison, bars, horizons=(2, 3, 5))

    two = result["posthoc_outcome"]["horizons"]["2"]
    three = result["posthoc_outcome"]["horizons"]["3"]
    assert two["baseline_close"] == 100
    assert two["end_close"] == 105
    assert two["bull_mfe_points"] == 6
    assert two["bull_mae_points"] == 1
    assert three["end_close"] == 98
    assert three["bull_mae_points"] == 3
    assert result["posthoc_outcome"]["horizons"]["5"]["status"] == "INSUFFICIENT_BARS"
    assert result["winner"] is None
    assert result["posthoc_outcome"]["winner"] is None
    assert comparison["posthoc_outcome"] is None


def test_posthoc_outcome_reports_missing_future_without_changing_original() -> None:
    comparison = {
        "comparison_id": "SC-final-bar",
        "causal_cutoff": "2026-08-11T13:30:00+08:00",
        "winner": None,
        "posthoc_outcome": None,
    }
    bars = [
        {"bar_time": "2026-08-11T13:30:00+08:00", "high": 100, "low": 99, "close": 100}
    ]

    result = attach_forward_outcomes(comparison, bars)

    assert result["posthoc_outcome"]["status"] == "INSUFFICIENT_FUTURE_BARS"
    assert comparison["posthoc_outcome"] is None
