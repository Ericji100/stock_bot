"""Build a second fresh held-out input set for the frozen R4 primary gate."""

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
from scripts.v2_core_stage_b1a1_evidence_options_v1 import generate_all as generate_options
from scripts.v2_core_stage_b1a1e_focus_input_v1 import (
    SOURCE_OPTION_DIRECTORY,
    SOURCE_OPTION_MANIFEST,
    SOURCE_PACKET_DIRECTORY,
    load_json,
)
from scripts.v2_core_stage_b1a1e_primary_gate_input_v1 import (
    SOURCE_INPUT_MANIFEST,
    _build_packets,
    _select_candidates,
)


SELECTION_SEED = "B1A1E-R4-PRIMARY-OBJECTIVE-GATE-HELDOUT-VALIDATION-R1"
CASE_COUNT = 4
EXCLUSION_MANIFESTS = (
    "stage_b1a1e_smoke_execution_manifest_candidate_r2.json",
    "stage_b1a1e_validation_execution_manifest_r1.json",
    "stage_b1a1e_primary_execution_manifest_candidate_r3.json",
    "stage_b1a1e_primary_gate_execution_manifest_candidate_r4.json",
)
OUTPUT_DIRECTORY = "stage_b1a1e_primary_gate_validation_input_packets_r1"
PROGRAM_GATE_DIRECTORY = "stage_b1a1e_primary_gate_validation_program_inputs_r1"
OUTPUT_MANIFEST = "stage_b1a1e_primary_gate_validation_input_manifest_r1.json"
OUTPUT_SELECTION = "stage_b1a1e_primary_gate_validation_selection_r1.json"


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join((SELECTION_SEED, *parts)).encode("utf-8")).hexdigest()


def _select_reviews(source_rows: list[dict[str, Any]], excluded: set[str]) -> list[str]:
    eligible = [str(row["review_id"]) for row in source_rows if row["review_id"] not in excluded]
    if len(eligible) < CASE_COUNT:
        raise ValueError("insufficient fresh held-out review cases")
    return sorted(eligible, key=lambda value: (_digest("CASE", value), value))[:CASE_COUNT]


def generate_all(artifact_dir: Path = ARTIFACT_DIR) -> dict[str, Any]:
    generate_options(artifact_dir)
    source_manifest = load_json(artifact_dir / SOURCE_INPUT_MANIFEST)
    source_rows = {str(row["review_id"]): row for row in source_manifest["rows"]}
    option_manifest = load_json(artifact_dir / SOURCE_OPTION_MANIFEST)
    option_rows = {str(row["review_id"]): row for row in option_manifest["rows"]}
    excluded: set[str] = set()
    for filename in EXCLUSION_MANIFESTS:
        excluded.update(str(row["review_id"]) for row in load_json(artifact_dir / filename)["rows"])
    review_ids = _select_reviews(list(source_rows.values()), excluded)
    output_dir = artifact_dir / OUTPUT_DIRECTORY
    gate_dir = artifact_dir / PROGRAM_GATE_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    gate_dir.mkdir(parents=True, exist_ok=True)
    selection_rows = []
    manifest_rows = []
    total_candidates = total_directional_options = total_contacts = 0

    for review_id in review_ids:
        source_row = source_rows[review_id]
        option_row = option_rows[review_id]
        source_path = artifact_dir / SOURCE_PACKET_DIRECTORY / source_row["input_packet_file"]
        option_path = artifact_dir / SOURCE_OPTION_DIRECTORY / option_row["catalog_file"]
        if sha256_path(source_path) != source_row["input_packet_sha256"]:
            raise ValueError(f"source packet hash mismatch for {review_id}")
        if sha256_path(option_path) != option_row["catalog_sha256"]:
            raise ValueError(f"option catalog hash mismatch for {review_id}")
        source_packet = load_json(source_path)
        chosen = _select_candidates(source_packet)
        ai_packet, program_gate = _build_packets(
            source_packet=source_packet,
            option_catalog=load_json(option_path),
            chosen=chosen,
        )
        packet_path = output_dir / f"{review_id}.json"
        gate_path = gate_dir / f"{review_id}.json"
        write_new_or_identical(packet_path, canonical_bytes(ai_packet))
        write_new_or_identical(gate_path, canonical_bytes(program_gate))
        selection_rows.append(
            {
                "review_id": review_id,
                "case_selection_digest": _digest("CASE", review_id),
                "selected_candidates": [
                    {
                        "candidate_id": row["candidate_id"],
                        "scale": row["scale"],
                        "direction": row["direction"],
                        "status": row["status"],
                    }
                    for row in chosen
                ],
            }
        )
        contact_count = sum(
            row["objective_same_scale_contact"] for row in program_gate["candidate_rows"]
        )
        total_candidates += len(chosen)
        total_directional_options += len(ai_packet["evidence_options"])
        total_contacts += contact_count
        manifest_rows.append(
            {
                "review_id": review_id,
                "as_of": ai_packet["as_of"],
                "input_packet_file": packet_path.name,
                "input_packet_sha256": sha256_path(packet_path),
                "program_gate_file": gate_path.name,
                "program_gate_sha256": sha256_path(gate_path),
                "selected_candidate_count": len(chosen),
                "directional_evidence_option_count": len(ai_packet["evidence_options"]),
                "objective_contact_candidate_count": contact_count,
                "source_packet_sha256": sha256_path(source_path),
                "source_option_catalog_sha256": sha256_path(option_path),
            }
        )

    selection = {
        "selection_version": "v2-core-stage-b1a1e-primary-gate-heldout-selection-r1",
        "selection_seed": SELECTION_SEED,
        "excluded_prior_review_ids": sorted(excluded),
        "uses_prior_ai_answers": False,
        "uses_future_performance": False,
        "uses_identity": False,
        "uses_sealed_labels": False,
        "rows": selection_rows,
    }
    write_new_or_identical(artifact_dir / OUTPUT_SELECTION, canonical_bytes(selection))
    manifest = {
        "manifest_version": "v2-core-stage-b1a1e-primary-gate-heldout-input-r1",
        "status": "FRESH_HELDOUT_INPUTS_COMPLETE_NOT_READY_FOR_AI（全新留出輸入完成、尚不可執行AI）",
        "case_count": len(manifest_rows),
        "selected_candidate_count": total_candidates,
        "directional_evidence_option_count": total_directional_options,
        "objective_contact_candidate_count": total_contacts,
        "formal_ai_calls": 0,
        "ready_for_formal_ai": False,
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "candidate_scales": ["LARGE", "SMALL"],
        "program_gate_hidden_from_ai": True,
        "prior_review_ids_excluded": True,
        "prior_answers_present_in_ai_packets": False,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "course_judgement_generated_by_program": False,
        "selection_file": OUTPUT_SELECTION,
        "selection_sha256": sha256_path(artifact_dir / OUTPUT_SELECTION),
        "input_packet_directory": OUTPUT_DIRECTORY,
        "program_gate_directory": PROGRAM_GATE_DIRECTORY,
        "rows": manifest_rows,
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
