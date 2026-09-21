from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import jsonschema
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "config/hybrid_atomic_semantics_v2.schema.json"
PROMPT_PATH = ROOT / "config/hybrid_semantic_prompt_v2.md"

PROTECTED_STRATEGY_HASHES = {
    "docs/enlightenment-ai-judgement-v1.md": "eb0d7023bdbdf04b958a512ee83f4e4fafa574500a09889c6b66a0cb7e9683bc",
    "config/enlightenment_ai_judgement_v1.schema.json": "592ca3820f1a760fbe10b9f7d0da5d291974ff77233e93b9319e8a9021305e22",
    "docs/enlightenment-ai-judgement-v2.md": "82b20f14dc94085f5cfb5c192788e41d36c9c6bd1fecc5de645cc6f317613d64",
    "config/enlightenment_ai_rules_v2.json": "29abcc4d0fd31d53045dc977d501830cfc23a2b4f11d19723dbc05eec87bb180",
    "config/enlightenment_ai_judgement_v2.schema.json": "4243e60a05ea74c75d09974b4ef995d07319c19575ac9d4653ab9f3564c63575",
    "docs/enlightenment-ai-judgement-v3.md": "1edb5f8c3cbaafbef3642a6a95ee02653f734c8fd2a994d28cacfd66ee274824",
    "config/enlightenment_ai_rules_v3.json": "4e78b5ddeea93d6469eca1fbd6e5309679bfdfc4193f1358cfde09789b201ca6",
    "config/enlightenment_ai_judgement_v3.schema.json": "e01729ea50c4a636c5ad0345c499f28bbe8b2d65dd68edc8ac88cbc96db7cf60",
}

EXPECTED_SCENARIO_GATES = {
    "MATURE_TREND_PULLBACK": {
        "ACTIVE_LARGE_UPTREND",
        "LONG_TREND_PERSISTENCE",
        "LONG_MA_HABIT",
        "CORRECTION_WITHIN_CAMPAIGN",
        "TAIJI_GENERATION_MAPPED",
        "DYNAMIC_QUADRANTS_SUPPORT",
        "SMALL_UP_CONTROL_CAUSAL",
        "EPISODE_STOP_CAUSAL",
    },
    "MACRO_COPY_RESONANCE": {
        "COMPLETED_PARENT_ANCHOR",
        "CORRECTION_INTACT",
        "TAIJI_GENERATION_MAPPED",
        "CORRECTION_BEAR_DOW_LINE_CAUSAL",
        "SMALL_UP_REANCHOR_BREAK",
        "DUAL_SCALE_LONG_ALIGNMENT",
        "NOT_Q3_OR_EXHAUSTED",
        "EPISODE_STOP_CAUSAL",
    },
    "BEAR_REVERSAL_LEFT_RIGHT": {
        "ACTIVE_LARGE_BEAR_ANCHOR",
        "LARGE_BEAR_DOW_DEFENSE_CAUSAL",
        "BEAR_LATE_STAGE_EVIDENCE",
        "LEFT_RIGHT_PHASE_MAPPED",
        "DUAL_SCALE_SEPARATED",
        "PHASE_STOP_CAUSAL",
    },
    "FRESH_Q1_EXPANSION": {
        "FRESH_UP_ANCHOR",
        "CLEAN_MEATY_DESTRUCTIVE_TRACEABLE",
        "DYNAMIC_Q1_EXPANSION",
        "EARLY_TAIJI_GENERATION",
        "MACD_SUPPORT_ONLY",
        "EARLY_LOCATION_WITH_SPACE",
        "FRESH_ANCHOR_STOP_CAUSAL",
    },
}

EXPECTED_V3_GUARDS = {
    "ACTIVE_WATCHLIST",
    "DATA_SUFFICIENT_FOR_ROUTE",
    "TRIGGER_COMPLETED",
    "CAUSAL_EPISODE_STOP",
    "CAMPAIGN_STOP_CAUSAL",
    "NOT_Q3",
    "NOT_LATE_OR_EXHAUSTED",
    "NO_SCALE_DIRECTION_CONFLICT",
    "NOT_SINGLE_INDICATOR_SIGNAL",
    "RISK_EXECUTABLE",
    "NO_V2_FAIL",
}


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(_schema(), format_checker=jsonschema.FormatChecker())


