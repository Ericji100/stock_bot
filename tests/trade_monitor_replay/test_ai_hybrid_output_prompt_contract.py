from __future__ import annotations

import json
import re

import pytest

from trade_monitor_replay.minimax_analyzer import build_replay_prompt
from trade_monitor_replay.semantic_contract import _active_directional_defense_refs


SCHEMA_TEXT = '{"properties":{"message_type":{},"reverse_anchor_candidate_ref":{}}}'


def _runtime(
    *,
    position_status: str = "FLAT",
    eligibility_status: str = "NONE",
    candidate_stage: str = "FORMING",
    candidate_execution_status: str = "OBSERVATION_ONLY",
    executable: bool = False,
    stop_status: str = "NOT_APPLICABLE",
    exit_status: str = "NOT_APPLICABLE",
) -> dict:
    setup_key = "SETUP-FALSE-BREAK"
    candidate = {
        "setup_key": setup_key,
        "candidate_source": "FALSE_BREAK_RECLAIM",
        "stage": candidate_stage,
        "execution_status": candidate_execution_status,
    }
    return {
        "simulated_position_state": {"status": position_status},
        "entry_eligibility": {"status": eligibility_status},
        "deterministic_evidence_events": [
            {"event_type": "STRUCTURE_EVENT", "event_id": "S-FALSE-BREAK"}
        ],
        "deterministic_evidence_ledger": {
            "structure_events": [
                {"id": "S-FALSE-BREAK", "event_type": "FALSE_BREAK_RECLAIM"}
            ],
            "trade_levels": {
                "false_break_arm_candidate": candidate,
                "continuation_arm_candidate": candidate if executable else None,
                "ai_executable_setup_keys": [setup_key] if executable else [],
            },
            "protective_stop_audit": {"status": stop_status},
            "program_behavior_exit_audit": {"status": exit_status},
        },
    }


def _prompt(runtime: dict) -> str:
    return build_replay_prompt(
        rules_text="# compact rules",
        schema_text=SCHEMA_TEXT,
        runtime_context=runtime,
        deterministic_contract=True,
        course_chain_contract=True,
        ai_hybrid=True,
        trade_direction_policy="LONG_ONLY",
        trade_setup_policy="LONG_Q2_Q4_ONLY",
    )


def _runtime_from_prompt(prompt: str) -> dict:
    match = re.search(r"(?s)<RUNTIME_CONTEXT>\n(.*?)\n</RUNTIME_CONTEXT>", prompt)
    assert match is not None
    return json.loads(match.group(1))


def test_observation_only_false_break_is_not_forced_to_preparation() -> None:
    source = _runtime()
    prompt = _prompt(source)
    serialized = _runtime_from_prompt(prompt)
    contract = serialized["ai_hybrid_output_contract"]

    assert "空手時必須輸出PREPARATION並引用該事件" not in prompt
    assert "不自動取得PREPARATION資格" in prompt
    assert next(iter(serialized)) == "ai_hybrid_output_contract"
    assert contract["required_message_type"] == "OBSERVATION"
    assert contract["required_reason"] == "FALSE_BREAK_RECLAIM_OBSERVATION_ONLY"
    assert contract["preparation_allowed_setup_keys"] == []
    assert contract["false_break_reclaim_policy"] == "OBSERVATION_ONLY"
    assert "ai_hybrid_output_contract" not in source


def test_executable_false_break_only_permits_preparation_after_ai_selects_it() -> None:
    contract = _runtime_from_prompt(
        _prompt(
            _runtime(
                candidate_stage="ARMED",
                candidate_execution_status="EXECUTABLE",
                executable=True,
            )
        )
    )["ai_hybrid_output_contract"]

    assert contract["required_message_type"] is None
    assert contract["preparation_allowed_setup_keys"] == ["SETUP-FALSE-BREAK"]
    assert (
        contract["false_break_reclaim_policy"]
        == "PREPARATION_ALLOWED_IF_AI_SELECTS_ACTIONABLE_SETUP"
    )


def test_entry_eligible_turn_requires_preparation() -> None:
    contract = _runtime_from_prompt(
        _prompt(
            _runtime(
                eligibility_status="ENTRY_ELIGIBLE",
                candidate_stage="ARMED",
                candidate_execution_status="EXECUTABLE",
                executable=True,
            )
        )
    )["ai_hybrid_output_contract"]

    assert contract["required_message_type"] == "PREPARATION"
    assert contract["required_reason"] == "ENTRY_ELIGIBLE_DECISION_DUE"


