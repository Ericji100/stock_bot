"""R9D transport repair: duplicate evidence references are legal, not extra votes.

R9C semantic gates, signal legality and prompts remain unchanged.  The strict
transport omits JSON Schema uniqueItems, so R9C rejected an otherwise parseable
answer containing repeated refs.  R9D accepts repeats without changing the raw
AI answer or counting a repeated ref as distinct evidence.
"""

from __future__ import annotations

import copy
from typing import Any

from jsonschema import Draft202012Validator

from scripts import v2_core_trigger_judgement_r9 as r9c
from scripts.v2_core_legacy_role_qualification_r7 import transport_schema


VERSION = "v2-core-trigger-judgement-r9d-candidate-r1"
SCENARIO_BY_RELATION = r9c.SCENARIO_BY_RELATION
GATES = r9c.GATES
TRIGGER_PATHS = r9c.TRIGGER_PATHS
confirmed_high_options = r9c.confirmed_high_options
parse_raw_without_duplicate_keys = r9c.parse_raw_without_duplicate_keys


def build_schema(packet: dict[str, Any], defense_id: str, working_id: str, scenario: str) -> dict[str, Any]:
    schema = copy.deepcopy(r9c.build_schema(packet, defense_id, working_id, scenario))
    schema["properties"]["schema_version"]["const"] = VERSION
    for gate in GATES[scenario]:
        schema["properties"]["gate_assessments"]["properties"][gate]["properties"]["supporting_evidence_refs"].pop("uniqueItems", None)
    schema["properties"]["trigger_evidence_refs"].pop("uniqueItems", None)
    return schema


def validate_and_gate(
    response: dict[str, Any], *, packet: dict[str, Any], defense_id: str,
    working_id: str, scenario: str,
) -> dict[str, Any]:
    schema = build_schema(packet, defense_id, working_id, scenario)
    issues = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda error: list(map(str, error.path)))
    if issues:
        return {"status": "INVALID", "errors": [issue.message for issue in issues[:20]]}
    # This copy is solely for reusing the frozen R9C semantic legality gate.
    # The original response is written to raw/normalized artifacts unchanged.
    verification_copy = copy.deepcopy(response)
    verification_copy["schema_version"] = r9c.VERSION
    duplicate_count = 0
    for gate in GATES[scenario]:
        refs = verification_copy["gate_assessments"][gate]["supporting_evidence_refs"]
        unique = list(dict.fromkeys(refs))
        duplicate_count += len(refs) - len(unique)
        verification_copy["gate_assessments"][gate]["supporting_evidence_refs"] = unique
    refs = verification_copy["trigger_evidence_refs"]
    unique = list(dict.fromkeys(refs))
    duplicate_count += len(refs) - len(unique)
    verification_copy["trigger_evidence_refs"] = unique
    result = r9c.validate_and_gate(
        verification_copy, packet=packet, defense_id=defense_id,
        working_id=working_id, scenario=scenario,
    )
    if result["status"] == "VALID":
        result["duplicate_evidence_reference_count"] = duplicate_count
        result["raw_response_unchanged"] = True
    return result


def transport_for(packet: dict[str, Any], defense_id: str, working_id: str, scenario: str) -> dict[str, Any]:
    return transport_schema(build_schema(packet, defense_id, working_id, scenario))
