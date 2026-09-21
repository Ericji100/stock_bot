"""R7 atomic candidate assessment and deterministic role resolution."""

from __future__ import annotations

import json

from scripts.v2_core_legacy_role_qualification_r7 import (
    ROLE_ATOMS,
    VERSION,
    build_schema,
    transport_schema,
    validate_and_resolve,
)
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR


REVIEW_ID = "FP-18b86f08f05563ff5886097b"


def data() -> tuple[dict, dict]:
    packet_path = ARTIFACT_DIR / "legacy_anchor_alignment_input_packets_candidate_r1" / f"{REVIEW_ID}.json"
    shortlist_path = ARTIFACT_DIR / "legacy_role_shortlists_candidate_r7_v2" / f"{REVIEW_ID}.json"
    return json.loads(packet_path.read_text(encoding="utf-8")), json.loads(shortlist_path.read_text(encoding="utf-8"))


def answer(packet: dict, shortlist: dict, role: str, eligible_indexes: tuple[int, ...] = ()) -> dict:
    rows = shortlist["roles"][role]["candidate_rows"]
    return {
        "schema_version": VERSION,
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "role": role,
        "candidate_assessments": [
            {
                "candidate_id": row["candidate_id"],
                "evidence_option_id": row["evidence_option_id"],
                "atoms": {atom: "PASS" if index in eligible_indexes else "FAIL" for atom in ROLE_ATOMS},
                "supporting_evidence_refs": [row["source_evidence_refs"][0]],
                "explanation": "Price structure supports this candidate assessment.",
            }
            for index, row in enumerate(rows)
        ],
        "non_applicability": {
            "judgement": "FAIL", "supporting_evidence_refs": [rows[0]["source_evidence_refs"][0]],
            "explanation": "A relevant role may exist in the visible history.",
        },
        "cross_basis_comparison": "Both basis families were evaluated independently where available.",
        "causal_attestation": {
            "as_of_only": True,
            "no_identity": True,
            "no_legacy_answer": True,
            "no_future_performance": True,
            "fixed_candidates_only": True,
            "no_scenario_or_trade_permission": True,
        },
    }


def test_unique_eligible_candidate_selected_without_scenario_or_trade() -> None:
    packet, shortlist = data()
    role = "CURRENT_EPISODE_UP"
    response = answer(packet, shortlist, role, (2,))
    schema = build_schema(packet, shortlist, role)
    assert schema["properties"]["candidate_assessments"]["minItems"] == len(response["candidate_assessments"])
    result = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=role)
    assert result["status"] == "VALID"
    assert result["role_status"] == "SELECTED"
    assert result["selected_candidate_id"] == response["candidate_assessments"][2]["candidate_id"]
    assert result["trade_permission_granted"] is False


def test_duplicate_candidate_invalid_even_when_array_length_is_correct() -> None:
    packet, shortlist = data()
    role = "CURRENT_EPISODE_UP"
    response = answer(packet, shortlist, role)
    response["candidate_assessments"][1] = response["candidate_assessments"][0].copy()
    result = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=role)
    assert result["status"] == "INVALID"
    assert any("duplicate assessment" in error for error in result["errors"])


def test_candidate_option_pair_cannot_be_swapped() -> None:
    packet, shortlist = data()
    role = "CURRENT_EPISODE_UP"
    response = answer(packet, shortlist, role)
    response["candidate_assessments"][0]["evidence_option_id"] = response["candidate_assessments"][1]["evidence_option_id"]
    result = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=role)
    assert result["status"] == "INVALID"
    assert any("candidate/evidence option mismatch" in error for error in result["errors"])


def test_multiple_non_equivalent_eligible_candidates_unresolved() -> None:
    packet, shortlist = data()
    role = "CURRENT_EPISODE_UP"
    response = answer(packet, shortlist, role, (0, 1))
    result = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=role)
    assert result["status"] == "VALID"
    assert result["role_status"] == "UNRESOLVED"
    assert result["resolution_reason"] == "MULTIPLE_NON_EQUIVALENT_ELIGIBLE_CANDIDATES"


def test_optional_role_requires_positive_non_applicability_evidence() -> None:
    packet, shortlist = data()
    role = "IMMEDIATE_COMPLETED_UP_PARENT"
    response = answer(packet, shortlist, role)
    assert validate_and_resolve(response, packet=packet, shortlist=shortlist, role=role)["role_status"] == "UNRESOLVED"
    response["non_applicability"]["judgement"] = "PASS"
    assert validate_and_resolve(response, packet=packet, shortlist=shortlist, role=role)["role_status"] == "NOT_APPLICABLE"


def test_episode_role_cannot_be_marked_not_applicable() -> None:
    packet, shortlist = data()
    role = "CURRENT_EPISODE_UP"
    response = answer(packet, shortlist, role)
    response["non_applicability"]["judgement"] = "PASS"
    assert validate_and_resolve(response, packet=packet, shortlist=shortlist, role=role)["status"] == "INVALID"


def test_all_generated_shortlists_have_valid_transport_schema() -> None:
    packet, shortlist = data()
    for role in shortlist["roles"]:
        schema = build_schema(packet, shortlist, role)
        transport = transport_schema(schema)
        assert "$ref" not in json.dumps(transport)
        assert "uniqueItems" not in json.dumps(transport)
        assert set(transport["properties"]) == set(transport["required"])
        assert "scenario" not in schema["properties"]
        assert "trade_permission" not in schema["properties"]
