from __future__ import annotations

import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1" / "v2_core_reproducible_r1.schema.candidate.json"


def verdict(result: str = "PASS") -> dict:
    if result == "PASS":
        return {
            "result": "PASS",
            "supporting_evidence_refs": ["PRICE:2023-01-02"],
            "contradicting_evidence_refs": [],
            "missing_evidence_codes": [],
            "reason_code": "VISIBLE_EVIDENCE_SATISFIES_DEFINITION",
        }
    if result == "FAIL":
        return {
            "result": "FAIL",
            "supporting_evidence_refs": [],
            "contradicting_evidence_refs": ["PIVOT:LARGE_HIGH_1"],
            "missing_evidence_codes": [],
            "reason_code": "VISIBLE_EVIDENCE_CONTRADICTS_DEFINITION",
        }
    return {
        "result": "UNKNOWN",
        "supporting_evidence_refs": [],
        "contradicting_evidence_refs": [],
        "missing_evidence_codes": ["MISSING_COMPARISON_SEGMENT"],
        "reason_code": "REQUIRED_EVIDENCE_MISSING",
    }


def scenario(overall: str, gates: list[str]) -> dict:
    gate_result = "PASS" if overall == "PASS" else "FAIL"
    return {
        "overall": overall,
        "blocking_reasons": [] if overall == "PASS" else ["required premise false"],
        "uncertainties": [],
        "gates": {gate: verdict(gate_result) for gate in gates},
    }


