"""Build causally directed Stage B1a2 active-link R2 calibration inputs."""

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

from scripts.v2_core_objective_candidate_catalog_v1 import ARTIFACT_DIR, canonical_bytes, sha256_path, write_new_or_identical
from scripts.v2_core_stage_b1a1e_primary_gate_validator_v1 import load_json
from scripts.v2_core_stage_b1a2_active_link_input_v1 import (
    SOURCE_B1A1_DIRECTORY, SOURCE_B1A1_MANIFEST, _r4_validations, compact_segment,
)

PACKET_VERSION = "v2-core-stage-b1a2-active-link-input-r2-candidate"
SOURCES = (
    ("stage_b1a1e_primary_gate_execution_manifest_candidate_r4.json", "stage_b1a1e_primary_gate_report_candidate_r4.json", None),
    ("stage_b1a1e_primary_gate_validation_execution_manifest_r1.json", "stage_b1a1e_primary_gate_validation_report_r1.json", "FP-d79297cd1f01c1600d27cb75"),
)
OUTPUT_DIRECTORY = "stage_b1a2_active_link_input_packets_candidate_r2"
OUTPUT_MANIFEST = "stage_b1a2_active_link_input_manifest_candidate_r2.json"
MAX_PATH_EDGES = 4
MAX_PATHS_PER_CANDIDATE = 4


