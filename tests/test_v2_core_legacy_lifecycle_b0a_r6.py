from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from scripts.v2_core_legacy_lifecycle_b0a_validator_v6 import validate_response
from scripts.v2_core_legacy_roles_b0b_validator_v6 import validate_response as validate_roles_response
from scripts.v2_core_legacy_lifecycle_runner_v6 import (
    MODEL,
    REASONING,
    RunnerError,
    run_b0a_case,
    run_b0b_case,
    validate_existing_b0a,
    validate_existing_b0b,
)
from scripts.v2_core_legacy_lifecycle_manifest_v6 import build_manifest


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = (
    ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"
)
SCHEMA = ARTIFACT_DIR / "v2_core_legacy_lifecycle_b0a.schema.candidate_r6.json"
TRUTH_TABLE = ARTIFACT_DIR / "legacy_lifecycle_route_truth_table.candidate_r6.json"
PROMPT = ARTIFACT_DIR / "v2_core_legacy_lifecycle_b0a.prompt.candidate_r6.md"
B0B_SCHEMA = ARTIFACT_DIR / "v2_core_legacy_roles_b0b.schema.candidate_r6.json"
B0B_TRUTH_TABLE = ARTIFACT_DIR / "legacy_scenario_role_truth_table.candidate_r6.json"
B0B_PROMPT = ARTIFACT_DIR / "v2_core_legacy_roles_b0b.prompt.candidate_r6.md"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _candidate(
    digit: str,
    *,
    direction: str,
    status: str,
    start: str,
    end: str | None,
) -> dict:
    return {
        "candidate_id": f"CAMSEG-{digit * 20}",
        "evidence_option_id": f"CAOPT-{digit * 20}",
        "direction": direction,
        "status": status,
        "start_date": start,
        "confirmed_end_date": end,
        "observed_through": end or "2023-01-31",
        "basis": "PIVOT_CONFIRMED_CAMPAIGN" if status == "CONFIRMED" else "PIVOT_FORMING_CAMPAIGN",
        "scale": "LARGE",
    }


def packet() -> dict:
    candidates = [
        _candidate("1", direction="DOWN", status="CONFIRMED", start="2021-01-01", end="2021-06-01"),
        _candidate("2", direction="UP", status="FORMING", start="2023-01-02", end=None),
        _candidate("3", direction="UP", status="CONFIRMED", start="2022-01-03", end="2022-03-01"),
        _candidate("4", direction="DOWN", status="CONFIRMED", start="2022-04-01", end="2022-05-02"),
        _candidate("5", direction="UP", status="CONFIRMED", start="2022-06-01", end="2022-08-01"),
        _candidate("6", direction="UP", status="FORMING", start="2022-09-01", end=None),
        _candidate("7", direction="UP", status="FORMING", start="2021-12-01", end=None),
    ]
    return {
        "review_id": "FP-synthetic-r6",
        "as_of": "2023-01-31",
        "candidate_pool": candidates,
        "candidate_evidence_options": [
            {
                "candidate_id": row["candidate_id"],
                "evidence_option_id": row["evidence_option_id"],
                "source_evidence_refs": [f"PRICE:{row['start_date']}"]
            }
            for row in candidates
        ],
        "proxy_evidence": [
            {"ref": "PROXY:LONG_MA_CONTEXT_252", "values": {"ma105_reclaim_count_252": 4}}
        ],
    }


def candidate_by_digit(packet_value: dict, digit: str) -> dict:
    return next(row for row in packet_value["candidate_pool"] if row["candidate_id"].endswith(digit * 20))


def atom(
    judgement: str,
    role: str,
    subject: dict | None,
    relation: dict | None = None,
    refs: list[str] | None = None,
) -> dict:
    return {
        "judgement": judgement,
        "evaluated_role": role,
        "subject_candidate_id": subject["candidate_id"] if subject else "NONE",
        "evidence_option_id": subject["evidence_option_id"] if subject else "NONE",
        "relation_candidate_id": relation["candidate_id"] if relation else "NONE",
        "relation_evidence_option_id": relation["evidence_option_id"] if relation else "NONE",
        "supporting_packet_evidence_refs": refs
        if refs is not None
        else ([f"PRICE:{subject['start_date']}"] if subject else []),
        "explanation": "固定候選與截至當日證據支持這一生命週期判讀結果。",
    }


