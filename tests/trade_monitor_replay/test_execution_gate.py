from __future__ import annotations

from datetime import datetime

from trade_monitor_replay.deterministic_state import _forming_leg
from trade_monitor_replay.execution_gate import (
    advance_program_risk_controls,
    build_event_lifecycle,
    derive_entry_eligibility,
    derive_position_behavior_audit,
    derive_program_behavior_exit_audit,
    derive_protective_stop_audit,
    expire_stale_armed_setups,
    fill_pending_entry,
    fill_pending_exit,
    invalidate_preentry_stop_touched_setup,
    mark_events_analyzed,
    schedule_pending_entry,
    schedule_pending_exit,
)


def _bar(at: str, open_: float, high: float, low: float, close: float) -> dict:
    return {
        "time": f"2026-08-25T{at}:00+08:00",
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 10,
    }


def _memory() -> dict:
    return {
        "version": 3,
        "as_of": "2026-08-25T09:36:00+08:00",
        "active_setups": [
            {
                "setup_key": "bull-q4",
                "name": "Q4順勢回檔",
                "direction": "LONG",
                "stage": "ARMED",
                "trigger": "收盤突破100。",
                "stop": "90外。",
                "reentry_status": "NOT_APPLICABLE",
                "trigger_level": 100,
                "trigger_operator": "CLOSE_ABOVE",
                "valid_bars": 3,
            }
        ],
    }


