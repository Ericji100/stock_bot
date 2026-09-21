from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

import trade_monitor.analysis_adapter as adapter
from trade_monitor.market_structure_state import (
    MarketStructureStateError,
    _is_explicit_taiji_lineage_replacement,
    _normalize_mechanical_transition_times,
    empty_market_structure_state,
    market_structure_notification_reasons,
    upgrade_market_structure_state,
    validate_market_structure_semantics,
    validate_market_structure_state,
    validate_market_structure_transition,
)


def _pivot(*, pivot_id: str, kind: str, state: str, bar: str, first: str, local=None, paired=None, terminal=None, replaced_by=None):
    return {
        "pivot_id": pivot_id,
        "kind": kind,
        "state": state,
        "bar_time": bar,
        "price_estimate": 46800.0,
        "first_seen_at": first,
        "locally_confirmed_at": local,
        "paired_confirmed_at": paired,
        "terminal_at": terminal,
        "replaced_by": replaced_by,
        "proportionality": "NORMAL",
    }


def _state(as_of: str, pivots: list[dict] | None = None) -> dict:
    state = empty_market_structure_state(as_of=as_of, session_key="2026-09-02-DAY")
    state["primary_pivots"] = pivots or []
    return state


def _state_v5(as_of: str) -> dict:
    return empty_market_structure_state(
        as_of=as_of,
        session_key="2026-09-03-NIGHT",
        version=5,
    )


def _state_v6(as_of: str) -> dict:
    return empty_market_structure_state(
        as_of=as_of,
        session_key="2026-09-03-DAY",
        version=6,
    )


def _payload(as_of: str, structure: dict) -> dict:
    return {
        "original_decision": "NOTIFY",
        "notification_reason": "樞紐狀態更新。",
        "latest_closed_k_price_estimate": "點位無法可靠估計",
        "latest_closed_k_details": ["只更新可驗證結構。"],
        "large_trend": {"classification": "資料不足", "details": ["二級結構尚未完成。"]},
        "current_trend": {"classification": "轉換中", "details": ["一級樞紐正在形成。"]},
        "market_state": ["日盤；結構轉換中。"],
        "pattern_observation": {"status": "觀望", "pattern": "趨勢拉回延續", "details": ["尚未觸發。"]},
        "missing_conditions_or_trigger": ["等待樞紐配對確認。"],
        "entry_and_structural_stop": ["不建立模擬持倉。"],
        "risk_and_nearest_obstacle": ["資料不足，無法計算。"],
        "single_contract_management_or_prohibition": ["目前觀望。"],
        "constitution_event": {
            "event_type": "NONE",
            "event_id": f"NONE|{as_of}",
            "setup_id": None,
            "position_id": None,
            "direction": None,
            "entry_price_estimate": None,
            "stop_price_estimate": None,
            "risk_points": None,
            "latest_closed_bar_time": as_of,
            "reason": "沒有交易憲法異動。",
        },
        "market_structure_state": structure,
    }


def test_confirmed_bear_dow_state_requires_active_same_grade_defense() -> None:
    as_of = "2026-09-03T10:06:00+08:00"
    state = _state_v6(as_of)
    state["dow_state_small"] = "BEAR"

    with pytest.raises(MarketStructureStateError, match="active same-grade bear defense"):
        validate_market_structure_semantics(state)


def test_bear_defense_requires_paired_high_attack_pivot() -> None:
    as_of = "2026-09-03T10:06:00+08:00"
    pivot = _pivot(
        pivot_id="P1-H-0956",
        kind="HIGH",
        state="PAIRED_CONFIRMED",
        bar="2026-09-03T09:56:00+08:00",
        first=as_of,
        local=as_of,
        paired=as_of,
    )
    state = _state_v6(as_of)
    state["primary_pivots"] = [pivot]
    state["dow_state_small"] = "BEAR"
    state["defense_lines"]["small_bear"] = {
        "direction": "BEAR",
        "pivot_id": pivot["pivot_id"],
        "price_estimate": pivot["price_estimate"],
        "qualified_at": as_of,
        "status": "ACTIVE",
        "ended_at": None,
    }

    normalized = validate_market_structure_semantics(state)
    assert normalized["defense_lines"]["small_bear"]["pivot_id"] == "P1-H-0956"

    wrong_kind = copy.deepcopy(state)
    wrong_kind["primary_pivots"][0]["kind"] = "LOW"
    with pytest.raises(MarketStructureStateError, match="high attack pivot"):
        validate_market_structure_semantics(wrong_kind)


def _working_anchor(
    *,
    anchor_id: str,
    grade: str,
    direction: str,
    start: str,
    extreme: str,
    first_seen: str,
    parent: str | None,
    evidence: list[str],
) -> dict:
    start_price = 45928.0 if direction == "BULL" else 46470.0
    extreme_price = 46470.0 if direction == "BULL" else 45928.0
    return {
        "anchor_id": anchor_id,
        "grade": grade,
        "direction": direction,
        "status": "CONFIRMED",
        "source": "STRUCTURE_BREAK",
        "start_bar_time": start,
        "extreme_bar_time": extreme,
        "start_price_estimate": start_price,
        "extreme_price_estimate": extreme_price,
        "first_seen_at": first_seen,
        "confirmed_at": first_seen,
        "terminal_at": None,
        "replaced_by": None,
        "quality": "CAUTION",
        "destructive_evidence": evidence,
        "invalidation_zone": {
            "low": 45920.0,
            "high": 45935.0,
            "reliability": "ESTIMATED",
            "reason": "父子錨結構失效區。",
        },
        "parent_anchor_id": parent,
        "notes": ["依可見歷史重建的作用中定錨。"],
    }


