from __future__ import annotations

import copy

import pytest

from trade_monitor.cclass_state import (
    CClassStateError,
    cclass_notification_reasons,
    empty_cclass_context,
    validate_cclass_context,
    validate_cclass_transition,
)


T0 = "2026-09-02T10:00:00+08:00"
T1 = "2026-09-02T10:01:00+08:00"
T2 = "2026-09-02T10:02:00+08:00"


def _active_taiji(as_of: str = T1) -> dict:
    context = empty_cclass_context(as_of=as_of)
    context.update(
        {
            "engine_mode": "TAIJI_ORDERED",
            "engine_changed_at": as_of,
            "order_state": "ORDERED",
            "order_reason": "多方定錨後的推進與修正可因果辨識。",
        }
    )
    context["taiji_context"] = {
        "dynasty_id": "DYNASTY-BULL-1",
        "anchor_id": "ANCHOR-SMALL-BULL-1",
        "active_leg_id": "LEG-1",
        "legs": [
            {
                "leg_id": "LEG-1",
                "sequence": "ANCHOR_1",
                "role": "ANCHOR",
                "direction": "BULL",
                "status": "FORMING",
                "start_bar_time": as_of,
                "extreme_bar_time": as_of,
                "first_seen_at": as_of,
                "confirmed_at": None,
                "terminal_at": None,
                "parent_leg_id": None,
                "copy_quality": "NOT_APPLICABLE",
                "correction_quality": "NOT_APPLICABLE",
                "amplitude_relation": "NOT_APPLICABLE",
                "duration_relation": "NOT_APPLICABLE",
                "slope_relation": "NOT_APPLICABLE",
                "cleanliness_relation": "NOT_APPLICABLE",
                "destructive_relation": "NOT_APPLICABLE",
                "notes": ["定錨段形成中，尚未完成後代比較。"],
            }
        ],
        "anchor_time_status": "FRESH",
        "previous_context_alignment": "UNDEFINED",
        "assessment_reason": "第一段定錨形成中。",
        "assessment_changed_at": as_of,
    }
    context["thesis_context"] = {
        "bias": "CONDITIONAL",
        "current_reason": "多方定錨形成中，但尚未完成原四型態觸發。",
        "hold_condition": "定錨確認且後續修正守住。",
        "downgrade_condition": "定錨品質轉弱或時間效力衰減。",
        "neutralize_condition": "多方定錨失效或同級防線收盤跌破。",
        "reverse_condition": "反向防線破壞、反向錨確認並完成第一次右左。",
        "changed_at": as_of,
    }
    return context


def _active_momentum(as_of: str = T1) -> dict:
    context = empty_cclass_context(as_of=as_of)
    context.update(
        {
            "engine_mode": "YIZHI_MOMENTUM",
            "engine_changed_at": as_of,
            "order_state": "ORDERED",
            "order_reason": "兩至三根同向擴張 K 已造成結構破壞。",
        }
    )
    context["momentum_context"] = {
        "episode_id": "MOM-BULL-1",
        "stage": "CENTRIFUGAL_CONFIRMED",
        "direction": "BULL",
        "first_seen_at": as_of,
        "stage_changed_at": as_of,
        "location": "BOUNDARY",
        "quality": "ACCEPTABLE",
        "dragon_grade": "NOT_APPLICABLE",
        "distance_phase": "EARLY",
        "bar_expansion": "EXPANDING",
        "pivot_pressure": "NONE",
        "gate_balance": "NOT_APPLICABLE",
        "mapped_pattern": "NONE",
        "failure_reason": "若停止創高且收回發動區，動能模式失效。",
    }
    context["thesis_context"] = {
        "bias": "BULLISH",
        "current_reason": "多方離心力已確認，逆勢猜頂暫停。",
        "hold_condition": "持續創高且不收回發動區。",
        "downgrade_condition": "樞紐增加、斜率下降或進入中後段。",
        "neutralize_condition": "停止創高並收回發動區。",
        "reverse_condition": "反向防線、反向錨及第一次右左全部確認。",
        "changed_at": as_of,
    }
    return context


def test_empty_context_is_valid_and_complete() -> None:
    context = validate_cclass_context(empty_cclass_context(as_of=T0), as_of=T0)
    assert context["engine_mode"] == "UNDEFINED"
    assert context["taiji_context"]["legs"] == []
    assert context["momentum_context"]["stage"] == "NONE"
    assert context["thesis_context"]["bias"] == "UNDEFINED"


