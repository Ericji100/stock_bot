from __future__ import annotations

from copy import deepcopy

import pytest

from trade_monitor.anchor_state import (
    AnchorStateError,
    anchor_notification_reasons,
    empty_anchor_context,
    normalize_legacy_quadrant_axis_consistency,
    validate_anchor_context,
    validate_anchor_transition,
)
from trade_monitor.market_structure_state import empty_market_structure_state, upgrade_market_structure_state


def _zone() -> dict[str, object]:
    return {"low": 99.0, "high": 100.0, "reliability": "ESTIMATED", "reason": "結構失效區。"}


def _anchor(
    anchor_id: str,
    *,
    grade: str = "LARGE",
    direction: str = "BULL",
    status: str = "FORMING",
    first_seen_at: str = "2026-09-02T10:00:00+08:00",
) -> dict[str, object]:
    return {
        "anchor_id": anchor_id,
        "grade": grade,
        "direction": direction,
        "status": status,
        "source": "STRUCTURE_BREAK",
        "start_bar_time": "2026-09-02T09:55:00+08:00",
        "extreme_bar_time": first_seen_at,
        "start_price_estimate": 100.0 if direction == "BULL" else 110.0,
        "extreme_price_estimate": 105.0 if direction == "BULL" else 105.0,
        "first_seen_at": first_seen_at,
        "confirmed_at": None,
        "terminal_at": None,
        "replaced_by": None,
        "quality": "CAUTION",
        "destructive_evidence": ["NONE"],
        "invalidation_zone": _zone(),
        "parent_anchor_id": None,
        "notes": ["定錨候選。"],
    }


def _with_large_forming(at: str = "2026-09-02T10:00:00+08:00") -> dict[str, object]:
    context = empty_anchor_context()
    context.update(
        {
            "anchors": [_anchor("A-LARGE-1", first_seen_at=at)],
            "active_large_anchor_id": "A-LARGE-1",
            "controlling_grade": "LARGE",
            "grade_relation": "ONLY_LARGE",
            "control_reason": "大級錨形成中並暫時掌握結構。",
            "control_changed_at": at,
            "working_quadrant": "TRANSITION",
            "primary_quadrant_candidate": "Q4",
            "secondary_quadrant_candidate": "Q1",
            "eliminated_quadrants": ["Q2", "Q3"],
            "quadrant_reason": "方向已出現，但第二段尚未完成。",
            "quadrant_changed_at": at,
        }
    )
    return context


def test_empty_anchor_context_is_valid() -> None:
    context = validate_anchor_context(empty_anchor_context(), as_of="2026-09-02T10:00:00+08:00")

    assert context["working_quadrant"] == "UNDEFINED"
    assert context["anchors"] == []


def test_empty_anchor_context_cannot_claim_a_quadrant_candidate() -> None:
    context = empty_anchor_context()
    context.update(
        {
            "working_quadrant": "TRANSITION",
            "primary_quadrant_candidate": "Q4",
            "quadrant_changed_at": "2026-09-02T10:00:00+08:00",
        }
    )

    with pytest.raises(AnchorStateError, match="require at least one causal anchor"):
        validate_anchor_context(context, as_of="2026-09-02T10:00:00+08:00")


def test_invalidated_anchor_clears_stale_replacement_pointer() -> None:
    at = "2026-09-02T10:01:00+08:00"
    context = empty_anchor_context()
    anchor = _anchor("A-LARGE-1")
    anchor.update(
        {
            "status": "INVALIDATED",
            "terminal_at": at,
            "replaced_by": "A-SMALL-BEAR-2",
            "notes": ["原方向錨已失效，新反向錨另行建立。"],
        }
    )
    context["anchors"] = [anchor]

    validated = validate_anchor_context(context, as_of=at)

    assert validated["anchors"][0]["status"] == "INVALIDATED"
    assert validated["anchors"][0]["replaced_by"] is None


def test_forming_anchor_can_confirm_without_rewriting_first_seen() -> None:
    before = _with_large_forming()
    after = deepcopy(before)
    anchor = after["anchors"][0]
    anchor.update(
        {
            "status": "CONFIRMED",
            "extreme_bar_time": "2026-09-02T10:01:00+08:00",
            "extreme_price_estimate": 108.0,
            "confirmed_at": "2026-09-02T10:01:00+08:00",
            "quality": "CLEAN",
            "destructive_evidence": ["STRUCTURE_BREAK"],
            "notes": ["大級錨已由結構突破確認。"],
        }
    )

    validate_anchor_transition(
        before,
        after,
        prior_as_of="2026-09-02T10:00:00+08:00",
        current_as_of="2026-09-02T10:01:00+08:00",
    )
    assert anchor["first_seen_at"] == "2026-09-02T10:00:00+08:00"