def test_entry_eligible_upgrade_exposes_atomic_reference_pair_and_preparation_priority() -> None:
    runtime = _runtime(
        eligibility_status="ENTRY_ELIGIBLE",
        candidate_stage="ARMED",
        candidate_execution_status="EXECUTABLE",
        executable=True,
    )
    runtime["deterministic_evidence_events"].append(
        {"event_type": "STRUCTURE_EVENT", "event_id": "S-UPGRADE"}
    )
    runtime["deterministic_evidence_ledger"]["structure_events"].append(
        {
            "id": "S-UPGRADE",
            "event_type": "GRADE_UPGRADE",
            "source_anchor_id": "L-UPGRADE-SOURCE",
        }
    )

    prompt = _prompt(runtime)
    contract = _runtime_from_prompt(prompt)["ai_hybrid_output_contract"]

    assert contract["required_message_type"] == "PREPARATION"
    assert contract["current_grade_upgrade_options"] == [
        {
            "event_ref": "S-UPGRADE",
            "controlling_anchor_ref": "L-UPGRADE-SOURCE",
        }
    ]
    assert "required_message_type非null時由STOP／EXIT／MANAGEMENT／PREPARATION優先" in prompt


def test_no_large_structure_requires_undefined_background_axes() -> None:
    contract = _runtime_from_prompt(_prompt(_runtime()))["ai_hybrid_output_contract"]

    assert contract["background_quadrant_policy"] == "MUST_BE_UNDEFINED_UNCLEAR"
    assert contract["active_background_anchor_ref"] is None
    assert contract["previous_ai_large_anchor_ref"] is None
    assert contract["current_grade_upgrade_refs"] == []


def test_contract_lists_exact_anchor_role_options_without_historical_legs() -> None:
    runtime = _runtime()
    runtime["deterministic_evidence_ledger"]["anchor_control"] = {
        "active_background_anchor_ref": "ANCHOR-LARGE",
        "active_child_anchor_ref": "ANCHOR-SMALL",
        "working_leg_ref": "WORKING-CURRENT",
        "reverse_anchor_candidate_ref": "REVERSE-CURRENT",
    }
    runtime["deterministic_evidence_ledger"]["legs"] = [
        {"id": "LEG-OLD-PARENT"},
        {"id": "LEG-OLD-COPY"},
    ]

    prompt = _prompt(runtime)
    contract = _runtime_from_prompt(prompt)["ai_hybrid_output_contract"]

    assert contract["anchor_role_ref_options"] == {
        "large_anchor_ref": [None, "ANCHOR-LARGE"],
        "small_anchor_ref": [None, "ANCHOR-SMALL"],
        "working_anchor_ref": [None, "WORKING-CURRENT"],
        "reverse_anchor_candidate_ref": [None, "REVERSE-CURRENT"],
    }
    assert "LEG-OLD-PARENT" not in json.dumps(
        contract["anchor_role_ref_options"], ensure_ascii=False
    )
    assert "working_anchor_ref只代表anchor_control目前工作段" in prompt


def test_active_grade_upgrade_remains_available_after_its_event_turn() -> None:
    runtime = _runtime()
    runtime["deterministic_evidence_ledger"]["structure_events"] = [
        {
            "id": "S-UPGRADE",
            "event_type": "GRADE_UPGRADE",
            "direction": "BULL",
            "from_level": "SMALL",
            "to_level": "LARGE",
            "source_anchor_id": "L-PROMOTED",
            "replacement_defense_pivot_id": "P-DEFENSE",
            "first_seen_at": "2040-01-02T09:10:00+08:00",
        }
    ]
    # The event is deliberately absent from deterministic_evidence_events: it
    # happened on a prior tick but its promoted structure remains active.
    runtime["deterministic_evidence_events"] = []

    contract = _runtime_from_prompt(_prompt(runtime))["ai_hybrid_output_contract"]

    assert contract["current_grade_upgrade_options"] == []
    assert contract["active_grade_upgrade_options"] == [
        {
            "event_ref": "S-UPGRADE",
            "controlling_anchor_ref": "L-PROMOTED",
            "direction": "BULL",
            "replacement_defense_ref": "P-DEFENSE",
        }
    ]
    assert contract["background_quadrant_policy"] == (
        "AI_COURSE_JUDGMENT_WITH_VALID_LARGE_STRUCTURE"
    )
    assert contract["large_active_directional_defense_refs"]["BULL"] == "P-DEFENSE"
    assert _active_directional_defense_refs(
        runtime["deterministic_evidence_ledger"],
        level="large",
    )["BULL"] == "P-DEFENSE"


