"""R7B relation truth table uses frozen roles and cannot grant a trade."""

from __future__ import annotations

import copy
import json

from scripts.v2_core_legacy_relation_r7b import ATOMS, VERSION, role_fingerprint, transport_for, validate_and_route
from scripts.v2_core_legacy_role_cross_object_preflight_r7b import build_report
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from tests.test_v2_core_legacy_role_qualification_r7 import REVIEW_ID, data


def report_and_packet() -> tuple[dict, dict]:
    packet, _shortlist = data()
    return build_report(ARTIFACT_DIR, REVIEW_ID), packet


def answer(report: dict, packet: dict, *, dominant: str = "FRESH_ANCHOR_CONTROL") -> dict:
    ref = packet["candidate_evidence_options"][0]["source_evidence_refs"][0]
    verdicts = {atom: "FAIL" for atom in ATOMS}
    verdicts["CHALLENGER_REPLACED_DOWN_CONTROL"] = "PASS"
    verdicts["CURRENT_EPISODE_IS_FRESH_FORMING_ANCHOR"] = "PASS"
    return {
        "schema_version": VERSION,
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "bound_roles_sha256": role_fingerprint(report),
        "relationship_atoms": {
            atom: {
                "judgement": verdicts[atom],
                "supporting_evidence_refs": [ref],
                "explanation": "This relationship follows the confirmed price structure at AS-OF.",
            }
            for atom in ATOMS
        },
        "dominant_relation": {
            "value": dominant,
            "supporting_evidence_refs": [ref],
            "explanation": "The current upward challenger directly controls the new forming episode.",
        },
        "mature_lineage_proof": {
            "parent_up_candidate_id": "NONE",
            "correction_down_candidate_id": "NONE",
            "successful_copy_up_candidate_id": "NONE",
            "supporting_evidence_refs": [ref],
            "explanation": "No mature lineage proof applies to the frozen episode at this date.",
        },
        "causal_attestation": {
            "as_of_only": True,
            "no_identity": True,
            "no_legacy_answer": True,
            "no_future_performance": True,
            "frozen_roles_only": True,
            "no_scenario_or_trade_permission": True,
        },
    }


def test_fresh_route_is_program_derived_not_ai_trade_permission() -> None:
    report, packet = report_and_packet()
    response = answer(report, packet)
    transport = transport_for(packet, report)
    assert "$ref" not in json.dumps(transport)
    result = validate_and_route(response, packet=packet, report=report)
    assert result["status"] == "VALID"
    assert result["program_derived_scenario"] == "FRESH_Q1_EXPANSION"
    assert result["program_working_candidate_id"] == report["selected_role_objects"]["UP_CONTROL_CHALLENGER"]["candidate_id"]
    assert result["trade_permission_granted"] is False


def test_dominant_relation_without_required_atoms_is_unresolved() -> None:
    report, packet = report_and_packet()
    result = validate_and_route(answer(report, packet, dominant="MATURE_CAMPAIGN_PULLBACK_CONTROL"), packet=packet, report=report)
    assert result["status"] == "VALID"
    assert result["routing_status"] == "UNRESOLVED_NO_TRADE"


def test_absent_down_role_forces_down_control_fail() -> None:
    report, packet = report_and_packet()
    response = answer(report, packet)
    response["relationship_atoms"]["DOWN_CONTROLS_CURRENT_EPISODE"]["judgement"] = "PASS"
    result = validate_and_route(response, packet=packet, report=report)
    assert result["status"] == "INVALID"


def test_mature_success_requires_real_prior_lineage_candidates() -> None:
    report, packet = report_and_packet()
    report = copy.deepcopy(report)
    report["selected_role_objects"]["CONTROLLING_MATURE_CAMPAIGN"] = packet["candidate_pool"][0]
    response = answer(report, packet)
    response["relationship_atoms"]["SAME_LINEAGE_SUCCESS_PREEXISTS_CURRENT_EPISODE"]["judgement"] = "PASS"
    result = validate_and_route(response, packet=packet, report=report)
    assert result["status"] == "INVALID"
    assert "three distinct lineage candidates" in result["errors"][0]
