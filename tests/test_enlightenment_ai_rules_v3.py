from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import jsonschema
import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_enlightenment_ai_rules_v3 import (  # noqa: E402
    DOC_PATH,
    RULES_PATH,
    SCHEMA_PATH,
    _judgement_validator,
    validate,
    validate_payload,
)


def _load_v2_example_builder():
    source = ROOT / "tests/test_enlightenment_ai_rules_v2.py"
    spec = importlib.util.spec_from_file_location("v2_contract_test_helper", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._example


_v2_example = _load_v2_example_builder()


def _rules() -> dict:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _gate(result: str = "PASS") -> dict:
    return {"result": result, "evidence": ["只使用判讀日收盤以前可確認的日期、價位與結構證據"]}


def _common_guards() -> dict:
    return {gate_id: _gate() for gate_id in _rules()["common_probe_hard_guards"]}


def _route_rows(route: str, unknown: str | None) -> list[dict]:
    if route == "V2_CORE":
        return []
    policy = _rules()["layers"][route]
    rows = []
    for gate_id in policy["hard_pass_gates"] + policy["soft_gates"]:
        rows.append(
            {
                "gate_id": gate_id,
                "result": "UNKNOWN" if gate_id == unknown else "PASS",
                "evidence": ["此gate保留當時可見的支持證據、反證與待確認事件"],
            }
        )
    return rows


def _payload(route: str, unknown: str | None = None) -> dict:
    core = route == "V2_CORE"
    base = _v2_example(triggered=core)
    scenario_by_route = {
        "NEAR_PASS_MACRO_COPY": "MACRO_COPY_RESONANCE",
        "NEAR_PASS_FRESH_Q1": "FRESH_Q1_EXPANSION",
        "BEAR_REVERSAL_PROBE": "BEAR_REVERSAL_LEFT_RIGHT",
    }
    if not core:
        base["structure"]["primary_scenario"] = scenario_by_route[route]
    if route == "BEAR_REVERSAL_PROBE":
        base["structure"]["left_right_phase"] = "LR"

    components = {
        "CLEAN": "NOT_APPLICABLE",
        "MEATY": "NOT_APPLICABLE",
        "TRACEABLE": "NOT_APPLICABLE",
        "HIGHER_SCALE_DESTRUCTION": "NOT_APPLICABLE",
    }
    if route == "NEAR_PASS_FRESH_Q1":
        components = {"CLEAN": "PASS", "MEATY": "PASS", "TRACEABLE": "PASS", "HIGHER_SCALE_DESTRUCTION": "PASS"}
        if unknown == "CLEAN_MEATY_DESTRUCTIVE_TRACEABLE":
            components["HIGHER_SCALE_DESTRUCTION"] = "UNKNOWN"

    layer = {
        "V2_CORE": "V2_CORE",
        "NEAR_PASS_MACRO_COPY": "NEAR_PASS",
        "NEAR_PASS_FRESH_Q1": "NEAR_PASS",
        "BEAR_REVERSAL_PROBE": "BEAR_REVERSAL_PROBE",
    }[route]
    status = {
        "V2_CORE": "CORE_TRIGGERED",
        "NEAR_PASS_MACRO_COPY": "NEAR_PASS_TRIGGERED",
        "NEAR_PASS_FRESH_Q1": "NEAR_PASS_TRIGGERED",
        "BEAR_REVERSAL_PROBE": "BEAR_PROBE_TRIGGERED",
    }[route]
    label = {
        "V2_CORE": "核心已觸發",
        "NEAR_PASS_MACRO_COPY": "近合格試單已觸發",
        "NEAR_PASS_FRESH_Q1": "近合格試單已觸發",
        "BEAR_REVERSAL_PROBE": "空頭反轉試單已觸發",
    }[route]

    return {
        "schema_version": "enlightenment-ai-judgement-v3",
        "rule_version": "enlightenment-ai-rules-v3",
        "base_v2_assessment": base,
        "v3_overlay": {
            "layer": layer,
            "route": route,
            "gate_deviation": {
                "unknown_gates": [] if unknown is None else [unknown],
                "fail_gates": [],
                "allowed_unknown_gate": unknown,
                "explanation": "UNKNOWN代表指定證據尚未完全形成，不代表已有反證或結構失效。",
                "upgrade_waiting_event": None if core else "等待唯一UNKNOWN gate取得新的獨立結構證據後轉PASS。",
            },
            "common_hard_guards": _common_guards(),
            "route_specific_audit": {
                "route_gate_results": _route_rows(route, unknown),
                "fresh_quality_components": components,
                "small_q1_expansion_pass": True if route == "NEAR_PASS_FRESH_Q1" else None,
                "same_direction_attack_number": 1 if route == "NEAR_PASS_FRESH_Q1" else None,
                "late_stage_partial_evidence": ["ATTACK_SHORTENING"] if route == "BEAR_REVERSAL_PROBE" else [],
                "left_right_phase": "LR" if route == "BEAR_REVERSAL_PROBE" else "NOT_APPLICABLE",
            },
            "trade_plan": {
                "signal_date": "2025-06-23",
                "signal_close": 45.0,
                "stop_price": 40.0,
                "stop_source_date": "2025-06-10",
                "stop_confirmed_on": "2025-06-20",
                "stop_distance_pct": 11.11,
                "stop_distance_atr": 1.8,
            },
            "decision": {
                "status": status,
                "status_label_zh": label,
                "intent": "ENTER_CORE" if core else "ENTER_PROBE",
                "entry_role": "MOTHER" if core else "PROBE_MOTHER",
                "execution_rule": "NEXT_TRADING_DAY_OPEN",
                "planned_nominal_twd": 10000,
                "same_day_evidence_deduplicated": True,
                "reason": "日K收盤完成獨立控制權轉換，下一交易日開盤執行。",
            },
            "promotion": {
                "state": "NOT_APPLICABLE",
                "v2_core_now_complete": core,
                "position_profitable_at_close": None,
                "independent_new_structure": False,
                "action": "NONE",
            },
        },
        "causal_attestation": {
            "latest_visible_bar": "2025-06-23",
            "used_future_data": False,
            "outcome_visible_to_ai": False,
            "known_winner_reference_used": False,
            "rules_changed_after_outcome": False,
        },
    }


@pytest.mark.parametrize(
    ("route", "unknown"),
    [
        ("V2_CORE", None),
        ("NEAR_PASS_MACRO_COPY", "TAIJI_GENERATION_MAPPED"),
        ("NEAR_PASS_FRESH_Q1", "CLEAN_MEATY_DESTRUCTIVE_TRACEABLE"),
        ("NEAR_PASS_FRESH_Q1", "DYNAMIC_Q1_EXPANSION"),
        ("NEAR_PASS_FRESH_Q1", "EARLY_TAIJI_GENERATION"),
        ("BEAR_REVERSAL_PROBE", "BEAR_LATE_STAGE_EVIDENCE"),
    ],
)
def test_v3_schema_and_semantics_accept_valid_routes(route: str, unknown: str | None) -> None:
    payload = _payload(route, unknown)
    assert validate_payload(payload) == []


def test_v3_contract_self_audit_passes_and_protects_v1_v2() -> None:
    result = validate()
    assert result["passed"], [check for check in result["checks"] if not check["passed"]]
    protected = next(check for check in result["checks"] if check["id"] == "v1_v2_protected_files_unchanged")
    assert len(protected["detail"]) == 7
    assert all(row["matched"] for row in protected["detail"])


def test_v3_schema_is_valid_and_wraps_v2_by_exact_ref() -> None:
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["properties"]["base_v2_assessment"]["$ref"] == "https://local.stock-ai-bot/schemas/enlightenment-ai-judgement-v2.json"
    _judgement_validator().validate(_payload("V2_CORE"))


def test_near_pass_rejects_two_unknowns_and_any_common_guard_failure() -> None:
    invalid = _payload("NEAR_PASS_MACRO_COPY", "TAIJI_GENERATION_MAPPED")
    invalid["v3_overlay"]["gate_deviation"]["unknown_gates"].append("DUAL_SCALE_LONG_ALIGNMENT")
    assert validate_payload(invalid)

    invalid = _payload("NEAR_PASS_MACRO_COPY", "TAIJI_GENERATION_MAPPED")
    invalid["v3_overlay"]["common_hard_guards"]["NO_V2_FAIL"] = _gate("FAIL")
    assert validate_payload(invalid)


def test_near_pass_rejects_wrong_scenario_and_mismatched_unknown_field() -> None:
    invalid = _payload("NEAR_PASS_MACRO_COPY", "TAIJI_GENERATION_MAPPED")
    invalid["base_v2_assessment"]["structure"]["primary_scenario"] = "FRESH_Q1_EXPANSION"
    assert validate_payload(invalid)

    invalid = _payload("NEAR_PASS_MACRO_COPY", "TAIJI_GENERATION_MAPPED")
    invalid["v3_overlay"]["gate_deviation"]["allowed_unknown_gate"] = "DUAL_SCALE_LONG_ALIGNMENT"
    errors = validate_payload(invalid)
    assert any("must equal unknown_gates" in error for error in errors)


def test_route_gate_audit_must_contain_exact_hard_and_soft_gates() -> None:
    invalid = _payload("NEAR_PASS_FRESH_Q1", "DYNAMIC_Q1_EXPANSION")
    invalid["v3_overlay"]["route_specific_audit"]["route_gate_results"].pop()
    errors = validate_payload(invalid)
    assert any("route gate set mismatch" in error for error in errors)

    invalid = _payload("NEAR_PASS_FRESH_Q1", "DYNAMIC_Q1_EXPANSION")
    invalid["v3_overlay"]["route_specific_audit"]["route_gate_results"][0]["result"] = "UNKNOWN"
    errors = validate_payload(invalid)
    assert any("hard route gate" in error for error in errors)


def test_fresh_residual_audit_is_enforced() -> None:
    invalid = _payload("NEAR_PASS_FRESH_Q1", "CLEAN_MEATY_DESTRUCTIVE_TRACEABLE")
    invalid["v3_overlay"]["route_specific_audit"]["fresh_quality_components"]["CLEAN"] = "UNKNOWN"
    assert any("CLEAN/MEATY/TRACEABLE" in error for error in validate_payload(invalid))

    invalid = _payload("NEAR_PASS_FRESH_Q1", "EARLY_TAIJI_GENERATION")
    invalid["v3_overlay"]["route_specific_audit"]["same_direction_attack_number"] = 3
    assert any("attack number <= 2" in error for error in validate_payload(invalid))


def test_bear_probe_rejects_ll_or_missing_late_stage_clue() -> None:
    invalid = _payload("BEAR_REVERSAL_PROBE", "BEAR_LATE_STAGE_EVIDENCE")
    invalid["v3_overlay"]["route_specific_audit"]["left_right_phase"] = "LL"
    assert validate_payload(invalid)

    invalid = _payload("BEAR_REVERSAL_PROBE", "BEAR_LATE_STAGE_EVIDENCE")
    invalid["v3_overlay"]["route_specific_audit"]["late_stage_partial_evidence"] = []
    assert validate_payload(invalid)


def test_outcome_visibility_and_known_winner_reference_are_forbidden() -> None:
    invalid = _payload("V2_CORE")
    invalid["causal_attestation"]["outcome_visible_to_ai"] = True
    assert validate_payload(invalid)

    invalid = _payload("V2_CORE")
    invalid["causal_attestation"]["known_winner_reference_used"] = True
    assert validate_payload(invalid)


def test_v3_does_not_use_v1_trigger_as_eligibility_authority() -> None:
    rules = _rules()
    assert rules["version_lineage"]["v1_trigger_is_eligibility_input"] is False
    assert "BENCHMARK_ONLY" in rules["version_lineage"]["v1_role"]
    assert "不把 V1 訊號本身當成 V3" in DOC_PATH.read_text(encoding="utf-8")