def test_broad_session_break_cannot_remain_the_only_small_anchor() -> None:
    as_of = "2026-09-03T11:41:00+08:00"
    state = _state_v6(as_of)
    small = _working_anchor(
        anchor_id="A-S-BULL-1042",
        grade="SMALL",
        direction="BULL",
        start="2026-09-03T10:42:00+08:00",
        extreme="2026-09-03T11:21:00+08:00",
        first_seen=as_of,
        parent=None,
        evidence=["STRUCTURE_BREAK", "SESSION_EXTREME_BREAK"],
    )
    state["anchor_context"].update(
        {
            "anchors": [small],
            "active_small_anchor_id": small["anchor_id"],
            "controlling_grade": "SMALL",
            "grade_relation": "ONLY_SMALL",
            "control_reason": "錯把跨越時段極值的完整推進只列成小錨。",
            "control_changed_at": as_of,
            "working_quadrant": "TRANSITION",
            "primary_quadrant_candidate": "Q4",
            "secondary_quadrant_candidate": "Q1",
            "eliminated_quadrants": ["Q2", "Q3"],
            "quadrant_reason": "等待父級定錨重建。",
            "quadrant_changed_at": as_of,
        }
    )

    with pytest.raises(MarketStructureStateError, match="requires an active large parent"):
        validate_market_structure_semantics(state)


def test_active_large_and_small_anchors_form_a_parent_child_hierarchy() -> None:
    as_of = "2026-09-03T11:41:00+08:00"
    state = _state_v6(as_of)
    large = _working_anchor(
        anchor_id="A-L-BULL-1000",
        grade="LARGE",
        direction="BULL",
        start="2026-09-03T10:00:00+08:00",
        extreme="2026-09-03T11:21:00+08:00",
        first_seen=as_of,
        parent=None,
        evidence=["STRUCTURE_BREAK", "SESSION_EXTREME_BREAK"],
    )
    small = _working_anchor(
        anchor_id="A-S-BULL-1042",
        grade="SMALL",
        direction="BULL",
        start="2026-09-03T10:42:00+08:00",
        extreme="2026-09-03T11:21:00+08:00",
        first_seen=as_of,
        parent=large["anchor_id"],
        evidence=["STRUCTURE_BREAK", "SESSION_EXTREME_BREAK"],
    )
    state["anchor_context"].update(
        {
            "anchors": [large, small],
            "active_large_anchor_id": large["anchor_id"],
            "active_small_anchor_id": small["anchor_id"],
            "controlling_grade": "SMALL",
            "grade_relation": "ALIGNED",
            "control_reason": "大錨保留方向，小錨管理目前修正與觸發。",
            "control_changed_at": as_of,
            "working_quadrant": "TRANSITION",
            "primary_quadrant_candidate": "Q4",
            "secondary_quadrant_candidate": "Q1",
            "eliminated_quadrants": ["Q2", "Q3"],
            "quadrant_reason": "父子錨同向，等待小級重新發動。",
            "quadrant_changed_at": as_of,
        }
    )

    normalized = validate_market_structure_semantics(state)

    assert normalized["anchor_context"]["active_large_anchor_id"] == large["anchor_id"]
    assert normalized["anchor_context"]["active_small_anchor_id"] == small["anchor_id"]


def test_promoted_large_anchor_cannot_discard_preceding_opposite_extreme() -> None:
    as_of = "2026-09-03T11:41:00+08:00"
    state = _state_v6(as_of)
    preceding_bear = _working_anchor(
        anchor_id="A-S-BEAR-0920",
        grade="SMALL",
        direction="BEAR",
        start="2026-09-03T09:20:00+08:00",
        extreme="2026-09-03T10:00:00+08:00",
        first_seen=as_of,
        parent=None,
        evidence=["STRUCTURE_BREAK"],
    )
    preceding_bear.update(
        {
            "status": "INVALIDATED",
            "terminal_at": as_of,
        }
    )
    late_large = _working_anchor(
        anchor_id="A-L-BULL-1042",
        grade="LARGE",
        direction="BULL",
        start="2026-09-03T10:42:00+08:00",
        extreme="2026-09-03T11:21:00+08:00",
        first_seen=as_of,
        parent=None,
        evidence=["STRUCTURE_BREAK", "SESSION_EXTREME_BREAK"],
    )
    state["anchor_context"].update(
        {
            "anchors": [preceding_bear, late_large],
            "active_large_anchor_id": late_large["anchor_id"],
            "controlling_grade": "LARGE",
            "grade_relation": "ONLY_LARGE",
            "control_reason": "錯把父級大錨起點放在反轉波段中途。",
            "control_changed_at": as_of,
            "working_quadrant": "TRANSITION",
            "primary_quadrant_candidate": "Q4",
            "secondary_quadrant_candidate": "Q1",
            "eliminated_quadrants": ["Q2", "Q3"],
            "quadrant_reason": "等待大錨起點修正。",
            "quadrant_changed_at": as_of,
        }
    )

    with pytest.raises(MarketStructureStateError, match="preceding opposite anchor extreme"):
        validate_market_structure_semantics(state)