def _verdict(result: str = "PASS", evidence_ref: str = "BAR:2023-06-01") -> dict[str, Any]:
    if result == "PASS":
        return {
            "result": "PASS",
            "supporting_evidence_refs": [evidence_ref],
            "contradicting_evidence_refs": [],
            "missing_evidence_codes": [],
            "reason_code": "VISIBLE_EVIDENCE_SATISFIES_DEFINITION",
        }
    if result == "FAIL":
        return {
            "result": "FAIL",
            "supporting_evidence_refs": [],
            "contradicting_evidence_refs": [evidence_ref],
            "missing_evidence_codes": [],
            "reason_code": "VISIBLE_EVIDENCE_CONTRADICTS_DEFINITION",
        }
    return {
        "result": "UNKNOWN",
        "supporting_evidence_refs": [evidence_ref],
        "contradicting_evidence_refs": [],
        "missing_evidence_codes": ["MISSING_CONFIRMED_ENDPOINT"],
        "reason_code": "REQUIRED_EVIDENCE_MISSING",
    }


def _answer_object(group: str) -> dict[str, Any]:
    return {question_id: _verdict() for question_id in _schema()["x-question-groups"][group]}


def _manifest() -> dict[str, Any]:
    schema = _schema()
    manifest = {
        "review_id": "D-0123456789abcdef01234567",
        "anonymous_stock_id": "S-0123456789abcdef",
        "as_of": "2023-06-01",
        "input_packet_sha256": "1" * 64,
        "question_manifest_sha256": "2" * 64,
        "evidence_catalog_sha256": "3" * 64,
        "anchor_candidates": [
            {"subject_ref": ref, "required_question_ids": list(schema["x-question-groups"]["anchor"])}
            for ref in ("ANCHOR_CANDIDATE:A-001", "ANCHOR_CANDIDATE:A-002")
        ],
        "relation_candidates": [
            {
                "subject_ref": "RELATION_CANDIDATE:R-001",
                "required_question_ids": list(schema["x-question-groups"]["relation"]),
            }
        ],
        "stop_candidates": [
            {
                "subject_ref": "STOP_CANDIDATE:T-001",
                "required_question_ids": list(schema["x-question-groups"]["stop"]),
            }
        ],
        "global_question_ids": list(schema["x-question-groups"]["global"]),
        "evidence_refs": {"BAR:2023-06-01", "PIVOT:SMALL:LOW:2023-05-29:2023-06-01"},
    }
    return manifest


def _payload() -> dict[str, Any]:
    manifest = _manifest()
    return {
        "schema_version": "hybrid-atomic-semantics-v2",
        "protocol_version": "hybrid-monitoring-protocol-v2",
        "contract_status": "FINAL",
        "review_id": manifest["review_id"],
        "anonymous_stock_id": manifest["anonymous_stock_id"],
        "as_of": manifest["as_of"],
        "input_packet_sha256": manifest["input_packet_sha256"],
        "question_manifest_sha256": manifest["question_manifest_sha256"],
        "evidence_catalog_sha256": manifest["evidence_catalog_sha256"],
        "candidate_answers": {
            "anchor_candidates": [
                {
                    "subject_ref": entry["subject_ref"],
                    "answers": {
                        question_id: _verdict() for question_id in entry["required_question_ids"]
                    },
                }
                for entry in manifest["anchor_candidates"]
            ],
            "relation_candidates": [
                {
                    "subject_ref": entry["subject_ref"],
                    "answers": {
                        question_id: _verdict() for question_id in entry["required_question_ids"]
                    },
                }
                for entry in manifest["relation_candidates"]
            ],
            "stop_candidates": [
                {
                    "subject_ref": entry["subject_ref"],
                    "answers": {
                        question_id: _verdict() for question_id in entry["required_question_ids"]
                    },
                }
                for entry in manifest["stop_candidates"]
            ],
        },
        "global_answers": {
            question_id: _verdict() for question_id in manifest["global_question_ids"]
        },
        "causal_attestation": {
            "latest_visible_bar": manifest["as_of"],
            "used_future_data": False,
            "identity_visible": False,
            "performance_visible": False,
            "invented_evidence_ref": False,
            "invented_candidate_ref": False,
            "answered_complete_manifest": True,
            "selected_scenario": False,
            "selected_phase": False,
            "selected_route": False,
            "decided_permission": False,
            "issued_trade_instruction": False,
        },
    }


