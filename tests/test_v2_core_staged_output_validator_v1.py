from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess

import pytest

from scripts.v2_core_source_audit_v1 import sha256
from scripts.v2_core_codex_staged_runner_v1 import (
    StagedRunnerError,
    invalid_output_path,
    raw_output_path,
    run_stage_b_case,
    run_stage_d_case,
    validate_existing_stage_b_artifacts,
    validate_existing_stage_d_artifacts,
)
from scripts.v2_core_staged_output_validator_v1 import (
    validate_stage_b,
    validate_stage_d,
)


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
MANIFEST = json.loads((OUT / "feasibility_probe_manifest.json").read_text(encoding="utf-8"))
PACKET_PATH = OUT / MANIFEST["packet_directory"] / MANIFEST["rows"][0]["packet_file"]
PACKET = json.loads(PACKET_PATH.read_text(encoding="utf-8"))
STAGE_B_SCHEMA = OUT / "v2_core_stage_b.schema.candidate.json"
STAGE_D_SCHEMA = OUT / "v2_core_stage_d.schema.candidate.json"
STAGE_B_PROMPT = OUT / "v2_core_stage_b.prompt.candidate_r2.md"
STAGE_D_PROMPT = OUT / "v2_core_stage_d.prompt.candidate_r2.md"
TRUTH = OUT / "permission_truth_table.json"
AS_OF_REF = f"PRICE:{PACKET['as_of']}"
FIRST_REF = f"PRICE:{PACKET['daily_structure_context_to_as_of'][0]['date']}"