def test_promoted_large_anchor_may_begin_at_preceding_opposite_extreme() -> None:
    as_of = "2026-09-03T11:41:00+08:00"
    state = _state_v6(as_of)
    preceding_bear = _working_anchor(
        anchor_id="A-S-BEAR-0920",
        grade="SMALL",
        direction="BEAR",
        start="2026-09-03T09:20:00+08:00",
        extreme="2026-09-03T10:00:00+08:00",
        first_seen=as_of,
        parent=None,
        evidence=["STRUCTURE_BREAK"],
    )
    preceding_bear.update(
        {
            "status": "INVALIDATED",
            "terminal_at": as_of,
        }
    )
    full_large = _working_anchor(
        anchor_id="A-L-BULL-1000",
        grade="LARGE",
        direction="BULL",
        start="2026-09-03T10:00:00+08:00",
        extreme="2026-09-03T11:21:00+08:00",
        first_seen=as_of,
        parent=None,
        evidence=["STRUCTURE_BREAK", "SESSION_EXTREME_BREAK"],
    )
    state["anchor_context"].update(
        {
            "anchors": [preceding_bear, full_large],
            "active_large_anchor_id": full_large["anchor_id"],
            "controlling_grade": "LARGE",
            "grade_relation": "ONLY_LARGE",
            "control_reason": "父級大錨承接前一反向錨的10:00極值。",
            "control_changed_at": as_of,
            "working_quadrant": "TRANSITION",
            "primary_quadrant_candidate": "Q4",
            "secondary_quadrant_candidate": "Q1",
            "eliminated_quadrants": ["Q2", "Q3"],
            "quadrant_reason": "大錨起點與完整反轉推進一致。",
            "quadrant_changed_at": as_of,
        }
    )

    normalized = validate_market_structure_semantics(state)

    assert normalized["anchor_context"]["active_large_anchor_id"] == full_large["anchor_id"]
    assert normalized["anchor_context"]["anchors"][1]["start_bar_time"].endswith("10:00:00+08:00")


def test_active_small_anchor_must_reference_the_active_large_parent() -> None:
    as_of = "2026-09-03T11:41:00+08:00"
    state = _state_v6(as_of)
    large = _working_anchor(
        anchor_id="A-L-BULL-1000",
        grade="LARGE",
        direction="BULL",
        start="2026-09-03T10:00:00+08:00",
        extreme="2026-09-03T11:21:00+08:00",
        first_seen=as_of,
        parent=None,
        evidence=["STRUCTURE_BREAK", "SESSION_EXTREME_BREAK"],
    )
    small = _working_anchor(
        anchor_id="A-S-BULL-1042",
        grade="SMALL",
        direction="BULL",
        start="2026-09-03T10:42:00+08:00",
        extreme="2026-09-03T11:21:00+08:00",
        first_seen=as_of,
        parent=None,
        evidence=["STRUCTURE_BREAK"],
    )
    state["anchor_context"].update(
        {
            "anchors": [large, small],
            "active_large_anchor_id": large["anchor_id"],
            "active_small_anchor_id": small["anchor_id"],
            "controlling_grade": "SMALL",
            "grade_relation": "ALIGNED",
            "control_reason": "小錨遺漏父級參照。",
            "control_changed_at": as_of,
            "working_quadrant": "TRANSITION",
            "primary_quadrant_candidate": "Q4",
            "secondary_quadrant_candidate": "Q1",
            "eliminated_quadrants": ["Q2", "Q3"],
            "quadrant_reason": "等待父子關係修正。",
            "quadrant_changed_at": as_of,
        }
    )

    with pytest.raises(MarketStructureStateError, match="must reference the active large parent"):
        validate_market_structure_semantics(state)


def test_visible_historical_pivot_may_be_discovered_now_without_backdating() -> None:
    previous = _state_v6("2026-09-03T10:05:00+08:00")
    as_of = "2026-09-03T10:06:00+08:00"
    pivot = _pivot(
        pivot_id="P1-H-0956",
        kind="HIGH",
        state="PAIRED_CONFIRMED",
        bar="2026-09-03T09:56:00+08:00",
        first=as_of,
        local=as_of,
        paired=as_of,
    )
    current = _state_v6(as_of)
    current["primary_pivots"] = [pivot]
    current["dow_state_small"] = "BEAR"
    current["defense_lines"]["small_bear"] = {
        "direction": "BEAR",
        "pivot_id": pivot["pivot_id"],
        "price_estimate": pivot["price_estimate"],
        "qualified_at": as_of,
        "status": "ACTIVE",
        "ended_at": None,
    }

    normalized = validate_market_structure_transition(previous, current, expected_as_of=as_of)
    assert normalized["primary_pivots"][0]["bar_time"] == "2026-09-03T09:56:00+08:00"
    assert normalized["primary_pivots"][0]["first_seen_at"] == as_of


def test_candidate_progression_preserves_first_confirmation_times() -> None:
    t0 = "2026-09-02T10:00:00+08:00"
    t1 = "2026-09-02T10:01:00+08:00"
    t2 = "2026-09-02T10:02:00+08:00"
    candidate = _pivot(pivot_id="P1-H-0959", kind="HIGH", state="CANDIDATE", bar="2026-09-02T09:59:00+08:00", first=t0)
    previous = _state(t0, [candidate])
    confirmed = dict(candidate, state="LOCAL_CONFIRMED", locally_confirmed_at=t1)
    current = _state(t1, [confirmed])

    normalized = validate_market_structure_transition(previous, current, expected_as_of=t1)
    assert normalized["primary_pivots"][0]["first_seen_at"] == t0
    assert normalized["primary_pivots"][0]["locally_confirmed_at"] == t1

    changed = _state(t2, [dict(confirmed, locally_confirmed_at=t2)])
    with pytest.raises(MarketStructureStateError, match="changed its first"):
        validate_market_structure_transition(current, changed, expected_as_of=t2)