def test_new_anchor_cannot_backdate_first_seen() -> None:
    before = _with_large_forming()
    after = deepcopy(before)
    small = _anchor(
        "A-SMALL-1",
        grade="SMALL",
        direction="BEAR",
        first_seen_at="2026-09-02T10:00:00+08:00",
    )
    after["anchors"].append(small)
    after.update(
        {
            "active_small_anchor_id": "A-SMALL-1",
            "controlling_grade": "SMALL",
            "grade_relation": "CONFLICT",
            "control_changed_at": "2026-09-02T10:01:00+08:00",
        }
    )

    with pytest.raises(AnchorStateError, match="backdate"):
        validate_anchor_transition(
            before,
            after,
            prior_as_of="2026-09-02T10:00:00+08:00",
            current_as_of="2026-09-02T10:01:00+08:00",
        )


def test_small_reverse_anchor_keeps_large_anchor_active() -> None:
    before = _with_large_forming()
    after = deepcopy(before)
    after["anchors"].append(
        _anchor(
            "A-SMALL-BEAR-1",
            grade="SMALL",
            direction="BEAR",
            first_seen_at="2026-09-02T10:01:00+08:00",
        )
    )
    after.update(
        {
            "active_small_anchor_id": "A-SMALL-BEAR-1",
            "controlling_grade": "SMALL",
            "grade_relation": "CONFLICT",
            "control_reason": "小級反向錨暫時掌握發球權，大級多方錨尚未失效。",
            "control_changed_at": "2026-09-02T10:01:00+08:00",
            "working_quadrant": "TRANSITION",
            "primary_quadrant_candidate": "Q2",
            "secondary_quadrant_candidate": "Q1",
            "eliminated_quadrants": ["Q3", "Q4"],
            "quadrant_reason": "大小錨衝突，等待小級失效或大級防線被破。",
            "quadrant_changed_at": "2026-09-02T10:01:00+08:00",
        }
    )

    validate_anchor_transition(
        before,
        after,
        prior_as_of="2026-09-02T10:00:00+08:00",
        current_as_of="2026-09-02T10:01:00+08:00",
    )
    assert after["active_large_anchor_id"] == "A-LARGE-1"
    assert after["active_small_anchor_id"] == "A-SMALL-BEAR-1"


def test_active_anchor_must_terminate_before_switch() -> None:
    before = _with_large_forming()
    after = deepcopy(before)
    after["anchors"].append(
        _anchor("A-LARGE-2", first_seen_at="2026-09-02T10:01:00+08:00")
    )
    after["active_large_anchor_id"] = "A-LARGE-2"

    with pytest.raises(AnchorStateError, match="must terminate"):
        validate_anchor_transition(
            before,
            after,
            prior_as_of="2026-09-02T10:00:00+08:00",
            current_as_of="2026-09-02T10:01:00+08:00",
        )


def test_quadrant_change_requires_new_change_time() -> None:
    before = _with_large_forming()
    after = deepcopy(before)
    after.update(
        {
            "working_quadrant": "Q4",
            "primary_quadrant_candidate": "Q4",
            "quadrant_reason": "同向延續且波動收縮。",
        }
    )

    with pytest.raises(AnchorStateError, match="quadrant_changed_at"):
        validate_anchor_transition(
            before,
            after,
            prior_as_of="2026-09-02T10:00:00+08:00",
            current_as_of="2026-09-02T10:01:00+08:00",
        )


def test_increasing_trend_cannot_rank_low_trend_quadrant() -> None:
    context = _with_large_forming()
    context.update(
        {
            "trend_dynamics": "INCREASING",
            "volatility_dynamics": "UNSTABLE",
            "working_quadrant": "TRANSITION",
            "primary_quadrant_candidate": "Q2",
            "secondary_quadrant_candidate": "Q1",
            "eliminated_quadrants": ["Q3", "Q4"],
            "quadrant_reason": "錯把大級震盪和小級趨勢混在一起。",
        }
    )

    with pytest.raises(AnchorStateError, match="controlling grade"):
        validate_anchor_context(context, as_of="2026-09-02T10:00:00+08:00")