def _iter_verdicts(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for group in ("anchor_candidates", "relation_candidates", "stop_candidates"):
        for row in payload["candidate_answers"][group]:
            yield from row["answers"].values()
    yield from payload["global_answers"].values()


def _cross_document_errors(payload: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in (
        "review_id",
        "anonymous_stock_id",
        "as_of",
        "input_packet_sha256",
        "question_manifest_sha256",
        "evidence_catalog_sha256",
    ):
        if payload.get(field) != manifest[field]:
            errors.append(f"{field} does not match input manifest")

    for output_group in ("anchor_candidates", "relation_candidates", "stop_candidates"):
        actual = [row["subject_ref"] for row in payload["candidate_answers"][output_group]]
        expected = [entry["subject_ref"] for entry in manifest[output_group]]
        if len(actual) != len(set(actual)) or set(actual) != set(expected):
            errors.append(f"{output_group} must answer every manifest candidate exactly once")
        required_by_subject = {
            entry["subject_ref"]: set(entry["required_question_ids"])
            for entry in manifest[output_group]
        }
        for row in payload["candidate_answers"][output_group]:
            if set(row["answers"]) != required_by_subject.get(row["subject_ref"], set()):
                errors.append(f"{output_group} must answer exact subject question subset")

    if set(payload["global_answers"]) != set(manifest["global_question_ids"]):
        errors.append("global_answers must answer exact manifest subset")

    evidence_catalog = set(manifest["evidence_refs"])
    for verdict in _iter_verdicts(payload):
        supporting = set(verdict["supporting_evidence_refs"])
        contradicting = set(verdict["contradicting_evidence_refs"])
        if not supporting.union(contradicting).issubset(evidence_catalog):
            errors.append("answer contains evidence ref absent from input catalog")
        if supporting.intersection(contradicting):
            errors.append("same evidence ref cannot support and contradict one answer")

    if payload["causal_attestation"]["latest_visible_bar"] != manifest["as_of"]:
        errors.append("latest_visible_bar must equal as_of")
    return errors


def test_schema_is_final_and_accepts_complete_payload() -> None:
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["x-schema-version"] == "hybrid-atomic-semantics-v2"
    assert schema["x-contract-status"] == "FINAL"
    assert not list(_validator().iter_errors(_payload()))
    assert _cross_document_errors(_payload(), _manifest()) == []


def test_schema_declares_one_strict_builder_packet_contract() -> None:
    contract = _schema()["x-input-packet-contract"]
    assert contract["status"] == "FINAL"
    assert contract["additional_top_level_allowed"] is False
    assert set(contract["required_top_level"]) == {
        "review_id", "anonymous_stock_id", "as_of", "input_packet_sha256",
        "question_manifest_sha256", "evidence_catalog_sha256", "question_manifest",
        "evidence", "objective_facts",
    }
    assert set(contract["question_manifest_required"]) == {
        "anchor_candidates", "relation_candidates", "stop_candidates", "global_question_ids",
    }
    assert set(contract["scenario_hypotheses_scenarios"]) == set(EXPECTED_SCENARIO_GATES)
    assert set(contract["scenario_hypothesis_required"]) == {
        "hypothesis_id", "anchor_ref", "relation_ref", "episode_stop_ref",
        "campaign_stop_ref", "position_role", "taiji_generation",
        "same_direction_attack_number", "completed_prior_copy_count",
    }
    assert {"program_verdicts", "scenario_subjects", "data_sufficient_for_route"} == set(
        contract["forbidden_objective_fields"]
    )
    assert set(contract["objective_facts_required"]) == {
        "data_sufficiency_by_route", "working_comparison_windows", "scenario_hypotheses",
    }
    route_data = _schema()["x-route-data-sufficiency"]
    assert route_data["classification"] == "OPERATIONAL_MONITORING_MINIMUM_NOT_COURSE_CLAIM"
    assert route_data["required_visible_bars"] == {
        "MATURE_TREND_PULLBACK": 750,
        "MACRO_COPY_RESONANCE": 750,
        "BEAR_REVERSAL_LEFT_RIGHT": 750,
        "FRESH_Q1_EXPANSION": 200,
    }
    assert _schema()["x-working-comparison-windows"]["required_scales"] == ["LARGE", "SMALL"]


def test_strategy_v1_v2_v3_files_are_unchanged() -> None:
    actual = {
        relative_path: hashlib.sha256((ROOT / relative_path).read_bytes()).hexdigest()
        for relative_path in PROTECTED_STRATEGY_HASHES
    }
    assert actual == PROTECTED_STRATEGY_HASHES


def test_question_groups_are_complete_and_schema_allows_manifest_subsets() -> None:
    schema = _schema()
    defs_by_group = {
        "anchor": "anchorAnswers",
        "relation": "relationAnswers",
        "stop": "stopAnswers",
        "global": "globalAnswers",
    }
    for group, definition in defs_by_group.items():
        declared = set(schema["x-question-groups"][group])
        assert declared == set(schema["$defs"][definition]["properties"])
        assert "required" not in schema["$defs"][definition]


def test_hypothesis_specific_taiji_location_exhaustion_and_bear_atoms_are_relation_bound() -> None:
    schema = _schema()
    relation = set(schema["x-question-groups"]["relation"])
    global_questions = set(schema["x-question-groups"]["global"])
    bound = {
        "TAIJI_RELATION_TRACEABLE",
        "TAIJI_CURRENT_LEG_INDEPENDENT",
        "LOCATION_REMAINING_SPACE_ADEQUATE",
        "LOCATION_NOT_EXTENDED_FROM_ORIGIN",
        "EXH_ATTACK_SHORTENING",
        "EXH_SLOPE_DECAY",
        "EXH_PRICE_VOLUME_DIVERGENCE",
        "EXH_FAILED_CONTINUATION",
        "EXH_TIME_SPACE_EXHAUSTION",
        "BEAR_ATTACK_IS_LATE_STAGE",
        "BEAR_LATE_STAGE_PARTIAL",
    }
    assert bound <= relation
    assert bound.isdisjoint(global_questions)
    assert "minItems" not in schema["properties"]["candidate_answers"]["properties"]["relation_candidates"]


def test_four_scenario_matrix_and_v3_guards_are_complete() -> None:
    schema = _schema()
    matrix = schema["x-four-scenario-gate-matrix"]
    assert set(matrix) == set(EXPECTED_SCENARIO_GATES)
    for scenario, gates in EXPECTED_SCENARIO_GATES.items():
        assert set(matrix[scenario]) == gates

    assert set(schema["x-v3-common-hard-guards"]) == EXPECTED_V3_GUARDS
    atomic_ids = set().union(*(set(rows) for rows in schema["x-question-groups"].values()))
    sources = [source for gates in matrix.values() for values in gates.values() for source in values]
    sources += [source for values in schema["x-v3-common-hard-guards"].values() for source in values]
    assert all(source.startswith("PROGRAM:") or source in atomic_ids for source in sources)
    consumed_atomic_ids = {source for source in sources if not source.startswith("PROGRAM:")}
    assert consumed_atomic_ids == atomic_ids, "required AI questions must have a gate/guard consumer"
    assert set(schema["x-critical-question-ids"]) == atomic_ids
    assert len(schema["x-critical-question-ids"]) == len(atomic_ids)


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "primary_scenario",
        "alternative_scenarios",
        "left_right_phase",
        "stage_location",
        "route",
        "permission",
        "triggered",
        "trade_plan",
        "position_role",
        "buy",
        "sell",
        "add",
        "exit",
    ],
)
def test_forbidden_policy_fields_are_rejected(forbidden_field: str) -> None:
    payload = _payload()
    payload[forbidden_field] = "FORBIDDEN"
    assert list(_validator().iter_errors(payload))