def causal_attestation() -> dict:
    return {
        "as_of_only": True,
        "no_identity": True,
        "no_legacy_answer": True,
        "no_future_performance": True,
        "fixed_candidates_only": True,
        "no_scenario_output": True,
        "no_trade_permission": True,
    }


def fresh_response() -> tuple[dict, dict]:
    packet_value = packet()
    down = candidate_by_digit(packet_value, "1")
    fresh = candidate_by_digit(packet_value, "2")
    response = {
        "schema_version": "v2-core-legacy-lifecycle-b0a-r6-candidate",
        "lifecycle_atoms": {
            "active_large_down_still_controls": atom("FAIL", "ACTIVE_DOWN_CONTROL", down),
            "current_up_replaced_down_control": atom("PASS", "UP_CONTROL_TRANSFER", fresh, down),
            "completed_up_parent_controls_current_correction": atom("FAIL", "COMPLETED_UP_PARENT", fresh),
            "current_is_first_independent_copy": atom("FAIL", "CURRENT_COPY", fresh),
            "mature_success_preexists_current_episode": atom("FAIL", "PREEXISTING_MATURE_SEQUENCE", fresh),
            "long_trend_habit_preexists_current_episode": atom("FAIL", "PREEXISTING_LONG_TREND_HABIT", fresh),
            "current_up_is_fresh_forming_anchor": atom("PASS", "CURRENT_FRESH_UP_ANCHOR", fresh, down),
        },
        "control_transfer_proof": {
            "result": "UP_REPLACED_DOWN",
            "down_candidate_id": down["candidate_id"],
            "down_evidence_option_id": down["evidence_option_id"],
            "up_candidate_id": fresh["candidate_id"],
            "up_evidence_option_id": fresh["evidence_option_id"],
            "supporting_packet_evidence_refs": [f"PRICE:{down['start_date']}", f"PRICE:{fresh['start_date']}"],
            "explanation": "截至當日的向上結構已取代先前向下控制，且兩段候選可分離。",
        },
        "maturity_proof": {
            "status": "FAIL",
            "parent_candidate_id": "NONE",
            "parent_evidence_option_id": "NONE",
            "correction_candidate_id": "NONE",
            "correction_evidence_option_id": "NONE",
            "successful_copy_candidate_id": "NONE",
            "successful_copy_evidence_option_id": "NONE",
            "current_episode_candidate_id": fresh["candidate_id"],
            "current_episode_evidence_option_id": fresh["evidence_option_id"],
            "sequence_preexists_current_episode": "FAIL",
            "supporting_packet_evidence_refs": [f"PRICE:{fresh['start_date']}"],
            "explanation": "本次作用段以前沒有完整父代、修正與成功複製序列。",
        },
        "confidence": "HIGH",
        "uncertainty_codes": ["NONE"],
        "causal_attestation": causal_attestation(),
    }
    return response, packet_value


