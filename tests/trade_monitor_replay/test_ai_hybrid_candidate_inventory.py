from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from trade_monitor_replay.deterministic_state import (
    _build_ai_hybrid_q2_q4_candidate_inventory,
    _q2_failed_reverse_entry_candidate,
)
from trade_monitor_replay.runner import _prompt_ledger_view


TAIPEI = ZoneInfo("Asia/Taipei")
AS_OF = datetime(2026, 8, 25, 10, 0, tzinfo=TAIPEI)


def _candidate(*, setup_key: str, source: str, stop_source_price: float = 90.0) -> dict:
    candidate = {
        "setup_key": setup_key,
        "setup_name": setup_key,
        "direction": "LONG",
        "stage": "ARMED",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 100.0,
        "trigger_time": "2026-08-25T09:58:00+08:00",
        "stop_source_time": "2026-08-25T09:57:00+08:00",
        "stop_source_price": stop_source_price,
        "first_seen_at": "2026-08-25T09:59:00+08:00",
        "valid_bars": 5,
        "candidate_source": source,
        "entry_strategy": (
            "Q2_FAILED_COUNTERTREND_REVERSAL"
            if source == "Q2_FAILED_REVERSE_CANDIDATE"
            else "Q4_PULLBACK_CONTINUATION"
        ),
        "stop_price": 89.0,
        "behavior_max_wait_bars": 3,
        "behavior_trigger_level": 100.0,
        "behavior_obstacles": [],
    }
    if source == "Q2_FAILED_REVERSE_CANDIDATE":
        candidate.update(
            {
                "q2_reverse_candidate_ref": "reverse-bear",
                "q2_controller_ref": "anchor-bull",
                "q2_controller_defense_ref": "def-bull",
            }
        )
    if source == "FALSE_BREAK_RECLAIM":
        candidate.update(
            {
                "facts_cutoff": "2026-08-25T09:59:00+08:00",
                "facts_hash": "FACTS-" + "a" * 64,
            }
        )
    return candidate


def _anchor_state() -> dict:
    defense = {"id": "def-bull", "state": "ACTIVE"}
    return {
        "background_anchor": {
            "id": "anchor-bull",
            "direction": "BULL",
            "status": "ACTIVE",
            "defense": defense,
        },
        "child_anchor": None,
        "reverse_candidate": {
            "id": "reverse-bear",
            "direction": "BEAR",
            "status": "QUALIFIED",
            "current_extreme_price": 90.0,
        },
        "working_leg": {"direction": "BEAR"},
        # These program interpretations deliberately conflict with both
        # candidate families.  They are advisory in AI_HYBRID, not hard facts.
        "quadrant_context": {
            "working_primary": "Q1",
            "working_candidates": ["Q1"],
            "working_trend_dynamics": "INCREASING",
        },
        "taiji_context": {"program_state": "COPY_IN_PROGRESS"},
        "dow_context": {"large_bull_defense": defense},
    }


def _course_methods() -> dict:
    return {
        "cclass_mode": "TAIJI_ORDERED",
        "numbered_market": {
            "status": "NUMBERED",
            "number": 0,
            "direction": "BULL",
        },
    }


def test_ai_inventory_keeps_all_hard_legal_q2_q4_candidates_despite_interpretive_conflicts() -> None:
    q4 = _candidate(setup_key="q4", source="CONFIRMED_PULLBACK_ENDPOINT_N2")
    q2 = _candidate(setup_key="q2", source="Q2_FAILED_REVERSE_CANDIDATE")

    inventory, audits = _build_ai_hybrid_q2_q4_candidate_inventory(
        [q4, q2],
        expected=AS_OF,
        anchor_state=_anchor_state(),
        course_method_state=_course_methods(),
        structure_events=[],
    )

    assert [item["setup_key"] for item in inventory] == ["q4", "q2"]
    assert all(item["decision_authority"] == "AI_HYBRID" for item in inventory)
    audit_by_key = {item["setup_key"]: item for item in audits}
    assert audit_by_key["q4"]["hard_gate_status"] == "PASSED"
    assert audit_by_key["q4"]["interpretive_status"] == "CONFLICTING"
    assert any("CONTINUATION_WORKING_QUADRANT" in code for code in audit_by_key["q4"]["interpretive_reason_codes"])
    assert audit_by_key["q2"]["hard_gate_status"] == "PASSED"
    assert any("Q2_REVERSAL_WORKING_QUADRANT" in code for code in audit_by_key["q2"]["interpretive_reason_codes"])


def test_ai_inventory_rejects_a_false_break_whose_defense_ref_is_not_current() -> None:
    false_break = {
        **_candidate(setup_key="false-break", source="FALSE_BREAK_RECLAIM"),
        "entry_strategy": "Q2_FALSE_BREAK_RECLAIM",
        "source_event_id": "event-1",
        "source_type": "DOW_DEFENSE",
        "source_id": "obsolete-defense",
    }
    inventory, audits = _build_ai_hybrid_q2_q4_candidate_inventory(
        [false_break],
        expected=AS_OF,
        anchor_state=_anchor_state(),
        course_method_state=_course_methods(),
        structure_events=[
            {
                "id": "event-1",
                "event_type": "FALSE_BREAK_RECLAIM",
                "source_type": "DOW_DEFENSE",
                "source_id": "obsolete-defense",
            }
        ],
    )

    assert inventory == []
    assert audits[0]["hard_gate_status"] == "REJECTED"
    assert "FALSE_BREAK_DOW_DEFENSE_NOT_CURRENT_COURSE_SLOT" in audits[0]["hard_reason_codes"]