def test_active_candidate_cannot_disappear_without_terminal_transition() -> None:
    t0 = "2026-09-02T10:00:00+08:00"
    t1 = "2026-09-02T10:01:00+08:00"
    previous = _state(t0, [_pivot(pivot_id="P1-L-0959", kind="LOW", state="CANDIDATE", bar="2026-09-02T09:59:00+08:00", first=t0)])
    with pytest.raises(MarketStructureStateError, match="cannot disappear"):
        validate_market_structure_transition(previous, _state(t1), expected_as_of=t1)


def test_terminal_provisional_boundary_may_be_replaced_in_its_directional_slot() -> None:
    t0 = "2026-09-03T12:40:00+08:00"
    t1 = "2026-09-03T13:08:00+08:00"
    previous = _state_v6(t0)
    previous["provisional_invalidation_boundaries"]["bear"] = {
        "kind": "STRUCTURE_BOUNDARY",
        "direction": "BEAR",
        "price_estimate": 46270.0,
        "established_at": "2026-09-03T09:28:00+08:00",
        "status": "BROKEN",
        "ended_at": "2026-09-03T11:11:00+08:00",
    }
    current = _state_v6(t1)
    current["provisional_invalidation_boundaries"]["bear"] = {
        "kind": "STRUCTURE_BOUNDARY",
        "direction": "BEAR",
        "price_estimate": 46340.0,
        "established_at": t1,
        "status": "ACTIVE",
        "ended_at": None,
    }

    normalized = validate_market_structure_transition(previous, current, expected_as_of=t1)

    assert normalized["provisional_invalidation_boundaries"]["bear"]["established_at"] == t1
    assert normalized["provisional_invalidation_boundaries"]["bear"]["status"] == "ACTIVE"


def test_taiji_lineage_repair_requires_an_explicit_one_hop_large_anchor_replacement() -> None:
    prior = {
        "anchor_context": {
            "active_large_anchor_id": "A-L-OLD",
            "anchors": [{"anchor_id": "A-L-OLD", "grade": "LARGE", "status": "FORMING"}],
        },
        "cclass_context": {
            "taiji_context": {
                "dynasty_id": "TJ-OLD",
                "anchor_id": "A-L-OLD",
                "active_leg_id": "LEG-OLD",
            }
        },
    }
    current = {
        "anchor_context": {
            "active_large_anchor_id": "A-L-NEW",
            "anchors": [
                {
                    "anchor_id": "A-L-OLD",
                    "grade": "LARGE",
                    "status": "REPLACED",
                    "replaced_by": "A-L-NEW",
                },
                {"anchor_id": "A-L-NEW", "grade": "LARGE", "status": "FORMING"},
            ],
        },
        "cclass_context": {
            "taiji_context": {
                "dynasty_id": "TJ-NEW",
                "anchor_id": "A-L-NEW",
                "active_leg_id": "LEG-NEW",
            }
        },
    }

    assert _is_explicit_taiji_lineage_replacement(prior, current) is True

    not_explicit = copy.deepcopy(current)
    not_explicit["anchor_context"]["anchors"][0]["replaced_by"] = None
    assert _is_explicit_taiji_lineage_replacement(prior, not_explicit) is False

    repair_then_flip = copy.deepcopy(current)
    repair_then_flip["anchor_context"]["anchors"][0]["replaced_by"] = "A-L-REPAIRED"
    repair_then_flip["anchor_context"]["anchors"].insert(
        1,
        {
            "anchor_id": "A-L-REPAIRED",
            "grade": "LARGE",
            "direction": "BULL",
            "status": "INVALIDATED",
            "start_bar_time": "2026-09-03T10:00:00+08:00",
            "extreme_bar_time": "2026-09-03T11:21:00+08:00",
            "first_seen_at": "2026-09-03T13:25:00+08:00",
        },
    )
    repair_then_flip["anchor_context"]["anchors"][0].update(
        {"direction": "BULL", "start_bar_time": "2026-09-03T10:42:00+08:00"}
    )
    repair_then_flip["anchor_context"]["anchors"][2].update(
        {
            "direction": "BEAR",
            "start_bar_time": "2026-09-03T11:21:00+08:00",
            "first_seen_at": "2026-09-03T13:25:00+08:00",
        }
    )
    repair_then_flip["cclass_context"]["taiji_context"]["anchor_id"] = "A-L-NEW"
    prior["anchor_context"]["anchors"][0].update(
        {"direction": "BULL", "start_bar_time": "2026-09-03T10:42:00+08:00"}
    )
    repair_then_flip["as_of"] = "2026-09-03T13:25:00+08:00"

    assert _is_explicit_taiji_lineage_replacement(prior, repair_then_flip) is True

    broken_handoff = copy.deepcopy(repair_then_flip)
    broken_handoff["anchor_context"]["anchors"][2]["start_bar_time"] = "2026-09-03T11:30:00+08:00"
    assert _is_explicit_taiji_lineage_replacement(prior, broken_handoff) is False