def _flat() -> dict:
    return {
        "version": 2,
        "as_of": "2026-08-25T09:36:00+08:00",
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


def test_armed_setup_is_invalidated_when_its_stop_is_touched_before_entry() -> None:
    candidate = {
        "setup_key": "bear-q4-old",
        "setup_name": "空方修正後複製",
        "direction": "SHORT",
        "stage": "ARMED",
        "decision_authority": "PROGRAM",
        "first_seen_at": "2026-08-25T10:51:00+08:00",
        "stop_price": 45075.0,
    }
    memory = {
        **_memory(),
        "as_of": "2026-08-25T10:54:00+08:00",
        "active_setups": [
            {
                **_memory()["active_setups"][0],
                "setup_key": "bear-q4-old",
                "direction": "SHORT",
            }
        ],
    }

    updated, audit = invalidate_preentry_stop_touched_setup(
        memory,
        [
            _bar("10:52", 45045, 45052, 45008, 45020),
            _bar("10:54", 45054, 45064, 45033, 45049),
            _bar("10:55", 45050, 45081, 45049, 45080),
            _bar("10:56", 45080, 45084, 45026, 45029),
        ],
        as_of="2026-08-25T10:56:00+08:00",
        current_candidate=candidate,
        position=_flat(),
    )

    assert audit["status"] == "INVALIDATED_BEFORE_ENTRY"
    assert audit["invalidated_at"].endswith("10:55:00+08:00")
    assert audit["reason"] == "STRUCTURAL_STOP_TOUCHED_BEFORE_ENTRY"
    assert updated["active_setups"][0]["stage"] == "INVALIDATED"
    assert "10:55" in updated["active_setups"][0]["trigger"]


def test_armed_setup_remains_active_when_preentry_stop_is_not_touched() -> None:
    candidate = {
        "setup_key": "bear-q4-live",
        "setup_name": "空方修正後複製",
        "direction": "SHORT",
        "stage": "ARMED",
        "decision_authority": "PROGRAM",
        "first_seen_at": "2026-08-25T10:51:00+08:00",
        "stop_price": 45075.0,
    }
    memory = {
        **_memory(),
        "as_of": "2026-08-25T10:54:00+08:00",
        "active_setups": [
            {
                **_memory()["active_setups"][0],
                "setup_key": "bear-q4-live",
                "direction": "SHORT",
            }
        ],
    }

    updated, audit = invalidate_preentry_stop_touched_setup(
        memory,
        [_bar("10:55", 45050, 45074, 45020, 45030)],
        as_of="2026-08-25T10:55:00+08:00",
        current_candidate=candidate,
        position=_flat(),
    )

    assert audit["status"] == "NONE"
    assert updated["active_setups"][0]["stage"] == "ARMED"


def test_unseen_program_candidate_is_retired_when_its_frozen_stop_was_already_touched() -> None:
    candidate = {
        "setup_key": "unseen-or5-retest",
        "setup_name": "OR5突破回踩重新發動",
        "direction": "LONG",
        "stage": "ARMED",
        "decision_authority": "AI_HYBRID",
        "first_seen_at": "2026-08-25T08:53:00+08:00",
        "trigger_level": 44914.0,
        "trigger_operator": "CLOSE_ABOVE",
        "valid_bars": 5,
        "stop_price": 44869.0,
    }
    memory = {
        **_memory(),
        "as_of": "2026-08-25T08:57:00+08:00",
        "active_setups": [],
    }

    updated, audit = invalidate_preentry_stop_touched_setup(
        memory,
        [
            _bar("08:54", 44955, 44967, 44901, 44910),
            _bar("08:58", 44924, 44925, 44857, 44865),
        ],
        as_of="2026-08-25T08:58:00+08:00",
        current_candidate=candidate,
        position=_flat(),
    )

    assert audit["status"] == "INVALIDATED_BEFORE_ENTRY"
    assert audit["setup_key"] == "unseen-or5-retest"
    assert audit["invalidated_at"].endswith("08:58:00+08:00")
    assert updated["active_setups"] == [
        {
            "setup_key": "unseen-or5-retest",
            "name": "OR5突破回踩重新發動",
            "direction": "LONG",
            "stage": "INVALIDATED",
            "trigger": "進場前於08:58觸及原結構停損；舊setup退役，須等待新的完整修正。",
            "stop": "原程式結構停損44869點已被觸及，不得沿用。",
            "reentry_status": "NOT_APPLICABLE",
            "trigger_level": 44914.0,
            "trigger_operator": "CLOSE_ABOVE",
            "valid_bars": 5,
        }
    ]


def test_preentry_invalidation_does_not_reclassify_a_setup_already_stopped_after_fill() -> None:
    candidate = {
        "setup_key": "filled-then-stopped",
        "setup_name": "多方一之早期動能",
        "direction": "LONG",
        "stage": "ARMED",
        "decision_authority": "PROGRAM",
        "first_seen_at": "2026-08-25T08:56:00+08:00",
        "stop_price": 45136.0,
    }
    memory = {
        **_memory(),
        "active_setups": [
            {
                **_memory()["active_setups"][0],
                "setup_key": "filled-then-stopped",
            }
        ],
    }
    stopped = {
        **_flat(),
        "active_setup_key": "filled-then-stopped",
        "last_stop_time": "2026-08-25T09:02:00+08:00",
        "last_action": "STOP",
    }

    updated, audit = invalidate_preentry_stop_touched_setup(
        memory,
        [_bar("09:02", 45180, 45190, 45120, 45130)],
        as_of="2026-08-25T09:04:00+08:00",
        current_candidate=candidate,
        position=stopped,
    )

    assert audit["status"] == "NONE"
    assert updated["active_setups"][0]["stage"] == "ARMED"


def test_behavior_exit_remains_pending_until_next_bar_open() -> None:
    position = {
        **_flat(),
        "status": "LONG",
        "entry_time": "2026-08-25T09:38:00+08:00",
        "entry_price": 103.0,
        "stop_price": 95.0,
        "direction": "LONG",
        "active_setup_key": "bull-q1",
    }
    analysis = {"action": {"position_action": "EXIT"}}
    audit = {
        "status": "PENDING_FILL",
        "direction": "LONG",
        "setup_key": "bull-q1",
        "signal_time": "2026-08-25T09:42:00+08:00",
        "reason_code": "EXPECTED_BEHAVIOR_EXPIRED_NO_PROGRESS",
    }

    queued = schedule_pending_exit(
        position,
        analysis,
        audit,
        as_of="2026-08-25T09:42:00+08:00",
    )

    assert queued["status"] == "LONG"
    assert queued["last_action"] == "EXIT_QUEUED"
    assert queued["pending_exit"]["eligible_from"].endswith("09:43:00+08:00")

    unchanged, event = fill_pending_exit(
        queued,
        [_bar("09:42", 102, 104, 100, 101)],
        as_of="2026-08-25T09:42:00+08:00",
    )
    assert unchanged["status"] == "LONG"
    assert event is None

    exited, event = fill_pending_exit(
        queued,
        [
            _bar("09:42", 102, 104, 100, 101),
            _bar("09:43", 101, 102, 99, 100),
        ],
        as_of="2026-08-25T09:43:00+08:00",
    )
    assert exited["status"] == "FLAT"
    assert exited["pending_exit"] is None
    assert event["event_type"] == "EXIT_FILLED"
    assert event["fill_time"].endswith("09:43:00+08:00")
    assert event["fill_price"] == 101.0


def test_pending_exit_blocks_a_second_exit_or_simultaneous_entry() -> None:
    position = {
        **_flat(),
        "status": "SHORT",
        "entry_time": "2026-08-25T09:38:00+08:00",
        "entry_price": 103.0,
        "stop_price": 110.0,
        "direction": "SHORT",
        "active_setup_key": "bear-q1",
        "pending_exit": {
            "setup_key": "bear-q1",
            "direction": "SHORT",
            "signal_time": "2026-08-25T09:42:00+08:00",
            "eligible_from": "2026-08-25T09:43:00+08:00",
        },
    }
    audit = {
        "status": "PENDING_FILL",
        "direction": "SHORT",
        "signal_time": "2026-08-25T09:42:00+08:00",
    }

    import pytest
    from trade_monitor_replay.execution_gate import ReplayExecutionGateError

    with pytest.raises(ReplayExecutionGateError, match="重複排入"):
        schedule_pending_exit(
            position,
            {"action": {"position_action": "EXIT"}},
            audit,
            as_of="2026-08-25T09:42:00+08:00",
        )


def test_protective_stop_audit_uses_locked_stop_and_first_touched_bar() -> None:
    position = {
        **_flat(),
        "as_of": "2026-08-25T09:40:00+08:00",
        "status": "LONG",
        "entry_time": "2026-08-25T09:38:00+08:00",
        "entry_price": 103.0,
        "stop_price": 95.5,
        "direction": "LONG",
        "active_setup_key": "bull-q4",
    }
    audit = derive_protective_stop_audit(
        position,
        [
            _bar("09:41", 100, 102, 96, 101),
            _bar("09:42", 100, 101, 94, 99),
            _bar("09:43", 90, 92, 88, 89),
        ],
        as_of="2026-08-25T09:43:00+08:00",
    )

    assert audit["status"] == "TRIGGERED"
    assert audit["trigger_time"].endswith("09:42:00+08:00")
    assert audit["fill_price"] == 95.5
    assert audit["gap_through_stop"] is False
    assert audit["event_id"].startswith("EG-")


def test_protective_stop_gap_fills_at_bar_open() -> None:
    position = {
        **_flat(),
        "as_of": "2026-08-25T09:40:00+08:00",
        "status": "LONG",
        "entry_time": "2026-08-25T09:38:00+08:00",
        "entry_price": 103.0,
        "stop_price": 95.5,
        "direction": "LONG",
        "active_setup_key": "bull-q4",
    }
    audit = derive_protective_stop_audit(
        position,
        [_bar("09:41", 93, 96, 91, 94)],
        as_of="2026-08-25T09:41:00+08:00",
    )

    assert audit["status"] == "TRIGGERED"
    assert audit["fill_price"] == 93
    assert audit["gap_through_stop"] is True


def test_entry_signal_is_eligible_then_fills_at_next_bar_open() -> None:
    bars = [
        _bar("09:36", 98, 100, 97, 99),
        _bar("09:37", 99, 102, 98, 101),
    ]
    gate = derive_entry_eligibility(
        _memory(),
        bars,
        as_of="2026-08-25T09:37:00+08:00",
        position=_flat(),
    )
    assert gate["status"] == "ENTRY_ELIGIBLE"
    assert gate["signal_time"].endswith("09:37:00+08:00")
    assert gate["eligible_from"].endswith("09:38:00+08:00")


    analysis = {
        "notification_reason": "09:37收盤完成多方觸發。",
        "course_reading": {
            "setup_stage": "ENTRY_ELIGIBLE",
            "main_strategy_family": "Q4",
        },
        "action": {
            "position_action": "ENTER",
            "direction": "LONG",
            "entry_role": "INITIAL",
            "setup_key": "bull-q4",
            "stop_price": 90,
            "trigger": "收盤突破100。",
            "expected_behavior": "3根內續強。",
            "max_wait_bars": 3,
            "obstacles": [
                {"price": 106, "role": "CHECKPOINT", "label": "前高", "reaction": "守住續強。"}
            ],
        },
    }
    queued = schedule_pending_entry(
        _flat(),
        analysis,
        gate,
        as_of="2026-08-25T09:37:00+08:00",
    )
    assert queued["status"] == "FLAT"
    assert queued["pending_entry"]["signal_time"].endswith("09:37:00+08:00")

    filled, event = fill_pending_entry(
        queued,
        bars + [_bar("09:38", 103, 106, 102, 105)],
        as_of="2026-08-25T09:38:00+08:00",
    )
    assert event is not None and event["event_type"] == "ENTRY_FILLED"
    assert event["fill_time"].endswith("09:38:00+08:00")
    assert event["fill_price"] == 103
    assert filled["status"] == "LONG"
    assert filled["entry_price"] == 103
    assert filled["behavior_plan"]["max_wait_bars"] == 3
    assert filled["behavior_plan"]["obstacles"][0]["price"] == 106
    assert filled["behavior_plan"]["post_achievement_policy"] == (
        "Q4_STRUCTURE_AND_COPY_MANAGEMENT"
    )
    assert event["post_achievement_policy"] == "Q4_STRUCTURE_AND_COPY_MANAGEMENT"


def test_q2_entry_freezes_post_achievement_management_policy() -> None:
    gate = derive_entry_eligibility(
        _memory(),
        [
            _bar("09:36", 98, 100, 97, 99),
            _bar("09:37", 99, 102, 98, 101),
        ],
        as_of="2026-08-25T09:37:00+08:00",
        position=_flat(),
    )
    analysis = {
        "notification_reason": "Q2收復觸發。",
        "course_reading": {
            "setup_stage": "ENTRY_ELIGIBLE",
            "main_strategy_family": "Q2",
        },
        "action": {
            "position_action": "ENTER",
            "direction": "LONG",
            "entry_role": "INITIAL",
            "setup_key": "bull-q4",
            "stop_price": 90,
            "trigger": "收復。",
            "expected_behavior": "3根內快速離開低點。",
            "max_wait_bars": 3,
            "obstacles": [],
        },
    }

    queued = schedule_pending_entry(
        _flat(), analysis, gate, as_of="2026-08-25T09:37:00+08:00"
    )
    filled, event = fill_pending_entry(
        queued,
        [_bar("09:38", 103, 106, 102, 105)],
        as_of="2026-08-25T09:38:00+08:00",
    )

    assert queued["pending_entry"]["post_achievement_policy"] == (
        "Q2_TARGET_OR_CONFIRMED_TREND_STRUCTURE_MANAGEMENT"
    )
    assert filled["behavior_plan"]["post_achievement_policy"] == (
        "Q2_TARGET_OR_CONFIRMED_TREND_STRUCTURE_MANAGEMENT"
    )
    assert event is not None
    assert event["post_achievement_policy"] == (
        "Q2_TARGET_OR_CONFIRMED_TREND_STRUCTURE_MANAGEMENT"
    )


def test_final_valid_minute_trigger_survives_until_next_two_minute_analysis() -> None:
    memory = _memory()
    memory["as_of"] = "2026-08-25T12:12:00+08:00"
    memory["active_setups"][0].update(
        {
            "setup_key": "bull-1210",
            "trigger_level": 44619.0,
            "trigger_operator": "CLOSE_ABOVE",
            "valid_bars": 3,
        }
    )
    gate = derive_entry_eligibility(
        memory,
        [
            _bar("12:12", 44585, 44596, 44575, 44594),
            _bar("12:13", 44595, 44631, 44588, 44620),
            _bar("12:14", 44618, 44679, 44618, 44670),
        ],
        as_of="2026-08-25T12:14:00+08:00",
        position=_flat(),
    )

    assert gate["status"] == "ENTRY_ELIGIBLE"
    assert gate["setup_key"] == "bull-1210"
    assert gate["signal_time"].endswith("12:13:00+08:00")
    assert gate["eligible_from"].endswith("12:14:00+08:00")

    retained = expire_stale_armed_setups(
        memory,
        armed_at_by_key={"bull-1210": "2026-08-25T12:10:00+08:00"},
        as_of="2026-08-25T12:14:00+08:00",
        protected_setup_key=gate["setup_key"],
    )
    assert retained["active_setups"][0]["setup_key"] == "bull-1210"


def test_sparse_trigger_reclaimed_before_cutoff_is_not_entry_eligible() -> None:
    memory = _memory()
    memory["as_of"] = "2026-08-25T10:00:00+08:00"
    memory["active_setups"][0].update(
        {
            "setup_key": "bull-reclaimed-before-cutoff",
            "trigger_level": 45007.0,
            "trigger_operator": "CLOSE_ABOVE",
            "valid_bars": 3,
        }
    )

    gate = derive_entry_eligibility(
        memory,
        [
            _bar("10:00", 44994, 45004, 44961, 44999),
            _bar("10:01", 44999, 45035, 44995, 45025),
            _bar("10:02", 45030, 45030, 44930, 44943),
        ],
        as_of="2026-08-25T10:02:00+08:00",
        position=_flat(),
    )

    assert gate["status"] == "NONE"


def test_sparse_trigger_can_requalify_after_reclaim_and_new_trigger() -> None:
    memory = _memory()
    memory["as_of"] = "2026-08-25T10:00:00+08:00"
    memory["active_setups"][0].update(
        {
            "setup_key": "bull-retriggered",
            "trigger_level": 45007.0,
            "trigger_operator": "CLOSE_ABOVE",
            "valid_bars": 3,
        }
    )

    gate = derive_entry_eligibility(
        memory,
        [
            _bar("10:00", 44994, 45004, 44961, 44999),
            _bar("10:01", 44999, 45035, 44995, 45025),
            _bar("10:02", 45030, 45030, 44930, 44943),
            _bar("10:03", 44950, 45020, 44947, 45014),
            _bar("10:04", 45014, 45030, 45008, 45022),
        ],
        as_of="2026-08-25T10:04:00+08:00",
        position=_flat(),
    )

    assert gate["status"] == "ENTRY_ELIGIBLE"
    assert gate["signal_time"].endswith("10:03:00+08:00")


def test_armed_close_trigger_fires_when_next_bar_remains_beyond_fixed_line() -> None:
    memory = _memory()
    memory["as_of"] = "2026-08-25T12:56:00+08:00"
    memory["active_setups"][0].update(
        {
            "setup_key": "bear-fixed-line",
            "direction": "SHORT",
            "trigger_level": 44888.0,
            "trigger_operator": "CLOSE_BELOW",
            "valid_bars": 3,
        }
    )
    gate = derive_entry_eligibility(
        memory,
        [
            _bar("12:56", 44873, 44890, 44860, 44883),
            _bar("12:57", 44884, 44910, 44872, 44873),
            _bar("12:58", 44877, 44881, 44850, 44859),
        ],
        as_of="2026-08-25T12:58:00+08:00",
        position=_flat(),
    )

    assert gate["status"] == "ENTRY_ELIGIBLE"
    assert gate["signal_time"].endswith("12:57:00+08:00")
    assert gate["signal_close"] == 44873


def test_candidate_confirmed_and_triggered_on_same_cutoff_is_immediately_eligible() -> None:
    candidate = {
        "setup_key": "bull-same-cutoff",
        "setup_name": "多方修正後複製",
        "direction": "LONG",
        "stage": "ARMED",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 100,
        "trigger_time": "2026-08-25T09:36:00+08:00",
        "first_seen_at": "2026-08-25T09:37:00+08:00",
        "valid_bars": 3,
    }
    gate = derive_entry_eligibility(
        {"version": 3, "as_of": "2026-08-25T09:36:00+08:00", "active_setups": []},
        [
            _bar("09:36", 98, 100, 97, 99),
            _bar("09:37", 99, 102, 98, 101),
        ],
        as_of="2026-08-25T09:37:00+08:00",
        position=_flat(),
        current_candidate=candidate,
    )

    assert gate["status"] == "ENTRY_ELIGIBLE"
    assert gate["setup_key"] == "bull-same-cutoff"
    assert gate["signal_time"] == "2026-08-25T09:37:00+08:00"
    assert gate["eligible_from"] == "2026-08-25T09:38:00+08:00"


def test_candidate_first_seen_between_ai_ticks_is_consumed_at_next_cutoff() -> None:
    candidate = {
        "setup_key": "bull-between-ticks",
        "setup_name": "多方Q4積極型拉回反轉",
        "direction": "LONG",
        "stage": "ARMED",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 100,
        "trigger_time": "2026-08-25T09:37:00+08:00",
        "first_seen_at": "2026-08-25T09:37:00+08:00",
        "valid_bars": 3,
        "decision_authority": "AI_HYBRID",
    }
    gate = derive_entry_eligibility(
        {"version": 3, "as_of": "2026-08-25T09:36:00+08:00", "active_setups": []},
        [
            _bar("09:37", 99, 102, 98, 101),
            _bar("09:38", 101, 103, 100, 102),
        ],
        as_of="2026-08-25T09:38:00+08:00",
        position=_flat(),
        current_candidate=candidate,
    )

    assert gate["status"] == "ENTRY_ELIGIBLE"
    assert gate["setup_key"] == "bull-between-ticks"
    assert gate["signal_time"] == "2026-08-25T09:37:00+08:00"
    # The decision is made at 09:38, so the historical 09:38 open is never
    # back-filled even though the structural signal occurred at 09:37.
    assert gate["eligible_from"] == "2026-08-25T09:39:00+08:00"


def test_candidate_already_visible_at_previous_ai_tick_is_not_revived() -> None:
    candidate = {
        "setup_key": "old-unselected-candidate",
        "setup_name": "舊候選",
        "direction": "LONG",
        "stage": "ARMED",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 100,
        "first_seen_at": "2026-08-25T09:35:00+08:00",
        "valid_bars": 3,
        "decision_authority": "AI_HYBRID",
    }
    gate = derive_entry_eligibility(
        {"version": 3, "as_of": "2026-08-25T09:36:00+08:00", "active_setups": []},
        [_bar("09:37", 99, 102, 98, 101)],
        as_of="2026-08-25T09:37:00+08:00",
        position=_flat(),
        current_candidate=candidate,
    )

    assert gate["status"] == "NONE"


def test_same_cutoff_bound_reentry_is_not_reset_to_initial() -> None:
    position = {
        **_flat(),
        "as_of": "2026-08-25T10:27:00+08:00",
        "active_setup_key": "originating-long",
        "last_stop_time": "2026-08-25T10:07:00+08:00",
    }
    candidate = {
        "setup_key": "originating-long",
        "setup_name": "多方修正後複製",
        "direction": "LONG",
        "stage": "ARMED",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 100,
        "first_seen_at": "2026-08-25T10:28:00+08:00",
        "valid_bars": 3,
        "entry_role": "REENTRY",
        "decision_authority": "PROGRAM",
    }

    gate = derive_entry_eligibility(
        {"version": 3, "as_of": "2026-08-25T10:27:00+08:00", "active_setups": []},
        [_bar("10:28", 99, 102, 98, 101)],
        as_of="2026-08-25T10:28:00+08:00",
        position=position,
        current_candidate=candidate,
    )

    assert gate["status"] == "ENTRY_ELIGIBLE"
    assert gate["entry_role"] == "REENTRY"


def test_entry_eligible_memory_row_can_be_rearmed_only_as_reentry() -> None:
    memory = _memory()
    memory["as_of"] = "2026-08-25T10:27:00+08:00"
    memory["active_setups"][0].update(
        {
            "setup_key": "originating-long",
            "stage": "ENTRY_ELIGIBLE",
            "reentry_status": "AVAILABLE",
            "trigger_level": 100,
        }
    )
    position = {
        **_flat(),
        "as_of": "2026-08-25T10:27:00+08:00",
        "active_setup_key": "originating-long",
        "last_stop_time": "2026-08-25T10:07:00+08:00",
    }
    candidate = {
        "setup_key": "originating-long",
        "direction": "LONG",
        "entry_role": "REENTRY",
        "decision_authority": "PROGRAM",
    }

    gate = derive_entry_eligibility(
        memory,
        [_bar("10:28", 99, 102, 98, 101)],
        as_of="2026-08-25T10:28:00+08:00",
        position=position,
        current_candidate=candidate,
    )

    assert gate["status"] == "ENTRY_ELIGIBLE"
    assert gate["entry_role"] == "REENTRY"


def test_used_reentry_setup_cannot_trigger_as_new_initial_trade() -> None:
    memory = _memory()
    memory["as_of"] = "2026-08-25T12:32:00+08:00"
    memory["active_setups"][0].update(
        {
            "setup_key": "exhausted-long",
            "stage": "ENTRY_ELIGIBLE",
            "reentry_status": "USED",
            "trigger_level": 100,
        }
    )
    position = {
        **_flat(),
        "as_of": "2026-08-25T12:32:00+08:00",
        "last_stop_time": "2026-08-25T12:31:00+08:00",
        "reentry_count": 1,
    }

    gate = derive_entry_eligibility(
        memory,
        [_bar("12:33", 99, 102, 98, 101)],
        as_of="2026-08-25T12:33:00+08:00",
        position=position,
    )

    assert gate["status"] == "NONE"


def test_program_owned_candidate_fixes_entry_action_and_stop() -> None:
    memory = _memory()
    candidate = {
        "setup_key": "bull-q4",
        "setup_name": "Q4順勢回檔",
        "direction": "LONG",
        "stage": "ARMED",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 100,
        "first_seen_at": "2026-08-25T09:36:00+08:00",
        "valid_bars": 3,
        "decision_authority": "PROGRAM",
        "stop_price": 89.5,
        "behavior_max_wait_bars": 5,
        "behavior_trigger_level": 100,
        "behavior_obstacles": [
            {"price": 100, "role": "TRIGGER", "source_time": "09:36"},
            {"price": 105, "role": "CHECKPOINT", "source_time": "09:30"},
        ],
    }
    gate = derive_entry_eligibility(
        memory,
        [_bar("09:36", 98, 100, 97, 99), _bar("09:37", 99, 102, 98, 101)],
        as_of="2026-08-25T09:37:00+08:00",
        position=_flat(),
        current_candidate=candidate,
    )

    assert gate["decision_authority"] == "PROGRAM"
    assert gate["required_position_action"] == "ENTER"
    assert gate["required_stop_price"] == 89.5
    assert gate["required_entry_rejection_reason"] == "NONE"
    assert gate["required_behavior_max_wait_bars"] == 5
    assert gate["required_behavior_obstacles"][1]["price"] == 105

    analysis = {
        "notification_reason": "程式化進場。",
        "course_reading": {"setup_stage": "ENTRY_ELIGIBLE"},
        "action": {
            "position_action": "ENTER",
            "direction": "LONG",
            "entry_role": "INITIAL",
            "setup_key": "bull-q4",
            "stop_price": 89.5,
            "trigger": "收盤突破100。",
            "expected_behavior": "3根內續強。",
            "max_wait_bars": 3,
            "obstacles": [
                {"price": 106, "role": "CHECKPOINT", "label": "AI自選", "reaction": "忽略。"}
            ],
        },
    }
    queued = schedule_pending_entry(
        _flat(), analysis, gate, as_of="2026-08-25T09:37:00+08:00"
    )
    assert queued["pending_entry"]["stop_price"] == 89.5
    assert queued["pending_entry"]["behavior_max_wait_bars"] == 5
    assert [
        (item["role"], item["price"])
        for item in queued["pending_entry"]["behavior_obstacles"]
    ] == [("TRIGGER", 100), ("CHECKPOINT", 105)]


def test_ai_hybrid_candidate_exposes_guardrails_without_forcing_entry() -> None:
    candidate = {
        "setup_key": "hybrid-bull-q4",
        "setup_name": "多方Q4修正後複製",
        "direction": "LONG",
        "stage": "ARMED",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 100,
        "first_seen_at": "2026-08-25T09:36:00+08:00",
        "valid_bars": 3,
        "decision_authority": "AI_HYBRID",
        "stop_price": 89.5,
        "behavior_max_wait_bars": 5,
        "behavior_trigger_level": 97,
        "behavior_obstacles": [
            {"price": 97, "role": "TRIGGER", "source_time": "09:36"},
        ],
    }
    memory = _memory()
    memory["active_setups"][0].update(
        {
            "setup_key": "hybrid-bull-q4",
            "name": "多方Q4修正後複製",
        }
    )

    gate = derive_entry_eligibility(
        memory,
        [_bar("09:36", 98, 100, 97, 99), _bar("09:37", 99, 102, 98, 101)],
        as_of="2026-08-25T09:37:00+08:00",
        position=_flat(),
        current_candidate=candidate,
    )

    assert gate["status"] == "ENTRY_ELIGIBLE"
    assert gate["decision_authority"] == "AI_HYBRID"
    assert gate["required_position_action"] is None
    assert gate["required_stop_price"] == 89.5
    assert gate["required_behavior_max_wait_bars"] == 5
    assert gate["required_behavior_trigger_level"] == 97

    analysis = {
        "notification_reason": "AI接受多方候選。",
        "course_reading": {"setup_stage": "ENTRY_ELIGIBLE"},
        "action": {
            "position_action": "ENTER",
            "direction": "LONG",
            "entry_role": "INITIAL",
            "setup_key": "hybrid-bull-q4",
            "stop_price": 89.5,
            "trigger": "收盤突破100。",
            "expected_behavior": "守住97並在5根內延伸。",
            "max_wait_bars": 5,
            "obstacles": [
                {"price": 97, "role": "TRIGGER", "label": "原結構線", "reaction": "守住。"}
            ],
        },
    }
    queued = schedule_pending_entry(
        _flat(), analysis, gate, as_of="2026-08-25T09:37:00+08:00"
    )
    assert queued["pending_entry"]["behavior_trigger_level"] == 97
    assert queued["pending_entry"]["behavior_obstacles"] == [
        {
            "price": 97.0,
            "role": "TRIGGER",
            "source_time": "09:36",
            "label": "09:36結構點",
        }
    ]


def test_ai_hybrid_obstacle_prose_cannot_change_deterministic_behavior_plan() -> None:
    gate = {
        "status": "ENTRY_ELIGIBLE",
        "event_id": "event-q4",
        "setup_key": "hybrid-bull-q4",
        "setup_name": "多方Q4修正後複製",
        "direction": "LONG",
        "signal_time": "2026-08-25T09:37:00+08:00",
        "signal_close": 101.0,
        "eligible_from": "2026-08-25T09:38:00+08:00",
        "expires_at": "2026-08-25T09:40:00+08:00",
        "valid_bars": 3,
        "decision_authority": "AI_HYBRID",
        "required_stop_price": 89.5,
        "required_behavior_max_wait_bars": 5,
        "required_behavior_trigger_level": 100.0,
        "required_behavior_policy": "Q4_AGGRESSIVE_STRUCTURE_AND_COPY_REVIEW",
        "required_behavior_obstacles": [
            {
                "price": 100.0,
                "role": "TRIGGER",
                "source_time": "2026-08-25T09:36:00+08:00",
                "source_field": "high",
            },
            {
                "price": 105.0,
                "role": "CHECKPOINT",
                "source_time": "2026-08-25T09:30:00+08:00",
                "source_field": "high",
            },
        ],
    }

    def analysis(label: str, reaction: str) -> dict:
        return {
            "notification_reason": "AI接受多方候選。",
            "course_reading": {
                "setup_stage": "ENTRY_ELIGIBLE",
                "main_strategy_family": "Q4",
            },
            "action": {
                "position_action": "ENTER",
                "direction": "LONG",
                "entry_role": "INITIAL",
                "setup_key": "hybrid-bull-q4",
                "stop_price": 89.5,
                "trigger": "收盤突破100。",
                "expected_behavior": "守住100並在5根內延伸。",
                "max_wait_bars": 5,
                "obstacles": [
                    {
                        "price": 100.0,
                        "role": "TRIGGER",
                        "label": "AI觸發文案",
                        "reaction": "AI觸發反應。",
                    },
                    {
                        "price": 105.0,
                        "role": "CHECKPOINT",
                        "label": label,
                        "reaction": reaction,
                    },
                ],
            },
        }

    first = schedule_pending_entry(
        _flat(),
        analysis("09:30前高", "突破續抱。"),
        gate,
        as_of="2026-08-25T09:37:00+08:00",
    )
    second = schedule_pending_entry(
        _flat(),
        analysis("前高檢查點", "站上後觀察。"),
        gate,
        as_of="2026-08-25T09:37:00+08:00",
    )

    assert first["pending_entry"]["behavior_obstacles"] == second["pending_entry"]["behavior_obstacles"]
    assert first["pending_entry"]["behavior_obstacles"] == [
        {
            "price": 100.0,
            "role": "TRIGGER",
            "source_time": "2026-08-25T09:36:00+08:00",
            "source_field": "high",
            "label": "09:36高點",
        },
        {
            "price": 105.0,
            "role": "CHECKPOINT",
            "source_time": "2026-08-25T09:30:00+08:00",
            "source_field": "high",
            "label": "09:30高點",
        },
    ]

    first_filled, _ = fill_pending_entry(
        first,
        [_bar("09:38", 101, 102, 100, 101.5)],
        as_of="2026-08-25T09:38:00+08:00",
    )
    second_filled, _ = fill_pending_entry(
        second,
        [_bar("09:38", 101, 102, 100, 101.5)],
        as_of="2026-08-25T09:38:00+08:00",
    )
    first_audit = derive_position_behavior_audit(
        first_filled,
        [_bar("09:38", 101, 102, 100, 101.5)],
        as_of="2026-08-25T09:38:00+08:00",
    )
    second_audit = derive_position_behavior_audit(
        second_filled,
        [_bar("09:38", 101, 102, 100, 101.5)],
        as_of="2026-08-25T09:38:00+08:00",
    )
    assert first_audit == second_audit
    assert first_audit["checkpoint_label"] == "Q4順向0.25R進展檢查"


def test_behavior_window_cannot_be_bypassed_when_setup_has_no_checkpoint() -> None:
    position = {
        **_flat(),
        "status": "LONG",
        "direction": "LONG",
        "entry_time": "2026-08-25T09:38:00+08:00",
        "entry_price": 100.0,
        "stop_price": 90.0,
        "active_setup_key": "q4-no-checkpoint",
        "behavior_plan": {
            "initial_stop_price": 90.0,
            "max_wait_bars": 3,
            "obstacles": [
                {"price": 100.0, "role": "TRIGGER", "label": "觸發線"},
            ],
        },
    }
    audit = derive_position_behavior_audit(
        position,
        [
            _bar("09:38", 100, 101, 99, 100.5),
            _bar("09:39", 100.5, 101.5, 99.5, 101),
            _bar("09:40", 101, 101.5, 100, 101),
        ],
        as_of="2026-08-25T09:40:00+08:00",
    )

    assert audit["checkpoint_price"] == 102.5
    assert audit["status"] == "EXPIRED_NO_PROGRESS"
    assert audit["failed_at"].endswith("09:40:00+08:00")


def test_program_owned_candidate_rejects_model_moved_stop() -> None:
    gate = {
        "status": "ENTRY_ELIGIBLE",
        "setup_key": "bull-q4",
        "direction": "LONG",
        "entry_role": "INITIAL",
        "required_stop_price": 89.5,
    }
    analysis = {
        "notification_reason": "錯誤移動停損。",
        "course_reading": {"setup_stage": "ENTRY_ELIGIBLE"},
        "action": {
            "position_action": "ENTER",
            "direction": "LONG",
            "entry_role": "INITIAL",
            "setup_key": "bull-q4",
            "stop_price": 90,
            "trigger": "收盤突破100。",
            "expected_behavior": "3根內續強。",
            "max_wait_bars": 3,
            "obstacles": [],
        },
    }

    import pytest
    from trade_monitor_replay.execution_gate import ReplayExecutionGateError

    with pytest.raises(ReplayExecutionGateError, match="程式化交易政策"):
        schedule_pending_entry(
            _flat(), analysis, gate, as_of="2026-08-25T09:37:00+08:00"
        )


def test_position_behavior_audit_consumes_entry_window_after_checkpoint_is_achieved() -> None:
    position = {
        "status": "LONG",
        "entry_time": "2026-08-25T09:49:00+08:00",
        "entry_price": 100.0,
        "behavior_plan": {
            "max_wait_bars": 3,
            "trigger_level": 99.0,
            "obstacles": [{"price": 106.0, "role": "CHECKPOINT", "label": "前高"}],
        },
    }
    expired = derive_position_behavior_audit(
        position,
        [
            _bar("09:49", 100, 103, 99, 101),
            _bar("09:50", 101, 104, 100, 102),
            _bar("09:51", 102, 105, 101, 103),
        ],
        as_of="2026-08-25T09:51:00+08:00",
    )
    assert expired["status"] == "EXPIRED_NO_PROGRESS"

    checkpoint_pullback = derive_position_behavior_audit(
        position,
        [
            _bar("09:49", 100, 103, 99, 101),
            _bar("09:50", 101, 107, 100, 106),
            _bar("09:51", 106, 106, 103, 105),
            _bar("09:52", 105, 105, 102, 104),
        ],
        as_of="2026-08-25T09:52:00+08:00",
    )
    assert checkpoint_pullback["status"] == "ACHIEVED"
    assert checkpoint_pullback["achieved_at"].endswith("09:50:00+08:00")
    assert checkpoint_pullback["hold_price"] is None
    assert checkpoint_pullback["consecutive_failed_hold_closes"] == 0

    post_achievement_pullback = derive_position_behavior_audit(
        position,
        [
            _bar("09:49", 100, 103, 99, 101),
            _bar("09:50", 101, 107, 100, 106),
            _bar("09:51", 106, 106, 97, 98),
            _bar("09:52", 98, 100, 96, 97),
        ],
        as_of="2026-08-25T09:52:00+08:00",
    )
    assert post_achievement_pullback["status"] == "ACHIEVED"
    assert post_achievement_pullback["achieved_at"].endswith("09:50:00+08:00")
    assert post_achievement_pullback["hold_price"] is None
    assert post_achievement_pullback["consecutive_failed_hold_closes"] == 0


def test_position_behavior_audit_does_not_let_tiny_checkpoint_hide_no_progress() -> None:
    position = {
        "status": "LONG",
        "entry_time": "2026-08-25T13:03:00+08:00",
        "entry_price": 45115.0,
        "stop_price": 45024.8,
        "behavior_plan": {
            "initial_stop_price": 45024.8,
            "max_wait_bars": 5,
            "trigger_level": 45107.0,
            "obstacles": [
                {"price": 45125.0, "role": "CHECKPOINT", "label": "近端檢查點"},
                {"price": 45150.0, "role": "CHECKPOINT", "label": "主要推進檢查點"},
            ],
        },
    }

    audit = derive_position_behavior_audit(
        position,
        [
            _bar("13:03", 45115, 45130, 45110, 45127),
            _bar("13:04", 45128, 45129, 45107, 45107),
            _bar("13:05", 45111, 45128, 45111, 45126),
            _bar("13:06", 45132, 45135, 45108, 45110),
            _bar("13:07", 45109, 45115, 45100, 45107),
            _bar("13:08", 45109, 45117, 45104, 45104),
        ],
        as_of="2026-08-25T13:08:00+08:00",
    )

    assert audit["checkpoint_price"] == 45150.0
    assert audit["status"] == "EXPIRED_NO_PROGRESS"


def test_q4_entry_hands_off_to_structure_management_after_meaningful_progress() -> None:
    position = {
        "status": "LONG",
        "direction": "LONG",
        "entry_time": "2026-08-25T10:10:00+08:00",
        "entry_price": 45311.0,
        "stop_price": 45238.5,
        "active_setup_key": "q4-structure-handoff",
        "behavior_plan": {
            "initial_stop_price": 45238.5,
            "max_wait_bars": 5,
            "trigger_level": 45289.0,
            "policy": "Q4_AGGRESSIVE_STRUCTURE_AND_COPY_REVIEW",
            "post_achievement_policy": "Q4_STRUCTURE_AND_COPY_MANAGEMENT",
            "obstacles": [
                {"price": 45420.0, "role": "CHECKPOINT", "label": "09:56高點"},
            ],
        },
    }
    bars = [
        _bar("10:10", 45311, 45340, 45300, 45325),
        _bar("10:11", 45325, 45370, 45320, 45360),
        _bar("10:12", 45360, 45395, 45350, 45388),
        _bar("10:13", 45388, 45405, 45370, 45382),
        _bar("10:14", 45382, 45410, 45365, 45390),
    ]

    audit = derive_position_behavior_audit(
        position,
        bars,
        as_of="2026-08-25T10:14:00+08:00",
    )
    exit_audit = derive_program_behavior_exit_audit(
        position,
        bars,
        as_of="2026-08-25T10:14:00+08:00",
    )

    assert audit["checkpoint_price"] == 45329.125
    assert audit["checkpoint_label"] == "Q4順向0.25R進展檢查"
    assert audit["status"] == "ACHIEVED"
    assert audit["achieved_at"].endswith("10:11:00+08:00")
    assert exit_audit["status"] == "NOT_TRIGGERED"
    assert exit_audit["behavior_status"] == "ACHIEVED"


def test_program_behavior_exit_uses_first_failure_close_and_next_open() -> None:
    position = {
        "version": 2,
        "status": "LONG",
        "entry_time": "2026-08-25T09:01:00+08:00",
        "entry_price": 100.0,
        "stop_price": 90.0,
        "active_setup_key": "long-q4",
        "behavior_plan": {
            "initial_stop_price": 90.0,
            "max_wait_bars": 3,
            "trigger_level": 99.0,
            "obstacles": [
                {"price": 105.0, "role": "CHECKPOINT", "label": "前高"}
            ],
        },
    }
    bars = [
        _bar("09:01", 100, 102, 99, 101),
        _bar("09:02", 101, 103, 100, 102),
        _bar("09:03", 102, 104, 101, 103),
    ]
    pending = derive_program_behavior_exit_audit(
        position,
        bars,
        as_of="2026-08-25T09:03:00+08:00",
    )
    assert pending["status"] == "PENDING_FILL"
    assert pending["signal_time"].endswith("09:03:00+08:00")
    assert pending["fill_time"] is None

    triggered = derive_program_behavior_exit_audit(
        position,
        [*bars, _bar("09:04", 102.5, 103, 101, 101.5)],
        as_of="2026-08-25T09:04:00+08:00",
    )
    assert triggered["status"] == "TRIGGERED"
    assert triggered["required_position_action"] == "EXIT"
    assert triggered["signal_time"].endswith("09:03:00+08:00")
    assert triggered["fill_time"].endswith("09:04:00+08:00")
    assert triggered["fill_price"] == 102.5


def test_behavior_exit_is_not_triggered_after_checkpoint_was_achieved() -> None:
    position = {
        "status": "SHORT",
        "entry_time": "2026-08-25T09:01:00+08:00",
        "entry_price": 100.0,
        "stop_price": 110.0,
        "active_setup_key": "short-q4",
        "behavior_plan": {
            "initial_stop_price": 110.0,
            "max_wait_bars": 3,
            "obstacles": [
                {"price": 95.0, "role": "CHECKPOINT", "label": "前低"}
            ],
        },
    }
    audit = derive_program_behavior_exit_audit(
        position,
        [
            _bar("09:01", 100, 101, 98, 99),
            _bar("09:02", 99, 100, 94, 95),
            _bar("09:03", 95, 98, 94, 97),
            _bar("09:04", 97, 99, 96, 98),
        ],
        as_of="2026-08-25T09:04:00+08:00",
    )
    assert audit["status"] == "NOT_TRIGGERED"
    assert audit["behavior_status"] == "ACHIEVED"


def test_q1_yizhi_exits_after_two_bars_stop_extending_favorable_extreme() -> None:
    position = {
        "status": "SHORT",
        "entry_time": "2026-08-25T09:01:00+08:00",
        "entry_price": 100.0,
        "stop_price": 110.0,
        "active_setup_key": "short-yizhi",
        "behavior_plan": {
            "initial_stop_price": 110.0,
            "max_wait_bars": 3,
            "policy": "Q1_YIZHI_FAST_CONTINUATION",
            "obstacles": [],
        },
    }
    bars = [
        _bar("09:01", 100, 101, 96, 97),
        _bar("09:02", 97, 98, 93, 94),
        _bar("09:03", 94, 96, 94, 95),
        _bar("09:04", 95, 97, 94.5, 96),
    ]

    audit = derive_position_behavior_audit(
        position,
        bars,
        as_of="2026-08-25T09:04:00+08:00",
    )
    assert audit["status"] == "FAILED_HOLD"
    assert audit["achieved_at"].endswith("09:01:00+08:00")
    assert audit["failed_at"].endswith("09:04:00+08:00")
    assert audit["failure_reason"] == "Q1_TWO_BARS_WITHOUT_NEW_FAVORABLE_EXTREME"

    exit_audit = derive_program_behavior_exit_audit(
        position,
        [*bars, _bar("09:05", 96, 97, 95, 96)],
        as_of="2026-08-25T09:05:00+08:00",
    )
    assert exit_audit["status"] == "TRIGGERED"
    assert exit_audit["signal_time"].endswith("09:04:00+08:00")
    assert exit_audit["fill_time"].endswith("09:05:00+08:00")


def test_q2_fast_reversal_uses_same_objective_extension_clock_with_distinct_reason() -> None:
    position = {
        "status": "SHORT",
        "entry_time": "2026-08-25T09:01:00+08:00",
        "entry_price": 100.0,
        "stop_price": 110.0,
        "active_setup_key": "short-q2",
        "behavior_plan": {
            "initial_stop_price": 110.0,
            "max_wait_bars": 3,
            "policy": "Q2_FAST_REVERSAL_CONFIRMATION",
            "obstacles": [],
        },
    }
    audit = derive_position_behavior_audit(
        position,
        [
            _bar("09:01", 100, 101, 96, 97),
            _bar("09:02", 97, 98, 93, 94),
            _bar("09:03", 94, 96, 94, 95),
            _bar("09:04", 95, 97, 94.5, 96),
        ],
        as_of="2026-08-25T09:04:00+08:00",
    )

    assert audit["status"] == "FAILED_HOLD"
    assert audit["checkpoint_label"] == "Q2回轉0.25R動能檢查"
    assert audit["failure_reason"] == "Q2_TWO_BARS_WITHOUT_NEW_FAVORABLE_EXTREME"


def test_current_program_candidate_preempts_stale_opposite_memory_setup() -> None:
    memory = _memory()
    memory["as_of"] = "2026-08-25T09:36:00+08:00"
    memory["active_setups"][0].update(
        {
            "setup_key": "old-long",
            "direction": "LONG",
            "trigger_level": 100.0,
            "trigger_operator": "CLOSE_ABOVE",
        }
    )
    current = {
        "setup_key": "new-short",
        "setup_name": "空方一之早期動能",
        "direction": "SHORT",
        "stage": "ARMED",
        "trigger_operator": "CLOSE_BELOW",
        "trigger_level": 95.0,
        "first_seen_at": "2026-08-25T09:37:00+08:00",
        "valid_bars": 2,
        "decision_authority": "PROGRAM",
        "candidate_source": "YIZHI_EARLY_BREAKOUT",
        "entry_strategy": "Q1_YIZHI_EARLY_MOMENTUM",
        "behavior_policy": "Q1_YIZHI_FAST_CONTINUATION",
        "stop_price": 101.0,
    }
    gate = derive_entry_eligibility(
        memory,
        [_bar("09:36", 98, 101, 97, 100.5), _bar("09:37", 100, 101, 93, 94)],
        as_of="2026-08-25T09:37:00+08:00",
        position=_flat(),
        current_candidate=current,
    )

    assert gate["status"] == "ENTRY_ELIGIBLE"
    assert gate["setup_key"] == "new-short"
    assert gate["required_candidate_source"] == "YIZHI_EARLY_BREAKOUT"


def test_sparse_sampling_does_not_revive_expired_entry_signal() -> None:
    bars = [
        _bar("09:36", 98, 100, 97, 99),
        _bar("09:37", 99, 102, 98, 101),
        _bar("09:38", 101, 104, 100, 103),
    ]
    gate = derive_entry_eligibility(
        _memory(),
        bars,
        as_of="2026-08-25T09:45:00+08:00",
        position=_flat(),
    )
    assert gate["status"] == "EXPIRED"
    assert gate["signal_time"].endswith("09:37:00+08:00")


def test_entry_gate_expires_when_no_unrevealed_fill_bar_remains() -> None:
    memory = _memory()
    memory["as_of"] = "2026-08-25T11:38:00+08:00"
    memory["active_setups"][0].update(
        {
            "setup_key": "bear-one-bar-window",
            "direction": "SHORT",
            "trigger_level": 45111.0,
            "trigger_operator": "CLOSE_BELOW",
            "valid_bars": 1,
        }
    )

    gate = derive_entry_eligibility(
        memory,
        [
            _bar("11:39", 45143, 45143, 45100, 45101),
            _bar("11:40", 45100, 45111, 45070, 45081),
        ],
        as_of="2026-08-25T11:40:00+08:00",
        position=_flat(),
    )

    assert gate["status"] == "EXPIRED"
    assert gate["signal_time"].endswith("11:39:00+08:00")
    assert gate["eligible_from"].endswith("11:40:00+08:00")
    assert gate["expires_at"].endswith("11:40:00+08:00")


def test_armed_setup_expires_by_real_one_minute_bars_before_a_late_trigger() -> None:
    memory = _memory()
    expired = expire_stale_armed_setups(
        memory,
        armed_at_by_key={"bull-q4": "2026-08-25T09:36:00+08:00"},
        as_of="2026-08-25T09:40:00+08:00",
    )

    assert expired["active_setups"][0]["stage"] == "NO_CHASE"
    assert "3根1分K有效窗已過期" in expired["active_setups"][0]["trigger"]
    assert _memory()["active_setups"][0]["stage"] == "ARMED"

    protected = expire_stale_armed_setups(
        memory,
        armed_at_by_key={"bull-q4": "2026-08-25T09:36:00+08:00"},
        as_of="2026-08-25T09:40:00+08:00",
        protected_setup_key="bull-q4",
    )
    assert protected["active_setups"][0]["stage"] == "ARMED"


def test_event_lifecycle_expires_stale_event_and_consumes_current_event() -> None:
    stale = {
        "event_type": "STRUCTURE_EVENT",
        "event_id": "S-stale",
        "event_time": "2026-08-25T11:10:00+08:00",
        "recorded_at": "2026-08-25T11:21:00+08:00",
    }
    state, active = build_event_lifecycle(
        None,
        [stale],
        as_of="2026-08-25T11:21:00+08:00",
        valid_bars=3,
    )
    assert active == []
    assert state["events"][0]["status"] == "EXPIRED"
    assert state["events"][0]["analyzed"] is False

    current = {**stale, "event_id": "S-current", "event_time": "2026-08-25T11:20:00+08:00"}
    state, active = build_event_lifecycle(
        state,
        [current],
        as_of="2026-08-25T11:21:00+08:00",
        valid_bars=3,
    )
    assert [item["event_id"] for item in active] == ["S-current"]
    consumed = mark_events_analyzed(state, ["S-current"], analyzed_at="2026-08-25T11:21:00+08:00")
    item = next(value for value in consumed["events"] if value["event_id"] == "S-current")
    assert item["status"] == "ANALYZED"
    assert item["analyzed"] is True
    assert item["consumed_at"].endswith("11:21:00+08:00")


def test_event_lifecycle_hands_unanalyzed_event_to_next_ai_cadence_bar() -> None:
    hidden_tick_event = {
        "event_type": "STRUCTURE_EVENT",
        "event_id": "S-hidden-upgrade",
        "event_time": "2026-08-25T10:07:00+08:00",
        "recorded_at": "2026-08-25T10:07:00+08:00",
    }
    state, active = build_event_lifecycle(
        None,
        [hidden_tick_event],
        as_of="2026-08-25T10:07:00+08:00",
        valid_bars=3,
    )
    assert active == [hidden_tick_event]

    # The objective ledger diff is empty on the following minute, but the
    # unconsumed event must still be supplied to the scheduled AI turn.
    state, active = build_event_lifecycle(
        state,
        [],
        as_of="2026-08-25T10:08:00+08:00",
        valid_bars=3,
    )
    assert active == [hidden_tick_event]

    consumed = mark_events_analyzed(
        state,
        ["S-hidden-upgrade"],
        analyzed_at="2026-08-25T10:08:00+08:00",
    )
    _, active_after_consumption = build_event_lifecycle(
        consumed,
        [],
        as_of="2026-08-25T10:09:00+08:00",
        valid_bars=3,
    )
    assert active_after_consumption == []


def test_forming_leg_does_not_create_pivot_using_an_atr_reversal_threshold() -> None:
    start = {
        "id": "P-high-1157",
        "kind": "HIGH",
        "bar_time": "2026-08-25T11:57:00+08:00",
        "price": 100.0,
    }
    bars = [
        _bar("11:57", 98, 100, 94, 95),
        _bar("11:58", 95, 96, 80, 82),
        _bar("11:59", 82, 92, 81, 91),
        _bar("12:00", 91, 98, 90, 97),
        _bar("12:01", 97, 112, 96, 110),
    ]
    leg = _forming_leg(
        [start],
        bars,
        level="SMALL",
        expected=datetime.fromisoformat("2026-08-25T12:01:00+08:00"),
    )
    assert leg is not None
    assert leg["direction"] == "BEAR"
    assert leg["start_time"].endswith("11:57:00+08:00")
    assert leg["start_price"] == 100
    assert leg["end_time"].endswith("11:58:00+08:00")
    assert leg["end_price"] == 80
    assert leg["source"] == "CAUSAL_PIVOT_EXTENSION"


def test_profit_milestone_is_applied_between_sparse_ai_cutoffs() -> None:
    position = {
        "version": 2,
        "as_of": "2026-08-25T09:01:00+08:00",
        "status": "SHORT",
        "entry_time": "2026-08-25T09:01:00+08:00",
        "entry_price": 100.0,
        "stop_price": 110.0,
        "active_setup_key": "short-copy",
        "behavior_plan": {"initial_stop_price": 110.0},
    }
    bars = [
        _bar("09:02", 99, 103, 84, 85),
        _bar("09:03", 86, 101, 83, 99),
    ]

    advanced, events, audit = advance_program_risk_controls(
        position,
        bars,
        as_of="2026-08-25T09:03:00+08:00",
    )

    assert len(events) == 1
    assert events[0]["effective_after"].endswith("09:02:00+08:00")
    assert events[0]["old_stop_price"] == 110.0
    assert events[0]["stop_price"] == 100.0
    assert advanced["stop_price"] == 100.0
    assert audit["status"] == "TRIGGERED"
    assert audit["trigger_time"].endswith("09:03:00+08:00")
    assert audit["fill_price"] == 100.0


def test_existing_stop_precedes_same_bar_close_based_profit_protection() -> None:
    position = {
        "version": 2,
        "as_of": "2026-08-25T09:01:00+08:00",
        "status": "LONG",
        "entry_time": "2026-08-25T09:01:00+08:00",
        "entry_price": 100.0,
        "stop_price": 90.0,
        "active_setup_key": "long-copy",
        "behavior_plan": {"initial_stop_price": 90.0},
    }

    advanced, events, audit = advance_program_risk_controls(
        position,
        [_bar("09:02", 100, 116, 89, 115)],
        as_of="2026-08-25T09:02:00+08:00",
    )

    assert events == []
    assert advanced["stop_price"] == 90.0
    assert audit["status"] == "TRIGGERED"
    assert audit["fill_price"] == 90.0


def test_new_favorable_defense_moves_stop_after_confirmation_and_catches_next_bar() -> None:
    position = {
        "version": 2,
        "as_of": "2026-08-25T09:01:00+08:00",
        "status": "LONG",
        "entry_time": "2026-08-25T09:01:00+08:00",
        "entry_price": 100.0,
        "stop_price": 90.0,
        "active_setup_key": "long-copy",
        "behavior_plan": {"initial_stop_price": 90.0},
    }
    bars = [
        _bar("09:02", 101, 105, 100, 104),
        _bar("09:03", 104, 105, 96, 97),
    ]
    defense = {
        "event_type": "PROGRAM_DEFENSE_AVAILABLE",
        "event_time": "2026-08-25T09:02:00+08:00",
        "direction": "LONG",
        "source_anchor_id": "A-long",
        "source_defense_id": "D-low",
        "source_time": "2026-08-25T09:00:00+08:00",
        "stop_price": 98.0,
    }
    advanced, events, audit = advance_program_risk_controls(
        position,
        bars,
        as_of="2026-08-25T09:03:00+08:00",
        structural_protection_events=[defense],
    )
    assert events[0]["protection_reason"] == "NEW_FAVORABLE_DEFENSE"
    assert events[0]["effective_after"].endswith("09:02:00+08:00")
    assert audit["status"] == "TRIGGERED"
    assert audit["trigger_time"].endswith("09:03:00+08:00")
    assert audit["fill_price"] == 98.0
    assert advanced["stop_price"] == 98.0


def test_new_defense_cannot_retroactively_stop_its_confirmation_bar() -> None:
    position = {
        "status": "SHORT",
        "entry_time": "2026-08-25T09:01:00+08:00",
        "entry_price": 100.0,
        "stop_price": 110.0,
        "active_setup_key": "short-copy",
        "behavior_plan": {"initial_stop_price": 110.0},
    }
    defense = {
        "event_type": "PROGRAM_DEFENSE_AVAILABLE",
        "event_time": "2026-08-25T09:02:00+08:00",
        "direction": "SHORT",
        "source_defense_id": "D-high",
        "stop_price": 102.0,
    }
    advanced, events, audit = advance_program_risk_controls(
        position,
        [_bar("09:02", 99, 103, 95, 98)],
        as_of="2026-08-25T09:02:00+08:00",
        structural_protection_events=[defense],
    )
    assert audit["status"] == "ACTIVE"
    assert advanced["stop_price"] == 102.0
    assert events[0]["protection_reason"] == "NEW_FAVORABLE_DEFENSE"
