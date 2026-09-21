"""Build B1b2 control-relevance inputs from three-round B1b1 consensus."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_objective_candidate_catalog_v1 import ARTIFACT_DIR, canonical_bytes, sha256_path, write_new_or_identical
from scripts.v2_core_stage_b1a2_active_link_validator_v1 import load_json


SOURCE_EXECUTION_MANIFEST = "stage_b1b1_parent_source_execution_manifest_candidate_r2.json"
SOURCE_REPORT = "stage_b1b1_parent_source_calibration_report_candidate_r2.json"
OUTPUT_DIRECTORY = "stage_b1b2_control_relevance_input_packets_candidate_r1"
OUTPUT_MANIFEST = "stage_b1b2_control_relevance_input_manifest_candidate_r1.json"


def _option_id(review_id: str, candidate_id: str, parent_option_id: str) -> str:
    digest = hashlib.sha256(f"B1B2|{review_id}|{candidate_id}|{parent_option_id}".encode()).hexdigest()[:20]
    return f"CRREVO-{digest}"


def generate_all(
    artifact_dir: Path = ARTIFACT_DIR,
    *,
    source_execution_manifest: str = SOURCE_EXECUTION_MANIFEST,
    source_report: str = SOURCE_REPORT,
    output_directory: str = OUTPUT_DIRECTORY,
    output_manifest: str = OUTPUT_MANIFEST,
    packet_version: str = "v2-core-stage-b1b2-control-relevance-input-r1-candidate",
    manifest_version: str = "v2-core-stage-b1b2-control-relevance-input-r1-candidate",
    include_relation_path_segments: bool = False,
) -> dict[str, Any]:
    execution_path = artifact_dir / source_execution_manifest
    report_path = artifact_dir / source_report
    execution = load_json(execution_path)
    report = load_json(report_path)
    if report.get("status") != "SMOKE_PASSED（Smoke通過）":
        raise ValueError("source B1b1 report must pass")
    source_input_manifest = load_json(artifact_dir / execution["input_manifest_file"])
    rows_by_id = {str(row["review_id"]): row for row in source_input_manifest["rows"]}
    output_dir = artifact_dir / output_directory
    output_dir.mkdir(parents=True, exist_ok=True)
    output_rows = []
    for row in execution["rows"]:
        review_id = str(row["review_id"])
        source_row = rows_by_id[review_id]
        source_packet_path = artifact_dir / execution["input_packet_directory"] / source_row["input_packet_file"]
        source_packet = load_json(source_packet_path)
        round_validations = []
        upstream_bindings = []
        for round_number in range(1, int(execution["required_rounds"]) + 1):
            receipt_path = artifact_dir / execution["run_directory"] / f"round_{round_number}" / "receipts" / f"{review_id}.stage_b1b1_parent_source.json"
            receipt = load_json(receipt_path)
            validation = receipt["validation"]
            if validation.get("status") != "VALID":
                raise ValueError(f"invalid upstream receipt: {receipt_path}")
            round_validations.append(validation)
            upstream_bindings.append(
                {
                    "round": round_number,
                    "receipt_file": str(receipt_path.relative_to(artifact_dir)).replace("\\", "/"),
                    "receipt_sha256": sha256_path(receipt_path),
                }
            )
        per_round = [
            {str(item["candidate_id"]): bool(item["parent_source_eligible"]) for item in validation["derived_candidate_results"]}
            for validation in round_validations
        ]
        candidate_ids = sorted(
            candidate_id
            for candidate_id in source_packet["parent_candidate_ids"]
            if all(index.get(candidate_id) is True for index in per_round)
        )
        if not candidate_ids:
            raise ValueError(f"no three-round parent-source consensus candidates: {review_id}")
        segments = {str(item["candidate_id"]): item for item in source_packet["candidate_segments"]}
        parent_options = {str(item["candidate_id"]): item for item in source_packet["parent_source_evidence_options"]}
        options = []
        for candidate_id in candidate_ids:
            parent_option = parent_options[candidate_id]
            options.append(
                {
                    "atom_name": "current_control_relevance",
                    "candidate_id": candidate_id,
                    "evidence_option_id": _option_id(review_id, candidate_id, str(parent_option["evidence_option_id"])),
                    "source_parent_evidence_option_id": parent_option["evidence_option_id"],
                    "candidate_status": segments[candidate_id]["status"],
                    "candidate_scale": segments[candidate_id]["scale"],
                    "candidate_direction": segments[candidate_id]["direction"],
                    "competing_parent_candidate_ids": [value for value in candidate_ids if value != candidate_id],
                    "fixed_relation_paths": parent_option["fixed_relation_paths"],
                    "not_unique_anchor_decision": True,
                }
            )
        relation_path_segments = []
        relation_source_binding = None
        if include_relation_path_segments:
            relation_manifest_file = str(source_packet["source_bindings"]["source_manifest_file"])
            relation_manifest_path = artifact_dir / relation_manifest_file
            relation_manifest = load_json(relation_manifest_path)
            relation_rows = [item for item in relation_manifest["rows"] if str(item["review_id"]) == review_id]
            if len(relation_rows) != 1:
                raise ValueError(f"relation source manifest review mismatch: {review_id}")
            relation_source_path = (
                artifact_dir
                / relation_manifest["input_packet_directory"]
                / relation_rows[0]["input_packet_file"]
            )
            relation_source = load_json(relation_source_path)
            all_segments = {
                str(item["candidate_id"]): item
                for item in relation_source["candidate_segments"] + relation_source["current_context_segments"]
            }
            objective_catalog_path = artifact_dir / "objective_candidate_catalogs_r1" / f"{review_id}.json"
            objective_catalog = load_json(objective_catalog_path)
            if objective_catalog["generator_contract"].get("future_performance_used") is not False:
                raise ValueError(f"objective catalog is not AS-OF blind: {review_id}")
            all_segments.update(
                {
                    str(item["candidate_id"]): item
                    for item in objective_catalog["objective_segment_candidates"]
                }
            )
            referenced_ids = {
                str(segment_id)
                for option in options
                for path in option["fixed_relation_paths"]
                for segment_id in path["segment_ids"]
            }
            visible_ids = set(candidate_ids) | {
                str(item["candidate_id"]) for item in source_packet["current_context_segments"]
            }
            missing_ids = referenced_ids - set(all_segments)
            if missing_ids:
                raise ValueError(f"fixed path segment snapshots missing: {sorted(missing_ids)}")
            relation_path_segments = [all_segments[value] for value in sorted(referenced_ids - visible_ids)]
            relation_source_binding = {
                "relation_source_manifest_file": relation_manifest_file,
                "relation_source_manifest_sha256": sha256_path(relation_manifest_path),
                "relation_source_packet_file": str(relation_source_path.relative_to(artifact_dir)).replace("\\", "/"),
                "relation_source_packet_sha256": sha256_path(relation_source_path),
                "objective_candidate_catalog_file": str(objective_catalog_path.relative_to(artifact_dir)).replace("\\", "/"),
                "objective_candidate_catalog_sha256": sha256_path(objective_catalog_path),
            }
        packet = {
            "packet_version": packet_version,
            "review_id": review_id,
            "as_of": source_packet["as_of"],
            "task": "Judge only whether each parent-source candidate still has independent structural control relevance at AS-OF.",
            "control_candidate_ids": candidate_ids,
            "candidate_segments": [segments[value] for value in candidate_ids],
            "current_context_segments": source_packet["current_context_segments"],
            "control_relevance_evidence_options": options,
            "source_bindings": {
                "source_execution_manifest_file": source_execution_manifest,
                "source_execution_manifest_sha256": sha256_path(execution_path),
                "source_report_file": source_report,
                "source_report_sha256": sha256_path(report_path),
                "source_packet_sha256": sha256_path(source_packet_path),
                "upstream_receipts": upstream_bindings,
            },
            "upstream_parent_source_consensus_used": True,
            "upstream_parent_source_answers_exposed": False,
            "future_performance_used": False,
            "identity_used": False,
            "sealed_labels_used": False,
            "prior_ai_control_answers_used": False,
        }
        if include_relation_path_segments:
            packet["relation_path_segments"] = relation_path_segments
            packet["all_fixed_path_segment_snapshots_present"] = True
            packet["source_bindings"]["relation_source"] = relation_source_binding
        output_path = output_dir / f"{review_id}.json"
        write_new_or_identical(output_path, canonical_bytes(packet))
        output_rows.append(
            {
                "review_id": review_id,
                "as_of": source_packet["as_of"],
                "input_packet_file": output_path.name,
                "input_packet_sha256": sha256_path(output_path),
                "candidate_count": len(candidate_ids),
            }
        )
    manifest = {
        "manifest_version": manifest_version,
        "status": "CALIBRATION_INPUTS_COMPLETE_NOT_READY_FOR_AI（校準輸入完成、尚不可執行AI）",
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "case_count": len(output_rows),
        "candidate_count": sum(row["candidate_count"] for row in output_rows),
        "input_packet_directory": output_directory,
        "source_execution_manifest_file": source_execution_manifest,
        "source_execution_manifest_sha256": sha256_path(execution_path),
        "source_report_file": source_report,
        "source_report_sha256": sha256_path(report_path),
        "upstream_parent_source_consensus_used": True,
        "upstream_parent_source_answers_exposed": False,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "prior_ai_control_answers_used": False,
        "rows": output_rows,
    }
    if include_relation_path_segments:
        manifest["all_fixed_path_segment_snapshots_present"] = True
    write_new_or_identical(artifact_dir / output_manifest, canonical_bytes(manifest))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    print(json.dumps(generate_all(args.artifact_dir), ensure_ascii=False, indent=2))