def test_active_provisional_boundary_must_close_before_replacement() -> None:
    t0 = "2026-09-03T12:40:00+08:00"
    t1 = "2026-09-03T13:08:00+08:00"
    previous = _state_v6(t0)
    previous["provisional_invalidation_boundaries"]["bull"] = {
        "kind": "STRUCTURE_BOUNDARY",
        "direction": "BULL",
        "price_estimate": 46255.0,
        "established_at": "2026-09-03T11:11:00+08:00",
        "status": "ACTIVE",
        "ended_at": None,
    }
    current = _state_v6(t1)
    current["provisional_invalidation_boundaries"]["bull"] = {
        "kind": "STRUCTURE_BOUNDARY",
        "direction": "BULL",
        "price_estimate": 46180.0,
        "established_at": t1,
        "status": "ACTIVE",
        "ended_at": None,
    }

    with pytest.raises(MarketStructureStateError, match="must close the old boundary"):
        validate_market_structure_transition(previous, current, expected_as_of=t1)


def test_paired_pivot_cannot_disappear_within_the_same_session() -> None:
    t0 = "2026-09-02T10:00:00+08:00"
    t1 = "2026-09-02T10:01:00+08:00"
    pivot = _pivot(
        pivot_id="P1-H-0955", kind="HIGH", state="PAIRED_CONFIRMED",
        bar="2026-09-02T09:55:00+08:00", first="2026-09-02T09:55:00+08:00",
        local="2026-09-02T09:58:00+08:00", paired=t0,
    )
    with pytest.raises(MarketStructureStateError, match="paired"):
        validate_market_structure_transition(_state(t0, [pivot]), _state(t1), expected_as_of=t1)


def test_legacy_v1_state_migrates_to_symmetric_defense_slots() -> None:
    as_of = "2026-09-02T10:00:00+08:00"
    pivot = _pivot(
        pivot_id="P1-L-0955", kind="LOW", state="PAIRED_CONFIRMED",
        bar="2026-09-02T09:55:00+08:00", first="2026-09-02T09:55:00+08:00",
        local="2026-09-02T09:58:00+08:00", paired=as_of,
    )
    legacy = {
        "version": 1,
        "as_of": as_of,
        "session_key": "2026-09-02-DAY",
        "primary_pivots": [pivot],
        "secondary_pivots": [],
        "small_defense_line": {
            "direction": "BULL", "pivot_id": pivot["pivot_id"], "price_estimate": 46800.0,
            "qualified_at": as_of, "status": "ACTIVE", "ended_at": None,
        },
        "large_defense_line": None,
        "provisional_invalidation_boundary": None,
        "dow_state_small": "BULL",
        "dow_state_large": "UNDEFINED",
        "reversal_type": "NONE",
        "maturity": "RIGHT_LEFT",
        "notes": ["legacy"],
    }
    migrated = validate_market_structure_state(legacy)
    assert migrated["version"] == 2
    assert migrated["defense_lines"]["small_bull"]["pivot_id"] == pivot["pivot_id"]
    assert migrated["defense_lines"]["small_bear"] is None
    assert migrated["scenario_context"]["setup"]["stage"] == "NONE"


def test_active_bull_and_bear_defenses_are_retained_and_cannot_vanish() -> None:
    t0 = "2026-09-02T10:00:00+08:00"
    t1 = "2026-09-02T10:01:00+08:00"
    low = _pivot(
        pivot_id="P1-L-0950", kind="LOW", state="PAIRED_CONFIRMED",
        bar="2026-09-02T09:50:00+08:00", first="2026-09-02T09:50:00+08:00",
        local="2026-09-02T09:53:00+08:00", paired="2026-09-02T09:55:00+08:00",
    )
    high = _pivot(
        pivot_id="P1-H-0955", kind="HIGH", state="PAIRED_CONFIRMED",
        bar="2026-09-02T09:55:00+08:00", first="2026-09-02T09:55:00+08:00",
        local="2026-09-02T09:58:00+08:00", paired=t0,
    )
    previous = _state(t0, [low, high])
    previous["defense_lines"]["small_bull"] = {
        "direction": "BULL", "pivot_id": low["pivot_id"], "price_estimate": 46800.0,
        "qualified_at": t0, "status": "ACTIVE", "ended_at": None,
    }
    previous["defense_lines"]["small_bear"] = {
        "direction": "BEAR", "pivot_id": high["pivot_id"], "price_estimate": 46850.0,
        "qualified_at": t0, "status": "ACTIVE", "ended_at": None,
    }
    current = _state(t1, [low, high])
    current["defense_lines"] = dict(previous["defense_lines"])
    normalized = validate_market_structure_transition(previous, current, expected_as_of=t1)
    assert normalized["defense_lines"]["small_bull"] is not None
    assert normalized["defense_lines"]["small_bear"] is not None

    vanished = _state(t1, [low, high])
    vanished["defense_lines"]["small_bear"] = previous["defense_lines"]["small_bear"]
    with pytest.raises(MarketStructureStateError, match="active defense cannot disappear"):
        validate_market_structure_transition(previous, vanished, expected_as_of=t1)


