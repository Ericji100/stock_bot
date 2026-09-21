"""Build independent LARGE/SMALL-only Stage B1a1E candidate R3 packets."""

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
from scripts.v2_core_stage_b1a1e_focus_input_v2 import build_focus_packet_v2


SELECTION_SEED = "B1A1E-R3-PRIMARY-ANCHOR-INDEPENDENT-PILOT-V1"
CASE_COUNT = 4
MAX_CANDIDATES_PER_CASE = 6
SOURCE_INPUT_MANIFEST = "stage_b1a1_input_manifest_candidate_r2.json"
EXCLUSION_MANIFESTS = (
    "stage_b1a1e_smoke_execution_manifest_candidate_r2.json",
    "stage_b1a1e_validation_execution_manifest_r1.json",
)
OUTPUT_DIRECTORY = "stage_b1a1e_primary_input_packets_candidate_r3"
OUTPUT_MANIFEST = "stage_b1a1e_primary_input_manifest_candidate_r3.json"
OUTPUT_SELECTION = "stage_b1a1e_primary_selection_candidate_r3.json"
PACKET_VERSION = "v2-core-stage-b1a1e-primary-anchor-input-r3-candidate"


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join((SELECTION_SEED, *parts)).encode("utf-8")).hexdigest()


def _select_reviews(source_rows: list[dict[str, Any]], excluded: set[str]) -> list[str]:
    eligible = [str(row["review_id"]) for row in source_rows if row["review_id"] not in excluded]
    if len(eligible) < CASE_COUNT:
        raise ValueError("insufficient independent review cases")
    return sorted(eligible, key=lambda value: (_digest("CASE", value), value))[:CASE_COUNT]


def _select_primary_candidates(packet: dict[str, Any]) -> list[dict[str, Any]]:
    review_id = str(packet["review_id"])
    eligible = [
        row for row in packet["focus_segments"] if str(row["scale"]) in {"LARGE", "SMALL"}
    ]
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in eligible:
        key = (str(row["scale"]), str(row["direction"]), str(row["status"]))
        buckets.setdefault(key, []).append(row)
    selected: dict[str, dict[str, Any]] = {}
    for key in sorted(buckets):
        winner = min(
            buckets[key],
            key=lambda row: (
                _digest("BUCKET", review_id, *key, str(row["candidate_id"])),
                str(row["candidate_id"]),
            ),
        )
        selected[str(winner["candidate_id"])] = winner
    if len(selected) > MAX_CANDIDATES_PER_CASE:
        keep = sorted(
            selected.values(),
            key=lambda row: (
                _digest("CAP", review_id, str(row["candidate_id"])),
                str(row["candidate_id"]),
            ),
        )[:MAX_CANDIDATES_PER_CASE]
        selected = {str(row["candidate_id"]): row for row in keep}
    if len(selected) < MAX_CANDIDATES_PER_CASE:
        remaining = [row for row in eligible if str(row["candidate_id"]) not in selected]
        remaining.sort(
            key=lambda row: (
                _digest("FILL", review_id, str(row["candidate_id"])),
                str(row["candidate_id"]),
            )
        )
        for row in remaining:
            selected[str(row["candidate_id"])] = row
            if len(selected) == MAX_CANDIDATES_PER_CASE:
                break
    if not selected:
        raise ValueError(f"no LARGE/SMALL candidates for {review_id}")
    return sorted(selected.values(), key=lambda row: str(row["candidate_id"]))


