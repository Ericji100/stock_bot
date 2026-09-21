"""Validate Enlightenment AI judgement V3 and prove V1/V2 stayed unchanged."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "config/enlightenment_ai_rules_v3.json"
DOC_PATH = ROOT / "docs/enlightenment-ai-judgement-v3.md"
SCHEMA_PATH = ROOT / "config/enlightenment_ai_judgement_v3.schema.json"
V2_SCHEMA_PATH = ROOT / "config/enlightenment_ai_judgement_v2.schema.json"
OUTPUT_DIR = ROOT / "reports/course_backtest/2026-09-06/enlightenment_ai_v3_rules"

EXPECTED_PRECEDENCE = [
    "V2_CORE",
    "NEAR_PASS_MACRO_COPY",
    "NEAR_PASS_FRESH_Q1",
    "BEAR_REVERSAL_PROBE",
    "NO_TRADE",
]
EXPECTED_COMMON_GUARDS = [
    "ACTIVE_WATCHLIST",
    "DATA_SUFFICIENT_FOR_ROUTE",
    "TRIGGER_COMPLETED",
    "CAUSAL_EPISODE_STOP",
    "NOT_Q3",
    "NOT_LATE_OR_EXHAUSTED",
    "NO_SCALE_DIRECTION_CONFLICT",
    "NOT_SINGLE_INDICATOR_SIGNAL",
    "RISK_EXECUTABLE",
    "NO_V2_FAIL",
]
KNOWN_OUTCOME_TOKENS = [
    "8054",
    "6535",
    "6140",
    "8096",
    "8059",
    "3548",
    "安國",
    "順藥",
    "訊達電腦",
    "擎亞",
    "凱碩",
    "兆利",
]


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _judgement_validator() -> jsonschema.Draft202012Validator:
    schema = _read(SCHEMA_PATH)
    v2_schema = _read(V2_SCHEMA_PATH)
    registry = Registry().with_resource(v2_schema["$id"], Resource.from_contents(v2_schema))
    return jsonschema.Draft202012Validator(
        schema,
        registry=registry,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


def validate_payload(payload: dict[str, Any]) -> list[str]:
    """Validate one AI output, including cross-field rules JSON Schema cannot express."""
    errors = [
        f"schema:{'/'.join(str(part) for part in error.absolute_path)}:{error.message}"
        for error in sorted(_judgement_validator().iter_errors(payload), key=lambda row: list(row.absolute_path))
    ]
    if errors:
        return errors

    rules = _read(RULES_PATH)
    overlay = payload["v3_overlay"]
    route = overlay["route"]
    status = overlay["decision"]["status"]
    deviation = overlay["gate_deviation"]
    triggered = status in {"CORE_TRIGGERED", "NEAR_PASS_TRIGGERED", "BEAR_PROBE_TRIGGERED"}

    if triggered:
        for guard_id, result in overlay["common_hard_guards"].items():
            if result["result"] != "PASS":
                errors.append(f"semantic:triggered common guard {guard_id} must PASS")
        if deviation["fail_gates"]:
            errors.append("semantic:triggered output cannot contain FAIL gate")

    if route == "V2_CORE":
        if payload["base_v2_assessment"]["decision"]["status"] != "TRIGGERED":
            errors.append("semantic:V2_CORE must wrap a V2 TRIGGERED decision")
        if deviation["unknown_gates"] or deviation["allowed_unknown_gate"] is not None:
            errors.append("semantic:V2_CORE cannot contain UNKNOWN gate")
        return errors

    if route in {"NEAR_PASS_MACRO_COPY", "NEAR_PASS_FRESH_Q1", "BEAR_REVERSAL_PROBE"}:
        route_rule = rules["layers"][route]
        unknown = deviation["unknown_gates"]
        if len(unknown) != route_rule["required_unknown_count"]:
            errors.append(f"semantic:{route} must contain exactly one UNKNOWN gate")
            return errors
        if deviation["allowed_unknown_gate"] != unknown[0]:
            errors.append("semantic:allowed_unknown_gate must equal unknown_gates[0]")
        if unknown[0] not in route_rule["soft_gates"]:
            errors.append(f"semantic:{unknown[0]} is not an allowed soft gate for {route}")

        rows = overlay["route_specific_audit"]["route_gate_results"]
        gate_results: dict[str, str] = {}
        for row in rows:
            if row["gate_id"] in gate_results:
                errors.append(f"semantic:duplicate route gate {row['gate_id']}")
            gate_results[row["gate_id"]] = row["result"]
        expected_gates = set(route_rule["hard_pass_gates"]) | set(route_rule["soft_gates"])
        if set(gate_results) != expected_gates:
            errors.append(
                "semantic:route gate set mismatch; "
                f"missing={sorted(expected_gates - set(gate_results))}, "
                f"extra={sorted(set(gate_results) - expected_gates)}"
            )
        for gate_id in route_rule["hard_pass_gates"]:
            if gate_results.get(gate_id) != "PASS":
                errors.append(f"semantic:hard route gate {gate_id} must PASS")
        for gate_id in route_rule["soft_gates"]:
            expected = "UNKNOWN" if gate_id == unknown[0] else "PASS"
            if gate_results.get(gate_id) != expected:
                errors.append(f"semantic:soft route gate {gate_id} must be {expected}")

    audit = overlay["route_specific_audit"]
    if route == "NEAR_PASS_FRESH_Q1":
        unknown = deviation["unknown_gates"][0]
        components = audit["fresh_quality_components"]
        if unknown == "CLEAN_MEATY_DESTRUCTIVE_TRACEABLE":
            if any(components[name] != "PASS" for name in ("CLEAN", "MEATY", "TRACEABLE")):
                errors.append("semantic:fresh quality UNKNOWN still requires CLEAN/MEATY/TRACEABLE PASS")
            if components["HIGHER_SCALE_DESTRUCTION"] != "UNKNOWN":
                errors.append("semantic:only HIGHER_SCALE_DESTRUCTION may remain UNKNOWN")
        if unknown == "DYNAMIC_Q1_EXPANSION" and audit["small_q1_expansion_pass"] is not True:
            errors.append("semantic:dynamic Q1 UNKNOWN still requires small-scale Q1 expansion PASS")
        if unknown == "EARLY_TAIJI_GENERATION":
            structure = payload["base_v2_assessment"]["structure"]
            generation = structure["taiji"]["generation"]
            if structure["stage_location"] != "EARLY" or audit["same_direction_attack_number"] is None:
                errors.append("semantic:early Taiji UNKNOWN still requires EARLY location and attack count")
            elif audit["same_direction_attack_number"] > 2:
                errors.append("semantic:early Taiji UNKNOWN allows same-direction attack number <= 2")
            if generation in {"COPY_LEG_5", "LATER_GENERATION"}:
                errors.append("semantic:late Taiji generation cannot use the fresh-Q1 near-pass route")

    if route == "BEAR_REVERSAL_PROBE":
        if audit["left_right_phase"] not in rules["layers"][route]["allowed_phases"]:
            errors.append("semantic:bear reversal probe phase must be LR/RL/RR/DIRECT_TO_RIGHT")
        if len(audit["late_stage_partial_evidence"]) < 1:
            errors.append("semantic:bear reversal probe needs at least one partial late-stage clue")

    return errors


def validate() -> dict[str, Any]:
    rules = _read(RULES_PATH)
    doc = DOC_PATH.read_text(encoding="utf-8")
    schema = _read(SCHEMA_PATH)
    v2_schema = _read(V2_SCHEMA_PATH)
    checks: list[dict[str, Any]] = []

    def add(check_id: str, passed: bool, detail: Any) -> None:
        checks.append({"id": check_id, "passed": bool(passed), "detail": detail})

    add("rule_version", rules.get("schema_version") == "enlightenment-ai-rules-v3", rules.get("schema_version"))
    lineage = rules.get("version_lineage", {})
    add(
        "direct_parent_is_judgement_v2",
        lineage.get("direct_parent") == "enlightenment-ai-judgement-v2",
        lineage.get("direct_parent"),
    )
    add(
        "v1_v2_read_only",
        lineage.get("v1_policy", "").startswith("READ_ONLY")
        and lineage.get("v2_policy", "").startswith("READ_ONLY"),
        {"v1": lineage.get("v1_policy"), "v2": lineage.get("v2_policy")},
    )
    add(
        "v1_is_benchmark_not_eligibility_input",
        lineage.get("v1_trigger_is_eligibility_input") is False
        and "BENCHMARK_ONLY" in lineage.get("v1_role", ""),
        {"role": lineage.get("v1_role"), "input": lineage.get("v1_trigger_is_eligibility_input")},
    )
    add(
        "source_of_truth",
        (ROOT / rules["source_of_truth"]).resolve() == DOC_PATH.resolve(),
        rules.get("source_of_truth"),
    )
    add(
        "judgement_schema_path",
        (ROOT / rules["judgement_schema"]).resolve() == SCHEMA_PATH.resolve(),
        rules.get("judgement_schema"),
    )

    schema_error = None
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except Exception as exc:  # pragma: no cover - emitted for the audit report
        schema_error = str(exc)
    add("judgement_schema_valid", schema_error is None, schema_error or schema.get("$id"))
    add(
        "v3_schema_wraps_exact_v2_contract",
        schema.get("properties", {}).get("base_v2_assessment", {}).get("$ref") == v2_schema.get("$id"),
        {
            "v3_ref": schema.get("properties", {}).get("base_v2_assessment", {}).get("$ref"),
            "v2_id": v2_schema.get("$id"),
        },
    )

    protected_rows = []
    for relative, expected in lineage.get("protected_files", {}).items():
        path = ROOT / relative
        actual = _sha256(path) if path.is_file() else None
        protected_rows.append(
            {"path": relative, "expected": expected, "actual": actual, "matched": actual == expected}
        )
    add(
        "v1_v2_protected_files_unchanged",
        len(protected_rows) == 7 and all(row["matched"] for row in protected_rows),
        protected_rows,
    )

    add("precedence_is_frozen", rules.get("precedence") == EXPECTED_PRECEDENCE, rules.get("precedence"))
    add(
        "one_decision_per_stock_day_direction",
        rules.get("same_stock_day_direction_max_decisions") == 1,
        rules.get("same_stock_day_direction_max_decisions"),
    )
    add(
        "ten_common_probe_guards_exact",
        rules.get("common_probe_hard_guards") == EXPECTED_COMMON_GUARDS,
        rules.get("common_probe_hard_guards"),
    )

    layers = rules.get("layers", {})
    add(
        "four_v3_routes_present",
        set(layers) == {"V2_CORE", "NEAR_PASS_MACRO_COPY", "NEAR_PASS_FRESH_Q1", "BEAR_REVERSAL_PROBE"},
        sorted(layers),
    )
    core = layers.get("V2_CORE", {})
    add(
        "core_is_strict_v2_identity",
        core.get("allowed_unknown_count") == 0
        and core.get("allowed_fail_count") == 0
        and core.get("v3_may_rewrite_v2_gate") is False
        and "V2 decision.status=TRIGGERED" in core.get("base_v2_requirement", ""),
        core,
    )

    macro = layers.get("NEAR_PASS_MACRO_COPY", {})
    fresh = layers.get("NEAR_PASS_FRESH_Q1", {})
    bear = layers.get("BEAR_REVERSAL_PROBE", {})
    add(
        "macro_near_pass_is_single_unknown_zero_fail",
        macro.get("base_v2_primary_scenario") == "MACRO_COPY_RESONANCE"
        and len(macro.get("hard_pass_gates", [])) == 5
        and set(macro.get("soft_gates", []))
        == {"COMPLETED_PARENT_ANCHOR", "TAIJI_GENERATION_MAPPED", "DUAL_SCALE_LONG_ALIGNMENT"}
        and macro.get("required_unknown_count") == 1
        and macro.get("allowed_fail_count") == 0,
        macro,
    )
    add(
        "fresh_near_pass_is_single_unknown_zero_fail_with_residual_audit",
        fresh.get("base_v2_primary_scenario") == "FRESH_Q1_EXPANSION"
        and len(fresh.get("hard_pass_gates", [])) == 4
        and set(fresh.get("soft_gates", []))
        == {"CLEAN_MEATY_DESTRUCTIVE_TRACEABLE", "DYNAMIC_Q1_EXPANSION", "EARLY_TAIJI_GENERATION"}
        and fresh.get("required_unknown_count") == 1
        and fresh.get("allowed_fail_count") == 0
        and set(fresh.get("residual_requirements", {})) == set(fresh.get("soft_gates", [])),
        fresh,
    )
    add(
        "bear_probe_is_late_stage_single_unknown_and_not_ll",
        bear.get("base_v2_primary_scenario") == "BEAR_REVERSAL_LEFT_RIGHT"
        and len(bear.get("hard_pass_gates", [])) == 5
        and bear.get("soft_gates") == ["BEAR_LATE_STAGE_EVIDENCE"]
        and bear.get("required_unknown_count") == 1
        and bear.get("allowed_fail_count") == 0
        and bear.get("minimum_partial_late_stage_clues") == 1
        and bear.get("forbidden_phases") == ["LL"]
        and bear.get("allowed_phases") == ["LR", "RL", "RR", "DIRECT_TO_RIGHT"],
        bear,
    )
    add(
        "every_probe_is_probe_mother",
        all(layers[name].get("entry_role") == "PROBE_MOTHER" for name in layers if name != "V2_CORE"),
        {name: row.get("entry_role") for name, row in layers.items()},
    )

    execution = rules.get("execution_contract", {})
    add(
        "causal_execution_and_no_loser_add",
        execution.get("fill_rule") == "NEXT_TRADING_DAY_OPEN"
        and execution.get("same_day_deduplication") is True
        and execution.get("promotion_is_automatic_purchase") is False
        and execution.get("losing_position_add_allowed") is False
        and execution.get("reentry_requires_new_episode") is True,
        execution,
    )
    exit_contract = rules.get("exit_contract", {})
    add(
        "entry_experiment_keeps_v2_exit",
        exit_contract.get("must_equal_v2_in_entry_quality_experiment") is True
        and exit_contract.get("separate_exit_experiment_required") is True,
        exit_contract,
    )

    protocol = rules.get("research_protocol", {})
    add(
        "research_matrix_separates_entry_and_position",
        protocol.get("entry_quality_variants")
        == [
            "V1_MOTHER_ONLY_10K",
            "V2_MOTHER_ONLY_10K",
            "V3_CORE_ONLY",
            "V3_CORE_PLUS_NEAR_PASS_EQUAL_UNIT",
            "V3_ALL_LAYERS_EQUAL_UNIT",
        ]
        and protocol.get("position_variant") == "V3_ALL_LAYERS_TIERED_POSITION"
        and protocol.get("equal_unit_nominal_twd") == 10000
        and protocol.get("tiered_position_ratios")
        == {"V2_CORE": 1.0, "NEAR_PASS": 0.5, "BEAR_REVERSAL_PROBE": 0.25},
        protocol,
    )
    add(
        "research_requires_ablation_three_windows_and_shadow",
        protocol.get("ablation_routes")
        == ["NEAR_PASS_MACRO_COPY_ONLY", "NEAR_PASS_FRESH_Q1_ONLY", "BEAR_REVERSAL_PROBE_ONLY"]
        and protocol.get("minimum_disjoint_historical_windows", 0) >= 3
        and protocol.get("forward_shadow_monitor_required") is True
        and protocol.get("signals_frozen_before_performance_unlock") is True
        and protocol.get("v3_core_must_equal_v2_trade_by_trade") is True,
        protocol,
    )

    anti = rules.get("anti_outcome_leakage", {})
    semantic_rules = {
        "precedence": rules.get("precedence"),
        "layers": layers,
        "blocking": rules.get("probe_blocking_conditions"),
        "execution": execution,
        "exit": exit_contract,
    }
    searchable = doc + json.dumps(semantic_rules, ensure_ascii=False)
    leaked_tokens = [token for token in KNOWN_OUTCOME_TOKENS if token in searchable]
    add(
        "anti_outcome_leakage_contract",
        anti.get("known_winner_or_loser_identifiers_allowed") is False
        and anti.get("future_mfe_pnl_or_rank_visible_to_ai") is False
        and anti.get("decision_ledger_sha256_before_outcome_unlock") is True
        and anti.get("silent_rule_rewrite_allowed") is False
        and not leaked_tokens,
        {"policy": anti, "leaked_tokens": leaked_tokens},
    )

    state_values = rules.get("decision_states", {})
    schema_states = set(
        schema.get("$defs", {}).get("decision", {}).get("properties", {}).get("status", {}).get("enum", [])
    )
    add(
        "all_states_have_chinese_and_schema_enum",
        set(state_values) == schema_states
        and all(value and all(ord(char) > 127 for char in value if "\u4e00" <= char <= "\u9fff") for value in state_values.values()),
        state_values,
    )

    required_sections = [
        "## 1. 版本定位",
        "## 5. 所有試單共同硬條件",
        "## 6. `NEAR_PASS_MACRO_COPY（大定錨複製近合格試單）`",
        "## 7. `NEAR_PASS_FRESH_Q1（新生定錨近合格試單）`",
        "## 8. `BEAR_REVERSAL_PROBE（空頭末端左右反轉試單）`",
        "## 11. 試單升級、同日去重與加碼",
        "## 14. 回測矩陣與歸因",
        "## 15. 防止結果回填",
        "## 17. 採用門檻",
    ]
    add("human_document_is_self_contained", all(section in doc for section in required_sections), required_sections)
    add(
        "document_states_show_english_with_chinese",
        all(f"`{state}（{label}）`" in doc for state, label in state_values.items()),
        state_values,
    )

    return {
        "schema_version": "enlightenment-ai-rules-v3-validation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rules_path": str(RULES_PATH.resolve()),
        "rules_sha256": _sha256(RULES_PATH),
        "document_path": str(DOC_PATH.resolve()),
        "document_sha256": _sha256(DOC_PATH),
        "judgement_schema_path": str(SCHEMA_PATH.resolve()),
        "judgement_schema_sha256": _sha256(SCHEMA_PATH),
        "passed": all(check["passed"] for check in checks),
        "check_count": len(checks),
        "checks": checks,
    }


def write_outputs(payload: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "validation.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# 啟蒙層 AI 綜合判讀規格 V3 自我檢查",
        "",
        f"- 整體結果：`{'PASS（通過）' if payload['passed'] else 'FAIL（未通過）'}`",
        f"- 檢查數：{payload['check_count']}",
        f"- 規則 SHA-256：`{payload['rules_sha256']}`",
        f"- 文件 SHA-256：`{payload['document_sha256']}`",
        f"- Schema SHA-256：`{payload['judgement_schema_sha256']}`",
        "",
        "| 檢查 | 結果 |",
        "|---|---|",
    ]
    for check in payload["checks"]:
        lines.append(f"| `{check['id']}` | {'PASS' if check['passed'] else 'FAIL'} |")
    lines += [
        "",
        "## 邊界結論",
        "",
        "- V3 是獨立新增版本，直接父版本為判讀規格 V2。",
        "- V1、V2 七個受保護檔案逐一以 SHA-256 核對；任一變動即驗證失敗。",
        "- V3 core 必須與 V2 逐筆相同；V1 只作績效對照，不是准入條件。",
        "- 近合格與空頭反轉試單最多只容許一個指定 soft gate 為 UNKNOWN，任何 FAIL 都不能交易。",
        "- 第一階段固定相同母單與 V2 出場，先隔離進場品質；分層部位另列第二階段。",
        "",
        "本檔只驗證規則完整性、版本保護與因果契約；不代表 V3 已證明獲利。",
    ]
    (OUTPUT_DIR / "rule_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    payload = validate()
    write_outputs(payload)
    failed = [check["id"] for check in payload["checks"] if not check["passed"]]
    print(
        json.dumps(
            {"passed": payload["passed"], "check_count": payload["check_count"], "failed": failed},
            ensure_ascii=False,
        )
    )
    if not payload["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