def stable_id(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "RLEVO-" + hashlib.sha256(f"{PACKET_VERSION}|{payload}".encode()).hexdigest()[:20]


def causal_paths(start: str, terminals: set[str], segments: dict[str, dict[str, Any]], relations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    adjacency: dict[str, list[tuple[str, str]]] = {}
    relation_map = {str(row["candidate_id"]): row for row in relations}
    for row in relations:
        if row["relation_basis"] == "SHARED_BOUNDARY":
            pairs = [(str(row["left_segment_id"]), str(row["right_segment_id"]))]
        else:
            outer, inner = str(row["outer_segment_id"]), str(row["inner_segment_id"])
            pairs = [(outer, inner), (inner, outer)]
        for left, right in pairs:
            if segments[left]["scale"] == "AUXILIARY" or segments[right]["scale"] == "AUXILIARY":
                continue
            adjacency.setdefault(left, []).append((right, str(row["candidate_id"])))
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
        if len(edges) >= MAX_PATH_EDGES:
            continue
        for next_node, relation_id in adjacency.get(node, []):
            if next_node not in nodes:
                queue.append((next_node, nodes + [next_node], edges + [relation_id]))
    return [
        {
            "terminal_segment_id": nodes[-1],
            "segment_ids": nodes,
            "relation_ids": edge_ids,
            "relations": [relation_map[value] for value in edge_ids],
        }
        for nodes, edge_ids in found
    ]


def build_packet(source: dict[str, Any], review_id: str, as_of: str, eligible_ids: list[str], report_sha: str) -> dict[str, Any]:
    segments = {str(row["candidate_id"]): row for row in source["focus_segments"]}
    relations = list(source["focus_relations"])
    terminals = {cid for cid, row in segments.items() if row["scale"] in {"LARGE", "SMALL"} and row["status"] == "FORMING" and row["observed_through"] == as_of}
    options, used_segments, used_relations, used_refs = [], set(eligible_ids) | terminals, set(), set()
    for candidate_id in sorted(eligible_ids):
        paths = causal_paths(candidate_id, terminals, segments, relations)
        for path in paths:
            used_segments.update(path["segment_ids"]); used_relations.update(path["relation_ids"])
        signature = [{"segment_ids": p["segment_ids"], "relation_ids": p["relation_ids"]} for p in paths]
        options.append({
            "evidence_option_id": stable_id(review_id, candidate_id, signature),
            "candidate_id": candidate_id,
            "atom_name": "active_campaign_link",
            "fixed_relation_paths": paths,
            "current_context_segment_ids": sorted(terminals),
            "path_limit_edges": MAX_PATH_EDGES,
            "no_fixed_path_found": not bool(paths),
            "causal_path_policy": "FORWARD_SHARED_BOUNDARY_AND_NON_AUXILIARY_CONTAINMENT_R2",
            "not_course_judgement": True,
        })
    for segment_id in used_segments:
        used_refs.update(str(v) for v in segments[segment_id]["source_evidence_refs"])
    return {
        "packet_version": PACKET_VERSION,
        "status": "CANDIDATE_INPUT_NOT_FORMALLY_RUN（候選輸入、尚未正式執行）",
        "task": "Judge only whether each fixed upstream candidate remains linked through a causally directed path to the AS-OF active campaign chain.",
        "review_id": review_id,
        "anonymous_stock_id": source["anonymous_stock_id"],
        "as_of": as_of,
        "data_quality": source["data_quality"],
        "daily_structure_context_to_as_of": source["daily_structure_context_to_as_of"],
        "upstream_partial_eligible_candidate_ids": sorted(eligible_ids),
        "candidate_segments": [compact_segment(segments[v]) for v in sorted(eligible_ids)],
        "current_context_segments": [compact_segment(segments[v]) for v in sorted(terminals)],
        "path_context_segments": [compact_segment(segments[v]) for v in sorted(used_segments)],
        "path_context_relations": [r for r in relations if str(r["candidate_id"]) in used_relations],
        "role_link_evidence_options": options,
        "evidence_catalog": [r for r in source["evidence_catalog"] if r["ref"] in used_refs],
        "source_bindings": {"upstream_r4_report_sha256": report_sha, "source_b1a1_packet_sha256": source["source_bindings"]["source_packet_sha256"]},
        "review_constraints": {
            "future_performance_blind": True, "identity_blind": True, "sealed_labels_blind": True,
            "prior_role_answers_hidden": True, "fixed_candidate_ids_only": True,
            "fixed_relation_paths_only": True, "backward_shared_boundary_paths_forbidden": True,
            "auxiliary_path_nodes_forbidden": True, "role_assignment_forbidden": True,
            "scenario_trigger_stop_permission_forbidden": True,
            "formal_model": "gpt-5.6-sol", "reasoning_effort": "xhigh",
        },
    }


def generate_all(artifact_dir: Path = ARTIFACT_DIR) -> dict[str, Any]:
    b1a1 = load_json(artifact_dir / SOURCE_B1A1_MANIFEST)
    b1a1_rows = {str(r["review_id"]): r for r in b1a1["rows"]}
    output_dir = artifact_dir / OUTPUT_DIRECTORY; output_dir.mkdir(parents=True, exist_ok=True)
    rows, seen, source_bindings = [], set(), []
    totals = {"candidate_count": 0, "fixed_path_count": 0}
    for manifest_name, report_name, only_review in SOURCES:
        execution = load_json(artifact_dir / manifest_name); report = load_json(artifact_dir / report_name)
        if report.get("status") != "SMOKE_PASSED（Smoke通過）": raise ValueError(f"upstream report not passed: {report_name}")
        source_bindings.append({"execution_manifest_file": manifest_name, "execution_manifest_sha256": sha256_path(artifact_dir / manifest_name), "report_file": report_name, "report_sha256": sha256_path(artifact_dir / report_name)})
        for row in execution["rows"]:
            review_id = str(row["review_id"])
            if only_review and review_id != only_review: continue
            if review_id in seen: raise ValueError(f"duplicate review: {review_id}")
            seen.add(review_id)
            validations = _r4_validations(artifact_dir, execution, row)
            eligible_ids = list(validations[0]["partial_eligible_ids"])
            source_row = b1a1_rows[review_id]
            source_path = artifact_dir / SOURCE_B1A1_DIRECTORY / source_row["input_packet_file"]
            if sha256_path(source_path) != source_row["input_packet_sha256"]: raise ValueError(f"B1a1 hash mismatch: {review_id}")
            packet = build_packet(load_json(source_path), review_id, str(row["as_of"]), eligible_ids, sha256_path(artifact_dir / report_name))
            output_path = output_dir / f"{review_id}.json"; write_new_or_identical(output_path, canonical_bytes(packet))
            path_count = sum(len(x["fixed_relation_paths"]) for x in packet["role_link_evidence_options"])
            totals["candidate_count"] += len(eligible_ids); totals["fixed_path_count"] += path_count
            rows.append({"review_id": review_id, "as_of": row["as_of"], "input_packet_file": output_path.name, "input_packet_sha256": sha256_path(output_path), "candidate_count": len(eligible_ids), "fixed_path_count": path_count, "source_b1a1_packet_sha256": sha256_path(source_path)})
    manifest = {
        "manifest_version": "v2-core-stage-b1a2-active-link-input-r2-candidate",
        "status": "CANDIDATE_INPUTS_COMPLETE_NOT_READY_FOR_AI（候選輸入完成、尚不可執行AI）",
        "case_count": len(rows), **totals, "formal_ai_calls": 0, "ready_for_formal_ai": False,
        "formal_model": "gpt-5.6-sol", "reasoning_effort": "xhigh",
        "input_packet_directory": OUTPUT_DIRECTORY, "source_bindings": source_bindings,
        "future_performance_used": False, "identity_used": False, "sealed_labels_used": False,
        "prior_role_answers_used": False, "course_role_generated_by_program": False, "rows": rows,
    }
    write_new_or_identical(artifact_dir / OUTPUT_MANIFEST, canonical_bytes(manifest)); return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR); args = parser.parse_args()
    print(json.dumps(generate_all(args.artifact_dir), ensure_ascii=False, indent=2)); return 0


if __name__ == "__main__": raise SystemExit(main())