def generate_all(artifact_dir: Path = ARTIFACT_DIR) -> dict[str, Any]:
    generate_options(artifact_dir)
    source_manifest = load_json(artifact_dir / SOURCE_INPUT_MANIFEST)
    source_rows = {str(row["review_id"]): row for row in source_manifest["rows"]}
    excluded: set[str] = set()
    for filename in EXCLUSION_MANIFESTS:
        excluded.update(
            str(row["review_id"]) for row in load_json(artifact_dir / filename)["rows"]
        )
    review_ids = _select_reviews(list(source_rows.values()), excluded)
    option_manifest = load_json(artifact_dir / SOURCE_OPTION_MANIFEST)
    option_rows = {str(row["review_id"]): row for row in option_manifest["rows"]}
    output_dir = artifact_dir / OUTPUT_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    selection_rows = []
    manifest_rows = []
    total_candidates = total_options = 0

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
        chosen = _select_primary_candidates(source_packet)
        selected_ids = {str(row["candidate_id"]) for row in chosen}
        packet = build_focus_packet_v2(
            source_packet=source_packet,
            option_catalog=load_json(option_path),
            selected_ids=selected_ids,
        )
        packet["packet_version"] = PACKET_VERSION
        packet["status"] = "PRIMARY_ANCHOR_CANDIDATE_R3_NOT_FORMALLY_RUN（R3主錨候選、尚未正式執行）"
        packet["review_constraints"]["selection_uses_prior_ai_answers"] = False
        packet["review_constraints"]["auxiliary_standalone_candidate_forbidden"] = True
        output_path = output_dir / f"{review_id}.json"
        write_new_or_identical(output_path, canonical_bytes(packet))
        selection_rows.append(
            {
                "review_id": review_id,
                "case_selection_digest": _digest("CASE", review_id),
                "selected_candidates": [
                    {
                        "candidate_id": str(row["candidate_id"]),
                        "scale": str(row["scale"]),
                        "direction": str(row["direction"]),
                        "status": str(row["status"]),
                    }
                    for row in chosen
                ],
            }
        )
        total_candidates += len(chosen)
        total_options += len(packet["evidence_options"])
        manifest_rows.append(
            {
                "review_id": review_id,
                "as_of": packet["as_of"],
                "input_packet_file": output_path.name,
                "input_packet_sha256": sha256_path(output_path),
                "selected_candidate_count": len(chosen),
                "evidence_option_count": len(packet["evidence_options"]),
                "source_packet_sha256": sha256_path(source_path),
                "source_option_catalog_sha256": sha256_path(option_path),
            }
        )

    selection = {
        "selection_version": "v2-core-stage-b1a1e-primary-anchor-selection-r3-candidate",
        "selection_seed": SELECTION_SEED,
        "selection_method": "SHA256 independent case sampling plus LARGE/SMALL scale-direction-status stratification",
        "excluded_prior_review_ids": sorted(excluded),
        "uses_prior_ai_answers": False,
        "uses_future_performance": False,
        "uses_identity": False,
        "uses_sealed_labels": False,
        "rows": selection_rows,
    }
    write_new_or_identical(artifact_dir / OUTPUT_SELECTION, canonical_bytes(selection))
    manifest = {
        "manifest_version": "v2-core-stage-b1a1e-primary-anchor-input-r3-candidate",
        "status": "INDEPENDENT_INPUTS_COMPLETE_NOT_READY_FOR_AI（獨立輸入完成、尚不可執行AI）",
        "case_count": len(manifest_rows),
        "selected_candidate_count": total_candidates,
        "evidence_option_count": total_options,
        "formal_ai_calls": 0,
        "ready_for_formal_ai": False,
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "primary_candidate_scales": ["LARGE", "SMALL"],
        "auxiliary_standalone_candidate_forbidden": True,
        "selection_uses_prior_calibration_outputs": False,
        "prior_review_ids_excluded": True,
        "prior_answers_present_in_ai_packets": False,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "course_judgement_generated_by_program": False,
        "selection_file": OUTPUT_SELECTION,
        "selection_sha256": sha256_path(artifact_dir / OUTPUT_SELECTION),
        "source_option_manifest_file": SOURCE_OPTION_MANIFEST,
        "source_option_manifest_sha256": sha256_path(artifact_dir / SOURCE_OPTION_MANIFEST),
        "input_packet_directory": OUTPUT_DIRECTORY,
        "rows": manifest_rows,
    }
    write_new_or_identical(artifact_dir / OUTPUT_MANIFEST, canonical_bytes(manifest))
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    return parser.parse_args()


def main() -> int:
    print(json.dumps(generate_all(parse_args().artifact_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
