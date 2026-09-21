from __future__ import annotations

from trade_monitor_replay.semantic_contract import (
    _canonicalize_ai_hybrid_discretionary_exit_card,
    _canonicalize_ai_hybrid_mechanical_memory_fields,
    _canonicalize_flat_noop_action,
    _canonicalize_program_message_direction,
    _canonicalize_program_action_envelope,
    _canonicalize_unavailable_background_quadrant_memory,
)


def _hybrid_policy() -> dict:
    return {
        "program_trade_policy": {
            "actionable_setups": "PROGRAM_CANDIDATES_AI_DECISION",
            "decision_authority": "AI_HYBRID",
        }
    }


def test_program_filled_stop_canonicalizes_model_copy_errors() -> None:
    analysis = {
        "original_decision": "DONT_NOTIFY",
        "message_type": "UNCHANGED",
        "message_direction": "BEAR",
        "notification_reason": "old",
        "course_reading": {"setup_stage": "FORMING"},
        "action": {
            "position_action": "NONE",
            "direction": "NONE",
            "entry_role": "NOT_APPLICABLE",
            "setup_key": "wrong",
            "stop_price": 88.0,
        },
    }
    ledger = _hybrid_policy()
    ledger["protective_stop_audit"] = {"status": "TRIGGERED", "stop_price": 94.0}

    _canonicalize_program_action_envelope(
        analysis,
        ledger=ledger,
        position={"status": "LONG", "active_setup_key": "held-long"},
        entry_gate={"status": "NONE"},
        preopen=False,
        ai_generated=True,
    )

    assert analysis["original_decision"] == "NOTIFY"
    assert analysis["message_type"] == "STOP"
    assert analysis["message_direction"] == "BULL"
    assert analysis["action"]["position_action"] == "STOP"
    assert analysis["action"]["setup_key"] == "held-long"
    assert analysis["action"]["stop_price"] == 94.0


def test_program_behavior_exit_canonicalizes_model_copy_errors() -> None:
    analysis = {
        "original_decision": "DONT_NOTIFY",
        "message_type": "UNCHANGED",
        "message_direction": "BEAR",
        "notification_reason": "old",
        "course_reading": {"setup_stage": "FORMING"},
        "action": {
            "position_action": "NONE",
            "direction": "NONE",
            "entry_role": "NOT_APPLICABLE",
            "setup_key": None,
            "stop_price": None,
        },
    }
    ledger = _hybrid_policy()
    ledger["program_behavior_exit_audit"] = {"status": "PENDING_FILL"}

    _canonicalize_program_action_envelope(
        analysis,
        ledger=ledger,
        position={"status": "LONG", "active_setup_key": "held-long"},
        entry_gate={"status": "NONE"},
        preopen=False,
        ai_generated=True,
    )

    assert analysis["original_decision"] == "NOTIFY"
    assert analysis["message_type"] == "EXIT"
    assert analysis["message_direction"] == "BULL"
    assert analysis["action"]["position_action"] == "EXIT"
    assert analysis["action"]["setup_key"] == "held-long"


def test_ai_discretionary_exit_preserves_action_and_repairs_only_card() -> None:
    analysis = {
        "original_decision": "DONT_NOTIFY",
        "message_type": "MANAGEMENT",
        "message_direction": "BULL",
        "action": {"position_action": "EXIT", "setup_key": "held-long"},
    }

    _canonicalize_ai_hybrid_discretionary_exit_card(
        analysis,
        ledger=_hybrid_policy(),
        position={"status": "LONG", "active_setup_key": "held-long"},
        preopen=False,
        ai_generated=True,
    )

    assert analysis["original_decision"] == "NOTIFY"
    assert analysis["message_type"] == "EXIT"
    assert analysis["action"] == {
        "position_action": "EXIT",
        "setup_key": "held-long",
    }


def test_flat_none_action_drops_only_executable_stop_price() -> None:
    action = {
        "position_action": "NONE",
        "structural_stop": "若成立，結構停損放在08:53低點外。",
        "stop_price": 44869.0,
    }

    _canonicalize_flat_noop_action(action, position={"status": "FLAT"})

    assert action["stop_price"] is None
    assert "08:53" in action["structural_stop"]