def mature_response() -> tuple[dict, dict]:
    response, packet_value = fresh_response()
    down = candidate_by_digit(packet_value, "1")
    parent = candidate_by_digit(packet_value, "3")
    correction = candidate_by_digit(packet_value, "4")
    copy = candidate_by_digit(packet_value, "5")
    current = candidate_by_digit(packet_value, "6")
    response["lifecycle_atoms"] = {
        "active_large_down_still_controls": atom("FAIL", "ACTIVE_DOWN_CONTROL", down),
        "current_up_replaced_down_control": atom("PASS", "UP_CONTROL_TRANSFER", current, down),
        "completed_up_parent_controls_current_correction": atom("PASS", "COMPLETED_UP_PARENT", parent, correction),
        "current_is_first_independent_copy": atom("UNKNOWN", "CURRENT_COPY", current, parent),
        "mature_success_preexists_current_episode": atom("PASS", "PREEXISTING_MATURE_SEQUENCE", copy, parent),
        "long_trend_habit_preexists_current_episode": atom(
            "PASS",
            "PREEXISTING_LONG_TREND_HABIT",
            parent,
            copy,
            [f"PRICE:{parent['start_date']}", "PROXY:LONG_MA_CONTEXT_252"],
        ),
        "current_up_is_fresh_forming_anchor": atom("FAIL", "CURRENT_FRESH_UP_ANCHOR", current, down),
    }
    response["control_transfer_proof"] = {
        "result": "UP_REPLACED_DOWN",
        "down_candidate_id": down["candidate_id"],
        "down_evidence_option_id": down["evidence_option_id"],
        "up_candidate_id": current["candidate_id"],
        "up_evidence_option_id": current["evidence_option_id"],
        "supporting_packet_evidence_refs": [f"PRICE:{down['start_date']}", f"PRICE:{current['start_date']}"],
        "explanation": "截至當日向上生命週期已取代較早的向下控制候選。",
    }
    response["maturity_proof"] = {
        "status": "PASS",
        "parent_candidate_id": parent["candidate_id"],
        "parent_evidence_option_id": parent["evidence_option_id"],
        "correction_candidate_id": correction["candidate_id"],
        "correction_evidence_option_id": correction["evidence_option_id"],
        "successful_copy_candidate_id": copy["candidate_id"],
        "successful_copy_evidence_option_id": copy["evidence_option_id"],
        "current_episode_candidate_id": current["candidate_id"],
        "current_episode_evidence_option_id": current["evidence_option_id"],
        "sequence_preexists_current_episode": "PASS",
        "supporting_packet_evidence_refs": [
            f"PRICE:{parent['start_date']}",
            f"PRICE:{correction['start_date']}",
            f"PRICE:{copy['start_date']}",
            f"PRICE:{current['start_date']}",
        ],
        "explanation": "父代、修正與已完成複製均早於目前episode，形成可稽核成熟序列。",
    }
    return response, packet_value


def validate(response: dict, packet_value: dict) -> dict:
    return validate_response(
        response=response,
        schema=load_json(SCHEMA),
        packet=packet_value,
        truth_table=load_json(TRUTH_TABLE),
    )


def role_atom(judgement: str, option_id: str) -> dict:
    return {
        "judgement": judgement,
        "evidence_option_id": option_id,
        "explanation": "固定候選證據支持這個角色原子與目前結構的關係。",
    }


def selected_role(candidate: dict) -> dict:
    option_id = candidate["evidence_option_id"]
    return {
        "selection_status": "SELECTED",
        "candidate_id": candidate["candidate_id"],
        "evidence_option_id": option_id,
        "alternative_candidate_ids": [],
        "alternative_changes_role_conclusion": False,
        "data_sufficiency": role_atom("PASS", option_id),
        "role_fit": role_atom("PASS", option_id),
        "structural_corroboration": role_atom("PASS", option_id),
        "relationship_to_current_context": role_atom("PASS", option_id),
        "explanation": "固定候選符合此情境下的角色，而且不是只依最近或最長原則選取。",
    }


def parent_not_applicable(working: dict) -> dict:
    option_id = working["evidence_option_id"]
    return {
        "selection_status": "NOT_APPLICABLE",
        "candidate_id": "NONE",
        "evidence_option_id": option_id,
        "alternative_candidate_ids": [],
        "alternative_changes_role_conclusion": False,
        "data_sufficiency": role_atom("PASS", option_id),
        "role_fit": role_atom("PASS", option_id),
        "structural_corroboration": role_atom("PASS", option_id),
        "relationship_to_current_context": role_atom("PASS", option_id),
        "explanation": "現有固定結構能正面證明此生命週期沒有適用的已完成UP父代。",
    }


def role_check(candidate: dict, result: str = "PASS") -> dict:
    return {
        "result": result,
        "candidate_id": candidate["candidate_id"],
        "evidence_option_id": candidate["evidence_option_id"],
        "explanation": "固定候選比較已完成，沒有依最近、最長或角色壓縮作為預設。",
    }