def test_nested_policy_field_is_rejected() -> None:
    payload = _payload()
    payload["global_answers"]["SIGNAL_HAS_INDEPENDENT_STRUCTURE"]["route"] = "V2_CORE"
    assert list(_validator().iter_errors(payload))


def test_every_manifest_candidate_and_every_atomic_question_are_required() -> None:
    payload = _payload()
    payload["candidate_answers"]["anchor_candidates"].pop()
    assert _cross_document_errors(payload, _manifest())

    payload = _payload()
    del payload["candidate_answers"]["anchor_candidates"][0]["answers"]["ANCHOR_CLEAN"]
    assert _cross_document_errors(payload, _manifest())


def test_schema_accepts_allowed_question_subset_but_cross_contract_rejects_extra_or_missing() -> None:
    manifest = _manifest()
    manifest["anchor_candidates"][0]["required_question_ids"] = ["ANCHOR_CLEAN"]
    payload = _payload()
    payload["candidate_answers"]["anchor_candidates"][0]["answers"] = {
        "ANCHOR_CLEAN": _verdict()
    }
    assert not list(_validator().iter_errors(payload))
    assert _cross_document_errors(payload, manifest) == []

    payload["candidate_answers"]["anchor_candidates"][0]["answers"]["ANCHOR_MEATY"] = _verdict()
    assert _cross_document_errors(payload, manifest)


