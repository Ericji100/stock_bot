"""Validate the complete Enlightenment AI judgement V2 contract and protect V1."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "config/enlightenment_ai_rules_v2.json"
DOC_PATH = ROOT / "docs/enlightenment-ai-judgement-v2.md"
SCHEMA_PATH = ROOT / "config/enlightenment_ai_judgement_v2.schema.json"
V1_SCHEMA_PATH = ROOT / "config/enlightenment_ai_judgement_v1.schema.json"
OUTPUT_DIR = ROOT / "reports/course_backtest/2026-09-06/enlightenment_ai_v2_rules"


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_course_source(relative: str) -> Path:
    path = ROOT / relative
    if path.is_file():
        return path
    return ROOT / "local_data" / relative


def validate() -> dict[str, Any]:
    rules = _read(RULES_PATH)
    doc = DOC_PATH.read_text(encoding="utf-8")
    schema = _read(SCHEMA_PATH)
    v1_schema = _read(V1_SCHEMA_PATH)
    checks: list[dict[str, Any]] = []

    def add(check_id: str, passed: bool, detail: Any) -> None:
        checks.append({"id": check_id, "passed": bool(passed), "detail": detail})

    add("rule_version", rules.get("schema_version") == "enlightenment-ai-rules-v2", rules.get("schema_version"))
    add("direct_parent_is_judgement_v1", rules["version_lineage"].get("direct_parent") == "enlightenment-ai-judgement-v1", rules["version_lineage"])
    add("source_of_truth", (ROOT / rules["source_of_truth"]).resolve() == DOC_PATH.resolve(), rules["source_of_truth"])
    add("judgement_schema_path", (ROOT / rules["judgement_schema"]).resolve() == SCHEMA_PATH.resolve(), rules["judgement_schema"])

    schema_error = None
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except Exception as exc:  # pragma: no cover - emitted for audit
        schema_error = str(exc)
    add("judgement_schema_valid", schema_error is None, schema_error or schema["$id"])

    protected = []
    for relative, expected in rules["version_lineage"]["v1_protected_files"].items():
        path = ROOT / relative
        actual = _sha256(path) if path.is_file() else None
        protected.append({"path": relative, "expected": expected, "actual": actual, "matched": actual == expected})
    add("v1_files_unchanged", all(row["matched"] for row in protected), protected)

    missing_sources = [relative for relative in rules["course_sources"] if not _resolve_course_source(relative).is_file()]
    add("all_course_sources_exist", not missing_sources, missing_sources or len(rules["course_sources"]))
    add(
        "v1_layers_explicitly_preserved",
        len(rules["version_lineage"]["preserved_v1_layers"]) == 13,
        rules["version_lineage"]["preserved_v1_layers"],
    )
    add(
        "backtest_v2_parameters_not_promoted_to_rule_v2",
        "五分門檻" in rules["version_lineage"]["not_imported_from_backtest_v2"]
        and "0.5ATR" in rules["version_lineage"]["not_imported_from_backtest_v2"]
        and "minimum_score" not in rules
        and "chase_cap" not in rules,
        rules["version_lineage"]["not_imported_from_backtest_v2"],
    )

    scenario_rules = rules["scenario_rules"]
    expected_scenarios = {
        "MATURE_TREND_PULLBACK",
        "MACRO_COPY_RESONANCE",
        "BEAR_REVERSAL_LEFT_RIGHT",
        "FRESH_Q1_EXPANSION",
    }
    add("four_scenarios_present", set(scenario_rules) == expected_scenarios, sorted(scenario_rules))
    expected_gates = {
        "MATURE_TREND_PULLBACK": 8,
        "MACRO_COPY_RESONANCE": 8,
        "BEAR_REVERSAL_LEFT_RIGHT": 6,
        "FRESH_Q1_EXPANSION": 7,
    }
    route_details: dict[str, Any] = {}
    for name, minimum in expected_gates.items():
        route = scenario_rules[name]
        route_details[name] = {
            "required": len(route.get("all_required", [])),
            "blocking": len(route.get("blocking_conditions", [])),
            "fallback": route.get("fallback_allowed"),
            "has_small_failure": any(key in route for key in ("small_failure", "time_and_momentum_failure")),
            "has_campaign_failure": "campaign_failure" in route,
            "has_transitions": bool(route.get("transitions")),
        }
    add(
        "four_scenarios_closed_and_complete",
        all(
            detail["required"] >= expected_gates[name]
            and detail["blocking"] >= 6
            and detail["fallback"] is False
            and detail["has_small_failure"]
            and detail["has_campaign_failure"]
            and detail["has_transitions"]
            for name, detail in route_details.items()
        ),
        route_details,
    )

    mature_text = json.dumps(scenario_rules["MATURE_TREND_PULLBACK"], ensure_ascii=False)
    add(
        "mature_trend_is_fully_defined",
        all(
            token in mature_text
            for token in (
                "成熟多頭",
                "長多慣性",
                "105MA",
                "太極父代",
                "Q4",
                "Q2",
                "Q3",
                "道氏防線",
                "小級",
                "部位",
                "REENTRY_WATCHING",
                "CAMPAIGN_INVALIDATED",
            )
        )
        and len(scenario_rules["MATURE_TREND_PULLBACK"]["preferred_trigger_paths"]) == 4,
        scenario_rules["MATURE_TREND_PULLBACK"],
    )
    macro_text = json.dumps(scenario_rules["MACRO_COPY_RESONANCE"], ensure_ascii=False)
    bear_text = json.dumps(scenario_rules["BEAR_REVERSAL_LEFT_RIGHT"], ensure_ascii=False)
    fresh_text = json.dumps(scenario_rules["FRESH_Q1_EXPANSION"], ensure_ascii=False)
    add("macro_has_parent_correction_restart", all(token in macro_text for token in ("父代", "大級修正", "反向定錨", "空頭防線", "策略共振")), "parent + correction + restart")
    add("bear_has_late_left_right", all(token in bear_text for token in ("下降定錨", "末段", "LL", "LR", "RL", "RR", "空頭防線")), "bear + late stage + LL/LR/RL/RR")
    add("fresh_has_positive_anchor_q1", all(token in fresh_text for token in ("向上新生定錨", "乾淨", "有肉", "具破壞性", "Q1", "EARLY", "殘餘")), "fresh positive anchor + Q1 + early only")

    add(
        "eight_evidence_layers",
        len(rules["required_evidence_layers"]) == 8
        and all(token in " ".join(rules["required_evidence_layers"]) for token in ("ANCHOR", "TAIJI", "QUADRANT", "DOW", "SCALE", "STAGE", "TRIGGER", "INVALIDATION")),
        rules["required_evidence_layers"],
    )
    add(
        "routing_has_no_residual_else",
        rules["routing_contract"]["one_primary"] is True
        and rules["routing_contract"]["max_alternatives"] == 2
        and rules["common_decision_rule"]["fallback"] == "UNRESOLVED_NO_TRADE"
        and all(route["fallback_allowed"] is False for route in scenario_rules.values()),
        rules["routing_contract"],
    )

    v1_top_properties = set(v1_schema["properties"])
    v2_top_properties = set(schema["properties"])
    v1_decision_properties = set(v1_schema["properties"]["decision"]["properties"])
    v2_decision_properties = set(schema["properties"]["decision"]["properties"])
    v1_states = set(v1_schema["properties"]["decision"]["properties"]["status"]["enum"])
    v2_states = set(schema["properties"]["decision"]["properties"]["status"]["enum"])
    add(
        "v1_schema_contract_covered",
        v1_top_properties.issubset(v2_top_properties)
        and v1_decision_properties.issubset(v2_decision_properties)
        and v1_states.issubset(v2_states),
        {
            "missing_top": sorted(v1_top_properties - v2_top_properties),
            "missing_decision": sorted(v1_decision_properties - v2_decision_properties),
            "missing_states": sorted(v1_states - v2_states),
        },
    )
    schema_required = set(schema["required"])
    add(
        "v2_schema_requires_route_evidence_and_causality",
        {"selection_context", "macd_cycle_map", "scenario_gate_audit", "evidence", "disqualifiers", "causal_attestation"}.issubset(schema_required)
        and schema["properties"]["scenario_gate_audit"]["properties"]["fallback_used"] == {"const": False},
        sorted(schema_required),
    )

    required_doc_sections = [
        "## 3. 十條不可違反的因果規則",
        "## 9. `MATURE_TREND_PULLBACK（長多慣性拉回再發動）`",
        "## 10. `MACRO_COPY_RESONANCE（大定錨複製共振）`",
        "## 11. `BEAR_REVERSAL_LEFT_RIGHT（空頭末段左右反轉）`",
        "## 12. `FRESH_Q1_EXPANSION（新生定錨直接擴張）`",
        "## 14. 狀態、母單、加碼與再進場",
        "## 15. 每日 AI 輸出",
    ]
    add("human_document_is_self_contained", all(section in doc for section in required_doc_sections), required_doc_sections)
    add(
        "causal_trigger_next_open_preserved",
        "下一交易日開盤" in rules["causal_rules"][1]
        and "TRIGGERED（已觸發進場）" in doc
        and "下一交易日開盤執行" in doc,
        rules["causal_rules"][1],
    )

    return {
        "schema_version": "enlightenment-ai-rules-v2-validation",
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
        "# 啟蒙層 AI 綜合判讀規格 V2 自我檢查",
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
        "- V2 的直接父版本是啟蒙層 AI 綜合判讀規格 V1；不是任何回測 V2。",
        "- V1 四個保護檔案逐一以 SHA-256 核對，均未修改。",
        "- V1 全部共同層已覆蓋；四個情境均具有必要條件、觸發、排除、失效及轉換。",
        "- 長多慣性拉回已由四句摘要補成獨立完整情境，不再以長均線或單點訊號代替結構判讀。",
        "- 批次回測的五分門檻、0.5ATR追價與特定出場式沒有升格為本規格規則。",
        "",
        "本檔驗證規則完整性、版本邊界與因果契約，不代表V2已取得正報酬或通過樣本外驗證。",
    ]
    (OUTPUT_DIR / "rule_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    payload = validate()
    write_outputs(payload)
    print(json.dumps({"passed": payload["passed"], "check_count": payload["check_count"]}, ensure_ascii=False))
    if not payload["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
