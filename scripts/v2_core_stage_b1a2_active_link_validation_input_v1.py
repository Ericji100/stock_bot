"""Build held-out B1a2 active-campaign-link inputs from the R4 validation set."""

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

from scripts.v2_core_objective_candidate_catalog_v1 import (
    ARTIFACT_DIR,
    canonical_bytes,
    sha256_path,
    write_new_or_identical,
)
from scripts.v2_core_stage_b1a1e_primary_gate_validator_v1 import load_json
from scripts.v2_core_stage_b1a2_active_link_input_v1 import (
    SOURCE_B1A1_DIRECTORY,
    SOURCE_B1A1_MANIFEST,
    _r4_validations,
    build_packet,
)


PACKET_VERSION = "v2-core-stage-b1a2-active-link-heldout-input-r1"
SOURCE_EXECUTION_MANIFEST = "stage_b1a1e_primary_gate_validation_execution_manifest_r1.json"
SOURCE_FINAL_REPORT = "stage_b1a1e_primary_gate_validation_report_r1.json"
OUTPUT_DIRECTORY = "stage_b1a2_active_link_validation_input_packets_r1"
OUTPUT_MANIFEST = "stage_b1a2_active_link_validation_input_manifest_r1.json"


def _stable_id(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "RLEVO-" + hashlib.sha256(
        f"{PACKET_VERSION}|{payload}".encode("utf-8")
    ).hexdigest()[:20]


def _reversion_packet(packet: dict[str, Any], review_id: str) -> dict[str, Any]:
    """Give the held-out packet an independent version and evidence-option namespace."""
    packet["packet_version"] = PACKET_VERSION
    packet["status"] = "HELDOUT_INPUT_NOT_FORMALLY_RUN（留出輸入、尚未正式執行）"
    for option in packet["role_link_evidence_options"]:
        signature = [
            {
                "segment_ids": value["segment_ids"],
                "relation_ids": value["relation_ids"],
            }
            for value in option["fixed_relation_paths"]
        ]
        option["evidence_option_id"] = _stable_id(
            review_id, option["candidate_id"], signature
        )
    return packet


def generate_all(artifact_dir: Path = ARTIFACT_DIR) -> dict[str, Any]:
    execution_path = artifact_dir / SOURCE_EXECUTION_MANIFEST
    report_path = artifact_dir / SOURCE_FINAL_REPORT
    execution = load_json(execution_path)
    report = load_json(report_path)
    if (
        report.get("status") != "SMOKE_PASSED（Smoke通過）"
        or report.get("completed_case_rounds") != report.get("expected_case_rounds")
    ):
        raise ValueError("upstream R4 held-out report is not passed and complete")

    b1a1_manifest = load_json(artifact_dir / SOURCE_B1A1_MANIFEST)
    b1a1_rows = {str(row["review_id"]): row for row in b1a1_manifest["rows"]}
    output_dir = artifact_dir / OUTPUT_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    total_candidates = 0
    total_paths = 0

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
        packet = _reversion_packet(packet, review_id)
        output_path = output_dir / f"{review_id}.json"
        write_new_or_identical(output_path, canonical_bytes(packet))
        path_count = sum(
            len(value["fixed_relation_paths"])
            for value in packet["role_link_evidence_options"]
        )
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
        "manifest_version": "v2-core-stage-b1a2-active-link-heldout-input-r1",
        "status": "HELDOUT_INPUTS_COMPLETE_NOT_READY_FOR_AI（留出輸入完成、尚不可執行AI）",
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
        "calibration_b1a2_answers_used": False,
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