def test_evidence_refs_must_exist_in_input_catalog_and_not_have_dual_role() -> None:
    payload = _payload()
    verdict = payload["global_answers"]["SIGNAL_HAS_INDEPENDENT_STRUCTURE"]
    verdict["supporting_evidence_refs"] = ["BAR:2099-01-01"]
    assert any("absent" in error for error in _cross_document_errors(payload, _manifest()))

    payload = _payload()
    verdict = payload["global_answers"]["SIGNAL_HAS_INDEPENDENT_STRUCTURE"]
    verdict["result"] = "UNKNOWN"
    verdict["missing_evidence_codes"] = ["CONFLICTING_VISIBLE_EVIDENCE"]
    verdict["reason_code"] = "VISIBLE_EVIDENCE_CONFLICTS"
    verdict["contradicting_evidence_refs"] = list(verdict["supporting_evidence_refs"])
    assert any("support and contradict" in error for error in _cross_document_errors(payload, _manifest()))


@pytest.mark.parametrize("result", ["PASS", "FAIL", "UNKNOWN"])
def test_result_specific_evidence_contract_is_enforced(result: str) -> None:
    payload = _payload()
    verdict = payload["global_answers"]["SIGNAL_HAS_INDEPENDENT_STRUCTURE"]
    verdict.clear()
    verdict.update(_verdict(result))
    if result == "PASS":
        verdict["supporting_evidence_refs"] = []
    elif result == "FAIL":
        verdict["contradicting_evidence_refs"] = []
    else:
        verdict["missing_evidence_codes"] = []
    assert list(_validator().iter_errors(payload))


def test_future_identity_performance_and_invented_refs_are_forbidden() -> None:
    for field in (
        "used_future_data",
        "identity_visible",
        "performance_visible",
        "invented_evidence_ref",
        "invented_candidate_ref",
        "selected_scenario",
        "selected_phase",
        "selected_route",
        "decided_permission",
        "issued_trade_instruction",
    ):
        payload = _payload()
        payload["causal_attestation"][field] = True
        assert list(_validator().iter_errors(payload)), field


def test_prompt_is_final_and_covers_every_atomic_question_and_safety_boundary() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    assert "狀態：`FINAL`" in prompt
    assert "hybrid-semantic-prompt-v2-draft" not in prompt
    assert "hybrid-atomic-semantics-v2-draft" not in prompt
    assert "不得使用 `as_of` 之後" in prompt
    assert "不得輸出或選擇 `primary_scenario`" in prompt
    assert "PASS／FAIL／UNKNOWN" in prompt
    for question_ids in _schema()["x-question-groups"].values():
        for question_id in question_ids:
            assert question_id in prompt
