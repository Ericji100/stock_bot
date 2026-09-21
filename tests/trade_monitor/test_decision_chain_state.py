from __future__ import annotations

import copy

import pytest

from trade_monitor.decision_chain_state import (
    DecisionChainStateError,
    decision_chain_notification_reasons,
    empty_decision_chain_context,
    validate_decision_chain_context,
    validate_decision_chain_transition,
)


T0 = "2026-09-02T08:59:00+08:00"
T1 = "2026-09-02T09:01:00+08:00"
T2 = "2026-09-02T09:03:00+08:00"


def _opening_context(as_of: str = T1) -> dict:
    context = empty_decision_chain_context(as_of=as_of)
    context.update(
        {
            "process_stage": "OPENING_EVIDENCE",
            "stage_changed_at": as_of,
            "assessment_changed_at": as_of,
        }
    )
    context["opening_context"] = {
        "cash_gap_source": "VERIFIED_CASH",
        "gap_direction": "BULL",
        "gap_size": "MODERATE",
        "previous_cash_close": 46000.0,
        "cash_open": 46080.0,
        "gap_observed_at": as_of,
        "opening_direction_relation": "SAME",
        "pre_cash_open_quality": "CLEAN",
        "first_endpoint_side": "NONE",
        "first_endpoint_style": "PENDING",
        "endpoint_first_seen_at": None,
        "endpoint_confirmed_at": None,
        "evidence_reason": "可靠現貨資料顯示適中向上跳空，期貨開盤第一段同向且影線少。",
    }
    context["structure_context"].update(
        {
            "analysis_lens": "OPENING_EVIDENCE_ONLY",
            "lens_changed_at": as_of,
            "structure_reason": "尚未形成兩腳，只保存開盤證據。",
        }
    )
    return context


def _graded_setup(as_of: str = T2) -> dict:
    context = _opening_context(as_of=as_of)
    context.update(
        {
            "process_stage": "SETUP_EVALUATION",
            "stage_changed_at": as_of,
            "assessment_changed_at": as_of,
        }
    )
    context["structure_context"] = {
        "confirmed_leg_count": 4,
        "analysis_lens": "QUADRANT_PRIMARY",
        "lens_changed_at": as_of,
        "family_dna": "CONSISTENT",
        "confluences": ["SAME_QUADRANT", "COPY_CORRECTION"],
        "structure_reason": "四腳結構清楚，以四象限為主，太極複製／修正提供同向確認。",
    }
    context["opportunity_context"] = {
        "mapped_pattern": "TREND_PULLBACK_CONTINUATION",
        "probability_evidence": "HIGH",
        "payoff_evidence": "MEDIUM",
        "course_grade": "B_CANDIDATE",
        "grade_reason": "方向、定錨與共振一致，停損可定義但最近障礙令賠率只有中等。",
        "expected_behavior": "觸發後三至五根已收盤 K 應離開反彈區並恢復原方向推進。",
        "max_wait_bars": 5,
        "behavior_invalidation": "五根內無推進或重新收回觸發區即評估退出。",
        "recovery_round_policy": "NOT_APPLICABLE",
    }
    return context


def test_empty_decision_chain_is_valid_and_complete() -> None:
    context = validate_decision_chain_context(empty_decision_chain_context(as_of=T0), as_of=T0)
    assert context["process_stage"] == "UNDEFINED"
    assert context["opening_context"]["cash_gap_source"] == "UNAVAILABLE"
    assert context["structure_context"]["confluences"] == []
    assert context["opportunity_context"]["mapped_pattern"] == "NONE"


def test_verified_cash_gap_requires_both_source_prices() -> None:
    context = _opening_context()
    context["opening_context"]["cash_open"] = None
    with pytest.raises(DecisionChainStateError, match="both source prices"):
        validate_decision_chain_context(context, as_of=T1)


