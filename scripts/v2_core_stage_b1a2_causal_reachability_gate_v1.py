"""Apply the deterministic B1a2 R5 causal-reachability gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_objective_candidate_catalog_v1 import canonical_bytes, sha256_path
from scripts.v2_core_stage_b1a2_active_link_validator_v1 import load_json


ALLOWED_RELATION_BASES = {"SHARED_BOUNDARY", "INTERVAL_CONTAINS"}


def _validate_path(candidate_id: str, context_ids: set[str], segment_basis: dict[str, str], path_limit_edges: int, path: dict[str, Any]) -> list[str]:
    errors = []
    relation_ids = list(path["relation_ids"])
    relations = list(path["relations"])
    segment_ids = list(path["segment_ids"])
    if not segment_ids or segment_ids[0] != candidate_id:
        errors.append("PATH_START_MISMATCH")
    if path["terminal_segment_id"] not in context_ids or segment_ids[-1] != path["terminal_segment_id"]:
        errors.append("PATH_TERMINAL_MISMATCH")
    if len(relation_ids) != len(relations) or len(segment_ids) != len(relations) + 1:
        errors.append("PATH_LENGTH_MISMATCH")
    if len(relation_ids) > path_limit_edges:
        errors.append("PATH_LIMIT_EXCEEDED")
    if any(segment_basis.get(segment_id) == "AUXILIARY" for segment_id in segment_ids):
        errors.append("AUXILIARY_SEGMENT_IN_PATH")
    for index, (relation_id, relation) in enumerate(zip(relation_ids, relations)):
        if relation_id != relation["candidate_id"]:
            errors.append("RELATION_ID_MISMATCH")
        if relation["relation_basis"] not in ALLOWED_RELATION_BASES:
            errors.append("FORBIDDEN_RELATION_BASIS")
            continue
        left, right = segment_ids[index], segment_ids[index + 1]
        if relation["relation_basis"] == "SHARED_BOUNDARY":
            if relation["left_segment_id"] != left or relation["right_segment_id"] != right:
                errors.append("SHARED_BOUNDARY_NOT_FORWARD")
        else:
            endpoints = {str(relation["outer_segment_id"]), str(relation["inner_segment_id"])}
            if endpoints != {left, right}:
                errors.append("CONTAINMENT_ENDPOINT_MISMATCH")
    return errors


def evaluate_packet(packet: dict[str, Any]) -> dict[str, Any]:
    context_ids = {str(value["candidate_id"]) for value in packet["current_context_segments"]}
    segment_basis = {
        str(value["candidate_id"]): str(value["basis"])
        for value in [*packet["candidate_segments"], *packet["current_context_segments"]]
    }
    upstream_ids = {str(value) for value in packet["upstream_partial_eligible_candidate_ids"]}
    options = {str(row["candidate_id"]): row for row in packet["role_link_evidence_options"]}
    errors = []
    if set(options) != upstream_ids:
        errors.append("UPSTREAM_OPTION_SET_MISMATCH")
    rows = []
    for candidate_id in sorted(upstream_ids):
        option = options[candidate_id]
        paths = list(option["fixed_relation_paths"])
        no_path = bool(option["no_fixed_path_found"])
        if no_path == bool(paths):
            errors.append(f"PATH_FLAG_MISMATCH:{candidate_id}")
        path_errors = []
        for path in paths:
            path_errors.extend(
                _validate_path(
                    candidate_id,
                    context_ids,
                    segment_basis,
                    int(option["path_limit_edges"]),
                    path,
                )
            )
        errors.extend(f"{error}:{candidate_id}" for error in path_errors)
        retain = bool(paths) and not no_path and not path_errors
        rows.append(
            {
                "candidate_id": candidate_id,
                "causal_path_count": len(paths),
                "min_fixed_path_edges": min((len(path["relation_ids"]) for path in paths), default=None),
                "max_fixed_path_edges": max((len(path["relation_ids"]) for path in paths), default=None),
                "r5_gate": "RETAIN_FOR_ROLE_AI" if retain else "DROP_NO_CAUSAL_PATH",
            }
        )
    return {
        "review_id": packet["review_id"],
        "as_of": packet["as_of"],
        "candidate_count": len(rows),
        "retained_candidate_ids": [row["candidate_id"] for row in rows if row["r5_gate"] == "RETAIN_FOR_ROLE_AI"],
        "dropped_candidate_ids": [row["candidate_id"] for row in rows if row["r5_gate"] == "DROP_NO_CAUSAL_PATH"],
        "candidate_results": rows,
        "errors": sorted(errors),
    }


def evaluate_manifests(*, artifact_dir: Path, manifest_paths: list[Path]) -> dict[str, Any]:
    cases = []
    source_bindings = []
    for manifest_path in manifest_paths:
        manifest = load_json(manifest_path)
        source_bindings.append({"manifest_file": manifest_path.name, "manifest_sha256": sha256_path(manifest_path)})
        for row in manifest["rows"]:
            packet_path = artifact_dir / manifest["input_packet_directory"] / row["input_packet_file"]
            if sha256_path(packet_path) != row["input_packet_sha256"]:
                raise ValueError(f"input packet hash mismatch: {packet_path}")
            result = evaluate_packet(load_json(packet_path))
            result["source_manifest_file"] = manifest_path.name
            result["input_packet_file"] = row["input_packet_file"]
            result["input_packet_sha256"] = row["input_packet_sha256"]
            cases.append(result)
    error_count = sum(len(case["errors"]) for case in cases)
    candidate_count = sum(case["candidate_count"] for case in cases)
    retained_count = sum(len(case["retained_candidate_ids"]) for case in cases)
    dropped_count = sum(len(case["dropped_candidate_ids"]) for case in cases)
    return {
        "gate_version": "v2-core-stage-b1a2-causal-reachability-r5",
        "status": "PASS（通過）" if error_count == 0 else "INVALID（資料無效）",
        "source_bindings": source_bindings,
        "case_count": len(cases),
        "candidate_count": candidate_count,
        "retained_for_role_ai_count": retained_count,
        "dropped_no_causal_path_count": dropped_count,
        "contract_error_count": error_count,
        "cases": cases,
        "course_role_assigned": False,
        "controlling_anchor_decided": False,
        "trade_permission_granted": False,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "prior_ai_active_link_answers_used": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--input-manifest", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate_manifests(artifact_dir=args.artifact_dir, manifest_paths=args.input_manifest)
    payload = canonical_bytes(report)
    if args.output.exists() and args.output.read_bytes() != payload:
        raise RuntimeError(f"refusing to overwrite changed versioned artifact: {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