def test_legacy_cross_grade_primary_promotes_compatible_secondary() -> None:
    context = _with_large_forming()
    context.update(
        {
            "trend_dynamics": "INCREASING",
            "volatility_dynamics": "UNSTABLE",
            "working_quadrant": "TRANSITION",
            "primary_quadrant_candidate": "Q2",
            "secondary_quadrant_candidate": "Q1",
            "eliminated_quadrants": ["Q3", "Q4"],
            "quadrant_reason": "舊版混用證據。",
        }
    )

    normalized = normalize_legacy_quadrant_axis_consistency(context)
    validated = validate_anchor_context(normalized, as_of="2026-09-02T10:00:00+08:00")

    assert validated["working_quadrant"] == "TRANSITION"
    assert validated["primary_quadrant_candidate"] == "Q1"
    assert validated["secondary_quadrant_candidate"] == "Q2"


def test_increasing_trend_with_unstable_volatility_keeps_q1_q4_candidates() -> None:
    context = _with_large_forming()
    context.update(
        {
            "trend_dynamics": "INCREASING",
            "volatility_dynamics": "UNSTABLE",
            "working_quadrant": "TRANSITION",
            "primary_quadrant_candidate": "Q4",
            "secondary_quadrant_candidate": "Q1",
            "eliminated_quadrants": ["Q2", "Q3"],
            "quadrant_reason": "同級趨勢明確，波動軸仍待確認。",
        }
    )

    validated = validate_anchor_context(context, as_of="2026-09-02T10:00:00+08:00")

    assert validated["primary_quadrant_candidate"] == "Q4"
    assert validated["secondary_quadrant_candidate"] == "Q1"


def test_contracting_volatility_cannot_rank_high_volatility_quadrant() -> None:
    context = _with_large_forming()
    context.update(
        {
            "trend_dynamics": "INCREASING",
            "volatility_dynamics": "CONTRACTING",
            "working_quadrant": "TRANSITION",
            "primary_quadrant_candidate": "Q1",
            "secondary_quadrant_candidate": "Q4",
            "eliminated_quadrants": ["Q2", "Q3"],
            "quadrant_reason": "波動已收斂卻仍把高波動象限列為主候選。",
        }
    )

    with pytest.raises(AnchorStateError, match="controlling grade"):
        validate_anchor_context(context, as_of="2026-09-02T10:00:00+08:00")


def test_secondary_quadrant_must_change_only_one_axis() -> None:
    context = _with_large_forming()
    context.update(
        {
            "trend_dynamics": "UNCLEAR",
            "volatility_dynamics": "UNCLEAR",
            "working_quadrant": "TRANSITION",
            "primary_quadrant_candidate": "Q4",
            "secondary_quadrant_candidate": "Q2",
            "eliminated_quadrants": ["Q1", "Q3"],
            "quadrant_reason": "錯把需要同時改變兩個軸的象限列為直接備選。",
        }
    )

    with pytest.raises(AnchorStateError, match="exactly one axis"):
        validate_anchor_context(context, as_of="2026-09-02T10:00:00+08:00")


def test_notifications_ignore_plain_extreme_extension_but_report_confirmation() -> None:
    before = validate_anchor_context(_with_large_forming(), as_of="2026-09-02T10:00:00+08:00")
    extended = deepcopy(before)
    extended["anchors"][0]["extreme_bar_time"] = "2026-09-02T10:01:00+08:00"
    extended["anchors"][0]["extreme_price_estimate"] = 107.0
    confirmed = deepcopy(extended)
    confirmed["anchors"][0].update(
        {
            "status": "CONFIRMED",
            "confirmed_at": "2026-09-02T10:01:00+08:00",
            "quality": "CLEAN",
            "destructive_evidence": ["STRUCTURE_BREAK"],
        }
    )

    assert anchor_notification_reasons(before, extended) == []
    assert "定錨確認或失效" in anchor_notification_reasons(before, confirmed)


def test_v2_structure_can_upgrade_to_v3_without_mutating_v2() -> None:
    v2 = empty_market_structure_state(
        as_of="2026-09-02T10:00:00+08:00",
        session_key="2026-09-02-DAY",
    )

    upgraded = upgrade_market_structure_state(v2, target_version=3)

    assert v2["version"] == 2
    assert "anchor_context" not in v2
    assert upgraded["version"] == 3
    assert upgraded["anchor_context"] == empty_anchor_context()
