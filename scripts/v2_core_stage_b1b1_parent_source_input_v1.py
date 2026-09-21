"""Build a small B1b1 parent-source calibration probe from R5 retained candidates."""

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


R5_REPORT = "stage_b1a2_causal_reachability_gate_r5_report_r2.json"
OUTPUT_DIRECTORY = "stage_b1b1_parent_source_input_packets_candidate_r1"
OUTPUT_MANIFEST = "stage_b1b1_parent_source_input_manifest_candidate_r1.json"
SELECTION_FILE = "stage_b1b1_parent_source_selection_candidate_r1.json"
SELECTION_SEED = "B1B1-PARENT-SOURCE-CALIBRATION-R1"
CASE_COUNT = 5


def _digest(*parts: str, selection_seed: str = SELECTION_SEED) -> str:
    return hashlib.sha256("|".join((selection_seed, *parts)).encode()).hexdigest()


def _option_id(review_id: str, candidate_id: str, source_option_id: str) -> str:
    digest = hashlib.sha256(f"B1B1|{review_id}|{candidate_id}|{source_option_id}".encode()).hexdigest()[:20]
    return f"PSREVO-{digest}"


def generate_all(
    artifact_dir: Path = ARTIFACT_DIR,
    *,
    selection_seed: str = SELECTION_SEED,
    case_count: int = CASE_COUNT,
    excluded_review_ids: frozenset[str] = frozenset(),
    output_directory: str = OUTPUT_DIRECTORY,
    output_manifest: str = OUTPUT_MANIFEST,
    selection_file: str = SELECTION_FILE,
    packet_version: str = "v2-core-stage-b1b1-parent-source-input-r1-candidate",
    selection_version: str = "v2-core-stage-b1b1-parent-source-selection-r1-candidate",
    manifest_version: str = "v2-core-stage-b1b1-parent-source-input-r1-candidate",
) -> dict[str, Any]:
    r5_path = artifact_dir / R5_REPORT
    r5 = load_json(r5_path)
    if r5.get("status") != "PASS（通過）":
        raise ValueError("R5 causal reachability gate must pass")
    manifests = {
        binding["manifest_file"]: load_json(artifact_dir / binding["manifest_file"])
        for binding in r5["source_bindings"]
    }
    eligible = []
    for case in r5["cases"]:
        if str(case["review_id"]) in excluded_review_ids:
            continue
        manifest = manifests[case["source_manifest_file"]]
        packet_path = artifact_dir / manifest["input_packet_directory"] / case["input_packet_file"]
        packet = load_json(packet_path)
        segment_index = {str(row["candidate_id"]): row for row in packet["candidate_segments"]}
        confirmed = [
            candidate_id for candidate_id in case["retained_candidate_ids"]
            if segment_index[candidate_id]["status"] == "CONFIRMED"
        ]
        if confirmed:
            eligible.append((case, manifest, packet_path, packet, sorted(confirmed)))
    selected = sorted(
        eligible,
        key=lambda item: (
            _digest(str(item[0]["review_id"]), selection_seed=selection_seed),
            str(item[0]["review_id"]),
        ),
    )[:case_count]
    if len(selected) != case_count:
        raise ValueError("insufficient R5-retained confirmed parent-source cases")
    output_dir = artifact_dir / output_directory
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    selection_rows = []
    for case, manifest, source_path, packet, candidate_ids in selected:
        review_id = str(case["review_id"])
        segment_index = {str(row["candidate_id"]): row for row in packet["candidate_segments"]}
        source_options = {str(row["candidate_id"]): row for row in packet["role_link_evidence_options"]}
        options = []
        for candidate_id in candidate_ids:
            source_option = source_options[candidate_id]
            terminal_ids = sorted({str(path["terminal_segment_id"]) for path in source_option["fixed_relation_paths"]})
            context_index = {str(row["candidate_id"]): row for row in packet["current_context_segments"]}
            options.append(
                {
                    "atom_name": "parent_source_relation",
                    "candidate_id": candidate_id,
                    "evidence_option_id": _option_id(review_id, candidate_id, str(source_option["evidence_option_id"])),
                    "candidate_status": segment_index[candidate_id]["status"],
                    "candidate_scale": segment_index[candidate_id]["scale"],
                    "candidate_direction": segment_index[candidate_id]["direction"],
                    "terminal_context_ids": terminal_ids,
                    "terminal_context_scales": sorted({str(context_index[value]["scale"]) for value in terminal_ids}),
                    "terminal_context_directions": sorted({str(context_index[value]["direction"]) for value in terminal_ids}),
                    "fixed_relation_paths": source_option["fixed_relation_paths"],
                    "source_active_link_option_id": source_option["evidence_option_id"],
                    "not_final_course_role": True,
                }
            )
        output = {
            "packet_version": packet_version,
            "review_id": review_id,
            "as_of": case["as_of"],
            "task": "Judge only whether each confirmed candidate is a visible predecessor/source parent of at least one current forming structure.",
            "parent_candidate_ids": candidate_ids,
            "candidate_segments": [segment_index[value] for value in candidate_ids],
            "current_context_segments": packet["current_context_segments"],
            "parent_source_evidence_options": options,
            "source_bindings": {
                "source_manifest_file": case["source_manifest_file"],
                "source_manifest_sha256": sha256_path(artifact_dir / case["source_manifest_file"]),
                "source_packet_sha256": sha256_path(source_path),
                "r5_report_sha256": sha256_path(r5_path),
            },
            "future_performance_used": False,
            "identity_used": False,
            "sealed_labels_used": False,
            "prior_ai_role_answers_used": False,
        }
        output_path = output_dir / f"{review_id}.json"
        write_new_or_identical(output_path, canonical_bytes(output))
        rows.append(
            {
                "review_id": review_id,
                "as_of": case["as_of"],
                "input_packet_file": output_path.name,
                "input_packet_sha256": sha256_path(output_path),
                "candidate_count": len(candidate_ids),
            }
        )
        selection_rows.append(
            {
                "review_id": review_id,
                "selection_digest": _digest(review_id, selection_seed=selection_seed),
                "candidate_ids": candidate_ids,
            }
        )
    selection = {
        "selection_version": selection_version,
        "selection_seed": selection_seed,
        "uses_future_performance": False,
        "uses_identity": False,
        "uses_sealed_labels": False,
        "uses_prior_ai_role_answers": False,
        "rows": selection_rows,
    }
    write_new_or_identical(artifact_dir / selection_file, canonical_bytes(selection))
    manifest = {
        "manifest_version": manifest_version,
        "status": "CALIBRATION_INPUTS_COMPLETE_NOT_READY_FOR_AI（校準輸入完成、尚不可執行AI）",
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "case_count": len(rows),
        "candidate_count": sum(row["candidate_count"] for row in rows),
        "input_packet_directory": output_directory,
        "selection_file": selection_file,
        "selection_sha256": sha256_path(artifact_dir / selection_file),
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "prior_ai_role_answers_used": False,
        "rows": rows,
    }
    write_new_or_identical(artifact_dir / output_manifest, canonical_bytes(manifest))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    print(json.dumps(generate_all(args.artifact_dir), ensure_ascii=False, indent=2))
