"""R7C transport-only revision: fixed candidate IDs are required object keys.

The AS-OF facts, episode binding, four course atoms and deterministic role
resolution are inherited unchanged from R7B.  Only cardinality representation
changes, preventing repeated/omitted candidate rows in structured output.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from scripts import v2_core_legacy_role_qualification_r7 as r7
from scripts import v2_core_legacy_role_qualification_r7b as r7b


VERSION = "v2-core-legacy-role-qualification-r7c-candidate-r1"


def build_schema(packet: dict[str, Any], shortlist: dict[str, Any], role: str, episode_result: dict[str, Any]) -> dict[str, Any]:
    schema = r7b.build_schema(packet, shortlist, role, episode_result)
    schema["properties"]["schema_version"]["const"] = VERSION
    rows = shortlist["roles"][role]["candidate_rows"]
    array_item = schema["properties"]["candidate_assessments"]["items"]
    properties: dict[str, Any] = {}
    for row in rows:
        item = json.loads(json.dumps(array_item))
        item["required"].remove("candidate_id")
        del item["properties"]["candidate_id"]
        item["properties"]["evidence_option_id"] = {"type": "string", "const": row["evidence_option_id"]}
        properties[row["candidate_id"]] = item
    schema["properties"]["candidate_assessments"] = {
        "type": "object", "additionalProperties": False,
        "required": sorted(properties), "properties": properties,
    }
    return schema


def validate_and_resolve(
    response: dict[str, Any], *, packet: dict[str, Any], shortlist: dict[str, Any],
    role: str, episode_result: dict[str, Any],
) -> dict[str, Any]:
    schema = build_schema(packet, shortlist, role, episode_result)
    issues = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda e: list(map(str, e.path)))
    if issues:
        return {"status": "INVALID", "errors": [issue.message for issue in issues[:20]]}
    converted = json.loads(json.dumps(response))
    converted["schema_version"] = r7b.VERSION
    converted["candidate_assessments"] = [
        {"candidate_id": candidate_id, **converted["candidate_assessments"][candidate_id]}
        for candidate_id in sorted(converted["candidate_assessments"])
    ]
    result = r7b.validate_and_resolve(
        converted, packet=packet, shortlist=shortlist, role=role,
        episode_result=episode_result,
    )
    if result["status"] == "VALID":
        result["transport_version"] = VERSION
    return result


def transport_for(packet: dict[str, Any], shortlist: dict[str, Any], role: str, episode_result: dict[str, Any]) -> dict[str, Any]:
    return r7.transport_schema(build_schema(packet, shortlist, role, episode_result))


def parse_raw_without_duplicate_keys(raw_bytes: bytes) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    parsed = json.loads(raw_bytes.decode("utf-8-sig"), object_pairs_hook=unique_pairs)
    if not isinstance(parsed, dict):
        raise ValueError("R7C root output must be a JSON object")
    return parsed
