"""Build fresh blind R4 inputs for the Stage B1a1E primary-candidate gate."""

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


SELECTION_SEED = "B1A1E-R4-PRIMARY-OBJECTIVE-GATE-FRESH-PILOT-V1"
CASE_COUNT = 4
MAX_CANDIDATES_PER_CASE = 6
SOURCE_INPUT_MANIFEST = "stage_b1a1_input_manifest_candidate_r2.json"
EXCLUSION_MANIFESTS = (
    "stage_b1a1e_smoke_execution_manifest_candidate_r2.json",
    "stage_b1a1e_validation_execution_manifest_r1.json",
    "stage_b1a1e_primary_execution_manifest_candidate_r3.json",
)
OUTPUT_DIRECTORY = "stage_b1a1e_primary_gate_input_packets_candidate_r4"
PROGRAM_GATE_DIRECTORY = "stage_b1a1e_primary_gate_program_inputs_candidate_r4"
OUTPUT_MANIFEST = "stage_b1a1e_primary_gate_input_manifest_candidate_r4.json"
OUTPUT_SELECTION = "stage_b1a1e_primary_gate_selection_candidate_r4.json"
PACKET_VERSION = "v2-core-stage-b1a1e-primary-gate-input-r4-candidate"
PROGRAM_GATE_VERSION = "v2-core-stage-b1a1e-primary-gate-program-input-r4-candidate"


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join((SELECTION_SEED, *parts)).encode("utf-8")).hexdigest()


def _select_reviews(source_rows: list[dict[str, Any]], excluded: set[str]) -> list[str]:
    eligible = [str(row["review_id"]) for row in source_rows if row["review_id"] not in excluded]
    if len(eligible) < CASE_COUNT:
        raise ValueError("insufficient fresh review cases")
    return sorted(eligible, key=lambda value: (_digest("CASE", value), value))[:CASE_COUNT]


def _select_candidates(packet: dict[str, Any]) -> list[dict[str, Any]]:
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


def _directional_option(option: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(option))
    metrics = value["path"]["objective_metrics"]
    move = abs(float(metrics["directional_move_pct"]))
    counter = abs(float(metrics["max_countermove_pct"]))
    metrics["countermove_to_directional_ratio"] = (
        round(counter / move, 6) if move else None
    )
    return value


def _build_packets(
    *, source_packet: dict[str, Any], option_catalog: dict[str, Any], chosen: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    selected_ids = {str(row["candidate_id"]) for row in chosen}
    grouped: dict[str, list[dict[str, Any]]] = {value: [] for value in selected_ids}
    for option in option_catalog["evidence_options"]:
        candidate_id = str(option["candidate_id"])
        if candidate_id in selected_ids:
            grouped[candidate_id].append(option)
    directional_options = []
    gate_rows = []
    used_refs = set()
    for segment in chosen:
        candidate_id = str(segment["candidate_id"])
        directional = [
            row for row in grouped[candidate_id] if row["atom_name"] == "directional_coherence"
        ]
        if len(directional) != 1:
            raise ValueError(f"expected one directional option for {candidate_id}")
        directional_value = _directional_option(directional[0])
        directional_options.append(directional_value)
        used_refs.update(directional_value["source_evidence_refs"])
        structural = [
            row
            for row in grouped[candidate_id]
            if row["atom_name"] == "structural_challenge_or_break"
        ]
        contacts = sorted(
            str(row["evidence_option_id"])
            for row in structural
            if row["option_kind"] == "FIXED_PRIOR_PIVOT_TARGET"
            and bool(row["target"]["same_scale_objective"])
            and bool(row["target"]["price_reached_or_crossed_objective"])
        )
        gate_rows.append(
            {
                "candidate_id": candidate_id,
                "candidate_scale": segment["scale"],
                "objective_same_scale_contact": bool(contacts),
                "supporting_evidence_option_ids": contacts,
            }
        )
    evidence_catalog = [
        row for row in source_packet["evidence_catalog"] if row["ref"] in used_refs
    ]
    if {row["ref"] for row in evidence_catalog} != used_refs:
        raise ValueError(f"directional evidence subset mismatch for {source_packet['review_id']}")
    ai_packet = {
        "packet_version": PACKET_VERSION,
        "status": "FRESH_CANDIDATE_INPUT_NOT_FORMALLY_RUN（全新候選輸入、尚未正式執行）",
        "task": "Assess each fixed complete directional path only; do not infer target pivots, roles, scenarios or trades.",
        "review_id": source_packet["review_id"],
        "anonymous_stock_id": source_packet["anonymous_stock_id"],
        "as_of": source_packet["as_of"],
        "data_quality": source_packet["data_quality"],
        "daily_structure_context_to_as_of": source_packet["daily_structure_context_to_as_of"],
        "selected_focus_segments": chosen,
        "evidence_options": sorted(
            directional_options, key=lambda row: str(row["candidate_id"])
        ),
        "evidence_catalog": evidence_catalog,
        "review_constraints": {
            "future_performance_blind": True,
            "identity_blind": True,
            "sealed_labels_blind": True,
            "prior_answers_hidden": True,
            "fixed_candidate_ids_only": True,
            "fixed_directional_evidence_option_ids_only": True,
            "program_gate_hidden_from_ai": True,
            "target_anchor_role_scenario_trigger_stop_permission_forbidden": True,
            "formal_model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
        },
    }
    program_gate = {
        "program_gate_version": PROGRAM_GATE_VERSION,
        "review_id": source_packet["review_id"],
        "as_of": source_packet["as_of"],
        "gate_definition": "LARGE_OR_SMALL_FIXED_PRIOR_PIVOT_SAME_SCALE_AND_PRICE_CONTACT",
        "course_judgement_generated": False,
        "candidate_rows": sorted(gate_rows, key=lambda row: str(row["candidate_id"])),
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
    }
    return ai_packet, program_gate


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
        "selection_version": "v2-core-stage-b1a1e-primary-gate-selection-r4-candidate",
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
        "manifest_version": "v2-core-stage-b1a1e-primary-gate-input-r4-candidate",
        "status": "FRESH_INPUTS_COMPLETE_NOT_READY_FOR_AI（全新輸入完成、尚不可執行AI）",
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    return parser.parse_args()


def main() -> int:
    print(json.dumps(generate_all(parse_args().artifact_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
