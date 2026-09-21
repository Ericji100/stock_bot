"""R7 ambiguity is resolved by evidenced AI semantics, never by date sorting."""

from __future__ import annotations

import json

from scripts.v2_core_legacy_role_adjudication_r7 import VERSION, transport_for, validate_and_resolve
from scripts.v2_core_legacy_role_qualification_r7 import validate_and_resolve as validate_qualification
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from tests.test_v2_core_legacy_role_qualification_r7 import REVIEW_ID, ROLE_ATOMS, answer, data


ROLE = "CURRENT_EPISODE_UP"


def ambiguous_inputs() -> tuple[dict, dict, dict]:
    packet, shortlist = data()
    response = answer(packet, shortlist, ROLE, (0, 1))
    qualification = validate_qualification(response, packet=packet, shortlist=shortlist, role=ROLE)
    assert qualification["resolution_reason"] == "MULTIPLE_NON_EQUIVALENT_ELIGIBLE_CANDIDATES"
    return packet, shortlist, qualification


def response_for(packet: dict, shortlist: dict, qualification: dict, direct_ids: set[str]) -> dict:
    rows = {row["candidate_id"]: row for row in shortlist["roles"][ROLE]["candidate_rows"]}
    return {
        "schema_version": VERSION,
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "role": ROLE,
        "priority_assessments": [
            {
                "candidate_id": candidate_id,
                "evidence_option_id": rows[candidate_id]["evidence_option_id"],
                "priority": "DIRECT_ROLE_CONTROLLER" if candidate_id in direct_ids else "BROADER_BACKGROUND_STRUCTURE",
                "supporting_evidence_refs": [rows[candidate_id]["source_evidence_refs"][0]],
                "comparative_reason": "This structure is directly relevant to the current episode at AS-OF.",
            }
            for candidate_id in qualification["eligible_candidate_ids"]
        ],
        "causal_attestation": {
            "as_of_only": True,
            "no_identity": True,
            "no_legacy_answer": True,
            "no_future_performance": True,
            "eligible_candidates_only": True,
            "no_scenario_or_trade_permission": True,
        },
    }


def test_one_evidenced_direct_controller_selected() -> None:
    packet, shortlist, qualification = ambiguous_inputs()
    chosen = qualification["eligible_candidate_ids"][1]
    response = response_for(packet, shortlist, qualification, {chosen})
    transport = transport_for(packet, shortlist, ROLE, qualification)
    assert "$ref" not in json.dumps(transport)
    result = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=ROLE, qualification=qualification)
    assert result["status"] == "VALID"
    assert result["selected_candidate_id"] == chosen
    assert result["trade_permission_granted"] is False


def test_two_direct_controllers_remain_unresolved() -> None:
    packet, shortlist, qualification = ambiguous_inputs()
    response = response_for(packet, shortlist, qualification, set(qualification["eligible_candidate_ids"]))
    result = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=ROLE, qualification=qualification)
    assert result["status"] == "VALID"
    assert result["role_status"] == "UNRESOLVED"


def test_candidate_option_swap_invalid() -> None:
    packet, shortlist, qualification = ambiguous_inputs()
    response = response_for(packet, shortlist, qualification, set())
    response["priority_assessments"][0]["evidence_option_id"] = response["priority_assessments"][1]["evidence_option_id"]
    result = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=ROLE, qualification=qualification)
    assert result["status"] == "INVALID"


def test_only_non_equivalent_multi_eligible_cases_can_request_adjudication() -> None:
    packet, shortlist = data()
    unique = validate_qualification(answer(packet, shortlist, ROLE, (0,)), packet=packet, shortlist=shortlist, role=ROLE)
    try:
        transport_for(packet, shortlist, ROLE, unique)
    except ValueError as exc:
        assert "only for non-equivalent" in str(exc)
    else:
        raise AssertionError("unique candidate must not enter adjudication")