def verdict(result: str = "PASS") -> dict:
    if result == "PASS":
        return {
            "result": "PASS",
            "supporting_evidence_refs": [AS_OF_REF],
            "contradicting_evidence_refs": [],
            "missing_evidence_codes": [],
            "reason_code": "VISIBLE_EVIDENCE_SATISFIES_DEFINITION",
        }
    if result == "FAIL":
        return {
            "result": "FAIL",
            "supporting_evidence_refs": [],
            "contradicting_evidence_refs": [AS_OF_REF],
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


def stage_b_record() -> dict:
    start = PACKET["daily_structure_context_to_as_of"][0]
    start_price = float(start.get("adj_close") or start["close"])
    stop_price = max(0.01, start_price * 0.9)
    anchor_atomic = {
        name: verdict("UNKNOWN" if name == "ANCHOR_COMPLETED" else "PASS")
        for name in (
            "ANCHOR_DIRECTION_COHERENT",
            "ANCHOR_CLEAN",
            "ANCHOR_MEATY",
            "ANCHOR_DESTRUCTIVE",
            "ANCHOR_TRACEABLE",
            "ANCHOR_COMPLETED",
            "ANCHOR_CONTROLS_CURRENT_CONTEXT",
        )
    }
    route_atoms = {
        "ACTIVE_LARGE_BEAR_ANCHOR": verdict("FAIL"),
        "FRESH_UP_ANCHOR_CONTROLS": verdict("FAIL"),
        "COMPLETED_PARENT_ANCHOR": verdict("PASS"),
        "FIRST_INDEPENDENT_REPLICATION": verdict("FAIL"),
        "LONG_CAMPAIGN_REPEATED_SUCCESS": verdict("PASS"),
        "LONG_MA_HABIT_SUPPORTED": verdict("PASS"),
        "CURRENT_CORRECTION_WITHIN_CAMPAIGN": verdict("PASS"),
    }
    scale = {
        "direction": "BULL",
        "quadrant": "Q4",
        "dow": "BULL",
        "trend_strength_change": "INCREASED",
        "volatility_change": "CONTRACTED",
        "controlling_stop_id": "STOP-C1",
        "supporting_evidence_refs": [AS_OF_REF],
    }
    return {
        "schema_version": "v2-core-stage-b-r1-candidate",
        "review_id": PACKET["review_id"],
        "anonymous_stock_id": PACKET["anonymous_stock_id"],
        "as_of": PACKET["as_of"],
        "packet_sha256": sha256(PACKET_PATH),
        "prompt_sha256": sha256(STAGE_B_PROMPT),
        "schema_sha256": sha256(STAGE_B_SCHEMA),
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "independent_review": False,
        "data_sufficiency": verdict(),
        "anchors": [
            {
                "anchor_id": "ANCHOR-A1",
                "role": "CONTROLLING",
                "scale": "LARGE",
                "direction": "UP",
                "start_date": start["date"],
                "start_price": start_price,
                "end_date": None,
                "end_price": None,
                "status": "FORMING",
                "invalidation_stop_id": "STOP-C1",
                "atomic": anchor_atomic,
            }
        ],
        "relations": [],
        "stops": [
            {
                "stop_id": "STOP-C1",
                "scope": "PARENT_CAMPAIGN",
                "scale": "LARGE",
                "direction": "BULL_DEFENSE",
                "price": stop_price,
                "source_date": start["date"],
                "confirmed_on": start["date"],
                "supporting_evidence_refs": [FIRST_REF],
            },
            {
                "stop_id": "STOP-E1",
                "scope": "TRADE_EPISODE",
                "scale": "SMALL",
                "direction": "BULL_DEFENSE",
                "price": stop_price,
                "source_date": start["date"],
                "confirmed_on": start["date"],
                "supporting_evidence_refs": [FIRST_REF],
            },
        ],
        "shared_structure": {
            "controlling_anchor_id": "ANCHOR-A1",
            "alternative_anchor_ids": [],
            "large_scale": scale,
            "small_scale": {**scale, "quadrant": "Q1", "controlling_stop_id": "STOP-E1"},
            "taiji_phase": "COPY_ATTACK",
            "taiji_leg_index": 5,
            "campaign_generation": 2,
            "left_right_phase": "NONE",
            "location_remaining_space": verdict(),
            "location_not_extended": verdict(),
            "exhaustion": verdict("FAIL"),
            "signal_has_independent_structure": verdict(),
        },
        "routing_atoms": route_atoms,
        "ai_recommended_scenario": "MATURE_TREND_PULLBACK",
        "causal_attestation": {
            "latest_visible_date": PACKET["as_of"],
            "future_bars_used": False,
            "future_performance_used": False,
            "legacy_answers_visible": False,
            "stock_identity_visible": False,
            "all_evidence_refs_from_packet": True,
            "monitor_start_used_to_choose_scenario": False,
        },
    }


def stage_d_record(stage_b_path: Path) -> dict:
    gates = [
        "ACTIVE_LARGE_UPTREND",
        "LONG_TREND_PERSISTENCE",
        "LONG_MA_HABIT",
        "CORRECTION_WITHIN_CAMPAIGN",
        "TAIJI_GENERATION_MAPPED",
        "DYNAMIC_QUADRANTS_SUPPORT",
        "SMALL_UP_CONTROL_CAUSAL",
        "EPISODE_STOP_CAUSAL",
    ]
    return {
        "schema_version": "v2-core-stage-d-r1-candidate",
        "review_id": PACKET["review_id"],
        "anonymous_stock_id": PACKET["anonymous_stock_id"],
        "as_of": PACKET["as_of"],
        "packet_sha256": sha256(PACKET_PATH),
        "stage_b_output_sha256": sha256(stage_b_path),
        "prompt_sha256": sha256(STAGE_D_PROMPT),
        "schema_sha256": sha256(STAGE_D_SCHEMA),
        "truth_table_sha256": sha256(TRUTH),
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "independent_review": False,
        "primary_scenario": "MATURE_TREND_PULLBACK",
        "scenario_evaluation": {
            "scenario": "MATURE_TREND_PULLBACK",
            "overall": "PASS",
            "gates": {gate: verdict() for gate in gates},
            "blocking_reasons": [],
            "uncertainties": [],
        },
        "blocking_atoms": {
            "Q3_ABSENT": verdict(),
            "EXHAUSTION_ABSENT": verdict(),
            "OVEREXTENSION_ABSENT": verdict(),
            "UNRESOLVED_CORPORATE_ACTION_ABSENT": verdict(),
        },
        "trigger": {
            "status": "TRIGGERED",
            "canonical_route": "MATURE_SMALL_DOW_REVERSAL",
            "trigger_date": PACKET["as_of"],
            "episode_stop_id": "STOP-E1",
            "campaign_stop_id": "STOP-C1",
            "supporting_evidence_refs": [AS_OF_REF],
            "uncertainties": [],
        },
        "location_remaining_space": verdict(),
        "signal_has_independent_structure": verdict(),
        "ai_recommended_permission": "TRADE_APPROVED",
        "causal_attestation": {
            "latest_visible_date": PACKET["as_of"],
            "future_bars_used": False,
            "future_performance_used": False,
            "legacy_answers_visible": False,
            "stock_identity_visible": False,
            "all_evidence_refs_from_packet": True,
            "other_scenarios_rejudged": False,
        },
    }


def validate_b(record: dict) -> dict:
    return validate_stage_b(
        record,
        packet_path=PACKET_PATH,
        schema_path=STAGE_B_SCHEMA,
        prompt_path=STAGE_B_PROMPT,
        truth_table_path=TRUTH,
    )


def validate_d(record: dict, stage_b_path: Path) -> dict:
    return validate_stage_d(
        record,
        packet_path=PACKET_PATH,
        stage_b_path=stage_b_path,
        stage_b_schema_path=STAGE_B_SCHEMA,
        stage_b_prompt_path=STAGE_B_PROMPT,
        schema_path=STAGE_D_SCHEMA,
        prompt_path=STAGE_D_PROMPT,
        truth_table_path=TRUTH,
    )


def test_valid_stage_b_routes_without_using_ai_recommendation() -> None:
    result = validate_b(stage_b_record())
    assert result["status"] == "PASS"
    assert result["program_derived_scenario"] == "MATURE_TREND_PULLBACK"


def test_stage_b_rejects_unknown_evidence_and_renamed_field() -> None:
    record = stage_b_record()
    record["data_sufficiency"]["supporting_evidence_refs"] = ["PRICE:2099-01-01"]
    assert any("UNKNOWN_REF" in error for error in validate_b(record)["errors"])

    record = stage_b_record()
    record["recommended_scenario"] = record.pop("ai_recommended_scenario")
    assert validate_b(record)["status"] == "SCHEMA_INVALID"


def test_stage_b_conflict_cannot_route_trade_scenario() -> None:
    record = stage_b_record()
    record["routing_atoms"]["ACTIVE_LARGE_BEAR_ANCHOR"] = verdict()
    record["routing_atoms"]["FRESH_UP_ANCHOR_CONTROLS"] = verdict()
    result = validate_b(record)
    assert result["program_derived_scenario"] == "UNRESOLVED_NO_TRADE"
    assert result["routing_status"] == "ADJUDICATION_REQUIRED"


def test_valid_stage_d_derives_trade(tmp_path: Path) -> None:
    stage_b_path = tmp_path / "stage_b.json"
    stage_b_path.write_text(json.dumps(stage_b_record(), ensure_ascii=False), encoding="utf-8")
    result = validate_d(stage_d_record(stage_b_path), stage_b_path)
    assert result["status"] == "PASS"
    assert result["program_derived_permission"] == "TRADE_APPROVED"


def test_stage_d_unknown_is_not_promoted_by_ai_recommendation(tmp_path: Path) -> None:
    stage_b_path = tmp_path / "stage_b.json"
    stage_b_path.write_text(json.dumps(stage_b_record(), ensure_ascii=False), encoding="utf-8")
    record = stage_d_record(stage_b_path)
    record["scenario_evaluation"]["gates"]["LONG_MA_HABIT"] = verdict("UNKNOWN")
    record["scenario_evaluation"]["overall"] = "UNKNOWN"
    record["ai_recommended_permission"] = "TRADE_APPROVED"
    result = validate_d(record, stage_b_path)
    assert result["status"] == "PASS"
    assert result["program_derived_permission"] == "UNKNOWN"
    assert result["recommendation_matches_program"] is False


def test_stage_d_rejects_wrong_route_and_changed_stage_b_hash(tmp_path: Path) -> None:
    stage_b_path = tmp_path / "stage_b.json"
    stage_b_path.write_text(json.dumps(stage_b_record(), ensure_ascii=False), encoding="utf-8")
    record = stage_d_record(stage_b_path)
    record["trigger"]["canonical_route"] = "FRESH_INITIAL_DESTRUCTIVE_EXPANSION"
    result = validate_d(record, stage_b_path)
    assert result["status"] == "FAIL"
    assert result["program_derived_permission"] == "INVALID"
    assert any("ROUTE_PREFIX" in error for error in result["errors"])

    record = stage_d_record(stage_b_path)
    record["stage_b_output_sha256"] = "0" * 64
    assert any("stage_b_output_sha256" in error for error in validate_d(record, stage_b_path)["errors"])


def test_full_staged_runner_preserves_raw_and_resumes_only_valid_artifacts(
    tmp_path: Path,
) -> None:
    stage_b_path = tmp_path / "stage_b" / "case.json"
    stage_b_receipt = tmp_path / "receipts" / "case.stage_b.json"
    seen_commands: list[list[str]] = []

    def fake_stage_b(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen_commands.append(command)
        last_message = Path(command[command.index("--output-last-message") + 1])
        last_message.write_text(
            json.dumps(stage_b_record(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result_b = run_stage_b_case(
        packet_path=PACKET_PATH,
        schema_path=STAGE_B_SCHEMA,
        prompt_path=STAGE_B_PROMPT,
        truth_table_path=TRUTH,
        output_path=stage_b_path,
        receipt_path=stage_b_receipt,
        timeout_seconds=30,
        run_command=fake_stage_b,
    )
    assert result_b["status"] == "PASS"
    assert raw_output_path(stage_b_path).is_file()
    assert validate_existing_stage_b_artifacts(
        packet_path=PACKET_PATH,
        schema_path=STAGE_B_SCHEMA,
        prompt_path=STAGE_B_PROMPT,
        truth_table_path=TRUTH,
        output_path=stage_b_path,
        receipt_path=stage_b_receipt,
    )["status"] == "PASS"

    stage_d_path = tmp_path / "stage_d" / "case.json"
    route_path = tmp_path / "program_route" / "case.json"
    permission_path = tmp_path / "final_permission" / "case.json"
    stage_d_receipt = tmp_path / "receipts" / "case.stage_d.json"

    def fake_stage_d(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen_commands.append(command)
        last_message = Path(command[command.index("--output-last-message") + 1])
        last_message.write_text(
            json.dumps(stage_d_record(stage_b_path), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result_d = run_stage_d_case(
        packet_path=PACKET_PATH,
        stage_b_path=stage_b_path,
        stage_b_schema_path=STAGE_B_SCHEMA,
        stage_b_prompt_path=STAGE_B_PROMPT,
        schema_path=STAGE_D_SCHEMA,
        prompt_path=STAGE_D_PROMPT,
        truth_table_path=TRUTH,
        output_path=stage_d_path,
        route_path=route_path,
        permission_path=permission_path,
        receipt_path=stage_d_receipt,
        timeout_seconds=30,
        run_command=fake_stage_d,
    )
    assert result_d["status"] == "COMPLETED"
    assert result_d["program_derived_permission"] == "TRADE_APPROVED"
    assert raw_output_path(stage_d_path).is_file()
    assert validate_existing_stage_d_artifacts(
        packet_path=PACKET_PATH,
        stage_b_path=stage_b_path,
        stage_b_schema_path=STAGE_B_SCHEMA,
        stage_b_prompt_path=STAGE_B_PROMPT,
        schema_path=STAGE_D_SCHEMA,
        prompt_path=STAGE_D_PROMPT,
        truth_table_path=TRUTH,
        output_path=stage_d_path,
        route_path=route_path,
        permission_path=permission_path,
        receipt_path=stage_d_receipt,
    )["program_derived_permission"] == "TRADE_APPROVED"
    assert all(command[command.index("--model") + 1] == "gpt-5.6-sol" for command in seen_commands)
    assert all('model_reasoning_effort="xhigh"' in command for command in seen_commands)

    permission = json.loads(permission_path.read_text(encoding="utf-8"))
    permission["program_derived_permission"] = "WAIT"
    permission_path.write_text(json.dumps(permission), encoding="utf-8")
    with pytest.raises(StagedRunnerError, match="permission is inconsistent"):
        validate_existing_stage_d_artifacts(
            packet_path=PACKET_PATH,
            stage_b_path=stage_b_path,
            stage_b_schema_path=STAGE_B_SCHEMA,
            stage_b_prompt_path=STAGE_B_PROMPT,
            schema_path=STAGE_D_SCHEMA,
            prompt_path=STAGE_D_PROMPT,
            truth_table_path=TRUTH,
            output_path=stage_d_path,
            route_path=route_path,
            permission_path=permission_path,
            receipt_path=stage_d_receipt,
        )


def test_invalid_model_output_is_preserved_without_repair(tmp_path: Path) -> None:
    output_path = tmp_path / "stage_b" / "invalid.json"

    def fake_invalid(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        last_message = Path(command[command.index("--output-last-message") + 1])
        invalid = stage_b_record()
        invalid["renamed_field"] = invalid.pop("ai_recommended_scenario")
        last_message.write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(StagedRunnerError, match="transport output invalid"):
        run_stage_b_case(
            packet_path=PACKET_PATH,
            schema_path=STAGE_B_SCHEMA,
            prompt_path=STAGE_B_PROMPT,
            truth_table_path=TRUTH,
            output_path=output_path,
            receipt_path=tmp_path / "receipts" / "invalid.json",
            timeout_seconds=30,
            run_command=fake_invalid,
        )
    assert invalid_output_path(output_path).is_file()
    assert not output_path.exists()
