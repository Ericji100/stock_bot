"""R6A transport-only revision for the two-stage legacy lifecycle runner.

R6A preserves the R6 prompts, local schemas, validators, truth tables, packet
matrix, model, and effort.  It only replaces the strict transport-schema
adapter so no `$ref` node receives forbidden sibling validation keywords.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Any, Iterator

from scripts import v2_core_legacy_lifecycle_runner_v6 as r6
from scripts import v2_core_stage_b1a_codex_runner_v1 as base


RUNNER_VERSION = "v2-core-legacy-lifecycle-runner-r6a-candidate"
MODEL = r6.MODEL
REASONING = r6.REASONING
RunnerError = r6.RunnerError


def _fixed_refs(packet: dict[str, Any]) -> list[str]:
    refs: set[str] = set()
    for row in packet.get("candidate_evidence_options", []):
        if isinstance(row, dict):
            refs.update(str(ref) for ref in row.get("source_evidence_refs", []))
    for row in packet.get("proxy_evidence", []):
        if isinstance(row, dict) and isinstance(row.get("ref"), str):
            refs.add(row["ref"])
    return sorted(refs)


def assert_no_ref_siblings(value: Any, *, path: str = "$") -> None:
    if isinstance(value, list):
        for index, child in enumerate(value):
            assert_no_ref_siblings(child, path=f"{path}/{index}")
        return
    if not isinstance(value, dict):
        return
    if "$ref" in value and set(value) != {"$ref"}:
        raise RunnerError(
            f"transport schema contains forbidden $ref siblings at {path}: "
            + ",".join(sorted(set(value) - {"$ref"}))
        )
    for key, child in value.items():
        assert_no_ref_siblings(child, path=f"{path}/{key}")


def transport_schema(schema: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
    transport = base.strict_transport_schema(schema, evidence_ref_values=_fixed_refs(packet))
    candidate_ids = sorted(str(row["candidate_id"]) for row in packet["candidate_pool"])
    option_ids = sorted(
        str(row["evidence_option_id"]) for row in packet["candidate_evidence_options"]
    )
    evidence_refs = _fixed_refs(packet)

    def constrain(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                constrain(child)
            return
        if not isinstance(value, dict):
            return
        properties = value.get("properties")
        if isinstance(properties, dict):
            for name in list(properties):
                if name == "alternative_candidate_ids":
                    properties[name] = {
                        "type": "array",
                        "items": {"type": "string", "enum": candidate_ids},
                        "minItems": 0,
                        "maxItems": 2,
                    }
                elif name.endswith("candidate_id"):
                    properties[name] = {
                        "type": "string",
                        "enum": [*candidate_ids, "NONE"],
                    }
                elif name.endswith("evidence_option_id"):
                    properties[name] = {
                        "type": "string",
                        "enum": [*option_ids, "NONE"],
                    }
                elif name == "supporting_packet_evidence_refs":
                    properties[name] = {
                        "type": "array",
                        "items": {"type": "string", "enum": evidence_refs},
                        "minItems": 0,
                        "maxItems": 8,
                    }
        for child in value.values():
            constrain(child)

    constrain(transport)
    assert_no_ref_siblings(transport)
    return transport


@contextmanager
def _patched_r6_transport() -> Iterator[None]:
    original_transport = r6._transport_schema
    original_version = r6.RUNNER_VERSION
    r6._transport_schema = transport_schema
    r6.RUNNER_VERSION = RUNNER_VERSION
    try:
        yield
    finally:
        r6._transport_schema = original_transport
        r6.RUNNER_VERSION = original_version


def run_b0a_case(**kwargs: Any) -> dict[str, Any]:
    with _patched_r6_transport():
        return r6.run_b0a_case(**kwargs)


def validate_existing_b0a(**kwargs: Any) -> dict[str, Any]:
    with _patched_r6_transport():
        return r6.validate_existing_b0a(**kwargs)


def run_b0b_case(**kwargs: Any) -> dict[str, Any]:
    with _patched_r6_transport():
        return r6.run_b0b_case(**kwargs)


def validate_existing_b0b(**kwargs: Any) -> dict[str, Any]:
    with _patched_r6_transport():
        return r6.validate_existing_b0b(**kwargs)


def main() -> int:
    with _patched_r6_transport():
        return r6.main()


if __name__ == "__main__":
    raise SystemExit(main())