def test_downgrade_retires_active_grade_upgrade_option_and_defense() -> None:
    runtime = _runtime()
    runtime["deterministic_evidence_ledger"]["structure_events"] = [
        {
            "id": "S-UPGRADE",
            "event_type": "GRADE_UPGRADE",
            "direction": "BULL",
            "from_level": "SMALL",
            "to_level": "LARGE",
            "source_anchor_id": "L-PROMOTED",
            "replacement_defense_pivot_id": "P-DEFENSE",
            "first_seen_at": "2040-01-02T09:10:00+08:00",
        },
        {
            "id": "S-DOWNGRADE",
            "event_type": "GRADE_DOWNGRADE",
            "source_upgrade_event_id": "S-UPGRADE",
            "first_seen_at": "2040-01-02T09:20:00+08:00",
        },
    ]
    runtime["deterministic_evidence_events"] = []

    contract = _runtime_from_prompt(_prompt(runtime))["ai_hybrid_output_contract"]

    assert contract["active_grade_upgrade_options"] == []
    assert contract["background_quadrant_policy"] == "MUST_BE_UNDEFINED_UNCLEAR"
    assert contract["large_active_directional_defense_refs"]["BULL"] is None
    assert _active_directional_defense_refs(
        runtime["deterministic_evidence_ledger"],
        level="large",
    ) == {}


def test_contract_exposes_both_active_directional_defenses_without_choosing_for_ai() -> None:
    runtime = _runtime()
    runtime["deterministic_evidence_ledger"]["anchor_lifecycle"] = {
        "dow_context": {
            "large_bull_defense": None,
            "large_bear_defense": {
                "id": "large-bear",
                "state": "ACTIVE",
            },
            "small_bull_defense": {
                "id": "small-bull",
                "state": "ACTIVE",
            },
            "small_bear_defense": {
                "id": "small-bear",
                "state": "ACTIVE",
            },
        }
    }

    contract = _runtime_from_prompt(_prompt(runtime))["ai_hybrid_output_contract"]

    assert contract["version"] == "ai-hybrid-output-contract-v7"
    assert contract["quadrant_axis_pair_policy"] == (
        "EXACT_QUADRANT_REQUIRED_FOR_COMPLETE_AXES"
    )
    assert contract["quadrant_axis_mapping"] == {
        "INCREASING|EXPANDING": "Q1",
        "DECREASING|EXPANDING": "Q2",
        "DECREASING|CONTRACTING": "Q3",
        "INCREASING|CONTRACTING": "Q4",
    }
    assert contract["large_active_directional_defense_refs"] == {
        "BULL": None,
        "BEAR": "large-bear",
    }
    assert contract["small_active_directional_defense_refs"] == {
        "BULL": "small-bull",
        "BEAR": "small-bear",
    }
    assert contract["directional_defense_policy"].startswith("AI_SELECTS_ONE")


def test_previous_ai_large_anchor_keeps_background_under_ai_judgment() -> None:
    runtime = _runtime()
    runtime["previous_semantic_memory"] = {
        "structure_control": {"active_large_anchor_ref": "LEG-LARGE"}
    }

    contract = _runtime_from_prompt(_prompt(runtime))["ai_hybrid_output_contract"]

    assert (
        contract["background_quadrant_policy"]
        == "AI_COURSE_JUDGMENT_WITH_VALID_LARGE_STRUCTURE"
    )
    assert contract["previous_ai_large_anchor_ref"] == "LEG-LARGE"


@pytest.mark.parametrize(
    ("stop_status", "exit_status", "expected_type", "expected_policy"),
    [
        ("NOT_APPLICABLE", "NOT_APPLICABLE", None, "POSITION_MANAGEMENT_OR_AI_EXIT"),
        ("TRIGGERED", "SUPPRESSED_BY_STOP", "STOP", "STOP_OVERRIDES_FALSE_BREAK"),
        ("NOT_APPLICABLE", "PENDING_FILL", "EXIT", "EXIT_OVERRIDES_FALSE_BREAK"),
    ],
)
def test_open_position_and_locked_execution_events_take_card_precedence(
    stop_status: str,
    exit_status: str,
    expected_type: str,
    expected_policy: str,
) -> None:
    contract = _runtime_from_prompt(
        _prompt(
            _runtime(
                position_status="LONG",
                stop_status=stop_status,
                exit_status=exit_status,
            )
        )
    )["ai_hybrid_output_contract"]

    assert contract["required_message_type"] == expected_type
    assert contract["false_break_reclaim_policy"] == expected_policy


