from __future__ import annotations

import copy
import json
import runpy
from pathlib import Path

from scripts.v2_core_contract_validator_v1 import validate_record


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
SCHEMA = json.loads((OUT / "v2_core_reproducible_r1.schema.candidate.json").read_text(encoding="utf-8"))
SAMPLE = runpy.run_path(str(ROOT / "tests" / "test_v2_core_candidate_schema_r0.py"))["sample"]


def test_semantic_validator_accepts_contract_sample() -> None:
    assert validate_record(SAMPLE(), SCHEMA) == []


def test_scenario_overall_is_derived_from_atomic_gates() -> None:
    record = SAMPLE()
    record["scenario_evaluations"]["MATURE_TREND_PULLBACK"]["gates"]["LONG_MA_HABIT"]["result"] = "FAIL"
    record["scenario_evaluations"]["MATURE_TREND_PULLBACK"]["gates"]["LONG_MA_HABIT"]["supporting_evidence_refs"] = []
    record["scenario_evaluations"]["MATURE_TREND_PULLBACK"]["gates"]["LONG_MA_HABIT"]["contradicting_evidence_refs"] = ["MA:MA105"]
    record["scenario_evaluations"]["MATURE_TREND_PULLBACK"]["gates"]["LONG_MA_HABIT"]["reason_code"] = "VISIBLE_EVIDENCE_CONTRADICTS_DEFINITION"
    errors = validate_record(record, SCHEMA)
    assert any("OVERALL_PASS_BUT_GATES_FAIL" in error for error in errors)


def test_monitor_state_cannot_override_add_contract() -> None:
    record = SAMPLE()
    record["position_lifecycle"]["position_role"] = "ADD_1"
    record["position_lifecycle"]["add_number"] = 1
    record["position_lifecycle"]["existing_position_profitable"] = "NO"
    errors = validate_record(record, SCHEMA)
    assert "SEMANTIC:ADD_REQUIRES_PROFIT_AND_MATCHING_ADD_NUMBER" in errors


def test_causal_dates_and_references_are_checked() -> None:
    record = SAMPLE()
    record["stops"][0]["confirmed_on"] = "2023-02-01"
    record["decision"]["episode_stop_id"] = "STOP-MISSING"
    errors = validate_record(record, SCHEMA)
    assert any("STOP_DATE_AFTER_AS_OF" in error for error in errors)
    assert any("TRADE_REQUIRES_KNOWN_EPISODE_STOP" in error for error in errors)


def test_route_must_match_primary_scenario() -> None:
    record = SAMPLE()
    record["decision"]["canonical_trigger_route"] = "FRESH_INITIAL_DESTRUCTIVE_EXPANSION"
    assert "SEMANTIC:ROUTE_PREFIX_DOES_NOT_MATCH_PRIMARY_SCENARIO" in validate_record(record, SCHEMA)


def test_location_metric_arithmetic_and_one_r_floor_are_enforced() -> None:
    record = SAMPLE()
    metrics = record["shared_structure"]["location_metrics"]
    metrics["nearest_overhead_resistance_price"] = 12.5
    metrics["remaining_space_points"] = 0.5
    metrics["space_to_risk"] = 0.5
    errors = validate_record(record, SCHEMA)
    assert "SEMANTIC:SPACE_BELOW_1R_CANNOT_PASS" in errors

    record = SAMPLE()
    record["shared_structure"]["location_metrics"]["episode_risk_points"] = 9.0
    assert "SEMANTIC:EPISODE_RISK_POINTS_ARITHMETIC_MISMATCH" in validate_record(record, SCHEMA)


def test_trade_location_stop_and_decision_stop_must_match() -> None:
    record = SAMPLE()
    record["shared_structure"]["location_metrics"]["episode_stop_id"] = "STOP-C1"
    record["shared_structure"]["location_metrics"]["episode_risk_points"] = 3.0
    errors = validate_record(record, SCHEMA)
    assert "SEMANTIC:LOCATION_METRICS_STOP_MUST_MATCH_DECISION_STOP" in errors
