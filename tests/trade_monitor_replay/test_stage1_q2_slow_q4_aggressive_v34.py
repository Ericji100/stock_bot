from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

from trade_monitor_replay import deterministic_state
from trade_monitor_replay.execution_gate import (
    derive_entry_eligibility,
    fill_pending_entry,
    schedule_pending_entry,
)
from trade_monitor_replay.semantic_contract import (
    SemanticReplayError,
    _validate_trade_setup_policy,
)


TZ = timezone(timedelta(hours=8))
BASE = datetime(2040, 1, 2, 9, 0, tzinfo=TZ)


def _at(offset: int) -> str:
    return (BASE + timedelta(minutes=offset)).isoformat()


def _bar(
    offset: int,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> dict[str, Any]:
    return {
        "time": _at(offset),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 100,
    }


def _builder(name: str) -> Callable[..., dict[str, Any] | None]:
    value = getattr(deterministic_state, name, None)
    assert callable(value), f"尚缺預期因果builder：{name}"
    return value


def _grade_upgrade_fixture() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pivots = [
        {"id": "P0", "kind": "LOW", "bar_time": _at(0), "first_seen_at": _at(2), "price": 100.0},
        {"id": "P1", "kind": "HIGH", "bar_time": _at(3), "first_seen_at": _at(5), "price": 110.0},
        {"id": "P2", "kind": "LOW", "bar_time": _at(6), "first_seen_at": _at(8), "price": 105.0},
        {"id": "P3", "kind": "HIGH", "bar_time": _at(9), "first_seen_at": _at(11), "price": 112.0},
        {"id": "P4", "kind": "LOW", "bar_time": _at(12), "first_seen_at": _at(14), "price": 102.0},
    ]
    bars = [
        _bar(0, 102.0, 104.0, 100.0, 103.0),
        _bar(3, 108.0, 110.0, 107.0, 109.0),
        _bar(6, 107.0, 108.0, 105.0, 106.0),
        # P2 becomes a qualified bullish defense here.
        _bar(9, 109.0, 112.0, 108.0, 111.0),
        # It is broken only after qualification; the parent origin still holds.
        _bar(12, 106.0, 107.0, 102.0, 104.0),
        _bar(14, 109.0, 111.5, 108.0, 111.0),
        _bar(15, 111.0, 114.0, 110.5, 113.0),
    ]
    return pivots, bars


def test_grade_upgrade_requires_a_previously_qualified_defense() -> None:
    pivots, bars = _grade_upgrade_fixture()
    expected = datetime.fromisoformat(_at(15))

    events, promoted = deterministic_state._grade_upgrade_evidence(
        pivots,
        bars,
        expected=expected,
    )
    assert len(events) == 1
    assert len(promoted) == 1
    assert events[0]["absorbed_defense_qualified_at"] == _at(9)
    assert events[0]["absorbed_defense_broken_at"] == _at(12)
    assert events[0]["first_seen_at"] == _at(15)
    assert events[0]["qualification_status"] == "PROGRAM_CAUSAL_CANDIDATE"
    assert events[0]["grade_decision_authority"] == "AI_HYBRID"

    not_qualified = [dict(item) for item in bars]
    not_qualified[3] = {
        **not_qualified[3],
        "close": 109.5,
    }
    rejected_events, rejected_promoted = deterministic_state._grade_upgrade_evidence(
        pivots,
        not_qualified,
        expected=expected,
    )
    assert rejected_events == []
    assert rejected_promoted == []


def test_grade_upgrade_requires_parent_survival_and_closed_boundary_reclaim() -> None:
    pivots, bars = _grade_upgrade_fixture()
    expected = datetime.fromisoformat(_at(15))

    parent_failed = [dict(item) for item in bars]
    parent_failed.insert(5, _bar(13, 103.0, 104.0, 99.0, 101.0))
    failed_events, failed_promoted = deterministic_state._grade_upgrade_evidence(
        pivots,
        parent_failed,
        expected=expected,
    )
    assert failed_events == []
    assert failed_promoted == []

    wick_only_reclaim = [dict(item) for item in bars]
    wick_only_reclaim[-1] = {
        **wick_only_reclaim[-1],
        "high": 114.0,
        "close": 111.5,
    }
    wick_events, wick_promoted = deterministic_state._grade_upgrade_evidence(
        pivots,
        wick_only_reclaim,
        expected=expected,
    )
    assert wick_events == []
    assert wick_promoted == []


def _flat(*, as_of: str) -> dict[str, Any]:
    return {
        "version": 2,
        "as_of": as_of,
        "status": "FLAT",
        "entry_time": None,
        "entry_price": None,
        "stop_price": None,
        "direction": None,
        "active_setup_key": None,
        "last_stop_time": None,
        "reentry_count": 0,
        "pending_entry": None,
        "pending_exit": None,
        "last_action": "NONE",
        "last_reason": "空手",
    }


def _q2_slow_fixture() -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    """Synthetic long Q2: outer break, reclaim, right-side break, pullback, relaunch."""

    bars = [
        _bar(0, 101.0, 102.0, 99.0, 99.0),
        _bar(1, 99.0, 100.0, 96.0, 97.0),
        _bar(2, 97.0, 100.0, 96.5, 99.5),
        _bar(3, 99.5, 103.0, 99.0, 102.5),
        _bar(4, 102.5, 106.0, 102.0, 105.5),
        _bar(5, 105.5, 105.7, 102.5, 103.0),
        _bar(6, 103.0, 106.5, 102.8, 106.0),
        _bar(7, 106.2, 108.0, 105.8, 107.5),
    ]
    events = [
        {
            # This is the existing structure-event evidence. The slow Q2
            # builder enriches it with the later anchor/Dow/right-left chain;
            # it must not create a second boundary or anchor system.
            "id": "OUTER-RECLAIM",
            "event_type": "FALSE_BREAK_RECLAIM",
            "direction": "BULL",
            "source_type": "OR15_LOW",
            "source_id": "OR15-LOW",
            "level_price": 100.0,
            "breach_time": _at(0),
            "breach_close": 99.0,
            "breach_extreme": 96.0,
            "reclaim_close": 102.5,
            "bars_to_reclaim": 3,
            "first_seen_at": _at(3),
        },
    ]
    anchor_state = {
        "background_anchor": {
            "id": "A-BEAR",
            "direction": "BEAR",
            "level": "SMALL",
            "status": "DEGRADED",
            "origin_time": _at(-4),
            "origin_price": 108.0,
            "first_seen_at": _at(-1),
            "defense": {
                "id": "D-BEAR-SMALL",
                "direction": "BEAR",
                "level": "SMALL",
                "time": _at(-2),
                "price": 104.0,
                "state": "BROKEN",
                "broken_at": _at(4),
                "first_seen_at": _at(-1),
            },
        },
        "child_anchor": {
            "id": "A-BULL-REVERSE",
            "direction": "BULL",
            "level": "SMALL",
            "status": "ACTIVE",
            "origin_time": _at(1),
            "origin_price": 96.0,
            "first_extreme_time": _at(4),
            "first_extreme_price": 106.0,
            "first_seen_at": _at(4),
        },
        "reverse_candidate": {
            "id": "A-BULL-REVERSE",
            "direction": "BULL",
            "status": "QUALIFIED",
            "origin_time": _at(1),
            "origin_price": 96.0,
            "current_extreme_time": _at(4),
            "current_extreme_price": 106.0,
            "first_seen_at": _at(4),
        },
        "dow_context": {
            "small_bear_defense": {
                "id": "D-BEAR-SMALL",
                "direction": "BEAR",
                "level": "SMALL",
                "time": _at(-2),
                "price": 104.0,
                "state": "BROKEN",
                "broken_at": _at(4),
                "first_seen_at": _at(-1),
            }
        },
        "quadrant_context": {
            "child_comparisons": {
                "anchor_ref": "A-BULL-REVERSE",
                "legs": [
                    {
                        "id": "L-REVERSE-PUSH",
                        "direction": "BULL",
                        "status": "LOCAL_CONFIRMED",
                        "start_time": _at(1),
                        "start_price": 96.0,
                        "end_time": _at(4),
                        "end_price": 106.0,
                        "observable_at": _at(4),
                    },
                    {
                        "id": "RIGHT-LEFT-PULLBACK",
                        "direction": "BEAR",
                        "status": "LOCAL_CONFIRMED",
                        "start_time": _at(4),
                        "start_price": 106.0,
                        "end_time": _at(5),
                        "end_price": 102.5,
                        "observable_at": _at(5),
                    },
                    {
                        "id": "L-RELAUNCH",
                        "direction": "BULL",
                        "status": "FORMING",
                        "start_time": _at(5),
                        "start_price": 102.5,
                        "end_time": _at(6),
                        "end_price": 106.5,
                        "observable_at": _at(6),
                    },
                ],
            }
        },
    }
    return bars, events, anchor_state


def _q4_aggressive_fixture() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Synthetic long Q4 with a signal bar but no two future right-side bars."""

    bars = [
        _bar(-4, 100.0, 102.0, 99.5, 101.5),
        _bar(-3, 101.5, 105.0, 101.0, 104.5),
        _bar(-2, 104.5, 108.0, 104.0, 107.5),
        _bar(-1, 107.5, 110.0, 107.0, 109.0),
        _bar(0, 109.0, 109.2, 106.5, 107.0),
        _bar(1, 107.0, 107.2, 105.0, 106.0),
        # The close is above the prior counter bar high. Its own low is the
        # complete correction extreme known at this close, without right bars.
        _bar(2, 106.0, 108.5, 104.5, 108.0),
        _bar(3, 108.2, 111.0, 107.8, 110.5),
        _bar(4, 110.5, 112.0, 103.0, 104.0),
    ]
    anchor_state = {
        "background_anchor": {
            "id": "A-BULL-CONTROLLER",
            "direction": "BULL",
            "level": "SMALL",
            "status": "ACTIVE",
            "origin_time": _at(-4),
            "origin_price": 99.5,
            "first_extreme_time": _at(-1),
            "first_extreme_price": 110.0,
            "first_seen_at": _at(-1),
            "defense": {
                "id": "D-BULL-SMALL",
                "direction": "BULL",
                "level": "SMALL",
                "time": _at(-3),
                "price": 105.0,
                "state": "ACTIVE",
                "first_seen_at": _at(-1),
            },
        },
        "child_anchor": None,
        "working_leg": {
            "id": "L-CORRECTION",
            "direction": "BEAR",
            "role": "BACKGROUND_RETEST",
            "status": "FORMING",
            "start_time": _at(-1),
            "start_price": 110.0,
            "current_extreme_time": _at(2),
            "current_extreme_price": 104.5,
            "first_seen_at": _at(0),
        },
        "dow_context": {
            "small_bull_defense": {
                "id": "D-BULL-SMALL",
                "direction": "BULL",
                "level": "SMALL",
                "time": _at(-3),
                "price": 105.0,
                "state": "ACTIVE",
                "first_seen_at": _at(-1),
            }
        },
        "quadrant_context": {
            "same_grade_comparisons": {
                "anchor_ref": "A-BULL-CONTROLLER",
                "legs": [
                    {
                        "id": "L-PARENT-PUSH",
                        "direction": "BULL",
                        "status": "LOCAL_CONFIRMED",
                        "start_time": _at(-4),
                        "start_price": 99.5,
                        "end_time": _at(-1),
                        "end_price": 110.0,
                    },
                    {
                        "id": "L-CORRECTION",
                        "direction": "BEAR",
                        "status": "FORMING",
                        "start_time": _at(-1),
                        "start_price": 110.0,
                        "end_time": _at(2),
                        "end_price": 104.5,
                    },
                ],
            }
        },
    }
    return bars, anchor_state


def _q4_promoted_structure_fixture() -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Bull grade upgrade, completed pullback, then first causal relaunch."""

    bars = [
        _bar(0, 100.0, 103.0, 99.5, 102.5),
        _bar(1, 102.5, 106.0, 102.0, 105.5),
        _bar(2, 105.5, 109.0, 105.0, 108.5),
        _bar(3, 108.5, 110.0, 108.0, 109.0),
        _bar(4, 109.0, 109.2, 107.0, 107.5),
        _bar(5, 107.5, 108.0, 106.0, 106.5),
        _bar(6, 106.5, 107.0, 105.0, 105.5),
        # The first relaunch closes above the immediately preceding bearish
        # candle's high. At this cutoff the lifecycle's newest working leg has
        # already turned BULL, so requiring working_leg=BEAR would miss it.
        _bar(7, 105.5, 108.5, 105.2, 108.0),
        _bar(8, 108.0, 111.0, 107.5, 110.5),
    ]
    event = {
        "id": "S-BULL-UPGRADE",
        "event_type": "GRADE_UPGRADE",
        "direction": "BULL",
        "from_level": "SMALL",
        "to_level": "LARGE",
        "parent_origin_pivot_id": "P-PARENT-LOW",
        "parent_origin_time": _at(-4),
        "parent_origin_price": 96.0,
        "absorbed_defense_pivot_id": "P-OLD-DEFENSE",
        "absorbed_defense_time": _at(-2),
        "absorbed_defense_price": 100.0,
        "replacement_defense_pivot_id": "P-NEW-DEFENSE",
        "replacement_defense_time": _at(-1),
        "replacement_defense_price": 98.0,
        "reclaimed_boundary_pivot_id": "P-BOUNDARY",
        "reclaimed_boundary_time": _at(1),
        "reclaimed_boundary_price": 106.0,
        "first_seen_at": _at(2),
        "source_anchor_id": "L-PROMOTED-BULL",
    }
    structural_legs = [
        {
            "id": "L-PARENT-PUSH-AFTER-UPGRADE",
            "level": "SMALL",
            "direction": "BULL",
            "status": "CONFIRMED",
            "start_time": _at(0),
            "start_price": 99.5,
            "end_time": _at(3),
            "end_price": 110.0,
            "first_seen_at": _at(5),
        },
        {
            "id": "L-PROMOTED-BULL",
            "level": "LARGE",
            "direction": "BULL",
            "status": "FORMING",
            "start_time": _at(-4),
            "start_price": 96.0,
            "end_time": _at(7),
            "end_price": 108.5,
            "first_seen_at": _at(2),
            "source": "GRADE_UPGRADE",
            "source_event_id": event["id"],
        },
    ]
    anchor_state = {
        "background_anchor": None,
        "child_anchor": {
            "id": "A-OPENING-BEAR",
            "direction": "BEAR",
            "level": "SMALL",
            "status": "ACTIVE",
            "first_seen_at": _at(-3),
        },
        "working_leg": {
            "id": "L-RELAUNCH-WORKING",
            "direction": "BULL",
            "status": "FORMING",
            "start_time": _at(6),
            "start_price": 105.0,
            "first_seen_at": _at(7),
        },
        "dow_context": {},
        "quadrant_context": {},
    }
    return bars, anchor_state, [event], structural_legs


def test_q2_slow_requires_the_complete_causal_outer_failure_sequence() -> None:
    builder = _builder("_q2_slow_outer_expansion_failure_candidate")
    bars, events, anchor_state = _q2_slow_fixture()

    for cutoff in (2, 4, 5):
        premature = builder(
            events,
            anchor_state=anchor_state,
            expected=datetime.fromisoformat(_at(cutoff)),
            bars=bars,
        )
        assert premature is None or premature.get("stage") not in {
            "ARMED",
            "ENTRY_ELIGIBLE",
            "AGGRESSIVE_CONFIRMED",
            "CONSERVATIVE_CONFIRMED",
        }

    candidate = builder(
        events,
        anchor_state=anchor_state,
        expected=datetime.fromisoformat(_at(6)),
        bars=bars,
    )

    assert candidate is not None
    assert candidate["direction"] == "LONG"
    assert candidate["candidate_source"] == "Q2_SLOW_OUTER_EXPANSION_FAILURE"
    assert candidate["entry_strategy"] == "Q2_SLOW_OUTER_EXPANSION_FAILURE"
    assert candidate["boundary_ref"] == "OR15-LOW"
    assert candidate["reverse_anchor_ref"] == "A-BULL-REVERSE"
    assert candidate["broken_defense_ref"] == "D-BEAR-SMALL"
    assert candidate["right_left_pullback_ref"] == "RIGHT-LEFT-PULLBACK"
    assert candidate["trigger_time"] == _at(6)
    assert candidate["trigger_level"] == 105.7
    # The Q2 false-break family owns the outer expansion extreme, not a later
    # convenient one-bar low, as its structural stop source.
    assert candidate["stop_source_time"] == _at(1)
    assert candidate["stop_source_price"] == 96.0
    assert candidate["validity_policy"]["kind"] == "STRUCTURAL"
    assert candidate["facts_cutoff"] == _at(6)
    assert 2 <= candidate["behavior_max_wait_bars"] <= 3


def test_q2_slow_does_not_read_a_later_lower_low() -> None:
    builder = _builder("_q2_slow_outer_expansion_failure_candidate")
    bars, events, anchor_state = _q2_slow_fixture()
    future = _bar(8, 106.0, 106.2, 90.0, 91.0)

    causal = builder(
        events,
        anchor_state=anchor_state,
        expected=datetime.fromisoformat(_at(6)),
        bars=bars[:7],
    )
    with_future_input = builder(
        events,
        anchor_state=anchor_state,
        expected=datetime.fromisoformat(_at(6)),
        bars=[*bars, future],
    )

    assert causal is not None and with_future_input is not None
    assert with_future_input["facts_hash"] == causal["facts_hash"]
    assert with_future_input["stop_source_price"] == 96.0
    assert with_future_input["facts_cutoff"] == _at(6)


def test_q2_slow_freezes_atr_stop_and_facts_at_first_signal() -> None:
    builder = _builder("_q2_slow_outer_expansion_failure_candidate")
    bars, events, anchor_state = _q2_slow_fixture()
    history = [_bar(index, 100.0, 101.0, 99.0, 100.0) for index in range(-20, -4)]
    later_wide_bar = {
        **bars[7],
        "high": 200.0,
    }

    at_signal = builder(
        events,
        anchor_state=anchor_state,
        expected=datetime.fromisoformat(_at(6)),
        bars=[*history, *bars[:7]],
    )
    reviewed_later = builder(
        events,
        anchor_state=anchor_state,
        expected=datetime.fromisoformat(_at(7)),
        bars=[*history, *bars[:7], later_wide_bar],
    )

    assert at_signal is not None and reviewed_later is not None
    assert reviewed_later["setup_key"] == at_signal["setup_key"]
    assert reviewed_later["facts_cutoff"] == at_signal["facts_cutoff"] == _at(6)
    assert reviewed_later["stop_buffer_points"] == at_signal["stop_buffer_points"]
    assert reviewed_later["stop_price"] == at_signal["stop_price"]
    assert reviewed_later["facts_hash"] == at_signal["facts_hash"]


def test_q4_aggressive_can_confirm_without_two_n2_right_bars() -> None:
    builder = _builder("_q4_aggressive_pullback_reversal_candidate")
    bars, anchor_state = _q4_aggressive_fixture()

    candidate = builder(
        anchor_state,
        expected=datetime.fromisoformat(_at(2)),
        bars=bars[:7],
    )

    assert candidate is not None
    assert candidate["direction"] == "LONG"
    assert candidate["candidate_source"] == "Q4_AGGRESSIVE_PULLBACK_REVERSAL"
    assert candidate["entry_strategy"] == "Q4_AGGRESSIVE_PULLBACK_REVERSAL"
    assert candidate["confirmation_style"] == "AGGRESSIVE"
    assert candidate["controller_anchor_ref"] == "A-BULL-CONTROLLER"
    assert candidate["controller_defense_ref"] == "D-BULL-SMALL"
    assert candidate["parent_push_ref"] == "L-PARENT-PUSH"
    assert candidate["observation_zone_refs"] == ["D-BULL-SMALL"]
    assert candidate["trigger_time"] == _at(2)
    assert candidate["trigger_level"] == 107.2
    assert candidate["stop_source_time"] == _at(2)
    assert candidate["stop_source_price"] == 104.5
    assert candidate["stop_extreme_status"] == "CAUSAL_SO_FAR_NOT_N2"
    assert candidate["validity_policy"]["kind"] == "STRUCTURAL"
    assert candidate["evidence_span_bars"] >= 2
    assert candidate["valid_bars"] == 3
    assert 3 <= candidate["behavior_max_wait_bars"] <= 5


def test_q4_aggressive_ignores_future_right_bars_and_future_extreme() -> None:
    builder = _builder("_q4_aggressive_pullback_reversal_candidate")
    bars, anchor_state = _q4_aggressive_fixture()

    causal = builder(
        anchor_state,
        expected=datetime.fromisoformat(_at(2)),
        bars=bars[:7],
    )
    with_future_input = builder(
        anchor_state,
        expected=datetime.fromisoformat(_at(2)),
        bars=bars,
    )

    assert causal is not None and with_future_input is not None
    assert with_future_input["facts_hash"] == causal["facts_hash"]
    assert with_future_input["stop_source_time"] == _at(2)
    assert with_future_input["stop_source_price"] == 104.5
    assert with_future_input["facts_cutoff"] == _at(2)


def test_q4_aggressive_freezes_atr_stop_and_facts_at_first_signal() -> None:
    builder = _builder("_q4_aggressive_pullback_reversal_candidate")
    bars, anchor_state = _q4_aggressive_fixture()
    history = [_bar(index, 100.0, 101.0, 99.0, 100.0) for index in range(-20, -4)]
    later_wide_bar = {
        **bars[7],
        "high": 200.0,
    }

    at_signal = builder(
        anchor_state,
        expected=datetime.fromisoformat(_at(2)),
        bars=[*history, *bars[:7]],
    )
    reviewed_later = builder(
        anchor_state,
        expected=datetime.fromisoformat(_at(3)),
        bars=[*history, *bars[:7], later_wide_bar],
    )

    assert at_signal is not None and reviewed_later is not None
    assert reviewed_later["setup_key"] == at_signal["setup_key"]
    assert reviewed_later["facts_cutoff"] == at_signal["facts_cutoff"] == _at(2)
    assert reviewed_later["stop_buffer_points"] == at_signal["stop_buffer_points"]
    assert reviewed_later["stop_price"] == at_signal["stop_price"]
    assert reviewed_later["facts_hash"] == at_signal["facts_hash"]


def test_q4_aggressive_accepts_a_multi_bar_micro_base_relaunch() -> None:
    builder = _builder("_q4_aggressive_pullback_reversal_candidate")
    bars, anchor_state = _q4_aggressive_fixture()
    bars[5] = _bar(1, 107.0, 107.2, 105.0, 107.0)  # doji/base, not a counter bar
    bars[6] = _bar(2, 107.0, 110.0, 104.5, 109.5)

    candidate = builder(
        anchor_state,
        expected=datetime.fromisoformat(_at(2)),
        bars=bars[:7],
    )

    assert candidate is not None
    assert candidate["trigger_time"] == _at(2)
    assert candidate["trigger_level"] == 109.2
    assert candidate["behavior_obstacles"][0]["source_time"] == _at(0)
    assert candidate["stop_source_time"] == _at(2)
    assert candidate["stop_source_price"] == 104.5


def test_q4_aggressive_can_publish_a_new_relaunch_after_the_first_stop_fails() -> None:
    builder = _builder("_q4_aggressive_pullback_reversal_candidate")
    bars, anchor_state = _q4_aggressive_fixture()
    bars = [
        *bars[:7],
        _bar(3, 108.2, 111.0, 107.8, 110.5),
        # This wick invalidates the first frozen stop without closing through
        # the controller defense; the bullish parent may still be reassessed.
        _bar(4, 110.5, 112.0, 103.0, 106.0),
        _bar(5, 106.0, 107.0, 104.0, 105.5),
        _bar(6, 105.5, 106.0, 104.5, 105.5),
        _bar(7, 105.5, 108.5, 105.0, 108.0),
    ]

    candidate = builder(
        anchor_state,
        expected=datetime.fromisoformat(_at(7)),
        bars=bars,
    )

    assert candidate is not None
    assert candidate["trigger_time"] == _at(7)
    assert candidate["trigger_level"] == 107.0
    assert candidate["stop_source_time"] == _at(4)
    assert candidate["stop_source_price"] == 103.0


def test_q4_aggressive_accepts_active_bull_grade_upgrade_after_working_leg_turns_up() -> None:
    builder = _builder("_q4_aggressive_pullback_reversal_candidate")
    bars, anchor_state, events, legs = _q4_promoted_structure_fixture()

    candidate = builder(
        anchor_state,
        expected=datetime.fromisoformat(_at(7)),
        bars=bars[:8],
        structure_events=events,
        structural_legs=legs,
    )

    assert candidate is not None
    assert candidate["controller_anchor_ref"] == "L-PROMOTED-BULL"
    assert candidate["controller_structure_event_ref"] == "S-BULL-UPGRADE"
    assert candidate["controller_defense_ref"] == "P-NEW-DEFENSE"
    assert candidate["parent_push_ref"] == "L-PARENT-PUSH-AFTER-UPGRADE"
    assert candidate["trigger_time"] == _at(7)
    assert candidate["trigger_level"] == 107.0
    assert candidate["stop_source_time"] == _at(6)
    assert candidate["stop_source_price"] == 105.0
    # Control evidence must not mutate/manufacture a formal large anchor.
    assert anchor_state["background_anchor"] is None


def test_q4_promoted_candidate_is_causal_and_retires_with_its_upgrade() -> None:
    builder = _builder("_q4_aggressive_pullback_reversal_candidate")
    bars, anchor_state, events, legs = _q4_promoted_structure_fixture()
    future_failure = _bar(9, 110.5, 111.0, 90.0, 91.0)

    causal = builder(
        anchor_state,
        expected=datetime.fromisoformat(_at(7)),
        bars=bars[:8],
        structure_events=events,
        structural_legs=legs,
    )
    with_future_input = builder(
        anchor_state,
        expected=datetime.fromisoformat(_at(7)),
        bars=[*bars, future_failure],
        structure_events=events,
        structural_legs=legs,
    )
    downgraded = builder(
        anchor_state,
        expected=datetime.fromisoformat(_at(8)),
        bars=bars,
        structure_events=[
            *events,
            {
                "id": "S-BULL-DOWNGRADE",
                "event_type": "GRADE_DOWNGRADE",
                "source_upgrade_event_id": "S-BULL-UPGRADE",
                "first_seen_at": _at(8),
            },
        ],
        structural_legs=legs,
    )

    assert causal is not None and with_future_input is not None
    assert with_future_input["facts_hash"] == causal["facts_hash"]
    assert downgraded is None


def test_local_bull_upgrade_cannot_override_retained_formal_large_bear_anchor() -> None:
    builder = _builder("_q4_aggressive_pullback_reversal_candidate")
    bars, anchor_state, events, legs = _q4_promoted_structure_fixture()
    anchor_state["background_anchor"] = {
        "id": "A-LARGE-BEAR",
        "direction": "BEAR",
        "level": "LARGE",
        "status": "ACTIVE",
        "first_seen_at": _at(-10),
    }

    assert builder(
        anchor_state,
        expected=datetime.fromisoformat(_at(7)),
        bars=bars[:8],
        structure_events=events,
        structural_legs=legs,
    ) is None


def test_new_candidates_retire_after_their_objective_stop_or_defense_fails() -> None:
    q2_builder = _builder("_q2_slow_outer_expansion_failure_candidate")
    q4_builder = _builder("_q4_aggressive_pullback_reversal_candidate")
    q2_bars, events, q2_state = _q2_slow_fixture()
    q4_bars, q4_state = _q4_aggressive_fixture()

    q2_invalidated = q2_builder(
        events,
        anchor_state=q2_state,
        expected=datetime.fromisoformat(_at(8)),
        bars=[*q2_bars, _bar(8, 106.0, 106.2, 90.0, 91.0)],
    )
    q4_invalidated = q4_builder(
        q4_state,
        expected=datetime.fromisoformat(_at(4)),
        bars=q4_bars,
    )

    assert q2_invalidated is None
    assert q4_invalidated is None


def test_q4_aggressive_signal_fills_only_at_the_next_unrevealed_open() -> None:
    builder = _builder("_q4_aggressive_pullback_reversal_candidate")
    bars, anchor_state = _q4_aggressive_fixture()
    candidate = builder(
        anchor_state,
        expected=datetime.fromisoformat(_at(2)),
        bars=bars[:7],
    )
    assert candidate is not None

    gate = derive_entry_eligibility(
        None,
        bars[:7],
        as_of=_at(2),
        position=_flat(as_of=_at(2)),
        current_candidate={
            **candidate,
            "stage": "ARMED",
            "decision_authority": "AI_HYBRID",
        },
    )
    assert gate["status"] == "ENTRY_ELIGIBLE"
    assert gate["signal_time"] == _at(2)
    assert gate["eligible_from"] == _at(3)

    stop = float(candidate["stop_price"])
    queued = schedule_pending_entry(
        _flat(as_of=_at(2)),
        {
            "course_reading": {"setup_stage": "ENTRY_ELIGIBLE"},
            "action": {
                "position_action": "ENTER",
                "direction": "LONG",
                "entry_role": "INITIAL",
                "stop_price": stop,
                "trigger": "已收盤K完成積極型止跌轉強。",
                "expected_behavior": "守住修正結構並恢復多方推進。",
                "max_wait_bars": candidate.get("behavior_max_wait_bars"),
                "obstacles": candidate.get("behavior_obstacles", []),
            },
        },
        gate,
        as_of=_at(2),
    )

    still_flat, event = fill_pending_entry(queued, bars[:7], as_of=_at(2))
    assert event is None
    assert still_flat["status"] == "FLAT"

    filled, event = fill_pending_entry(queued, bars[:8], as_of=_at(3))
    assert event is not None
    assert event["event_type"] == "ENTRY_FILLED"
    assert filled["entry_time"] == _at(3)
    assert filled["entry_price"] == bars[7]["open"]
    assert filled["entry_price"] != bars[6]["close"]


def test_new_hard_fact_candidates_are_published_together_for_ai_review() -> None:
    q2_builder = _builder("_q2_slow_outer_expansion_failure_candidate")
    q4_builder = _builder("_q4_aggressive_pullback_reversal_candidate")
    inventory_builder = _builder("_build_ai_hybrid_q2_q4_candidate_inventory")
    q2_bars, events, q2_state = _q2_slow_fixture()
    q4_bars, q4_state = _q4_aggressive_fixture()
    q2 = q2_builder(
        events,
        anchor_state=q2_state,
        expected=datetime.fromisoformat(_at(6)),
        bars=q2_bars[:7],
    )
    q4 = q4_builder(
        q4_state,
        expected=datetime.fromisoformat(_at(2)),
        bars=q4_bars[:7],
    )
    assert q2 is not None and q4 is not None

    inventory, audits = inventory_builder(
        (q2, q4),
        expected=datetime.fromisoformat(_at(6)),
        anchor_state=q2_state,
        course_method_state={},
        structure_events=events,
    )

    assert {item["setup_key"] for item in inventory} == {
        q2["setup_key"],
        q4["setup_key"],
    }
    assert all(item["decision_authority"] == "AI_HYBRID" for item in inventory)
    assert all(item["hard_fact_gate"]["status"] == "PASSED" for item in inventory)
    assert {item["hard_gate_status"] for item in audits} == {"PASSED"}


def test_false_break_ai_candidate_requires_frozen_facts_identity() -> None:
    hard_reasons = _builder("_ai_hybrid_candidate_basic_hard_reasons")
    candidate = {
        "setup_key": "SETUP-Q2-FAST",
        "direction": "LONG",
        "candidate_source": "FALSE_BREAK_RECLAIM",
        "entry_strategy": "Q2_FALSE_BREAK_RECLAIM",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 105.0,
        "stop_source_price": 95.0,
        "stop_price": 94.0,
        "valid_bars": 3,
        "first_seen_at": _at(2),
        "trigger_time": _at(2),
        "stop_source_time": _at(1),
        "source_event_id": "FALSE-BREAK-EVENT",
        "facts_cutoff": _at(2),
    }

    reasons = hard_reasons(
        candidate,
        expected=datetime.fromisoformat(_at(2)),
    )
    assert "FACTS_HASH_MISSING" in reasons

    candidate["facts_hash"] = "FACTS-" + "a" * 64
    assert "FACTS_HASH_MISSING" not in hard_reasons(
        candidate,
        expected=datetime.fromisoformat(_at(2)),
    )


def _entry_analysis(
    family: str,
    *,
    setup_key: str,
    facts_hash: str,
    assessment: str,
) -> dict[str, Any]:
    axes = {
        "Q2": ("DECREASING", "EXPANDING"),
        "Q4": ("INCREASING", "CONTRACTING"),
    }
    trend, volatility = axes[family]
    return {
        "course_reading": {
            "working_quadrant": family,
            "primary_quadrant_candidate": family,
            "working_trend_dynamics": trend,
            "working_volatility_dynamics": volatility,
            "main_strategy_family": family,
        },
        "ai_course_assessment": {
            "setup_key": setup_key,
            "facts_hash": facts_hash,
            "overall": assessment,
            "checks": {
                "anchor_control": assessment,
                "grade_control": assessment,
                "dow_defense": assessment,
                "quadrant_axes": assessment,
                "taiji_relation": assessment,
                "location_quality": assessment,
                "trigger_quality": assessment,
                "stop_integrity": assessment,
                "expected_behavior": assessment,
                "course_permission": assessment,
            },
        },
        "action": {"position_action": "ENTER", "direction": "LONG"},
    }


def _q2_q4_policy_ledger() -> dict[str, Any]:
    return {
        "program_trade_policy": {
            "decision_authority": "AI_HYBRID",
            "hard_fact_authority": "PROGRAM",
            "interpretive_authority": "AI",
            "trade_direction_policy": "LONG_ONLY",
            "trade_setup_policy": "LONG_Q2_Q4_ONLY",
        }
    }


@pytest.mark.parametrize(
    ("family", "source"),
    [
        ("Q2", "Q2_SLOW_OUTER_EXPANSION_FAILURE"),
        ("Q4", "Q4_AGGRESSIVE_PULLBACK_REVERSAL"),
    ],
)
def test_ai_unknown_cannot_authorize_entry(family: str, source: str) -> None:
    setup_key = f"{family}-SYNTHETIC"
    facts_hash = f"FACTS-{family}"

    with pytest.raises(SemanticReplayError, match="UNKNOWN|不明"):
        _validate_trade_setup_policy(
            _entry_analysis(
                family,
                setup_key=setup_key,
                facts_hash=facts_hash,
                assessment="UNKNOWN",
            ),
            ledger=_q2_q4_policy_ledger(),
            position={"status": "FLAT"},
            entry_gate={
                "status": "ENTRY_ELIGIBLE",
                "setup_key": setup_key,
                "direction": "LONG",
                "required_candidate_source": source,
                "required_entry_strategy": source,
                "required_facts_hash": facts_hash,
            },
        )


@pytest.mark.parametrize(
    ("family", "source"),
    [
        ("Q2", "Q2_SLOW_OUTER_EXPANSION_FAILURE"),
        ("Q4", "Q4_AGGRESSIVE_PULLBACK_REVERSAL"),
    ],
)
def test_ai_pass_with_matching_facts_can_reach_entry_eligible(
    family: str,
    source: str,
) -> None:
    setup_key = f"{family}-SYNTHETIC"
    facts_hash = f"FACTS-{family}"

    _validate_trade_setup_policy(
        _entry_analysis(
            family,
            setup_key=setup_key,
            facts_hash=facts_hash,
            assessment="PASS",
        ),
        ledger=_q2_q4_policy_ledger(),
        position={"status": "FLAT"},
        entry_gate={
            "status": "ENTRY_ELIGIBLE",
            "setup_key": setup_key,
            "direction": "LONG",
            "required_candidate_source": source,
            "required_entry_strategy": source,
            "required_facts_hash": facts_hash,
        },
    )


@pytest.mark.parametrize(
    ("family", "source", "strategy"),
    [
        ("Q2", "Q2_SLOW_OUTER_EXPANSION_FAILURE", "Q2_SLOW_OUTER_EXPANSION_FAILURE"),
        ("Q4", "Q4_AGGRESSIVE_PULLBACK_REVERSAL", "Q4_AGGRESSIVE_PULLBACK_REVERSAL"),
    ],
)
def test_frozen_q2_q4_origin_can_enter_after_trigger_bar_transitions_to_q1(
    family: str,
    source: str,
    strategy: str,
) -> None:
    setup_key = f"{family}-TRIGGER-TO-Q1"
    facts_hash = f"FACTS-{family}-TRIGGER-TO-Q1"
    analysis = _entry_analysis(
        family,
        setup_key=setup_key,
        facts_hash=facts_hash,
        assessment="PASS",
    )
    analysis["course_reading"].update(
        {
            "working_quadrant": "Q1",
            "primary_quadrant_candidate": "Q1",
            "working_trend_dynamics": "INCREASING",
            "working_volatility_dynamics": "EXPANDING",
        }
    )
    ledger = _q2_q4_policy_ledger()
    ledger["trade_levels"] = {
        "ai_armable_setup_keys": [setup_key],
        "ai_candidate_inventory": [
            {
                "setup_key": setup_key,
                "candidate_source": source,
                "entry_strategy": strategy,
                "facts_hash": facts_hash,
                "hard_fact_gate": {"status": "PASSED"},
            }
        ],
    }

    _validate_trade_setup_policy(
        analysis,
        ledger=ledger,
        position={"status": "FLAT"},
        entry_gate={
            "status": "ENTRY_ELIGIBLE",
            "setup_key": setup_key,
            "direction": "LONG",
            "required_candidate_source": source,
            "required_entry_strategy": strategy,
            "required_facts_hash": facts_hash,
        },
    )


def test_arbitrary_q1_cannot_enter_by_borrowing_q2_family_without_frozen_assessment() -> None:
    analysis = {
        "course_reading": {
            "working_quadrant": "Q1",
            "primary_quadrant_candidate": "Q1",
            "working_trend_dynamics": "INCREASING",
            "working_volatility_dynamics": "EXPANDING",
            "main_strategy_family": "Q2",
        },
        "action": {"position_action": "ENTER", "direction": "LONG"},
    }

    with pytest.raises(SemanticReplayError, match="合法重新發動"):
        _validate_trade_setup_policy(
            analysis,
            ledger=_q2_q4_policy_ledger(),
            position={"status": "FLAT"},
            entry_gate={
                "status": "ENTRY_ELIGIBLE",
                "required_candidate_source": "FALSE_BREAK_RECLAIM",
                "required_entry_strategy": "Q2_FALSE_BREAK_RECLAIM",
            },
        )


def test_trigger_q1_requires_a_program_armable_candidate_with_passed_hard_gate() -> None:
    setup_key = "Q2-HARD-GATE-FAILED"
    facts_hash = "FACTS-Q2-HARD-GATE-FAILED"
    analysis = _entry_analysis(
        "Q2",
        setup_key=setup_key,
        facts_hash=facts_hash,
        assessment="PASS",
    )
    analysis["course_reading"].update(
        {
            "working_quadrant": "Q1",
            "primary_quadrant_candidate": "Q1",
            "working_trend_dynamics": "INCREASING",
            "working_volatility_dynamics": "EXPANDING",
        }
    )
    ledger = _q2_q4_policy_ledger()
    ledger["trade_levels"] = {
        "ai_armable_setup_keys": [setup_key],
        "ai_candidate_inventory": [
            {
                "setup_key": setup_key,
                "candidate_source": "Q2_SLOW_OUTER_EXPANSION_FAILURE",
                "entry_strategy": "Q2_SLOW_OUTER_EXPANSION_FAILURE",
                "facts_hash": facts_hash,
                "hard_fact_gate": {"status": "FAILED"},
            }
        ],
    }

    with pytest.raises(SemanticReplayError, match="合法重新發動"):
        _validate_trade_setup_policy(
            analysis,
            ledger=ledger,
            position={"status": "FLAT"},
            entry_gate={
                "status": "ENTRY_ELIGIBLE",
                "setup_key": setup_key,
                "direction": "LONG",
                "required_candidate_source": "Q2_SLOW_OUTER_EXPANSION_FAILURE",
                "required_entry_strategy": "Q2_SLOW_OUTER_EXPANSION_FAILURE",
                "required_facts_hash": facts_hash,
            },
        )