def test_open_position_contract_allows_management_or_ai_exit() -> None:
    prompt = _prompt(_runtime(position_status="LONG"))
    contract = _runtime_from_prompt(prompt)["ai_hybrid_output_contract"]

    assert contract["required_message_type"] is None
    assert contract["required_reason"] == "OPEN_POSITION_AI_MANAGEMENT_OR_EXIT"
    assert contract["allowed_message_types"] == ["MANAGEMENT", "EXIT"]
    assert contract["action_tuple_policy"] == (
        "EXACT_POSITION_ACTION_DIRECTION_ENTRY_ROLE"
    )
    assert contract["required_action_tuple"] is None
    assert contract["allowed_action_tuples"] == [
        {
            "position_action": "NONE",
            "direction": "NONE",
            "entry_role": "NOT_APPLICABLE",
        },
        {
            "position_action": "EXIT",
            "direction": "NONE",
            "entry_role": "NOT_APPLICABLE",
        },
    ]
    assert "MANAGEMENT＋position_action=NONE" in prompt
    assert "EXIT＋position_action=EXIT" in prompt
    assert "position_action=ENTER才可填LONG" in prompt
    assert contract["open_position_management_contract"]["status"] == "ACTIVE"
    assert "成交時已凍結的戰法家族" in prompt


def test_open_q2_position_contract_exposes_frozen_plan_and_structural_evidence() -> None:
    runtime = _runtime(position_status="LONG")
    runtime["simulated_position_state"].update(
        {
            "active_setup_key": "SETUP-Q2",
            "entry_time": "2026-08-11T10:32:00+08:00",
            "entry_price": 44838.0,
            "stop_price": 44822.8,
            "behavior_plan": {
                "main_strategy_family": "Q2",
                "entry_strategy": "Q2_FALSE_BREAK_RECLAIM",
                "expected_behavior": "3根內快速離開掃低區。",
                "max_wait_bars": 3,
                "trigger_level": 44836.0,
                "obstacles": [{"role": "CHECKPOINT", "price": 44840.0}],
                "policy": "FALSE_BREAK_RECLAIM_CAUSAL_CHECKPOINT",
                "post_achievement_policy": (
                    "Q2_TARGET_OR_CONFIRMED_TREND_STRUCTURE_MANAGEMENT"
                ),
            },
        }
    )
    ledger = runtime["deterministic_evidence_ledger"]
    ledger["position_behavior_audit"] = {
        "status": "ACHIEVED",
        "bars_since_entry": 21,
        "max_wait_bars": 3,
        "checkpoint_price": 44872.375,
        "checkpoint_label": "Q2回轉0.25R動能檢查",
        "initial_r": 137.5,
        "achieved_at": "2026-08-11T10:33:00+08:00",
    }
    ledger["anchor_lifecycle"] = {
        "child_anchor": {
            "id": "ANCHOR-BULL",
            "level": "SMALL",
            "direction": "BULL",
            "status": "ACTIVE",
            "origin_time": "2026-08-11T10:12:00+08:00",
            "origin_price": 44650.0,
            "latest_extreme_time": "2026-08-11T10:48:00+08:00",
            "latest_extreme_price": 44968.0,
        },
        "dow_context": {
            "small_bull_defense": {
                "id": "DOW-BULL",
                "time": "2026-08-11T10:35:00+08:00",
                "price": 44832.0,
                "state": "ACTIVE",
            }
        },
        "taiji_context": {
            "current_relation": "COPY",
            "leg_evidence": {
                "current_leg": {
                    "start_time": "2026-08-11T10:35:00+08:00",
                    "start_price": 44832.0,
                    "end_time": "2026-08-11T10:48:00+08:00",
                    "end_price": 44968.0,
                    "direction": "BULL",
                    "amplitude_points": 136.0,
                    "duration_minutes": 13,
                    "slope_points_per_minute": 10.46,
                    "status": "FORMING",
                },
                "current_parent": {
                    "start_time": "2026-08-11T10:26:00+08:00",
                    "start_price": 44711.0,
                    "end_time": "2026-08-11T10:34:00+08:00",
                    "end_price": 44902.0,
                    "direction": "BULL",
                    "amplitude_points": 191.0,
                    "duration_minutes": 8,
                    "slope_points_per_minute": 23.875,
                    "status": "LOCAL_CONFIRMED",
                },
            },
        },
    }

    prompt = _prompt(runtime)
    contract = _runtime_from_prompt(prompt)["ai_hybrid_output_contract"]
    management = contract["open_position_management_contract"]

    assert management["accepted_main_strategy_family"] == "Q2"
    assert management["accepted_behavior_plan"]["post_achievement_policy"] == (
        "Q2_TARGET_OR_CONFIRMED_TREND_STRUCTURE_MANAGEMENT"
    )
    assert management["behavior_audit"]["status"] == "ACHIEVED"
    assert management["same_direction_controller"]["id"] == "ANCHOR-BULL"
    assert management["active_same_direction_defenses"] == [
        {
            "level": "SMALL",
            "ref": "DOW-BULL",
            "time": "2026-08-11T10:35:00+08:00",
            "price": 44832.0,
            "state": "ACTIVE",
        }
    ]
    assert management["taiji_leg_evidence"][
        "same_direction_leg_extends_parent_extreme"
    ] is True
    assert management["taiji_leg_evidence"]["interpretation_authority"] == (
        "AI_COURSE_JUDGMENT"
    )
    assert "反向加速但尚未形成反向錨" in prompt