def fresh_roles_response(packet_value: dict) -> dict:
    down = candidate_by_digit(packet_value, "1")
    fresh = candidate_by_digit(packet_value, "2")
    return {
        "schema_version": "v2-core-legacy-roles-b0b-r6-candidate",
        "roles": {
            "campaign_context": selected_role(down),
            "scenario_working": selected_role(fresh),
            "parent": parent_not_applicable(fresh),
            "episode_structure": selected_role(fresh),
        },
        "basis_comparison": {
            "availability": "PIVOT_ONLY",
            "macd_candidate_id": "NONE",
            "pivot_candidate_id": fresh["candidate_id"],
            "selected_working_candidate_id": fresh["candidate_id"],
            "conclusion": "ONE_FAMILY_ONLY",
            "explanation": "本假資料只有pivot候選，所選形成中UP符合FRESH工作角色。",
        },
        "anti_drift_checks": {
            "no_recency_default": role_check(fresh),
            "no_longest_default": role_check(fresh),
            "cross_family_comparison_completed": role_check(fresh),
            "role_compression_absent": role_check(fresh),
            "b0a_route_respected": role_check(fresh),
        },
        "confidence": "HIGH",
        "uncertainty_codes": ["NONE"],
        "causal_attestation": {
            "as_of_only": True,
            "no_identity": True,
            "no_legacy_answer": True,
            "no_future_performance": True,
            "fixed_candidates_only": True,
            "program_route_not_reopened": True,
            "no_trade_permission": True,
        },
    }


def validate_b0b(response: dict, packet_value: dict, b0a_response: dict) -> dict:
    return validate_roles_response(
        response=response,
        schema=load_json(B0B_SCHEMA),
        packet=packet_value,
        b0a_response=b0a_response,
        b0a_schema=load_json(SCHEMA),
        lifecycle_truth_table=load_json(TRUTH_TABLE),
        role_truth_table=load_json(B0B_TRUTH_TABLE),
    )


def test_r6_fail_atom_can_cite_rejected_down_subject_and_fresh_routes() -> None:
    response, packet_value = fresh_response()
    result = validate(response, packet_value)
    assert result["status"] == "VALID"
    assert result["program_derived_scenario_family"] == "FRESH_Q1_EXPANSION"
    assert result["route_status"] == "ROUTED"
    assert result["validator_contract"]["negative_atoms_may_cite_rejected_subjects"] is True
    assert result["validator_contract"]["trade_permission_granted"] is False


def test_r6_rejects_subject_option_mismatch() -> None:
    response, packet_value = fresh_response()
    other = candidate_by_digit(packet_value, "3")
    response["lifecycle_atoms"]["active_large_down_still_controls"]["evidence_option_id"] = other[
        "evidence_option_id"
    ]
    result = validate(response, packet_value)
    assert result["status"] == "INVALID"
    assert any("evidence option does not match subject candidate" in error for error in result["errors"])


def test_r6_mature_routes_only_with_complete_preepisode_sequence() -> None:
    response, packet_value = mature_response()
    result = validate(response, packet_value)
    assert result["status"] == "VALID"
    assert result["program_derived_scenario_family"] == "MATURE_TREND_PULLBACK"
    assert result["validator_contract"]["maturity_requires_preepisode_sequence"] is True


def test_r6_mature_atom_without_matching_proof_is_invalid() -> None:
    response, packet_value = mature_response()
    response["maturity_proof"]["status"] = "FAIL"
    response["maturity_proof"]["sequence_preexists_current_episode"] = "FAIL"
    result = validate(response, packet_value)
    assert result["status"] == "INVALID"
    assert "maturity proof status conflicts with mature-success atom" in result["errors"]