def test_ai_inventory_exposes_q2_false_break_when_controller_alignment_needs_ai_judgment() -> None:
    false_break = {
        **_candidate(setup_key="q2-reclaim", source="FALSE_BREAK_RECLAIM"),
        "entry_strategy": "Q2_FALSE_BREAK_RECLAIM",
        "source_event_id": "event-q2",
        "source_type": "OR15_LOW",
        "source_id": "or15-low",
    }
    bear_state = _anchor_state()
    bear_state["background_anchor"] = {
        "id": "stale-bear-controller",
        "direction": "BEAR",
        "status": "ACTIVE",
        "defense": {"id": "bear-defense", "state": "ACTIVE"},
    }
    bear_state["dow_context"] = {
        "large_bear_defense": {"id": "bear-defense", "state": "ACTIVE"},
    }

    inventory, audits = _build_ai_hybrid_q2_q4_candidate_inventory(
        [false_break],
        expected=AS_OF,
        anchor_state=bear_state,
        course_method_state=_course_methods(),
        structure_events=[
            {
                "id": "event-q2",
                "event_type": "FALSE_BREAK_RECLAIM",
                "direction": "BULL",
                "source_type": "OR15_LOW",
                "source_id": "or15-low",
            }
        ],
    )

    assert [item["setup_key"] for item in inventory] == ["q2-reclaim"]
    assert audits[0]["hard_gate_status"] == "PASSED"
    assert audits[0]["interpretive_status"] == "CONFLICTING"
    assert set(audits[0]["interpretive_reason_codes"]) >= {
        "COUNTERTREND_TO_HIGHEST_ACTIVE_ANCHOR",
        "FALSE_BREAK_NOT_ALIGNED_WITH_RETAINED_COURSE_CONTROLLER",
    }


def test_ai_prompt_view_exposes_every_armable_candidate_but_redacts_program_interpretation() -> None:
    q4 = _candidate(setup_key="q4", source="CONFIRMED_PULLBACK_ENDPOINT_N2")
    q2 = _candidate(setup_key="q2", source="Q2_FAILED_REVERSE_CANDIDATE")
    ledger = {
        "trade_levels": {
            "continuation_arm_candidate": q4,
            "q4_continuation_candidate": q4,
            "q2_failed_reverse_candidate": q2,
            "ai_candidate_inventory": [q4, q2],
            "ai_armable_setup_keys": ["q4", "q2"],
            "ai_candidate_interpretive_audits": [
                {
                    "setup_key": "q4",
                    "interpretive_reason_codes": ["CONTINUATION_TAIJI_COPY_FAILED"],
                }
            ],
            "course_entry_quality_audit": {
                "status": "OBSERVATION_ONLY",
                "reason_codes": ["CONTINUATION_TAIJI_COPY_FAILED"],
            },
            "course_filtered_candidate": q2,
            "selected_entry_candidate_before_course_gate": q2,
        },
        "anchor_lifecycle": {
            "quadrant_context": {"working_primary": "Q1", "working_trend_dynamics": "INCREASING"},
            "taiji_context": {"program_state": "COPY_FAILED", "engine_mode": "RESETTING"},
        },
        "course_method_state": {"cclass_mode": "RESETTING"},
    }

    view = _prompt_ledger_view(ledger, ai_hybrid=True)
    levels = view["trade_levels"]

    assert [item["setup_key"] for item in levels["ai_candidate_inventory"]] == ["q4", "q2"]
    assert levels["ai_armable_setup_keys"] == ["q2", "q4"]
    assert levels["ai_executable_setup_keys"] == ["q2", "q4"]
    assert "ai_candidate_interpretive_audits" not in levels
    assert "course_entry_quality_audit" not in levels
    assert "course_filtered_candidate" not in levels
    assert "selected_entry_candidate_before_course_gate" not in levels
    assert "course_method_state" not in view
    assert "working_primary" not in view["anchor_lifecycle"]["quadrant_context"]
    assert "program_state" not in view["anchor_lifecycle"]["taiji_context"]
    assert levels["q4_continuation_candidate"]["stage"] == "ARMED"
    assert levels["q2_failed_reverse_candidate"]["stage"] == "ARMED"


def test_q2_failed_reverse_can_skip_only_interpretive_prefilter_for_ai_inventory() -> None:
    continuation = _candidate(
        setup_key="q4-source",
        source="CONFIRMED_PULLBACK_ENDPOINT_N2",
        stop_source_price=90.0,
    )
    anchor_state = _anchor_state()
    methods = _course_methods()

    assert _q2_failed_reverse_entry_candidate(
        continuation,
        anchor_state=anchor_state,
        course_method_state=methods,
    ) is None

    candidate = _q2_failed_reverse_entry_candidate(
        continuation,
        anchor_state=anchor_state,
        course_method_state=methods,
        apply_interpretive_prefilter=False,
    )
    assert candidate is not None
    assert candidate["candidate_source"] == "Q2_FAILED_REVERSE_CANDIDATE"
    assert candidate["entry_strategy"] == "Q2_FAILED_COUNTERTREND_REVERSAL"
