from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)
SCHEMA_PATH = REPORT_DIR / "v2_core_stage_b1a.schema.candidate_r1.json"
PROMPT_PATH = REPORT_DIR / "v2_core_stage_b1a.prompt.candidate_r1.md"
SCRIPT_PATH = ROOT / "scripts" / "v2_core_stage_b1a_candidate_validator_v1.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("stage_b1a_validator", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _atomic(result: str, ref: str = "PRICE:2023-01-02") -> dict:
    if result == "PASS":
        return {
            "result": "PASS",
            "supporting_evidence_refs": [ref],
            "contradicting_evidence_refs": [],
            "missing_evidence_codes": [],
            "reason_code": "VISIBLE_EVIDENCE_SATISFIES_DEFINITION",
        }
    if result == "FAIL":
        return {
            "result": "FAIL",
            "supporting_evidence_refs": [],
            "contradicting_evidence_refs": [ref],
            "missing_evidence_codes": [],
            "reason_code": "VISIBLE_EVIDENCE_CONTRADICTS_DEFINITION",
        }
    return {
        "result": "UNKNOWN",
        "supporting_evidence_refs": [],
        "contradicting_evidence_refs": [],
        "missing_evidence_codes": ["OTHER_REQUIRED_EVIDENCE"],
        "reason_code": "REQUIRED_EVIDENCE_MISSING",
    }


def _fixtures():
    segment_a = "SEG-" + "a" * 20
    segment_b = "SEG-" + "b" * 20
    focus = {
        "review_id": "FP-test",
        "as_of": "2023-01-02",
        "source_segment_catalog_sha256": "not-checked-without-path",
        "focus_segments": [
            {"segment_id": segment_a, "selection_reasons": ["TEST"]},
            {"segment_id": segment_b, "selection_reasons": ["TEST"]},
        ],
    }
    candidates = {
        "review_id": "FP-test",
        "as_of": "2023-01-02",
        "source_packet_sha256": "not-checked-without-path",
        "objective_segment_candidates": [
            {"candidate_id": segment_a, "status": "CONFIRMED", "source_evidence_refs": []},
            {"candidate_id": segment_b, "status": "FORMING", "source_evidence_refs": []},
        ],
    }
    packet = {
        "review_id": "FP-test",
        "as_of": "2023-01-02",
        "evidence_catalog": [{"ref": "PRICE:2023-01-02"}],
    }
    response = {
        "schema_version": "v2-core-stage-b1a-r1-candidate",
        "segment_assessments": [
            {
                "candidate_id": segment_a,
                "coarse_anchor_fit": _atomic("PASS"),
                "as_of_structural_relevance": _atomic("PASS"),
                "role_assignability": _atomic("PASS"),
                "role_candidates": ["PARENT", "CONTROLLING"],
            },
            {
                "candidate_id": segment_b,
                "coarse_anchor_fit": _atomic("UNKNOWN"),
                "as_of_structural_relevance": _atomic("PASS"),
                "role_assignability": _atomic("UNKNOWN"),
                "role_candidates": [],
            },
        ],
    }
    return focus, candidates, packet, response


def test_schema_is_valid_draft_2020_12():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)


def test_prompt_forbids_free_dates_prices_and_trade_decision():
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    assert "不得自行新增線段、日期、價格" in prompt
    assert "TRADE／WAIT／REMOVE" in prompt
    assert "每個候選恰好出現一次" in prompt


def test_valid_response_and_deep_pool_are_deterministic():
    module = _load_module()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    focus, candidates, packet, response = _fixtures()
    first = module.validate_stage_b1a_response(
        response=response,
        schema=schema,
        focus=focus,
        candidates=candidates,
        packet=packet,
    )
    second = module.validate_stage_b1a_response(
        response=response,
        schema=schema,
        focus=focus,
        candidates=candidates,
        packet=packet,
    )
    assert first == second
    assert first["status"] == "VALID"
    assert first["deep_review_pool_ids"] == ["SEG-" + "a" * 20]
    assert first["validator_contract"]["course_judgement_generated_by_program"] is False


def test_missing_extra_duplicate_and_unknown_evidence_are_rejected():
    module = _load_module()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    focus, candidates, packet, response = _fixtures()
    duplicate = dict(response["segment_assessments"][0])
    duplicate["candidate_id"] = "SEG-" + "c" * 20
    duplicate["coarse_anchor_fit"] = _atomic("PASS", "PRICE:2099-01-01")
    response["segment_assessments"] = [response["segment_assessments"][0], duplicate, duplicate]
    report = module.validate_stage_b1a_response(
        response=response,
        schema=schema,
        focus=focus,
        candidates=candidates,
        packet=packet,
    )
    joined = "\n".join(report["errors"])
    assert report["status"] == "INVALID"
    assert "DUPLICATE_CANDIDATE_IDS" in joined
    assert "MISSING_CANDIDATE_IDS" in joined
    assert "EXTRA_CANDIDATE_IDS" in joined
    assert "UNKNOWN_EVIDENCE_REFS" in joined


def test_forming_segment_cannot_be_parent():
    module = _load_module()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    focus, candidates, packet, response = _fixtures()
    item = response["segment_assessments"][1]
    item["coarse_anchor_fit"] = _atomic("PASS")
    item["as_of_structural_relevance"] = _atomic("PASS")
    item["role_assignability"] = _atomic("PASS")
    item["role_candidates"] = ["PARENT"]
    report = module.validate_stage_b1a_response(
        response=response,
        schema=schema,
        focus=focus,
        candidates=candidates,
        packet=packet,
    )
    assert report["status"] == "INVALID"
    assert any("UNCONFIRMED_SEGMENT_AS_PARENT" in item for item in report["errors"])


def test_role_pass_requires_both_prior_atoms_pass():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    _, _, _, response = _fixtures()
    item = response["segment_assessments"][0]
    item["coarse_anchor_fit"] = _atomic("UNKNOWN")
    errors = list(Draft202012Validator(schema).iter_errors(response))
    assert errors