@pytest.mark.parametrize(
    ("stop_status", "exit_status", "expected_action"),
    [
        ("TRIGGERED", "SUPPRESSED_BY_STOP", "STOP"),
        ("NOT_APPLICABLE", "PENDING_FILL", "EXIT"),
    ],
)
def test_locked_execution_event_requires_non_entry_action_tuple(
    stop_status: str,
    exit_status: str,
    expected_action: str,
) -> None:
    prompt = _prompt(
        _runtime(
            position_status="LONG",
            stop_status=stop_status,
            exit_status=exit_status,
        )
    )
    contract = _runtime_from_prompt(prompt)["ai_hybrid_output_contract"]

    assert contract["required_action_tuple"] == {
        "position_action": expected_action,
        "direction": "NONE",
        "entry_role": "NOT_APPLICABLE",
    }
    assert contract["allowed_action_tuples"] == []
    assert "required_action_tuple非null時" in prompt


def test_open_position_without_current_facts_hash_candidate_forces_null_assessment() -> None:
    prompt = _prompt(_runtime(position_status="LONG"))
    contract = _runtime_from_prompt(prompt)["ai_hybrid_output_contract"]

    assert contract["assessment_policy"] == "MUST_BE_NULL"
    assert contract["assessment_allowed_candidates"] == []
    assert "即使正在管理既有持倉" in prompt


def test_current_facts_hash_candidate_is_the_only_optional_assessment_pair() -> None:
    runtime = _runtime(
        candidate_stage="ARMED",
        candidate_execution_status="EXECUTABLE",
        executable=True,
    )
    candidate = runtime["deterministic_evidence_ledger"]["trade_levels"][
        "false_break_arm_candidate"
    ]
    candidate["facts_hash"] = "FACTS-current"
    runtime["deterministic_evidence_ledger"]["trade_levels"][
        "ai_candidate_inventory"
    ] = [candidate]
    runtime["deterministic_evidence_ledger"]["trade_levels"][
        "ai_armable_setup_keys"
    ] = [candidate["setup_key"]]

    contract = _runtime_from_prompt(_prompt(runtime))["ai_hybrid_output_contract"]

    assert contract["assessment_policy"] == "OPTIONAL_CURRENT_CANDIDATE_ONLY"
    assert contract["assessment_allowed_candidates"] == [
        {"setup_key": "SETUP-FALSE-BREAK", "facts_hash": "FACTS-current"}
    ]


def test_entry_eligible_facts_hash_pair_requires_assessment() -> None:
    runtime = _runtime(
        eligibility_status="ENTRY_ELIGIBLE",
        candidate_stage="ARMED",
        candidate_execution_status="EXECUTABLE",
        executable=True,
    )
    runtime["entry_eligibility"].update(
        {
            "setup_key": "SETUP-FALSE-BREAK",
            "required_facts_hash": "FACTS-entry",
        }
    )

    contract = _runtime_from_prompt(_prompt(runtime))["ai_hybrid_output_contract"]

    assert contract["assessment_policy"] == "REQUIRED_ENTRY_ELIGIBLE_CANDIDATE"
    assert contract["assessment_allowed_candidates"] == [
        {"setup_key": "SETUP-FALSE-BREAK", "facts_hash": "FACTS-entry"}
    ]


def test_non_hybrid_prompt_does_not_inject_hybrid_contract() -> None:
    prompt = build_replay_prompt(
        rules_text="# compact rules",
        schema_text=SCHEMA_TEXT,
        runtime_context={"marker": "unchanged"},
        deterministic_contract=True,
        course_chain_contract=True,
        ai_hybrid=False,
    )

    assert _runtime_from_prompt(prompt) == {"marker": "unchanged"}