def test_taiji_context_preserves_all_five_quality_dimensions() -> None:
    context = validate_cclass_context(_active_taiji(), as_of=T1)
    leg = context["taiji_context"]["legs"][0]
    assert set(
        key
        for key in leg
        if key.endswith("_relation")
    ) == {
        "amplitude_relation",
        "duration_relation",
        "slope_relation",
        "cleanliness_relation",
        "destructive_relation",
    }


def test_yizhi_mode_requires_a_causal_momentum_episode() -> None:
    context = empty_cclass_context(as_of=T1)
    context["engine_mode"] = "YIZHI_MOMENTUM"
    context["engine_changed_at"] = T1
    with pytest.raises(CClassStateError, match="active momentum stage"):
        validate_cclass_context(context, as_of=T1)


def test_non_dragon_stage_clears_stale_dragon_grade() -> None:
    context = _active_momentum()
    context["momentum_context"]["stage"] = "EXHAUSTION_WARNING"
    context["momentum_context"]["dragon_grade"] = "CROOKED"

    validated = validate_cclass_context(context, as_of=T1)

    assert validated["momentum_context"]["dragon_grade"] == "NOT_APPLICABLE"


def test_new_taiji_leg_cannot_backfill_first_seen_time() -> None:
    before = empty_cclass_context(as_of=T0)
    after = _active_taiji(as_of=T1)
    after["taiji_context"]["legs"][0]["first_seen_at"] = T0
    with pytest.raises(CClassStateError, match="cannot backfill"):
        validate_cclass_transition(before, after, prior_as_of=T0, current_as_of=T1)


def test_active_taiji_lineage_can_only_disappear_under_explicit_replacement_flag() -> None:
    before = _active_taiji(as_of=T1)
    after = _active_taiji(as_of=T2)
    after["engine_changed_at"] = before["engine_changed_at"]
    after["thesis_context"]["changed_at"] = before["thesis_context"]["changed_at"]
    after["taiji_context"]["dynasty_id"] = "DYNASTY-BULL-2"
    after["taiji_context"]["anchor_id"] = "ANCHOR-LARGE-BULL-2"
    after["taiji_context"]["active_leg_id"] = "LEG-NEW-1"
    after["taiji_context"]["legs"][0]["leg_id"] = "LEG-NEW-1"

    with pytest.raises(CClassStateError, match="cannot disappear"):
        validate_cclass_transition(before, after, prior_as_of=T1, current_as_of=T2)

    validate_cclass_transition(
        before,
        after,
        prior_as_of=T1,
        current_as_of=T2,
        allow_taiji_lineage_replacement=True,
    )


def test_momentum_stage_change_requires_stage_changed_at_to_advance() -> None:
    before = _active_momentum(as_of=T1)
    after = copy.deepcopy(before)
    after["as_of"] = T2
    after["momentum_context"]["stage"] = "DRAGON_EARLY"
    after["momentum_context"]["dragon_grade"] = "K_GOLD"
    with pytest.raises(CClassStateError, match="stage_changed_at must advance"):
        validate_cclass_transition(before, after, prior_as_of=T1, current_as_of=T2)


def test_copy_failure_changes_quality_but_does_not_force_reverse_bias() -> None:
    context = _active_taiji(as_of=T1)
    leg = context["taiji_context"]["legs"][0]
    leg.update(
        {
            "sequence": "COPY_3",
            "role": "COPY",
            "copy_quality": "FAILED",
            "amplitude_relation": "WEAKER",
            "duration_relation": "WEAKER",
            "slope_relation": "WEAKER",
            "cleanliness_relation": "WEAKER",
            "destructive_relation": "WEAKER",
        }
    )
    context["thesis_context"]["bias"] = "NEUTRAL"
    validated = validate_cclass_context(context, as_of=T1)
    assert validated["taiji_context"]["legs"][0]["copy_quality"] == "FAILED"
    assert validated["thesis_context"]["bias"] == "NEUTRAL"


def test_material_mode_and_momentum_changes_force_notification() -> None:
    before = empty_cclass_context(as_of=T0)
    after = _active_momentum(as_of=T1)
    reasons = cclass_notification_reasons(before, after)
    assert "太極／一之主模式或市場秩序改變" in reasons
    assert "離心力／一條龍／生死門階段改變" in reasons
    assert "工作看法改變" in reasons
