"""R7B relative role adjudication bound to the frozen current episode."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from scripts import v2_core_legacy_role_adjudication_r7 as r7
from scripts.v2_core_legacy_role_qualification_r7b import episode_object


VERSION = "v2-core-legacy-role-adjudication-r7b-candidate-r1"


def build_schema(
    packet: dict[str, Any], shortlist: dict[str, Any], role: str,
    qualification: dict[str, Any], episode_result: dict[str, Any],
) -> dict[str, Any]:
    episode = episode_object(shortlist, episode_result)
    if qualification.get("bound_current_episode_candidate_id") != episode["candidate_id"]:
        raise ValueError("R7B qualification and frozen episode differ")
    schema = r7.build_schema(packet, shortlist, role, qualification)
    schema["properties"]["schema_version"]["const"] = VERSION
    schema["properties"]["bound_current_episode_candidate_id"] = {"type": "string", "const": episode["candidate_id"]}
    schema["properties"]["bound_current_episode_evidence_option_id"] = {"type": "string", "const": episode["evidence_option_id"]}
    schema["required"].extend(("bound_current_episode_candidate_id", "bound_current_episode_evidence_option_id"))
    attestation = schema["properties"]["causal_attestation"]
    attestation["properties"]["frozen_episode_binding_used"] = {"type": "boolean", "const": True}
    attestation["required"].append("frozen_episode_binding_used")
    return schema


def validate_and_resolve(
    response: dict[str, Any], *, packet: dict[str, Any], shortlist: dict[str, Any],
    role: str, qualification: dict[str, Any], episode_result: dict[str, Any],
) -> dict[str, Any]:
    schema = build_schema(packet, shortlist, role, qualification, episode_result)
    issues = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda e: list(map(str, e.path)))
    if issues:
        return {"status": "INVALID", "errors": [issue.message for issue in issues[:20]]}
    episode = episode_object(shortlist, episode_result)
    base_response = json.loads(json.dumps(response))
    base_response["schema_version"] = r7.VERSION
    del base_response["bound_current_episode_candidate_id"]
    del base_response["bound_current_episode_evidence_option_id"]
    del base_response["causal_attestation"]["frozen_episode_binding_used"]
    result = r7.validate_and_resolve(base_response, packet=packet, shortlist=shortlist, role=role, qualification=qualification)
    if result["status"] != "VALID":
        return result
    selected = result["selected_candidate_id"]
    if role == "IMMEDIATE_COMPLETED_UP_PARENT" and selected:
        parent = next(row for row in shortlist["roles"][role]["candidate_rows"] if row["candidate_id"] == selected)
        if not parent["confirmed_end_date"] or parent["confirmed_end_date"] >= episode["start_date"]:
            return {"status": "INVALID", "errors": ["adjudicated parent ends on/after frozen episode starts"]}
    result["bound_current_episode_candidate_id"] = episode["candidate_id"]
    result["bound_current_episode_evidence_option_id"] = episode["evidence_option_id"]
    return result


def transport_for(
    packet: dict[str, Any], shortlist: dict[str, Any], role: str,
    qualification: dict[str, Any], episode_result: dict[str, Any],
) -> dict[str, Any]:
    return r7.transport_schema(build_schema(packet, shortlist, role, qualification, episode_result))