def test_r6_unknown_control_and_lifecycle_stays_unresolved() -> None:
    response, packet_value = fresh_response()
    down = candidate_by_digit(packet_value, "1")
    fresh = candidate_by_digit(packet_value, "2")
    response["lifecycle_atoms"]["active_large_down_still_controls"] = atom(
        "UNKNOWN", "ACTIVE_DOWN_CONTROL", down
    )
    response["lifecycle_atoms"]["current_up_replaced_down_control"] = atom(
        "UNKNOWN", "UP_CONTROL_TRANSFER", fresh, down
    )
    response["control_transfer_proof"]["result"] = "UNRESOLVED"
    result = validate(response, packet_value)
    assert result["status"] == "VALID"
    assert result["program_derived_scenario_family"] == "UNRESOLVED_NO_TRADE"
    assert result["route_status"] == "UNRESOLVED"
    assert "CONTROL_TRANSFER_UNRESOLVED" in result["route_reasons"]


def test_r6_control_proof_cannot_contradict_atoms() -> None:
    response, packet_value = fresh_response()
    response["control_transfer_proof"]["result"] = "DOWN_STILL_CONTROLS"
    result = validate(response, packet_value)
    assert result["status"] == "INVALID"
    assert "control transfer proof conflicts with control atoms" in result["errors"]


def test_r6_maturity_sequence_must_finish_before_current_episode() -> None:
    response, packet_value = mature_response()
    current = candidate_by_digit(packet_value, "6")
    current["start_date"] = "2022-07-01"
    result = validate(response, packet_value)
    assert result["status"] == "INVALID"
    assert any("parent-correction-copy-episode order" in error or "complete before" in error for error in result["errors"])


def test_r6_rejects_nonpacket_evidence_ref() -> None:
    response, packet_value = fresh_response()
    response["lifecycle_atoms"]["current_up_is_fresh_forming_anchor"][
        "supporting_packet_evidence_refs"
    ] = ["FUTURE:NOT_IN_PACKET"]
    result = validate(response, packet_value)
    assert result["status"] == "INVALID"
    assert any("evidence ref not in packet" in error for error in result["errors"])


def test_r6_schema_forbids_ai_scenario_and_trade_permission_fields() -> None:
    response, packet_value = fresh_response()
    response["recommended_scenario_family"] = "FRESH_Q1_EXPANSION"
    response["trade_permission"] = "TRADE_APPROVED"
    result = validate(response, packet_value)
    assert result["status"] == "INVALID"
    assert any("Additional properties are not allowed" in error for error in result["errors"])


def test_r6_prompt_schema_and_truth_table_hold_stage_boundary() -> None:
    schema = load_json(SCHEMA)
    truth_table = load_json(TRUTH_TABLE)
    prompt = PROMPT.read_text(encoding="utf-8-sig")
    assert "recommended_scenario_family" not in schema["properties"]
    assert "trade_permission" not in schema["properties"]
    assert truth_table["no_trade_permission_at_this_stage"] is True
    assert truth_table["program_contract"]["ai_outputs_scenario"] is False
    assert truth_table["program_contract"]["trade_permission_granted"] is False
    assert set(truth_table["required_atom_names"]) == set(
        schema["properties"]["lifecycle_atoms"]["required"]
    )
    assert "不得輸出`recommended_scenario_family`" in prompt


def test_r6_route_profiles_are_pairwise_exclusive() -> None:
    routes = load_json(TRUTH_TABLE)["routes"]
    for index, left in enumerate(routes):
        for right in routes[index + 1 :]:
            left_requirements = {
                **{f"atom:{key}": value for key, value in left["required_atoms"].items()},
                **{f"proof:{key}": value for key, value in left["required_proofs"].items()},
            }
            right_requirements = {
                **{f"atom:{key}": value for key, value in right["required_atoms"].items()},
                **{f"proof:{key}": value for key, value in right["required_proofs"].items()},
            }
            shared = set(left_requirements) & set(right_requirements)
            assert any(left_requirements[key] != right_requirements[key] for key in shared), (
                left["scenario_family"],
                right["scenario_family"],
            )


def test_r6_does_not_modify_response_during_validation() -> None:
    response, packet_value = fresh_response()
    original = deepcopy(response)
    result = validate(response, packet_value)
    assert result["status"] == "VALID"
    assert response == original