def test_setup_stage_and_zone_changes_force_notification_reasons() -> None:
    t0 = "2026-09-02T10:00:00+08:00"
    t1 = "2026-09-02T10:01:00+08:00"
    previous = _state_v5(t0)
    current = _state_v5(t1)
    current["scenario_context"].update({
        "wave_phase": "REBOUND",
        "locations": ["NEAR_RESISTANCE", "DEFENSE_RETEST"],
        "grade_alignment": "ALIGNED",
        "hold_scenario": "壓力守住且反彈受阻，等待空方重新發動。",
        "break_scenario": "若收盤突破壓力，空方候選失效並重新判斷。",
    })
    setup = current["scenario_context"]["setup"]
    setup.update({
        "setup_id": "TREND_PULLBACK_SHORT|1001",
        "pattern": "TREND_PULLBACK_CONTINUATION",
        "direction": "SHORT",
        "stage": "ARMED",
        "first_seen_at": t1,
        "stage_changed_at": t1,
    })
    setup["observation_zone"] = {"low": 46840.0, "high": 46850.0, "reliability": "ESTIMATED", "reason": "反彈壓力區。"}
    setup["trigger_zone"] = {"low": 46810.0, "high": 46820.0, "reliability": "ESTIMATED", "reason": "重新轉弱確認區。"}
    setup["invalidation_zone"] = {"low": 46852.0, "high": 46858.0, "reliability": "ESTIMATED", "reason": "完整反彈高點外。"}
    setup["nearest_obstacle_zone"] = {"low": 46780.0, "high": 46790.0, "reliability": "ESTIMATED", "reason": "前低。"}
    normalized = validate_market_structure_transition(previous, current, expected_as_of=t1)
    reasons = market_structure_notification_reasons(previous, normalized)
    assert "候選型態階段改變" in reasons
    assert "波段階段改變" in reasons


def test_no_chase_setup_can_later_be_invalidated() -> None:
    t0 = "2026-09-03T09:05:00+08:00"
    t1 = "2026-09-03T09:11:00+08:00"
    previous = _state_v6(t0)
    current = _state_v6(t1)
    for state, stage, changed_at in (
        (previous, "NO_CHASE", t0),
        (current, "INVALIDATED", t1),
    ):
        state["scenario_context"]["setup"].update(
            {
                "setup_id": "SETUP-DAY-YIZHI-BULL-01",
                "pattern": "YIZHI_CENTRIFUGAL",
                "direction": "LONG",
                "stage": stage,
                "first_seen_at": t0,
                "stage_changed_at": changed_at,
            }
        )

    normalized = validate_market_structure_transition(previous, current, expected_as_of=t1)

    assert normalized["scenario_context"]["setup"]["stage"] == "INVALIDATED"


def test_stale_terminal_yizhi_method_is_cleared_after_engine_reset() -> None:
    at = "2026-09-03T09:11:00+08:00"
    state = _state_v6(at)
    state["scenario_context"]["setup"].update(
        {
            "setup_id": "SETUP-DAY-YIZHI-BULL-01",
            "pattern": "YIZHI_CENTRIFUGAL",
            "direction": "LONG",
            "stage": "INVALIDATED",
            "first_seen_at": "2026-09-03T09:05:00+08:00",
            "stage_changed_at": at,
        }
    )
    opportunity = state["decision_chain_context"]["opportunity_context"]
    opportunity["mapped_pattern"] = "YIZHI_CENTRIFUGAL"
    opportunity["course_grade"] = "OBSERVE"

    normalized = validate_market_structure_state(state, expected_as_of=at)

    assert normalized["decision_chain_context"]["opportunity_context"]["mapped_pattern"] == "NONE"


def test_new_hypothesis_id_gets_current_causal_first_seen_time() -> None:
    t0 = "2026-09-03T09:05:00+08:00"
    t1 = "2026-09-03T09:25:00+08:00"
    previous = _state_v6(t0)
    current = _state_v6(t1)
    hypothesis = current["prospective_context"]["primary_hypothesis"]
    hypothesis.update(
        {
            "hypothesis_id": "HYP-BEAR-20260903-0925-01",
            "direction": "BEAR",
            "status": "STRENGTHENING",
            "confidence": "MEDIUM",
            "first_seen_at": t0,
            "status_changed_at": t0,
        }
    )

    _normalize_mechanical_transition_times(previous, current)

    assert hypothesis["first_seen_at"] == t1
    assert hypothesis["status_changed_at"] == t1


def test_market_transition_preserves_committed_terminal_anchor_wording() -> None:
    t0 = "2026-09-03T09:28:00+08:00"
    t1 = "2026-09-03T09:31:00+08:00"
    previous = _state_v6(t0)
    terminal_anchor = {
        "anchor_id": "A-S-BULL-20260903-0845-01",
        "grade": "SMALL",
        "direction": "BULL",
        "status": "INVALIDATED",
        "source": "STRUCTURE_BREAK",
        "start_bar_time": "2026-09-03T08:45:00+08:00",
        "extreme_bar_time": "2026-09-03T09:20:00+08:00",
        "start_price_estimate": 46140.0,
        "extreme_price_estimate": 46270.0,
        "first_seen_at": "2026-09-03T09:20:00+08:00",
        "confirmed_at": None,
        "terminal_at": t0,
        "replaced_by": None,
        "quality": "CAUTION",
        "destructive_evidence": ["STRUCTURE_BREAK"],
        "invalidation_zone": {
            "low": 46130.0,
            "high": 46150.0,
            "reliability": "ESTIMATED",
            "reason": "跌破後失效。",
        },
        "parent_anchor_id": None,
        "notes": ["已提交的失效說明。"],
    }
    previous["anchor_context"]["anchors"] = [terminal_anchor]

    current = _state_v6(t1)
    rewritten = copy.deepcopy(terminal_anchor)
    rewritten["notes"] = ["模型在下一輪改寫了同一歷史事實的說明。"]
    current["anchor_context"]["anchors"] = [rewritten]

    normalized = validate_market_structure_transition(previous, current, expected_as_of=t1)

    assert normalized["anchor_context"]["anchors"][0] == terminal_anchor