def test_unavailable_gap_cannot_invent_direction_or_size() -> None:
    context = empty_decision_chain_context(as_of=T0)
    context["opening_context"]["gap_direction"] = "BULL"
    with pytest.raises(DecisionChainStateError, match="must not contain inferred values"):
        validate_decision_chain_context(context, as_of=T0)


def test_quadrant_primary_is_allowed_after_two_confirmed_legs_when_clearer() -> None:
    context = _graded_setup()
    context["structure_context"]["confirmed_leg_count"] = 2
    validated = validate_decision_chain_context(context, as_of=T2)
    assert validated["structure_context"]["analysis_lens"] == "QUADRANT_PRIMARY"


def test_quadrant_primary_still_requires_two_confirmed_legs() -> None:
    context = _graded_setup()
    context["structure_context"]["confirmed_leg_count"] = 1
    with pytest.raises(DecisionChainStateError, match="at least two"):
        validate_decision_chain_context(context, as_of=T2)


def test_structure_building_can_keep_opening_lens_until_two_legs_exist() -> None:
    context = _opening_context(T2)
    context["process_stage"] = "STRUCTURE_BUILDING"
    context["stage_changed_at"] = T2
    context["assessment_changed_at"] = T2

    validated = validate_decision_chain_context(context, as_of=T2)

    assert validated["structure_context"]["confirmed_leg_count"] == 0
    assert validated["structure_context"]["analysis_lens"] == "OPENING_EVIDENCE_ONLY"


def test_late_or_resetting_clears_stale_opening_only_lens() -> None:
    context = _opening_context(T2)
    context["process_stage"] = "LATE_OR_RESETTING"
    context["stage_changed_at"] = T2
    context["assessment_changed_at"] = T2

    validated = validate_decision_chain_context(context, as_of=T2)

    assert validated["structure_context"]["analysis_lens"] == "UNDEFINED"
    assert validated["structure_context"]["lens_changed_at"] is None


def test_course_grade_is_two_axis_and_requires_a_selected_strategy_method() -> None:
    context = _graded_setup()
    validated = validate_decision_chain_context(context, as_of=T2)
    assert validated["opportunity_context"]["course_grade"] == "B_CANDIDATE"
    context["opportunity_context"]["mapped_pattern"] = "NONE"
    with pytest.raises(DecisionChainStateError, match="selected strategy method"):
        validate_decision_chain_context(context, as_of=T2)


def test_course_grade_can_select_a_course_method_outside_original_four() -> None:
    context = _graded_setup()
    context["opportunity_context"]["mapped_pattern"] = "TAIJI_COPY_AFTER_CORRECTION"
    validated = validate_decision_chain_context(context, as_of=T2)
    assert validated["opportunity_context"]["mapped_pattern"] == "TAIJI_COPY_AFTER_CORRECTION"


def test_first_endpoint_sample_cannot_backfill_or_rewrite() -> None:
    before = _opening_context(T1)
    after = copy.deepcopy(before)
    after["as_of"] = T2
    after["process_stage"] = "FIRST_ENDPOINT_SAMPLE"
    after["stage_changed_at"] = T2
    after["assessment_changed_at"] = T2
    after["opening_context"].update(
        {
            "first_endpoint_side": "DH",
            "first_endpoint_style": "TRUE_LIKE",
            "endpoint_first_seen_at": T0,
            "endpoint_confirmed_at": T2,
        }
    )
    with pytest.raises(DecisionChainStateError, match="cannot backfill"):
        validate_decision_chain_transition(before, after, prior_as_of=T1, current_as_of=T2)


def test_material_decision_changes_force_notification() -> None:
    before = empty_decision_chain_context(as_of=T0)
    after = _opening_context(T1)
    reasons = decision_chain_notification_reasons(before, after)
    assert "X 決策鏈階段改變" in reasons
    assert "跳空／開盤品質證據改變" in reasons
    assert "主要判讀工具／家族 DNA／共振改變" in reasons