def test_r6_b0b_fresh_roles_resolve_without_reopening_scenario_or_trade() -> None:
    b0a_response, packet_value = fresh_response()
    response = fresh_roles_response(packet_value)
    result = validate_b0b(response, packet_value, b0a_response)
    assert result["status"] == "VALID"
    assert result["program_derived_scenario_family"] == "FRESH_Q1_EXPANSION"
    assert result["role_resolution_status"] == "RESOLVED"
    assert result["validator_contract"]["ai_revotes_scenario"] is False
    assert result["validator_contract"]["trade_permission_granted"] is False


def test_r6_b0b_rejects_working_anchor_that_breaks_b0a_binding() -> None:
    b0a_response, packet_value = fresh_response()
    response = fresh_roles_response(packet_value)
    other = candidate_by_digit(packet_value, "7")
    response["roles"]["scenario_working"] = selected_role(other)
    response["roles"]["parent"] = parent_not_applicable(other)
    response["basis_comparison"]["selected_working_candidate_id"] = other["candidate_id"]
    result = validate_b0b(response, packet_value, b0a_response)
    assert result["status"] == "INVALID"
    assert "scenario working candidate violates B0A binding" in result["errors"]


def test_r6_b0b_antidrift_failure_downgrades_role_resolution() -> None:
    b0a_response, packet_value = fresh_response()
    response = fresh_roles_response(packet_value)
    response["anti_drift_checks"]["no_recency_default"]["result"] = "FAIL"
    result = validate_b0b(response, packet_value, b0a_response)
    assert result["status"] == "VALID"
    assert result["role_resolution_status"] == "UNRESOLVED"
    assert "NO_RECENCY_DEFAULT_NOT_PASS" in result["role_resolution_reasons"]


def test_r6_b0b_refuses_unresolved_b0a_route() -> None:
    b0a_response, packet_value = fresh_response()
    down = candidate_by_digit(packet_value, "1")
    fresh = candidate_by_digit(packet_value, "2")
    b0a_response["lifecycle_atoms"]["active_large_down_still_controls"] = atom(
        "UNKNOWN", "ACTIVE_DOWN_CONTROL", down
    )
    b0a_response["lifecycle_atoms"]["current_up_replaced_down_control"] = atom(
        "UNKNOWN", "UP_CONTROL_TRANSFER", fresh, down
    )
    b0a_response["control_transfer_proof"]["result"] = "UNRESOLVED"
    result = validate_b0b(fresh_roles_response(packet_value), packet_value, b0a_response)
    assert result["status"] == "INVALID"
    assert "B0B requires one uniquely routed B0A scenario" in result["errors"]


def test_r6_b0b_mature_profile_binds_parent_and_episode_but_leaves_working_for_ai() -> None:
    b0a_response, packet_value = mature_response()
    broad_working = candidate_by_digit(packet_value, "7")
    parent = candidate_by_digit(packet_value, "3")
    current = candidate_by_digit(packet_value, "6")
    response = fresh_roles_response(packet_value)
    response["roles"] = {
        "campaign_context": selected_role(broad_working),
        "scenario_working": selected_role(broad_working),
        "parent": selected_role(parent),
        "episode_structure": selected_role(current),
    }
    response["basis_comparison"]["pivot_candidate_id"] = broad_working["candidate_id"]
    response["basis_comparison"]["selected_working_candidate_id"] = broad_working["candidate_id"]
    for check in response["anti_drift_checks"].values():
        check.update(
            {
                "candidate_id": broad_working["candidate_id"],
                "evidence_option_id": broad_working["evidence_option_id"],
            }
        )
    result = validate_b0b(response, packet_value, b0a_response)
    assert result["status"] == "VALID"
    assert result["program_derived_scenario_family"] == "MATURE_TREND_PULLBACK"
    assert result["role_resolution_status"] == "RESOLVED"
    assert result["selected_role_candidate_ids"]["PARENT"] == parent["candidate_id"]
    assert result["selected_role_candidate_ids"]["EPISODE_STRUCTURE"] == current["candidate_id"]