def test_hybrid_memory_derives_change_times_and_matching_gate_stage() -> None:
    as_of = "2026-08-11T11:23:00+08:00"
    old_time = "2026-08-11T10:40:00+08:00"
    analysis = {"course_reading": {"setup_stage": "ENTRY_ELIGIBLE"}}
    memory = {
        "structure_control": {
            "background_quadrant": "Q2",
            "working_quadrant": "Q4",
            "background_quadrant_changed_at": as_of,
            "working_quadrant_changed_at": old_time,
        },
        "active_setups": [
            {
                "setup_key": "setup-long",
                "direction": "LONG",
                "stage": "ARMED",
                "trigger_operator": "CLOSE_ABOVE",
                "trigger_level": 44941.0,
                "valid_bars": 3,
            }
        ],
    }
    previous = {
        "version": 3,
        "session_key": "2026-08-11:DAY",
        "structure_control": {
            "background_quadrant": "Q2",
            "working_quadrant": "Q3",
            "background_quadrant_changed_at": old_time,
            "working_quadrant_changed_at": old_time,
        },
    }
    gate = {
        "status": "ENTRY_ELIGIBLE",
        "setup_key": "setup-long",
        "direction": "LONG",
        "trigger_operator": "CLOSE_ABOVE",
        "trigger_level": 44941.0,
        "valid_bars": 3,
    }

    _canonicalize_ai_hybrid_mechanical_memory_fields(
        analysis,
        memory,
        ledger=_hybrid_policy(),
        position={"status": "FLAT"},
        entry_gate=gate,
        previous_memory=previous,
        expected_as_of=as_of,
        expected_session_key="2026-08-11:DAY",
        ai_generated=True,
    )

    control = memory["structure_control"]
    assert control["background_quadrant_changed_at"] == old_time
    assert control["working_quadrant_changed_at"] == as_of
    assert memory["active_setups"][0]["stage"] == "ENTRY_ELIGIBLE"


def test_hybrid_memory_does_not_promote_mismatched_setup_facts() -> None:
    as_of = "2026-08-11T11:23:00+08:00"
    memory = {
        "structure_control": {
            "background_quadrant": "Q2",
            "working_quadrant": "Q4",
            "background_quadrant_changed_at": as_of,
            "working_quadrant_changed_at": as_of,
        },
        "active_setups": [
            {
                "setup_key": "setup-long",
                "direction": "LONG",
                "stage": "ARMED",
                "trigger_operator": "CLOSE_ABOVE",
                "trigger_level": 44940.0,
                "valid_bars": 3,
            }
        ],
    }

    _canonicalize_ai_hybrid_mechanical_memory_fields(
        {"course_reading": {"setup_stage": "ENTRY_ELIGIBLE"}},
        memory,
        ledger=_hybrid_policy(),
        position={"status": "FLAT"},
        entry_gate={
            "status": "ENTRY_ELIGIBLE",
            "setup_key": "setup-long",
            "direction": "LONG",
            "trigger_operator": "CLOSE_ABOVE",
            "trigger_level": 44941.0,
            "valid_bars": 3,
        },
        previous_memory=None,
        expected_as_of=as_of,
        expected_session_key="2026-08-11:DAY",
        ai_generated=True,
    )

    assert memory["active_setups"][0]["stage"] == "ARMED"


def test_hybrid_memory_clears_background_without_large_structure_authority() -> None:
    as_of = "2026-08-11T08:54:00+08:00"
    ledger = _hybrid_policy()
    ledger.update(
        {
            "anchor_lifecycle": {
                "background_anchor": None,
                "quadrant_context": {"authority": "EVIDENCE_ONLY"},
            },
            "structure_events": [],
        }
    )
    analysis = {
        "course_reading": {
            "large_anchor_ref": None,
            "structure_event_ref": None,
            "background_quadrant": "UNDEFINED",
        }
    }
    memory = {
        "session_key": "2026-08-11:DAY",
        "structure_control": {
            "background_quadrant": "Q1",
            "background_quadrant_changed_at": as_of,
        },
    }

    _canonicalize_unavailable_background_quadrant_memory(
        analysis,
        memory,
        ledger=ledger,
        previous_memory=None,
        expected_as_of=as_of,
        ai_generated=True,
    )

    assert memory["structure_control"]["background_quadrant"] == "UNDEFINED"


def test_hybrid_message_direction_is_derived_from_actual_open_position() -> None:
    analysis = {
        "message_direction": "NEUTRAL",
        "action": {"position_action": "NONE", "direction": "NONE"},
    }

    _canonicalize_program_message_direction(
        analysis,
        ledger=_hybrid_policy(),
        position={"status": "LONG"},
        ai_generated=True,
    )

    assert analysis["message_direction"] == "BULL"


def test_hybrid_entry_card_direction_is_derived_from_chosen_order_side() -> None:
    analysis = {
        "message_direction": "NEUTRAL",
        "action": {"position_action": "ENTER", "direction": "LONG"},
    }

    _canonicalize_program_message_direction(
        analysis,
        ledger=_hybrid_policy(),
        position={"status": "FLAT"},
        ai_generated=True,
    )

    assert analysis["message_direction"] == "BULL"