def test_new_session_may_reset_pivot_sequence() -> None:
    previous = _state("2026-09-02T13:44:00+08:00")
    current = empty_market_structure_state(as_of="2026-09-02T15:00:00+08:00", session_key="2026-09-03-NIGHT")
    assert validate_market_structure_transition(previous, current, expected_as_of=current["as_of"])["primary_pivots"] == []


def test_market_transition_normalizes_stale_decision_assessment_time() -> None:
    t0 = "2026-09-02T22:43:00+08:00"
    t1 = "2026-09-02T22:45:00+08:00"
    previous = _state_v5(t0)
    previous_decision = previous["decision_chain_context"]
    previous_decision.update(
        {
            "process_stage": "LATE_OR_RESETTING",
            "stage_changed_at": t0,
            "assessment_changed_at": t0,
        }
    )

    current = _state_v5(t1)
    current_decision = copy.deepcopy(previous_decision)
    current_decision["as_of"] = t1
    current_decision["opportunity_context"]["expected_behavior"] = "等待右端整理邊界被收盤確認。"
    # This stale value reproduced the live v2.0 failure.  The content decision
    # remains model-owned; only its mechanical change time is normalized.
    current_decision["assessment_changed_at"] = t0
    current["decision_chain_context"] = current_decision

    normalized = validate_market_structure_transition(previous, current, expected_as_of=t1)

    assert normalized["decision_chain_context"]["assessment_changed_at"] == t1


def test_market_transition_allows_undefined_engine_order_to_become_transitional() -> None:
    t0 = "2026-09-02T22:53:00+08:00"
    t1 = "2026-09-02T22:55:00+08:00"
    previous = _state_v5(t0)
    current = _state_v5(t1)
    current["cclass_context"]["order_state"] = "TRANSITION"
    current["cclass_context"]["order_reason"] = "已建立小級錨，但主模式仍未定。"

    normalized = validate_market_structure_transition(previous, current, expected_as_of=t1)

    assert normalized["cclass_context"]["engine_mode"] == "UNDEFINED"
    assert normalized["cclass_context"]["engine_changed_at"] is None


def test_market_transition_can_reset_opening_lens_to_undefined() -> None:
    t0 = "2026-09-02T22:41:00+08:00"
    t1 = "2026-09-02T22:43:00+08:00"
    previous = _state_v5(t0)
    previous_decision = previous["decision_chain_context"]
    previous_decision.update(
        {
            "process_stage": "STRUCTURE_BUILDING",
            "stage_changed_at": t0,
            "assessment_changed_at": t0,
        }
    )
    previous_decision["structure_context"].update(
        {
            "analysis_lens": "OPENING_EVIDENCE_ONLY",
            "lens_changed_at": t0,
            "structure_reason": "開盤證據仍是唯一可用主鏡頭。",
        }
    )

    current = _state_v5(t1)
    current_decision = copy.deepcopy(previous_decision)
    current_decision.update(
        {
            "as_of": t1,
            "process_stage": "LATE_OR_RESETTING",
            "stage_changed_at": t1,
            "assessment_changed_at": t1,
        }
    )
    current_decision["structure_context"].update(
        {
            "analysis_lens": "UNDEFINED",
            "lens_changed_at": None,
            "structure_reason": "開盤鏡頭失效，等待新的因果結構。",
        }
    )
    current["decision_chain_context"] = current_decision

    normalized = validate_market_structure_transition(previous, current, expected_as_of=t1)

    assert normalized["decision_chain_context"]["structure_context"]["lens_changed_at"] is None


def test_v3_upgrades_to_v4_with_empty_cclass_context() -> None:
    as_of = "2026-09-02T10:00:00+08:00"
    v3 = empty_market_structure_state(as_of=as_of, session_key="2026-09-02-DAY", version=3)
    upgraded = upgrade_market_structure_state(v3, target_version=4)
    assert upgraded["version"] == 4
    assert upgraded["anchor_context"] == v3["anchor_context"]
    assert upgraded["cclass_context"]["engine_mode"] == "UNDEFINED"
    assert upgraded["cclass_context"]["taiji_context"]["legs"] == []


def test_v2_can_upgrade_directly_to_v4_without_persisting_guesses() -> None:
    as_of = "2026-09-02T10:00:00+08:00"
    v2 = empty_market_structure_state(as_of=as_of, session_key="2026-09-02-DAY", version=2)
    upgraded = upgrade_market_structure_state(v2, target_version=4)
    assert upgraded["version"] == 4
    assert upgraded["anchor_context"]["anchors"] == []
    assert upgraded["cclass_context"]["engine_mode"] == "UNDEFINED"


def test_v4_upgrades_to_v5_with_empty_decision_chain_without_backfilling() -> None:
    as_of = "2026-09-02T10:00:00+08:00"
    v4 = empty_market_structure_state(as_of=as_of, session_key="2026-09-02-DAY", version=4)
    assert validate_market_structure_state(v4)["version"] == 4

    upgraded = upgrade_market_structure_state(v4, target_version=5)
    decision = upgraded["decision_chain_context"]
    assert upgraded["version"] == 5
    assert decision["process_stage"] == "UNDEFINED"
    assert decision["opening_context"]["first_endpoint_side"] == "NONE"
    assert decision["stage_changed_at"] is None
    assert decision["assessment_changed_at"] is None


