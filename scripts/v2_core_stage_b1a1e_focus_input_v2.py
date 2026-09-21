"""Build Stage B1a1E R2 focus inputs with balanced AUXILIARY targets."""

from __future__ import annotations

import argparse
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
from scripts.v2_core_stage_b1a1e_focus_input_v1 import (
    SELECTION_FILE,
    SOURCE_OPTION_DIRECTORY,
    SOURCE_OPTION_MANIFEST,
    SOURCE_PACKET_DIRECTORY,
    generate_all as generate_r1,
    load_json,
)


PACKET_VERSION = "v2-core-stage-b1a1e-focus-input-r2-candidate"
MANIFEST_VERSION = "v2-core-stage-b1a1e-focus-manifest-r2-candidate"
OUTPUT_DIRECTORY = "stage_b1a1e_input_packets_candidate_r2"
OUTPUT_MANIFEST = "stage_b1a1e_input_manifest_candidate_r2.json"


def _select_options_v2(
    option_catalog: dict[str, Any], segments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    selected_ids = {segment["candidate_id"] for segment in segments}
    segment_index = {segment["candidate_id"]: segment for segment in segments}
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for option in option_catalog["evidence_options"]:
        candidate_id = option["candidate_id"]
        if candidate_id not in selected_ids:
            continue
        grouped.setdefault(candidate_id, {}).setdefault(option["atom_name"], []).append(option)

    output = []
    for candidate_id in sorted(selected_ids):
        segment = segment_index[candidate_id]
        directional = grouped[candidate_id]["directional_coherence"]
        if len(directional) != 1:
            raise ValueError(f"expected one directional option for {candidate_id}")
        option = json.loads(json.dumps(directional[0]))
        move = abs(float(option["path"]["objective_metrics"]["directional_move_pct"]))
        counter = abs(float(option["path"]["objective_metrics"]["max_countermove_pct"]))
        option["path"]["objective_metrics"]["countermove_to_directional_ratio"] = (
            round(counter / move, 6) if move else None
        )
        output.append(option)

        structural = sorted(
            grouped[candidate_id]["structural_challenge_or_break"],
            key=lambda row: int(row["evidence_rank"]),
        )
        if segment["scale"] == "AUXILIARY":
            large = [row for row in structural if row["target"]["target_scale"] == "LARGE"][:3]
            small = [row for row in structural if row["target"]["target_scale"] == "SMALL"][:3]
            chosen = [*large, *small]
        else:
            same = [row for row in structural if row["target"]["same_scale_objective"]][:3]
            other = [row for row in structural if not row["target"]["same_scale_objective"]][:1]
            chosen = [*same, *other]
        if not chosen:
            raise ValueError(f"no structural options for {candidate_id}")
        chosen.sort(
            key=lambda row: (
                0 if row["target"]["same_scale_objective"] else 1,
                int(row["evidence_rank"]),
                row["evidence_option_id"],
            )
        )
        for focus_rank, selected in enumerate(chosen, start=1):
            output.append({**selected, "focus_evidence_rank": focus_rank})
    return output


def build_focus_packet_v2(
    *, source_packet: dict[str, Any], option_catalog: dict[str, Any], selected_ids: set[str]
) -> dict[str, Any]:
    segments = [
        row for row in source_packet["focus_segments"] if row["candidate_id"] in selected_ids
    ]
    segments.sort(key=lambda row: row["candidate_id"])
    if {row["candidate_id"] for row in segments} != selected_ids:
        raise ValueError(f"selected segment mismatch for {source_packet['review_id']}")
    options = _select_options_v2(option_catalog, segments)
    used_refs = {ref for option in options for ref in option["source_evidence_refs"]}
    evidence_catalog = [
        row for row in source_packet["evidence_catalog"] if row["ref"] in used_refs
    ]
    if {row["ref"] for row in evidence_catalog} != used_refs:
        raise ValueError(f"evidence subset mismatch for {source_packet['review_id']}")
    return {
        "packet_version": PACKET_VERSION,
        "status": "CANDIDATE_INPUT_NOT_FORMALLY_RUN（候選輸入、尚未正式執行）",
        "task": "Assess fixed evidence options only; do not reconstruct free evidence or aggregate trade decisions.",
        "review_id": source_packet["review_id"],
        "anonymous_stock_id": source_packet["anonymous_stock_id"],
        "as_of": source_packet["as_of"],
        "data_quality": source_packet["data_quality"],
        "daily_structure_context_to_as_of": source_packet[
            "daily_structure_context_to_as_of"
        ],
        "selected_focus_segments": segments,
        "evidence_options": options,
        "evidence_catalog": evidence_catalog,
        "review_constraints": {
            "future_performance_blind": True,
            "identity_blind": True,
            "sealed_labels_blind": True,
            "prior_smoke_answers_hidden": True,
            "fixed_candidate_ids_only": True,
            "fixed_evidence_option_ids_only": True,
            "free_dates_prices_pivots_relations_forbidden": True,
            "aggregate_atom_result_forbidden": True,
            "scenario_trigger_stop_permission_forbidden": True,
            "formal_model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
        },
    }


def generate_all(artifact_dir: Path = ARTIFACT_DIR) -> dict[str, Any]:
    generate_r1(artifact_dir)
    selection = load_json(artifact_dir / SELECTION_FILE)
    selection_rows = {row["review_id"]: row for row in selection["rows"]}
    option_manifest = load_json(artifact_dir / SOURCE_OPTION_MANIFEST)
    option_rows = {row["review_id"]: row for row in option_manifest["rows"]}
    source_manifest = load_json(
        artifact_dir / "stage_b1a1_input_manifest_candidate_r2.json"
    )
    source_rows = {row["review_id"]: row for row in source_manifest["rows"]}
    output_dir = artifact_dir / OUTPUT_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    total_candidates = total_options = auxiliary_candidates = 0

    for review_id in sorted(selection_rows):
        selected_ids = {
            row["candidate_id"] for row in selection_rows[review_id]["selected"]
        }
        source_row = source_rows[review_id]
        option_row = option_rows[review_id]
        source_path = artifact_dir / SOURCE_PACKET_DIRECTORY / source_row["input_packet_file"]
        option_path = artifact_dir / SOURCE_OPTION_DIRECTORY / option_row["catalog_file"]
        if sha256_path(source_path) != source_row["input_packet_sha256"]:
            raise ValueError(f"source packet hash mismatch for {review_id}")
        if sha256_path(option_path) != option_row["catalog_sha256"]:
            raise ValueError(f"option catalog hash mismatch for {review_id}")
        packet = build_focus_packet_v2(
            source_packet=load_json(source_path),
            option_catalog=load_json(option_path),
            selected_ids=selected_ids,
        )
        output_path = output_dir / f"{review_id}.json"
        write_new_or_identical(output_path, canonical_bytes(packet))
        candidate_count = len(packet["selected_focus_segments"])
        option_count = len(packet["evidence_options"])
        auxiliary_candidates += sum(
            segment["scale"] == "AUXILIARY"
            for segment in packet["selected_focus_segments"]
        )
        total_candidates += candidate_count
        total_options += option_count
        rows.append(
            {
                "review_id": review_id,
                "as_of": packet["as_of"],
                "input_packet_file": output_path.name,
                "input_packet_sha256": sha256_path(output_path),
                "selected_candidate_count": candidate_count,
                "evidence_option_count": option_count,
                "source_packet_sha256": sha256_path(source_path),
                "source_option_catalog_sha256": sha256_path(option_path),
            }
        )
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "status": "FOCUSED_INPUTS_COMPLETE_NOT_READY_FOR_AI（聚焦輸入完成、尚不可執行AI）",
        "case_count": len(rows),
        "selected_candidate_count": total_candidates,
        "auxiliary_candidate_count": auxiliary_candidates,
        "evidence_option_count": total_options,
        "formal_ai_calls": 0,
        "ready_for_formal_ai": False,
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "selection_uses_prior_calibration_outputs": True,
        "prior_answers_present_in_ai_packets": False,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "course_judgement_generated_by_program": False,
        "source_selection_file": SELECTION_FILE,
        "source_selection_sha256": sha256_path(artifact_dir / SELECTION_FILE),
        "source_option_manifest_file": SOURCE_OPTION_MANIFEST,
        "source_option_manifest_sha256": sha256_path(
            artifact_dir / SOURCE_OPTION_MANIFEST
        ),
        "input_packet_directory": OUTPUT_DIRECTORY,
        "rows": rows,
    }
    write_new_or_identical(artifact_dir / OUTPUT_MANIFEST, canonical_bytes(manifest))
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    return parser.parse_args()


def main() -> int:
    manifest = generate_all(parse_args().artifact_dir)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
