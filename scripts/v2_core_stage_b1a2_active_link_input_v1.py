"""Build fixed-path inputs for the Stage B1a2 active-campaign-link probe."""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_objective_candidate_catalog_v1 import (
    ARTIFACT_DIR,
    canonical_bytes,
    sha256_path,
    write_new_or_identical,
)
from scripts.v2_core_stage_b1a1e_primary_gate_runner_v1 import validate_existing_artifacts
from scripts.v2_core_stage_b1a1e_primary_gate_validator_v1 import load_json


PACKET_VERSION = "v2-core-stage-b1a2-active-link-input-r1-candidate"
SOURCE_EXECUTION_MANIFEST = "stage_b1a1e_primary_gate_execution_manifest_candidate_r4.json"
SOURCE_FINAL_REPORT = "stage_b1a1e_primary_gate_report_candidate_r4.json"
SOURCE_B1A1_MANIFEST = "stage_b1a1_input_manifest_candidate_r2.json"
SOURCE_B1A1_DIRECTORY = "stage_b1a1_input_packets_candidate_r2"
OUTPUT_DIRECTORY = "stage_b1a2_active_link_input_packets_candidate_r1"
OUTPUT_MANIFEST = "stage_b1a2_active_link_input_manifest_candidate_r1.json"
MAX_PATH_EDGES = 4
MAX_PATHS_PER_CANDIDATE = 4


def stable_id(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "RLEVO-" + hashlib.sha256(f"{PACKET_VERSION}|{payload}".encode("utf-8")).hexdigest()[:20]


def relation_nodes(row: dict[str, Any]) -> tuple[str, str]:
    if row["relation_basis"] == "SHARED_BOUNDARY":
        return str(row["left_segment_id"]), str(row["right_segment_id"])
    return str(row["outer_segment_id"]), str(row["inner_segment_id"])


def compact_segment(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "candidate_id", "basis", "scale", "direction", "status", "start_date",
            "start_price", "confirmed_end_date", "confirmed_end_price", "observed_through",
            "observed_end_price", "source_evidence_refs", "objective_metrics",
        )
    }


def shortest_paths(
    *, start: str, terminals: set[str], relations: list[dict[str, Any]], max_edges: int
) -> list[dict[str, Any]]:
    adjacency: dict[str, list[tuple[str, str]]] = {}
    relation_map = {str(row["candidate_id"]): row for row in relations}
    for row in relations:
        left, right = relation_nodes(row)
        adjacency.setdefault(left, []).append((right, str(row["candidate_id"])))
        adjacency.setdefault(right, []).append((left, str(row["candidate_id"])))
    for values in adjacency.values():
        values.sort(key=lambda value: (value[1], value[0]))

    found: list[tuple[list[str], list[str]]] = []
    queue: deque[tuple[str, list[str], list[str]]] = deque([(start, [start], [])])
    while queue:
        node, nodes, edges = queue.popleft()
        if node in terminals and (node != start or not edges):
            found.append((nodes, edges))
            if len(found) >= MAX_PATHS_PER_CANDIDATE:
                break
        if len(edges) >= max_edges:
            continue
        for next_node, relation_id in adjacency.get(node, []):
            if next_node in nodes:
                continue
            queue.append((next_node, nodes + [next_node], edges + [relation_id]))
    result = []
    for nodes, edge_ids in found:
        result.append(
            {
                "terminal_segment_id": nodes[-1],
                "segment_ids": nodes,
                "relation_ids": edge_ids,
                "relations": [relation_map[value] for value in edge_ids],
            }
        )
    return result