def test_adapter_persists_structure_atomically_and_prepare_returns_it(tmp_path: Path) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(b"chart-a")
    state_path = tmp_path / "analysis-state.json"
    prepared = adapter.prepare_capture(
        image,
        datetime.fromisoformat("2026-09-02T10:02:05+08:00"),
        state_path=state_path,
        constitution_state_path=tmp_path / "constitution-state.json",
    )
    as_of = str(prepared["context"]["expected_latest_closed_k_iso"])
    structure = _state(as_of)
    config = tmp_path / "config.json"
    config.write_text('{"trade_monitor_telegram_enabled":false}', encoding="utf-8")
    result = asyncio.run(
        adapter.finalize_analysis(
            context_id=str(prepared["context_id"]),
            analysis_payload=_payload(as_of, structure),
            force_notify=False,
            run_id=None,
            state_path=state_path,
            config_path=config,
            delivery_state_path=tmp_path / "delivery.json",
            runtime_state_path=tmp_path / "runtime.json",
            constitution_state_path=tmp_path / "constitution-state.json",
            dry_run=True,
        )
    )
    assert result["analysis_valid"] is True
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["market_structure_state"]["as_of"] == as_of
    assert not list(tmp_path.glob(".analysis-state.json.*.tmp"))

    image.write_bytes(b"chart-b")
    next_prepare = adapter.prepare_capture(
        image,
        datetime.fromisoformat("2026-09-02T10:03:05+08:00"),
        state_path=state_path,
        constitution_state_path=tmp_path / "constitution-state.json",
    )
    assert next_prepare["market_structure_state"] == saved["market_structure_state"]


def test_adapter_commits_v2_to_v3_anchor_upgrade_only_after_valid_finalize(tmp_path: Path) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(b"chart-anchor")
    state_path = tmp_path / "analysis-state.json"
    prior = _state("2026-09-02T10:00:00+08:00")
    state_path.write_text(
        json.dumps({"version": 1, "pending": {}, "market_structure_state": prior}),
        encoding="utf-8",
    )

    prepared = adapter.prepare_capture(
        image,
        datetime.fromisoformat("2026-09-02T10:02:05+08:00"),
        state_path=state_path,
        constitution_state_path=tmp_path / "constitution-state.json",
        target_market_structure_version=3,
    )
    as_of = str(prepared["context"]["expected_latest_closed_k_iso"])
    structure = prepared["market_structure_state"]
    structure["as_of"] = as_of
    structure["anchor_context"].update(
        {
            "anchors": [
                {
                    "anchor_id": "ANCHOR-LARGE-BULL-1",
                    "grade": "LARGE",
                    "direction": "BULL",
                    "status": "FORMING",
                    "source": "STRUCTURE_BREAK",
                    "start_bar_time": "2026-09-02T09:55:00+08:00",
                    "extreme_bar_time": as_of,
                    "start_price_estimate": 46800.0,
                    "extreme_price_estimate": 46840.0,
                    "first_seen_at": as_of,
                    "confirmed_at": None,
                    "terminal_at": None,
                    "replaced_by": None,
                    "quality": "CAUTION",
                    "destructive_evidence": ["NONE"],
                    "invalidation_zone": {
                        "low": 46790.0,
                        "high": 46800.0,
                        "reliability": "ESTIMATED",
                        "reason": "大級多方錨的暫定失效區。",
                    },
                    "parent_anchor_id": None,
                    "notes": ["定錨候選只在本根收盤後建立。"],
                }
            ],
            "active_large_anchor_id": "ANCHOR-LARGE-BULL-1",
            "controlling_grade": "LARGE",
            "grade_relation": "ONLY_LARGE",
            "control_reason": "目前只有大級多方錨候選。",
            "control_changed_at": as_of,
            "working_quadrant": "TRANSITION",
            "primary_quadrant_candidate": "Q4",
            "secondary_quadrant_candidate": "Q1",
            "eliminated_quadrants": ["Q2", "Q3"],
            "quadrant_reason": "方向已出現，等待第二段確認趨勢與波動關係。",
            "quadrant_changed_at": as_of,
        }
    )
    config = tmp_path / "config.json"
    config.write_text('{"trade_monitor_telegram_enabled":false}', encoding="utf-8")

    result = asyncio.run(
        adapter.finalize_analysis(
            context_id=str(prepared["context_id"]),
            analysis_payload=_payload(as_of, structure),
            force_notify=False,
            run_id=None,
            state_path=state_path,
            config_path=config,
            delivery_state_path=tmp_path / "delivery.json",
            runtime_state_path=tmp_path / "runtime.json",
            constitution_state_path=tmp_path / "constitution-state.json",
            dry_run=True,
        )
    )

    assert result["analysis_valid"] is True
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["market_structure_state"]["version"] == 3
    assert saved["market_structure_state"]["anchor_context"]["active_large_anchor_id"] == "ANCHOR-LARGE-BULL-1"
    assert not list(tmp_path.glob(".analysis-state.json.*.tmp"))


def test_corrupt_structure_state_fails_safe_during_prepare(tmp_path: Path) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(b"chart")
    state_path = tmp_path / "analysis-state.json"
    state_path.write_text(json.dumps({"version": 1, "pending": {}, "market_structure_state": {"bad": True}}), encoding="utf-8")

    with pytest.raises(MarketStructureStateError):
        adapter.prepare_capture(
            image,
            datetime.fromisoformat("2026-09-02T10:02:05+08:00"),
            state_path=state_path,
            constitution_state_path=tmp_path / "constitution-state.json",
        )
