"""R7B preserves one AS-OF current episode across every later role call."""

from __future__ import annotations

import json

from scripts.v2_core_legacy_role_qualification_r7 import ROLE_ATOMS
from scripts.v2_core_legacy_role_qualification_r7b import VERSION, build_schema, transport_for, validate_and_resolve
from scripts.v2_core_legacy_role_adjudication_runner_r7 import validate_existing as episode_validation
from scripts.v2_core_legacy_role_shortlist_r7 import ARTIFACT_DIR
from tests.test_v2_core_legacy_role_qualification_r7 import REVIEW_ID, answer, data


def episode_result() -> dict:
    return episode_validation(artifact_dir=ARTIFACT_DIR, review_id=REVIEW_ID, role="CURRENT_EPISODE_UP")


def bound_answer(packet: dict, shortlist: dict, role: str) -> dict:
    episode = episode_result()
    response = answer(packet, shortlist, role)
    response["schema_version"] = VERSION
    response["bound_current_episode_candidate_id"] = episode["selected_candidate_id"]
    response["bound_current_episode_evidence_option_id"] = episode["selected_evidence_option_id"]
    response["causal_attestation"]["frozen_episode_binding_used"] = True
    return response


def test_r7b_requires_immutable_episode_binding() -> None:
    packet, shortlist = data()
    episode = episode_result()
    response = bound_answer(packet, shortlist, "ACTIVE_DOWN_CONTROLLER")
    transport = transport_for(packet, shortlist, "ACTIVE_DOWN_CONTROLLER", episode)
    assert "$ref" not in json.dumps(transport)
    result = validate_and_resolve(response, packet=packet, shortlist=shortlist, role="ACTIVE_DOWN_CONTROLLER", episode_result=episode)
    assert result["status"] == "VALID"
    assert result["bound_current_episode_candidate_id"] == episode["selected_candidate_id"]
    response["bound_current_episode_candidate_id"] = "CAMSEG-fake"
    assert validate_and_resolve(response, packet=packet, shortlist=shortlist, role="ACTIVE_DOWN_CONTROLLER", episode_result=episode)["status"] == "INVALID"


def test_completed_parent_after_episode_start_cannot_pass() -> None:
    packet, shortlist = data()
    episode = episode_result()
    role = "IMMEDIATE_COMPLETED_UP_PARENT"
    response = bound_answer(packet, shortlist, role)
    late_parent = next(
        row for row in shortlist["roles"][role]["candidate_rows"]
        if row["confirmed_end_date"] >= "2023-03-20"
    )
    assessment = next(row for row in response["candidate_assessments"] if row["candidate_id"] == late_parent["candidate_id"])
    assessment["atoms"] = {atom: "PASS" for atom in ROLE_ATOMS}
    result = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=role, episode_result=episode)
    assert result["status"] == "INVALID"
    assert "parent candidate cannot finish" in result["errors"][0]


def test_early_parent_may_pass_but_is_not_program_selected_by_recency() -> None:
    packet, shortlist = data()
    episode = episode_result()
    role = "IMMEDIATE_COMPLETED_UP_PARENT"
    response = bound_answer(packet, shortlist, role)
    early_parent = next(
        row for row in shortlist["roles"][role]["candidate_rows"]
        if row["confirmed_end_date"] < "2023-03-20"
    )
    assessment = next(row for row in response["candidate_assessments"] if row["candidate_id"] == early_parent["candidate_id"])
    assessment["atoms"] = {atom: "PASS" for atom in ROLE_ATOMS}
    result = validate_and_resolve(response, packet=packet, shortlist=shortlist, role=role, episode_result=episode)
    assert result["status"] == "VALID"
    assert result["selected_candidate_id"] == early_parent["candidate_id"]


def test_episode_role_cannot_be_rejudged_in_r7b() -> None:
    packet, shortlist = data()
    try:
        build_schema(packet, shortlist, "CURRENT_EPISODE_UP", episode_result())
    except ValueError as exc:
        assert "does not rejudge" in str(exc)
    else:
        raise AssertionError("R7B must not rejudge frozen episode")
