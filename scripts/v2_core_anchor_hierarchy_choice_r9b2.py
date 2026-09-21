"""Independent R9B2 transport contract for repeated evidence references.

R9B's local schema disallowed duplicate array items although its transport
schema did not. R9B2 preserves the AI's raw answer and treats repeated refs
as one piece of evidence only in a *verification copy*. It does not change
anchor roles, course atoms, the R9B prompt, or any historical R9B result.
"""

from __future__ import annotations

import copy
from typing import Any

from jsonschema import Draft202012Validator

from scripts import v2_core_anchor_hierarchy_choice_r9 as r9b
from scripts.v2_core_legacy_role_qualification_r7 import transport_schema


VERSION = "v2-core-anchor-hierarchy-choice-r9b2-candidate-r1"
parse_raw_without_duplicate_keys = r9b.parse_raw_without_duplicate_keys


def build_schema(
    packet: dict[str, Any], defense: dict[str, Any], defense_id: str,
    retrieval: dict[str, Any],
) -> dict[str, Any]:
    schema = copy.deepcopy(r9b.build_schema(packet, defense, defense_id, retrieval))
    schema["properties"]["schema_version"]["const"] = VERSION
    for assessment in schema["properties"]["representative_assessments"]["properties"].values():
        assessment["properties"]["supporting_evidence_refs"].pop("uniqueItems", None)
    return schema


def validate_and_choose(
    response: dict[str, Any], *, packet: dict[str, Any],
    defense: dict[str, Any], defense_id: str, retrieval: dict[str, Any],
) -> dict[str, Any]:
    schema = build_schema(packet, defense, defense_id, retrieval)
    issues = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda error: list(map(str, error.path)))
    if issues:
        return {"status": "INVALID", "errors": [issue.message for issue in issues[:20]]}

    verification_copy = copy.deepcopy(response)
    verification_copy["schema_version"] = r9b.VERSION
    duplicate_count = 0
    for item in verification_copy["representative_assessments"].values():
        refs = item["supporting_evidence_refs"]
        unique = list(dict.fromkeys(refs))
        duplicate_count += len(refs) - len(unique)
        item["supporting_evidence_refs"] = unique
    result = r9b.validate_and_choose(
        verification_copy, packet=packet, defense=defense,
        defense_id=defense_id, retrieval=retrieval,
    )
    if result["status"] == "VALID":
        result["duplicate_evidence_reference_count"] = duplicate_count
        result["raw_response_unchanged"] = True
        result["repeated_refs_count_as_distinct_evidence"] = False
    return result


def transport_for(
    packet: dict[str, Any], defense: dict[str, Any], defense_id: str,
    retrieval: dict[str, Any],
) -> dict[str, Any]:
    return transport_schema(build_schema(packet, defense, defense_id, retrieval))