def sample() -> dict:
    mature = ["ACTIVE_LARGE_UPTREND", "LONG_TREND_PERSISTENCE", "LONG_MA_HABIT", "CORRECTION_WITHIN_CAMPAIGN", "TAIJI_GENERATION_MAPPED", "DYNAMIC_QUADRANTS_SUPPORT", "SMALL_UP_CONTROL_CAUSAL", "EPISODE_STOP_CAUSAL"]
    macro = ["COMPLETED_PARENT_ANCHOR", "CORRECTION_INTACT", "TAIJI_GENERATION_MAPPED", "CORRECTION_BEAR_DOW_LINE_CAUSAL", "SMALL_UP_REANCHOR_BREAK", "DUAL_SCALE_LONG_ALIGNMENT", "NOT_Q3_OR_EXHAUSTED", "EPISODE_STOP_CAUSAL"]
    bear = ["ACTIVE_LARGE_BEAR_ANCHOR", "LARGE_BEAR_DOW_DEFENSE_CAUSAL", "BEAR_LATE_STAGE_EVIDENCE", "LEFT_RIGHT_PHASE_MAPPED", "DUAL_SCALE_SEPARATED", "PHASE_STOP_CAUSAL"]
    fresh = ["FRESH_UP_ANCHOR", "CLEAN_MEATY_DESTRUCTIVE_TRACEABLE", "DYNAMIC_Q1_EXPANSION", "EARLY_TAIJI_GENERATION", "MACD_SUPPORT_ONLY", "EARLY_LOCATION_WITH_SPACE", "FRESH_ANCHOR_STOP_CAUSAL"]
    anchor_atomic = {name: verdict("PASS" if name != "ANCHOR_COMPLETED" else "UNKNOWN") for name in ["ANCHOR_DIRECTION_COHERENT", "ANCHOR_CLEAN", "ANCHOR_MEATY", "ANCHOR_DESTRUCTIVE", "ANCHOR_TRACEABLE", "ANCHOR_COMPLETED", "ANCHOR_CONTROLS_CURRENT_CONTEXT"]}
    scale = {
        "direction": "BULL",
        "quadrant": "Q4",
        "dow": "BULL",
        "trend_strength_change": "INCREASED",
        "volatility_change": "CONTRACTED",
        "next_up_direction_supported": verdict(),
        "controlling_stop_id": "STOP-E1",
        "supporting_evidence_refs": ["PIVOT:LARGE_LOW_1"],
    }
    return {
        "schema_version": "v2-core-reproducible-r1-candidate",
        "protocol_version": "v2-core-monitoring-protocol-r1-candidate",
        "contract_status": "CANDIDATE_NOT_FROZEN",
        "review_id": "D-0123456789abcdef01234567",
        "anonymous_stock_id": "S-0123456789abcdef",
        "as_of": "2023-01-03",
        "input_packet_sha256": "a" * 64,
        "spec_sha256": "b" * 64,
        "prompt_sha256": "c" * 64,
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "data_sufficiency": verdict(),
        "anchors": [{
            "anchor_id": "ANCHOR-A1",
            "role": "CONTROLLING",
            "scale": "LARGE",
            "direction": "UP",
            "start_date": "2022-01-03",
            "start_price": 10.0,
            "end_date": None,
            "end_price": None,
            "status": "FORMING",
            "invalidation_stop_id": "STOP-C1",
            "legacy_raw_label": None,
            "atomic": anchor_atomic,
        }],
        "relations": [],
        "stops": [
            {"stop_id": "STOP-E1", "scope": "TRADE_EPISODE", "scale": "SMALL", "direction": "BULL_DEFENSE", "price": 11.0, "source_date": "2022-12-20", "confirmed_on": "2022-12-23", "supporting_evidence_refs": ["PIVOT:SMALL_LOW_1"]},
            {"stop_id": "STOP-C1", "scope": "PARENT_CAMPAIGN", "scale": "LARGE", "direction": "BULL_DEFENSE", "price": 9.0, "source_date": "2022-10-20", "confirmed_on": "2022-11-03", "supporting_evidence_refs": ["PIVOT:LARGE_LOW_1"]},
        ],
        "shared_structure": {
            "controlling_anchor_id": "ANCHOR-A1",
            "alternative_anchor_ids": [],
            "large_scale": scale,
            "small_scale": {**scale, "quadrant": "Q1"},
            "taiji_phase": "COPY_ATTACK",
            "taiji_leg_index": 3,
            "campaign_generation": 2,
            "left_right_phase": "NONE",
            "location_metrics": {
                "signal_close": 12.0,
                "episode_stop_id": "STOP-E1",
                "episode_risk_points": 1.0,
                "nearest_overhead_resistance_date": "2022-12-30",
                "nearest_overhead_resistance_price": 14.0,
                "nearest_overhead_resistance_scale": "LARGE",
                "pressure_broken_on_as_of": False,
                "remaining_space_points": 2.0,
                "space_to_risk": 2.0,
                "program_calculated": True,
            },
            "location_remaining_space": verdict(),
            "location_not_extended": verdict(),
            "exhaustion": verdict("FAIL"),
            "signal_has_independent_structure": verdict(),
        },
        "scenario_evaluations": {
            "MATURE_TREND_PULLBACK": scenario("PASS", mature),
            "MACRO_COPY_RESONANCE": scenario("FAIL", macro),
            "BEAR_REVERSAL_LEFT_RIGHT": scenario("FAIL", bear),
            "FRESH_Q1_EXPANSION": scenario("FAIL", fresh),
        },
        "decision": {
            "primary_scenario": "MATURE_TREND_PULLBACK",
            "secondary_scenarios": [],
            "alternative_conflict": False,
            "permission": "TRADE_APPROVED",
            "trigger_status": "TRIGGERED",
            "canonical_trigger_route": "MATURE_FIRST_SHALLOW_PULLBACK",
            "trigger_date": "2023-01-03",
            "episode_stop_id": "STOP-E1",
            "campaign_stop_id": "STOP-C1",
            "supporting_evidence_refs": ["PRICE:2023-01-03"],
            "blocking_reasons": [],
            "uncertainties": [],
        },
        "position_lifecycle": {
            "position_role": "MOTHER",
            "episode_number": 1,
            "add_number": None,
            "existing_position_profitable": "NOT_APPLICABLE",
            "structure_scenario_independent_of_position_state": True,
        },
        "causal_attestation": {
            "latest_visible_date": "2023-01-03",
            "future_bars_used": False,
            "future_performance_used": False,
            "legacy_answers_visible": False,
            "stock_identity_visible": False,
            "all_evidence_refs_from_packet": True,
            "monitor_start_used_to_choose_scenario": False,
        },
    }


def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def test_candidate_schema_accepts_full_valid_record() -> None:
    assert list(validator().iter_errors(sample())) == []


def test_trade_primary_scenario_must_have_pass_overall() -> None:
    record = sample()
    record["decision"]["primary_scenario"] = "MACRO_COPY_RESONANCE"
    assert list(validator().iter_errors(record))


def test_trade_requires_triggered_and_episode_stop() -> None:
    record = sample()
    record["decision"]["trigger_status"] = "ARMED"
    record["decision"]["episode_stop_id"] = None
    assert list(validator().iter_errors(record))


def test_unknown_atomic_answer_requires_missing_evidence() -> None:
    record = sample()
    record["data_sufficiency"]["result"] = "UNKNOWN"
    record["data_sufficiency"]["reason_code"] = "REQUIRED_EVIDENCE_MISSING"
    record["data_sufficiency"]["missing_evidence_codes"] = []
    assert list(validator().iter_errors(record))


def test_unknown_extra_property_is_rejected() -> None:
    record = sample()
    record["unexpected"] = True
    assert list(validator().iter_errors(record))
