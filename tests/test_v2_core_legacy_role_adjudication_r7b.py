"""R7B adjudication cannot silently switch the fixed episode."""

from __future__ import annotations

from scripts.v2_core_legacy_role_adjudication_r7b import VERSION, transport_for, validate_and_resolve
from scripts.v2_core_legacy_role_qualification_runner_r7 import _bound_inputs
from scripts.v2_core_legacy_role_qualification_runner_r7b import frozen_episode, validate_existing as validate_qualification
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR


REVIEW_ID = "FP-38178c2820dd71aa800fd8d7"
ROLE = "UP_CONTROL_CHALLENGER"


def context() -> tuple[dict, dict, dict, dict]:
    _manifest, _row, packet, shortlist = _bound_inputs(ARTIFACT_DIR, REVIEW_ID, ROLE)
    episode, _paths = frozen_episode(ARTIFACT_DIR, REVIEW_ID)
    qualification = validate_qualification(artifact_dir=ARTIFACT_DIR, review_id=REVIEW_ID, role=ROLE)
    return packet, shortlist, qualification, episode


def answer(packet: dict, shortlist: dict, qualification: dict, episode: dict) -> dict:
    rows = {row["candidate_id"]: row for row in shortlist["roles"][ROLE]["candidate_rows"]}
    eligible = qualification["eligible_candidate_ids"]
    return {
        "schema_version": VERSION,
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "role": ROLE,
        "bound_current_episode_candidate_id": episode["selected_candidate_id"],
        "bound_current_episode_evidence_option_id": episode["selected_evidence_option_id"],
        "priority_assessments": [
            {
                "candidate_id": candidate_id,
                "evidence_option_id": rows[candidate_id]["evidence_option_id"],
                "priority": "DIRECT_ROLE_CONTROLLER" if candidate_id == eligible[0] else "BROADER_BACKGROUND_STRUCTURE",
                "supporting_evidence_refs": [rows[candidate_id]["source_evidence_refs"][0]],
                "comparative_reason": "This candidate has the stronger direct role relation to the frozen episode.",
            }
            for candidate_id in eligible
        ],
        "causal_attestation": {
            "as_of_only": True,
            "no_identity": True,
            "no_legacy_answer": True,
            "no_future_performance": True,
            "eligible_candidates_only": True,
            "no_scenario_or_trade_permission": True,
            "frozen_episode_binding_used": True,
        },
    }


def test_bound_adjudication_can_select_one_of_three_without_trade() -> None:
    packet, shortlist, qualification, episode = context()
    response = answer(packet, shortlist, qualification, episode)
    assert transport_for(packet, shortlist, ROLE, qualification, episode)
    result = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=ROLE, qualification=qualification, episode_result=episode)
    assert result["status"] == "VALID"
    assert result["selected_candidate_id"] == qualification["eligible_candidate_ids"][0]
    assert result["bound_current_episode_candidate_id"] == episode["selected_candidate_id"]
    assert result["trade_permission_granted"] is False


def test_episode_switch_is_invalid() -> None:
    packet, shortlist, qualification, episode = context()
    response = answer(packet, shortlist, qualification, episode)
    response["bound_current_episode_candidate_id"] = "CAMSEG-fake"
    result = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=ROLE, qualification=qualification, episode_result=episode)
    assert result["status"] == "INVALID"