def _r4_validations(artifact_dir: Path, manifest: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
    review_id = str(row["review_id"])
    validations = []
    for round_number in range(1, int(manifest["required_rounds"]) + 1):
        run_dir = artifact_dir / manifest["run_directory"] / f"round_{round_number}"
        validations.append(
            validate_existing_artifacts(
                input_packet_path=artifact_dir / manifest["input_packet_directory"] / row["input_packet_file"],
                program_gate_path=artifact_dir / manifest["program_gate_directory"] / row["program_gate_file"],
                input_manifest_path=artifact_dir / manifest["input_manifest_file"],
                schema_path=artifact_dir / manifest["schema_file"],
                prompt_path=artifact_dir / manifest["prompt_file"],
                output_path=run_dir / "stage_b1a1e_primary_gate" / f"{review_id}.json",
                receipt_path=run_dir / "receipts" / f"{review_id}.stage_b1a1e_primary_gate.json",
            )
        )
    sets = {tuple(value["partial_eligible_ids"]) for value in validations}
    if len(sets) != 1:
        raise ValueError(f"R4 eligible set is not stable for {review_id}")
    return validations


def build_packet(
    *, source: dict[str, Any], review_id: str, as_of: str, eligible_ids: list[str],
    upstream_report_sha256: str,
) -> dict[str, Any]:
    segments = {str(row["candidate_id"]): row for row in source["focus_segments"]}
    relations = list(source["focus_relations"])
    if not set(eligible_ids).issubset(segments):
        raise ValueError(f"eligible segment missing from B1a1 packet: {review_id}")
    terminals = {
        candidate_id for candidate_id, row in segments.items()
        if row["status"] == "FORMING" and row["observed_through"] == as_of
    }
    if not terminals:
        raise ValueError(f"no AS-OF forming terminal for {review_id}")

    options = []
    used_segments: set[str] = set(eligible_ids) | terminals
    used_relations: set[str] = set()
    used_refs: set[str] = set()
    for candidate_id in sorted(eligible_ids):
        paths = shortest_paths(
            start=candidate_id,
            terminals=terminals,
            relations=relations,
            max_edges=MAX_PATH_EDGES,
        )
        for path in paths:
            used_segments.update(path["segment_ids"])
            used_relations.update(path["relation_ids"])
        path_signature = [
            {"segment_ids": value["segment_ids"], "relation_ids": value["relation_ids"]}
            for value in paths
        ]
        option_id = stable_id(review_id, candidate_id, path_signature)
        options.append(
            {
                "evidence_option_id": option_id,
                "candidate_id": candidate_id,
                "atom_name": "active_campaign_link",
                "fixed_relation_paths": paths,
                "current_context_segment_ids": sorted(terminals),
                "path_limit_edges": MAX_PATH_EDGES,
                "no_fixed_path_found": not bool(paths),
                "not_course_judgement": True,
            }
        )
    for segment_id in used_segments:
        used_refs.update(str(value) for value in segments[segment_id]["source_evidence_refs"])
    evidence_catalog = [row for row in source["evidence_catalog"] if row["ref"] in used_refs]
    return {
        "packet_version": PACKET_VERSION,
        "status": "CANDIDATE_INPUT_NOT_FORMALLY_RUN（候選輸入、尚未正式執行）",
        "task": "Judge only whether each fixed upstream candidate remains linked to the AS-OF active campaign chain.",
        "review_id": review_id,
        "anonymous_stock_id": source["anonymous_stock_id"],
        "as_of": as_of,
        "data_quality": source["data_quality"],
        "daily_structure_context_to_as_of": source["daily_structure_context_to_as_of"],
        "upstream_partial_eligible_candidate_ids": sorted(eligible_ids),
        "candidate_segments": [compact_segment(segments[value]) for value in sorted(eligible_ids)],
        "current_context_segments": [compact_segment(segments[value]) for value in sorted(terminals)],
        "path_context_segments": [compact_segment(segments[value]) for value in sorted(used_segments)],
        "path_context_relations": [
            row for row in relations if str(row["candidate_id"]) in used_relations
        ],
        "role_link_evidence_options": options,
        "evidence_catalog": evidence_catalog,
        "source_bindings": {
            "upstream_r4_report_sha256": upstream_report_sha256,
            "source_b1a1_packet_sha256": source["source_bindings"]["source_packet_sha256"],
        },
        "review_constraints": {
            "future_performance_blind": True,
            "identity_blind": True,
            "sealed_labels_blind": True,
            "prior_role_answers_hidden": True,
            "fixed_candidate_ids_only": True,
            "fixed_relation_paths_only": True,
            "role_assignment_forbidden": True,
            "scenario_trigger_stop_permission_forbidden": True,
            "formal_model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
        },
    }


def generate_all(artifact_dir: Path = ARTIFACT_DIR) -> dict[str, Any]:
    execution_path = artifact_dir / SOURCE_EXECUTION_MANIFEST
    report_path = artifact_dir / SOURCE_FINAL_REPORT
    execution = load_json(execution_path)
    report = load_json(report_path)
    if report.get("status") != "SMOKE_PASSED（Smoke通過）" or report.get("completed_case_rounds") != report.get("expected_case_rounds"):
        raise ValueError("upstream R4 final report is not passed and complete")
    b1a1_manifest = load_json(artifact_dir / SOURCE_B1A1_MANIFEST)
    b1a1_rows = {str(row["review_id"]): row for row in b1a1_manifest["rows"]}
    output_dir = artifact_dir / OUTPUT_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    total_candidates = total_paths = 0
    for row in execution["rows"]:
        review_id = str(row["review_id"])
        validations = _r4_validations(artifact_dir, execution, row)
        eligible_ids = list(validations[0]["partial_eligible_ids"])
        source_row = b1a1_rows[review_id]
        source_path = artifact_dir / SOURCE_B1A1_DIRECTORY / source_row["input_packet_file"]
        if sha256_path(source_path) != source_row["input_packet_sha256"]:
            raise ValueError(f"B1a1 source hash mismatch for {review_id}")
        packet = build_packet(
            source=load_json(source_path),
            review_id=review_id,
            as_of=str(row["as_of"]),
            eligible_ids=eligible_ids,
            upstream_report_sha256=sha256_path(report_path),
        )
        output_path = output_dir / f"{review_id}.json"
        write_new_or_identical(output_path, canonical_bytes(packet))
        path_count = sum(len(value["fixed_relation_paths"]) for value in packet["role_link_evidence_options"])
        total_candidates += len(eligible_ids)
        total_paths += path_count
        rows.append(
            {
                "review_id": review_id,
                "as_of": row["as_of"],
                "input_packet_file": output_path.name,
                "input_packet_sha256": sha256_path(output_path),
                "candidate_count": len(eligible_ids),
                "fixed_path_count": path_count,
                "source_b1a1_packet_sha256": sha256_path(source_path),
            }
        )
    manifest = {
        "manifest_version": "v2-core-stage-b1a2-active-link-input-r1-candidate",
        "status": "CANDIDATE_INPUTS_COMPLETE_NOT_READY_FOR_AI（候選輸入完成、尚不可執行AI）",
        "case_count": len(rows),
        "candidate_count": total_candidates,
        "fixed_path_count": total_paths,
        "formal_ai_calls": 0,
        "ready_for_formal_ai": False,
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "source_r4_execution_manifest_file": SOURCE_EXECUTION_MANIFEST,
        "source_r4_execution_manifest_sha256": sha256_path(execution_path),
        "source_r4_final_report_file": SOURCE_FINAL_REPORT,
        "source_r4_final_report_sha256": sha256_path(report_path),
        "input_packet_directory": OUTPUT_DIRECTORY,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "prior_role_answers_used": False,
        "course_role_generated_by_program": False,
        "rows": rows,
    }
    write_new_or_identical(artifact_dir / OUTPUT_MANIFEST, canonical_bytes(manifest))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    print(json.dumps(generate_all(args.artifact_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
