"""Audit completed M2A case-rounds without opening labels or future outcomes.

This report is diagnostic only.  It revalidates every immutable artifact set,
then explains how the frozen candidate truth table converted the AI atomic
answers into the program permission.  It deliberately does not calculate
triplicate consistency or trading performance.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_codex_staged_runner_v1 import (
    OUT,
    StagedRunnerError,
    STAGE_B_PROMPT_FILENAME,
    STAGE_D_PROMPT_FILENAME,
    load_json,
    validate_existing_stage_b_artifacts,
    validate_existing_stage_d_artifacts,
    verify_execution_manifest,
)
from scripts.v2_core_source_audit_v1 import sha256
from scripts.v2_core_codex_stage_d_runner_v2 import (
    STAGE_D_PROMPT_FILENAME as STAGE_D_PROMPT_FILENAME_V7,
    validate_existing_stage_d_artifacts as validate_existing_stage_d_artifacts_v7,
)
from scripts.v2_core_codex_stage_d_runner_v3 import (
    validate_existing_stage_d_artifacts as validate_existing_stage_d_artifacts_v8,
)


AUDITOR_VERSION = "v2-core-partial-case-audit-r4"
STATUS = "PARTIAL_DIAGNOSTIC_ONLY（部分診斷、非正式一致性結果）"
STOP_GATE_BY_SCENARIO = {
    "MATURE_TREND_PULLBACK": "EPISODE_STOP_CAUSAL",
    "MACRO_COPY_RESONANCE": "EPISODE_STOP_CAUSAL",
    "BEAR_REVERSAL_LEFT_RIGHT": "PHASE_STOP_CAUSAL",
    "FRESH_Q1_EXPANSION": "FRESH_ANCHOR_STOP_CAUSAL",
}
VERDICTS = ("PASS", "FAIL", "UNKNOWN")
PERMISSIONS = ("TRADE_APPROVED", "WAIT", "REMOVE", "UNKNOWN", "INVALID")
STATUS_ZH = {
    "TRADE_APPROVED": "核准交易",
    "WAIT": "等待",
    "REMOVE": "移除監控",
    "UNKNOWN": "證據不足、不交易",
    "INVALID": "資料或契約無效",
    "PASS": "合格",
    "FAIL": "不合格",
    "TRIGGERED": "已觸發",
    "ARMED": "已建立進場計畫、尚未觸發",
    "NOT_TRIGGERED": "尚未觸發",
    "UNRESOLVED": "未解決",
}


def _verdict(value: Any) -> str:
    if isinstance(value, dict) and value.get("result") in VERDICTS:
        return str(value["result"])
    return "MISSING"


def _count_verdicts(values: list[str]) -> dict[str, int]:
    counts = Counter(values)
    return {name: counts.get(name, 0) for name in (*VERDICTS, "MISSING")}


def _non_pass_atoms(atoms: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, value in sorted(atoms.items()):
        result = _verdict(value)
        if result == "PASS":
            continue
        rows.append(
            {
                "atom": name,
                "result": result,
                "reason_code": value.get("reason_code") if isinstance(value, dict) else None,
                "missing_evidence_codes": sorted(
                    str(item)
                    for item in ((value or {}).get("missing_evidence_codes") or [])
                )
                if isinstance(value, dict)
                else [],
            }
        )
    return rows


def _stop_detail(stage_b: dict[str, Any], stop_id: str | None) -> dict[str, Any] | None:
    if stop_id is None:
        return None
    matches = [row for row in stage_b["stops"] if row["stop_id"] == stop_id]
    if len(matches) != 1:
        return {"stop_id": stop_id, "status": "UNRESOLVED（未解決）"}
    stop = matches[0]
    return {
        "stop_id": stop_id,
        "scope": stop["scope"],
        "scale": stop["scale"],
        "direction": stop["direction"],
        "source_date": stop["source_date"],
        "confirmed_on": stop["confirmed_on"],
        "price": stop["price"],
    }


def _recommendation_relation(ai_permission: str | None, program_permission: str) -> str:
    if ai_permission == program_permission:
        return "MATCH（相同）"
    if ai_permission == "TRADE_APPROVED" and program_permission != "TRADE_APPROVED":
        return "AI_MORE_PERMISSIVE（AI建議較寬鬆）"
    if program_permission == "TRADE_APPROVED" and ai_permission != "TRADE_APPROVED":
        return "PROGRAM_MORE_PERMISSIVE（程式結果較寬鬆）"
    return "DIFFERENT_NON_TRADE_CLASS（不交易分類不同）"


def _permission_gap_reason(ai_permission: str | None, program_permission: str) -> str:
    if ai_permission == program_permission:
        return "EXACT_MATCH（完全相同）"
    if ai_permission == "REMOVE" and program_permission in {"WAIT", "UNKNOWN"}:
        return "AI_REMOVE_WITHOUT_PROGRAM_REMOVE_PRECONDITION（AI建議移除但程式移除前提未成立）"
    if ai_permission == "WAIT" and program_permission == "UNKNOWN":
        return "PROGRAM_UNKNOWN_PRECEDENCE（程式依必要UNKNOWN優先）"
    if ai_permission == "TRADE_APPROVED" and program_permission != "TRADE_APPROVED":
        return "AI_TRADE_BLOCKED_BY_PROGRAM（AI建議交易但程式未核准）"
    if program_permission == "TRADE_APPROVED" and ai_permission != "TRADE_APPROVED":
        return "PROGRAM_TRADE_NOT_RECOMMENDED_BY_AI（程式核准但AI未建議交易）"
    return "OTHER_PERMISSION_CLASS_GAP（其他權限分類差異）"


def collect_case(
    artifact_dir: Path,
    manifest: dict[str, Any],
    case: dict[str, Any],
    round_number: int,
) -> dict[str, Any]:
    review_id = str(case["review_id"])
    probe = load_json(artifact_dir / "feasibility_probe_manifest.json")
    packet_path = artifact_dir / str(probe["packet_directory"]) / str(case["packet_file"])
    run_root = artifact_dir / str(manifest["run_directory"]) / f"round_{round_number}"
    stage_b_path = run_root / "stage_b" / f"{review_id}.json"
    stage_d_path = run_root / "stage_d" / f"{review_id}.json"
    route_path = run_root / "program_route" / f"{review_id}.json"
    permission_path = run_root / "final_permission" / f"{review_id}.json"
    stage_b_receipt = run_root / "receipts" / f"{review_id}.stage_b.json"
    stage_d_receipt = run_root / "receipts" / f"{review_id}.stage_d.json"

    validate_existing_stage_b_artifacts(
        packet_path=packet_path,
        schema_path=artifact_dir / "v2_core_stage_b.schema.candidate.json",
        prompt_path=artifact_dir / STAGE_B_PROMPT_FILENAME,
        truth_table_path=artifact_dir / "permission_truth_table.json",
        output_path=stage_b_path,
        receipt_path=stage_b_receipt,
    )
    execution_version = str(manifest.get("execution_manifest_version", ""))
    if execution_version.startswith("v2-core-m2a-triplicate-execution-r8"):
        stage_d_validator = validate_existing_stage_d_artifacts_v8
        stage_d_prompt_filename = STAGE_D_PROMPT_FILENAME_V7
    elif execution_version.startswith("v2-core-m2a-triplicate-execution-r7"):
        stage_d_validator = validate_existing_stage_d_artifacts_v7
        stage_d_prompt_filename = STAGE_D_PROMPT_FILENAME_V7
    else:
        stage_d_validator = validate_existing_stage_d_artifacts
        stage_d_prompt_filename = STAGE_D_PROMPT_FILENAME
    validation = stage_d_validator(
        packet_path=packet_path,
        stage_b_path=stage_b_path,
        stage_b_schema_path=artifact_dir / "v2_core_stage_b.schema.candidate.json",
        stage_b_prompt_path=artifact_dir / STAGE_B_PROMPT_FILENAME,
        schema_path=artifact_dir / "v2_core_stage_d.schema.candidate.json",
        prompt_path=artifact_dir / stage_d_prompt_filename,
        truth_table_path=artifact_dir / "permission_truth_table.json",
        output_path=stage_d_path,
        route_path=route_path,
        permission_path=permission_path,
        receipt_path=stage_d_receipt,
    )

    stage_b = load_json(stage_b_path)
    route = load_json(route_path)
    permission = load_json(permission_path)
    program_scenario = str(validation["program_derived_scenario"])
    program_permission = str(validation["program_derived_permission"])
    if not stage_d_path.is_file():
        return {
            "review_id": review_id,
            "as_of": str(case["as_of"]),
            "artifact_validation": "PASS（通過）",
            "program_scenario": program_scenario,
            "ai_recommended_scenario": route.get("ai_recommended_scenario"),
            "scenario_recommendation_matches": route.get("recommendation_matches_program"),
            "routing_status": route["routing_status"],
            "program_permission": program_permission,
            "ai_recommended_permission": None,
            "permission_recommendation_matches": None,
            "trade_action_matches": None,
            "recommendation_relation": "NOT_APPLICABLE（不適用）",
            "permission_gap_reason": "STAGE_D_NOT_CALLED（未呼叫情境檢核）",
            "permission_reasons": list(permission["permission_reasons"]),
            "scenario_overall": None,
            "trigger": None,
            "episode_stop": None,
            "required_gate_counts": _count_verdicts([]),
            "non_pass_required_gates": [],
            "blocking_atom_counts": _count_verdicts([]),
            "non_pass_blocking_atoms": [],
            "common_atoms": {},
            "ai_blocking_reasons": [],
        }

    stage_d = load_json(stage_d_path)
    gates = stage_d["scenario_evaluation"]["gates"]
    blockers = stage_d["blocking_atoms"]
    trigger = stage_d["trigger"]
    common_atoms = {
        "DATA_SUFFICIENCY": _verdict(stage_b["data_sufficiency"]),
        "LOCATION_REMAINING_SPACE": _verdict(stage_d["location_remaining_space"]),
        "SIGNAL_HAS_INDEPENDENT_STRUCTURE": _verdict(
            stage_d["signal_has_independent_structure"]
        ),
    }
    stop_gate = STOP_GATE_BY_SCENARIO[program_scenario]
    common_atoms["EPISODE_OR_PHASE_STOP_CAUSAL"] = _verdict(gates[stop_gate])
    ai_permission = str(stage_d["ai_recommended_permission"])
    trade_action_matches = (ai_permission == "TRADE_APPROVED") == (
        program_permission == "TRADE_APPROVED"
    )
    return {
        "review_id": review_id,
        "as_of": str(case["as_of"]),
        "artifact_validation": "PASS（通過）",
        "program_scenario": program_scenario,
        "ai_recommended_scenario": route.get("ai_recommended_scenario"),
        "scenario_recommendation_matches": bool(route["recommendation_matches_program"]),
        "routing_status": route["routing_status"],
        "program_permission": program_permission,
        "ai_recommended_permission": ai_permission,
        "permission_recommendation_matches": bool(
            permission["recommendation_matches_program"]
        ),
        "trade_action_matches": trade_action_matches,
        "recommendation_relation": _recommendation_relation(
            ai_permission, program_permission
        ),
        "permission_gap_reason": _permission_gap_reason(
            ai_permission, program_permission
        ),
        "permission_reasons": list(permission["permission_reasons"]),
        "scenario_overall": stage_d["scenario_evaluation"]["overall"],
        "trigger": {
            "status": trigger["status"],
            "canonical_route": trigger["canonical_route"],
            "trigger_date": trigger["trigger_date"],
            "episode_stop_id": trigger["episode_stop_id"],
            "campaign_stop_id": trigger["campaign_stop_id"],
        },
        "episode_stop": _stop_detail(stage_b, trigger["episode_stop_id"]),
        "required_gate_counts": _count_verdicts(
            [_verdict(value) for value in gates.values()]
        ),
        "non_pass_required_gates": _non_pass_atoms(gates),
        "blocking_atom_counts": _count_verdicts(
            [_verdict(value) for value in blockers.values()]
        ),
        "non_pass_blocking_atoms": _non_pass_atoms(blockers),
        "common_atoms": common_atoms,
        "ai_blocking_reasons": list(
            stage_d["scenario_evaluation"].get("blocking_reasons") or []
        ),
    }


def _frequency(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter((row["atom"], row["result"]) for row in rows)
    return [
        {"atom": atom, "result": result, "count": count}
        for (atom, result), count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0][0], item[0][1])
        )
    ]


def build_report(
    artifact_dir: Path = OUT,
    manifest_path: Path | None = None,
    round_number: int = 1,
    operational_stop_threshold_percent: float = 20.0,
) -> dict[str, Any]:
    artifact_dir = artifact_dir.resolve()
    manifest_path = (
        manifest_path or artifact_dir / "feasibility_probe_execution_manifest_v6.json"
    ).resolve()
    manifest = verify_execution_manifest(manifest_path, artifact_dir)
    run_root = artifact_dir / str(manifest["run_directory"]) / f"round_{round_number}"
    invalid_paths = sorted(
        [path for path in (run_root / "stage_b").glob("*.invalid.raw") if path.is_file()]
        + [path for path in (run_root / "stage_d").glob("*.invalid.raw") if path.is_file()]
    )
    completed_ids = {
        path.stem
        for path in (run_root / "final_permission").glob("*.json")
        if path.is_file()
    }
    cases: list[dict[str, Any]] = []
    validation_failures: list[str] = []
    for case in manifest["cases"]:
        if case["review_id"] not in completed_ids:
            continue
        try:
            cases.append(collect_case(artifact_dir, manifest, case, round_number))
        except StagedRunnerError:
            validation_failures.append(str(case["review_id"]))

    scenario_counts = Counter(row["program_scenario"] for row in cases)
    permission_counts = Counter(row["program_permission"] for row in cases)
    ai_permission_counts = Counter(
        row["ai_recommended_permission"]
        for row in cases
        if row["ai_recommended_permission"] is not None
    )
    scenario_permission: dict[str, Counter[str]] = defaultdict(Counter)
    for row in cases:
        scenario_permission[row["program_scenario"]][row["program_permission"]] += 1

    gate_rows = [item for row in cases for item in row["non_pass_required_gates"]]
    blocker_rows = [item for row in cases for item in row["non_pass_blocking_atoms"]]
    permission_reason_counts = Counter(
        reason for row in cases for reason in row["permission_reasons"]
    )
    trigger_counts = Counter(
        (row["trigger"] or {}).get("status", "STAGE_D_NOT_CALLED") for row in cases
    )
    relation_counts = Counter(row["recommendation_relation"] for row in cases)
    permission_gap_counts = Counter(row["permission_gap_reason"] for row in cases)
    common_atom_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in cases:
        for name, result in row["common_atoms"].items():
            common_atom_counts[name][result] += 1

    return {
        "audit_version": AUDITOR_VERSION,
        "status": STATUS,
        "scope": {
            "execution_manifest": manifest_path.name,
            "execution_manifest_sha256": sha256(manifest_path),
            "run_directory": str(manifest["run_directory"]),
            "round": round_number,
            "formal_model": manifest["formal_model"],
            "reasoning_effort": manifest["reasoning_effort"],
            "expected_cases_in_round": int(manifest["case_count"]),
            "published_permission_count": len(completed_ids),
            "completed_case_count": len(cases),
            "remaining_case_count_in_round": int(manifest["case_count"]) - len(cases),
        },
        "causal_scope": {
            "as_of_only": True,
            "sealed_labels_used": False,
            "stock_identity_used": False,
            "legacy_answers_used": False,
            "future_performance_used": False,
        },
        "artifact_validation": {
            "validated_complete_sets": len(cases),
            "failed_sets": len(invalid_paths) + len(validation_failures),
            "status": (
                "PASS（通過）"
                if not invalid_paths and not validation_failures
                else "PARTIAL_FAILURE_PRESERVED（已保留部分失敗）"
            ),
        },
        "aggregate": {
            "program_scenario_counts": dict(sorted(scenario_counts.items())),
            "program_permission_counts": {
                name: permission_counts.get(name, 0) for name in PERMISSIONS
            },
            "ai_recommended_permission_counts": dict(
                sorted(ai_permission_counts.items())
            ),
            "scenario_recommendation_match_count": sum(
                row["scenario_recommendation_matches"] is True for row in cases
            ),
            "permission_recommendation_match_count": sum(
                row["permission_recommendation_matches"] is True for row in cases
            ),
            "trade_action_match_count": sum(
                row.get("trade_action_matches") is True for row in cases
            ),
            "recommendation_relation_counts": dict(sorted(relation_counts.items())),
            "permission_gap_reason_counts": dict(sorted(permission_gap_counts.items())),
            "permission_by_scenario": {
                scenario: {
                    permission: counts.get(permission, 0) for permission in PERMISSIONS
                }
                for scenario, counts in sorted(scenario_permission.items())
            },
            "trigger_status_counts": dict(sorted(trigger_counts.items())),
            "permission_reason_counts": dict(sorted(permission_reason_counts.items())),
            "common_atom_verdict_counts": {
                atom: {name: counts.get(name, 0) for name in VERDICTS}
                for atom, counts in sorted(common_atom_counts.items())
            },
            "frequent_non_pass_required_gates": _frequency(gate_rows),
            "frequent_non_pass_blocking_atoms": _frequency(blocker_rows),
        },
        "cases": cases,
        "limitations": [
            "Only completed case-rounds are described; missing runs are not imputed.",
            "No triplicate consistency rate is calculated before all 144 case-rounds exist.",
            "No legacy-label accuracy or course-compliance conclusion is calculated here.",
            "No trading performance, MFE, MAE, return, or winner information is opened.",
            "The candidate rules, prompts, schemas, truth table, and AI outputs are not changed.",
        ],
        "next_safe_ai_resume": _next_safe_resume(
            manifest=manifest,
            run_root=run_root,
            invalid_paths=invalid_paths,
            validation_failures=validation_failures,
            operational_stop_threshold_percent=operational_stop_threshold_percent,
        ),
    }


def _next_safe_resume(
    *,
    manifest: dict[str, Any],
    run_root: Path,
    invalid_paths: list[Path],
    validation_failures: list[str],
    operational_stop_threshold_percent: float,
) -> str:
    if invalid_paths or validation_failures:
        return (
            "Do not resume this execution manifest. Preserve invalid raw output and "
            "create a versioned protocol correction before any new AI call."
        )
    for shard in manifest["shards"]:
        review_ids = [str(value) for value in shard["review_ids"]]
        if any(not (run_root / "stage_b" / f"{review_id}.json").is_file() for review_id in review_ids):
            return (
                f"Round {run_root.name.removeprefix('round_')} shard {shard['shard_id']} "
                "STAGE_B after primary Codex remaining usage is rechecked above "
                f"{operational_stop_threshold_percent:g}%."
            )
        if any(not (run_root / "final_permission" / f"{review_id}.json").is_file() for review_id in review_ids):
            return (
                f"Round {run_root.name.removeprefix('round_')} shard {shard['shard_id']} "
                "STAGE_D after primary Codex remaining usage is rechecked above "
                f"{operational_stop_threshold_percent:g}%."
            )
    return (
        "This round is complete; continue the next round only after usage is rechecked above "
        f"{operational_stop_threshold_percent:g}%."
    )


def _status(value: str | None) -> str:
    if value is None:
        return "—"
    return f"{value}（{STATUS_ZH[value]}）" if value in STATUS_ZH else value


def markdown(report: dict[str, Any]) -> str:
    scope = report["scope"]
    aggregate = report["aggregate"]
    lines = [
        f"# V2核心M2A {scope['execution_manifest']} 已完成案例確定性稽核",
        "",
        f"狀態：`{report['status']}`",
        "",
        "本報告只說明已完成案例的AI原子答案如何經程式真值表得到最終權限。"
        "它不是三輪一致性結果，也不是績效回測。",
        "",
        "## 範圍與合法性",
        "",
        f"- 正式設定：`{scope['formal_model']}／{scope['reasoning_effort']}`",
        f"- 本輪完成：{scope['completed_case_count']}／{scope['expected_cases_in_round']}案",
            f"- 完整產物組驗證：{report['artifact_validation']['validated_complete_sets']}案通過",
            f"- 已保留invalid raw：{report['artifact_validation']['failed_sets']}案",
        "- 股票身分、舊答案、sealed標籤及未來績效：全部未讀取",
        "- 144案例輪次完成前：不計算正式一致率",
        "",
        "## 權限與情境分布",
        "",
        "| 項目 | 數量 |",
        "| --- | ---: |",
    ]
    for permission in PERMISSIONS:
        lines.append(
            f"| 程式 `{_status(permission)}` | "
            f"{aggregate['program_permission_counts'][permission]} |"
        )
    lines.extend(["", "| 程式主要情境 | 數量 |", "| --- | ---: |"])
    for scenario, count in aggregate["program_scenario_counts"].items():
        lines.append(f"| `{scenario}` | {count} |")
    lines.extend(
        [
            "",
            f"- AI建議情境與程式路由相同："
            f"{aggregate['scenario_recommendation_match_count']}／{scope['completed_case_count']}案。",
            f"- AI建議權限與程式真值表相同："
            f"{aggregate['permission_recommendation_match_count']}／{scope['completed_case_count']}案。",
            f"- 若只比較立即動作『交易／不交易』："
            f"{aggregate['trade_action_match_count']}／{scope['completed_case_count']}案相同。",
            "- 目前所有五分類差異都發生在WAIT／REMOVE／UNKNOWN之間，"
            "沒有把AI建議交易改成不交易，也沒有由程式自行新增交易。",
            "- 兩者不同時，一律保留差異並採程式真值表結果，沒有挑選較有利答案。",
            "",
            "### AI建議與程式權限的分類差異",
            "",
            "| 差異類型 | 案數 |",
            "| --- | ---: |",
        ]
    )
    for reason, count in aggregate["permission_gap_reason_counts"].items():
        lines.append(f"| `{reason}` | {count} |")
    lines.extend(
        [
            "",
            "AI建議REMOVE不等於campaign已失效；程式只有在監控失效或campaign失效的"
            "客觀前提成立時才允許REMOVE。必要原子含UNKNOWN時，程式則依凍結優先序保留為UNKNOWN。",
            "",
            "## 程式未核准交易的直接原因",
            "",
            "| 真值表原因 | 案數 |",
            "| --- | ---: |",
        ]
    )
    for reason, count in aggregate["permission_reason_counts"].items():
        lines.append(f"| `{reason}` | {count} |")
    lines.extend(
        [
            "",
            "### 最常出現的非PASS必要gate",
            "",
            "| 原子 | 結果 | 案次 |",
            "| --- | --- | ---: |",
        ]
    )
    for row in aggregate["frequent_non_pass_required_gates"]:
        lines.append(f"| `{row['atom']}` | `{_status(row['result'])}` | {row['count']} |")
    lines.extend(
        [
            "",
            "### 共通交易原子",
            "",
            "| 原子 | PASS | FAIL | UNKNOWN |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for atom, counts in aggregate["common_atom_verdict_counts"].items():
        lines.append(
            f"| `{atom}` | {counts['PASS']} | {counts['FAIL']} | {counts['UNKNOWN']} |"
        )
    lines.extend(
        [
            "",
            "## 逐案說明",
            "",
            "| 案例 | AS-OF | 程式情境 | 觸發 | AI建議權限 | 程式權限 | 真值表原因 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in report["cases"]:
        trigger_status = (row["trigger"] or {}).get("status")
        reasons = "、".join(f"`{item}`" for item in row["permission_reasons"])
        lines.append(
            f"| `{row['review_id']}` | {row['as_of']} | `{row['program_scenario']}` | "
            f"`{_status(trigger_status)}` | `{_status(row['ai_recommended_permission'])}` | "
            f"`{_status(row['program_permission'])}` | {reasons} |"
        )
    for row in report["cases"]:
        lines.extend(["", f"### {row['review_id']}", ""])
        trigger = row["trigger"] or {}
        lines.extend(
            [
                f"- AS-OF：{row['as_of']}",
                f"- 情境：`{row['program_scenario']}`；AI建議情境："
                f"`{row['ai_recommended_scenario']}`",
                f"- 權限：AI建議 `{_status(row['ai_recommended_permission'])}`；"
                f"程式 `{_status(row['program_permission'])}`；"
                f"關係 `{row['recommendation_relation']}`",
                f"- 觸發：`{_status(trigger.get('status'))}`／"
                f"`{trigger.get('canonical_route', '—')}`／{trigger.get('trigger_date') or '—'}",
            ]
        )
        stop = row["episode_stop"]
        if stop:
            lines.append(
                f"- episode防線：`{stop['stop_id']}`；來源日 {stop.get('source_date', '—')}；"
                f"確認日 {stop.get('confirmed_on', '—')}；價格 {stop.get('price', '—')}"
            )
        else:
            lines.append("- episode防線：未提供可用因果防線")
        non_pass = row["non_pass_required_gates"] + row["non_pass_blocking_atoms"]
        if non_pass:
            lines.extend(
                [
                    "- 非PASS原子：",
                    "",
                    "  | 原子 | 結果 | 原因碼／缺少證據 |",
                    "  | --- | --- | --- |",
                ]
            )
            for atom in non_pass:
                detail = atom["reason_code"] or "—"
                if atom["missing_evidence_codes"]:
                    detail += "／" + ", ".join(atom["missing_evidence_codes"])
                lines.append(
                    f"  | `{atom['atom']}` | `{_status(atom['result'])}` | `{detail}` |"
                )
        if row["ai_blocking_reasons"]:
            lines.append("- AI原始阻擋說明：")
            lines.append("")
            for reason in row["ai_blocking_reasons"]:
                lines.append(f"  - {reason}")
    lines.extend(
        [
            "",
            "## 解讀限制與下一步",
            "",
            "- 目前只能確認機制有依凍結真值表約束AI建議，不能據此判定策略勝率或正期望。",
            "- UNKNOWN具有比WAIT更高的權限優先序；同案即使另有FAIL，只要必要原子含UNKNOWN，"
            "最終先歸為UNKNOWN，不代表其他條件已合格。",
            f"- 不得依這{scope['completed_case_count']}案修改候選規則；須待同一有效版本的三輪144案例輪次完成後才計算正式一致率。",
            f"- 安全續跑：{report['next_safe_ai_resume']}",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=OUT)
    parser.add_argument("--execution-manifest", type=Path)
    parser.add_argument("--round", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--operational-stop-threshold", type=float, default=20.0)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args()
    artifact_dir = args.artifact_dir.resolve()
    manifest_path = args.execution_manifest or (
        artifact_dir / "feasibility_probe_execution_manifest_v6.json"
    )
    report = build_report(
        artifact_dir,
        manifest_path,
        args.round,
        args.operational_stop_threshold,
    )
    generation = "v8" if "_v8" in manifest_path.name else "v7" if "_v7" in manifest_path.name else "v6"
    json_output = args.json_output or artifact_dir / f"m2a_partial_case_audit_{generation}.json"
    markdown_output = (
        args.markdown_output or artifact_dir / f"m2a_partial_case_audit_{generation}.md"
    )
    json_output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_output.write_text(markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "completed_case_count": report["scope"]["completed_case_count"],
                "json_output": str(json_output.resolve()),
                "markdown_output": str(markdown_output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
