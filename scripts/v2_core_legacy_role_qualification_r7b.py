"""R7B qualification: every non-episode role is bound to one frozen UP episode."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from scripts import v2_core_legacy_role_qualification_r7 as r7


VERSION = "v2-core-legacy-role-qualification-r7b-candidate-r1"


def episode_object(shortlist: dict[str, Any], episode_result: dict[str, Any]) -> dict[str, Any]:
    if episode_result["role"] != "CURRENT_EPISODE_UP" or episode_result["role_status"] != "SELECTED":
        raise ValueError("R7B requires a unique frozen current episode")
    candidate_id = episode_result["selected_candidate_id"]
    matches = [
        row for row in shortlist["roles"]["CURRENT_EPISODE_UP"]["candidate_rows"]
        if row["candidate_id"] == candidate_id
    ]
    if len(matches) != 1 or matches[0]["evidence_option_id"] != episode_result["selected_evidence_option_id"]:
        raise ValueError("frozen current episode binding mismatch")
    if matches[0]["direction"] != "UP" or matches[0]["status"] != "FORMING":
        raise ValueError("current episode must be forming UP")
    return matches[0]


def build_schema(packet: dict[str, Any], shortlist: dict[str, Any], role: str, episode_result: dict[str, Any]) -> dict[str, Any]:
    if role == "CURRENT_EPISODE_UP":
        raise ValueError("R7B does not rejudge the frozen current episode")
    episode = episode_object(shortlist, episode_result)
    schema = r7.build_schema(packet, shortlist, role)
    schema["properties"]["schema_version"]["const"] = VERSION
    schema["properties"]["bound_current_episode_candidate_id"] = {"type": "string", "const": episode["candidate_id"]}
    schema["properties"]["bound_current_episode_evidence_option_id"] = {"type": "string", "const": episode["evidence_option_id"]}
    schema["required"].extend(["bound_current_episode_candidate_id", "bound_current_episode_evidence_option_id"])
    attestation = schema["properties"]["causal_attestation"]
    attestation["properties"]["frozen_episode_binding_used"] = {"type": "boolean", "const": True}
    attestation["required"].append("frozen_episode_binding_used")
    return schema


def validate_and_resolve(
    response: dict[str, Any],
    *,
    packet: dict[str, Any],
    shortlist: dict[str, Any],
    role: str,
    episode_result: dict[str, Any],
) -> dict[str, Any]:
    schema = build_schema(packet, shortlist, role, episode_result)
    issues = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda e: list(map(str, e.path)))
    if issues:
        return {"status": "INVALID", "errors": [issue.message for issue in issues[:20]]}
    episode = episode_object(shortlist, episode_result)
    if role == "IMMEDIATE_COMPLETED_UP_PARENT":
        candidates = {row["candidate_id"]: row for row in shortlist["roles"][role]["candidate_rows"]}
        for assessment in response["candidate_assessments"]:
            if all(assessment["atoms"][atom] == "PASS" for atom in r7.ROLE_ATOMS):
                parent = candidates[assessment["candidate_id"]]
                if not parent["confirmed_end_date"] or parent["confirmed_end_date"] >= episode["start_date"]:
                    return {
                        "status": "INVALID",
                        "errors": [f"parent candidate cannot finish on/after bound episode begins: {parent['candidate_id']}"],
                    }
    base_response = json.loads(json.dumps(response))
    base_response["schema_version"] = r7.VERSION
    del base_response["bound_current_episode_candidate_id"]
    del base_response["bound_current_episode_evidence_option_id"]
    del base_response["causal_attestation"]["frozen_episode_binding_used"]
    resolved = r7.validate_and_resolve(base_response, packet=packet, shortlist=shortlist, role=role)
    if resolved["status"] == "VALID":
        resolved["bound_current_episode_candidate_id"] = episode["candidate_id"]
        resolved["bound_current_episode_evidence_option_id"] = episode["evidence_option_id"]
    return resolved


def transport_for(packet: dict[str, Any], shortlist: dict[str, Any], role: str, episode_result: dict[str, Any]) -> dict[str, Any]:
    return r7.transport_schema(build_schema(packet, shortlist, role, episode_result))