def test_r6_b0b_schema_forbids_ai_scenario_and_trade_fields() -> None:
    b0a_response, packet_value = fresh_response()
    response = fresh_roles_response(packet_value)
    response["recommended_scenario_family"] = "FRESH_Q1_EXPANSION"
    response["trade_permission"] = "TRADE_APPROVED"
    result = validate_b0b(response, packet_value, b0a_response)
    assert result["status"] == "INVALID"
    assert any("Additional properties are not allowed" in error for error in result["errors"])


def test_r6_b0b_prompt_schema_and_truth_table_preserve_routed_boundary() -> None:
    schema = load_json(B0B_SCHEMA)
    truth_table = load_json(B0B_TRUTH_TABLE)
    prompt = B0B_PROMPT.read_text(encoding="utf-8-sig")
    assert "recommended_scenario_family" not in schema["properties"]
    assert "trade_permission" not in schema["properties"]
    assert truth_table["no_trade_permission_at_this_stage"] is True
    assert truth_table["program_contract"]["ai_revotes_scenario"] is False
    assert "不得重新投票情境" in prompt


def _write_runner_inputs(tmp_path: Path, packet_value: dict) -> tuple[Path, Path]:
    packet_path = tmp_path / "FP-synthetic-r6.json"
    packet_path.write_text(json.dumps(packet_value, ensure_ascii=False), encoding="utf-8")
    manifest_path = tmp_path / "input_manifest.json"
    manifest = {
        "formal_model": MODEL,
        "reasoning_effort": REASONING,
        "rows": [
            {
                "review_id": packet_value["review_id"],
                "as_of": packet_value["as_of"],
                "input_packet_file": packet_path.name,
                "input_packet_sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest(),
            }
        ],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return packet_path, manifest_path


def _write_usage(tmp_path: Path, name: str, remaining: float = 90.0) -> Path:
    path = tmp_path / f"usage-{name}.json"
    path.write_text(
        json.dumps(
            {
                "source": "CODEX_APP_GET_USAGE_LIMITS",
                "limit_id": "codex",
                "used_percent": 100.0 - remaining,
                "remaining_percent": remaining,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "check_id": name,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_r6_two_stage_fake_transport_is_immutable_and_revalidates(tmp_path: Path) -> None:
    b0a_response, packet_value = fresh_response()
    b0b_response = fresh_roles_response(packet_value)
    packet_path, manifest_path = _write_runner_inputs(tmp_path, packet_value)
    b0a_output = tmp_path / "b0a" / "FP-synthetic-r6.json"
    b0a_receipt = tmp_path / "b0a-receipts" / "FP-synthetic-r6.json"
    b0b_output = tmp_path / "b0b" / "FP-synthetic-r6.json"
    b0b_receipt = tmp_path / "b0b-receipts" / "FP-synthetic-r6.json"
    calls: list[dict] = []

    def fake_for(payload: dict):
        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append({"command": command, "prompt": kwargs.get("input")})
            target = Path(command[command.index("--output-last-message") + 1])
            target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        return fake_run

    b0a_validation = run_b0a_case(
        input_packet_path=packet_path,
        input_manifest_path=manifest_path,
        schema_path=SCHEMA,
        prompt_path=PROMPT,
        lifecycle_truth_table_path=TRUTH_TABLE,
        usage_attestation_path=_write_usage(tmp_path, "b0a"),
        stop_remaining_percent_lte=5,
        output_path=b0a_output,
        receipt_path=b0a_receipt,
        timeout_seconds=30,
        run_command=fake_for(b0a_response),
    )
    assert b0a_validation["program_derived_scenario_family"] == "FRESH_Q1_EXPANSION"
    assert validate_existing_b0a(
        input_packet_path=packet_path,
        input_manifest_path=manifest_path,
        schema_path=SCHEMA,
        prompt_path=PROMPT,
        lifecycle_truth_table_path=TRUTH_TABLE,
        output_path=b0a_output,
        receipt_path=b0a_receipt,
    ) == b0a_validation

    b0b_validation = run_b0b_case(
        input_packet_path=packet_path,
        input_manifest_path=manifest_path,
        b0a_schema_path=SCHEMA,
        b0a_prompt_path=PROMPT,
        lifecycle_truth_table_path=TRUTH_TABLE,
        b0a_output_path=b0a_output,
        b0a_receipt_path=b0a_receipt,
        b0b_schema_path=B0B_SCHEMA,
        b0b_prompt_path=B0B_PROMPT,
        role_truth_table_path=B0B_TRUTH_TABLE,
        usage_attestation_path=_write_usage(tmp_path, "b0b"),
        stop_remaining_percent_lte=5,
        output_path=b0b_output,
        receipt_path=b0b_receipt,
        timeout_seconds=30,
        run_command=fake_for(b0b_response),
    )
    assert b0b_validation["role_resolution_status"] == "RESOLVED"
    assert validate_existing_b0b(
        input_packet_path=packet_path,
        input_manifest_path=manifest_path,
        b0a_schema_path=SCHEMA,
        b0a_prompt_path=PROMPT,
        lifecycle_truth_table_path=TRUTH_TABLE,
        b0a_output_path=b0a_output,
        b0a_receipt_path=b0a_receipt,
        b0b_schema_path=B0B_SCHEMA,
        b0b_prompt_path=B0B_PROMPT,
        role_truth_table_path=B0B_TRUTH_TABLE,
        output_path=b0b_output,
        receipt_path=b0b_receipt,
    ) == b0b_validation
    assert len(calls) == 2
    for call in calls:
        command = call["command"]
        assert command[command.index("--model") + 1] == MODEL
        assert f'model_reasoning_effort="{REASONING}"' in command
    assert "program_routed_scenario_family" not in str(calls[0]["prompt"])
    assert '"program_routed_scenario_family":"FRESH_Q1_EXPANSION"' in str(calls[1]["prompt"])
    assert '"teacher":' not in str(calls[1]["prompt"]).lower()
    assert '"legacy_answer":' not in str(calls[1]["prompt"]).lower()


def test_r6_runner_budget_gate_blocks_before_transport(tmp_path: Path) -> None:
    b0a_response, packet_value = fresh_response()
    packet_path, manifest_path = _write_runner_inputs(tmp_path, packet_value)
    called = False

    def should_not_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        raise AssertionError("transport must not run at the budget stop")

    with pytest.raises(RunnerError, match="BUDGET_STOP"):
        run_b0a_case(
            input_packet_path=packet_path,
            input_manifest_path=manifest_path,
            schema_path=SCHEMA,
            prompt_path=PROMPT,
            lifecycle_truth_table_path=TRUTH_TABLE,
            usage_attestation_path=_write_usage(tmp_path, "stop", remaining=5.0),
            stop_remaining_percent_lte=5,
            output_path=tmp_path / "blocked.json",
            receipt_path=tmp_path / "blocked.receipt.json",
            timeout_seconds=30,
            run_command=should_not_run,
        )
    assert called is False
    assert b0a_response["schema_version"] == "v2-core-legacy-lifecycle-b0a-r6-candidate"


def test_r6_execution_manifest_binds_two_stage_contract() -> None:
    manifest = build_manifest(ARTIFACT_DIR)
    assert manifest["formal_model"] == MODEL
    assert manifest["reasoning_effort"] == REASONING
    assert manifest["expected_case_rounds"] == 14
    assert manifest["stop_remaining_percent_lte"] == 5.0
    assert manifest["legacy_answers_available_to_ai"] is False
    assert manifest["locked_reproduction_set_opened"] is False
    assert manifest["trade_permission_granted"] is False
    assert manifest["prior_r5_invalid_output_reused"] is False
    assert manifest["separate_usage_attestation_required_before_each_stage"] is True
    for key in (
        "recovery_design",
        "teacher_distillation_design",
        "teacher_pairwise_audit",
        "selection",
        "input_manifest",
        "b0a_prompt",
        "b0a_schema",
        "lifecycle_truth_table",
        "b0b_prompt",
        "b0b_schema",
        "role_truth_table",
        "operational_policy",
    ):
        path = ARTIFACT_DIR / manifest[f"{key}_file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest[f"{key}_sha256"]
    for key in ("runner", "b0a_validator", "b0b_validator"):
        path = ROOT / manifest[f"{key}_file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest[f"{key}_sha256"]
