"""Build focused, answer-blind Stage B1a1E calibration packets.

Selection uses completed R4 calibration outputs to locate unstable candidates
and stable controls.  Prior verdicts and control roles are recorded separately
and are never embedded in the AI input packets.
"""

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
from scripts.v2_core_stage_b1a1_candidate_validator_v2 import ATOMIC_FIELDS
from scripts.v2_core_stage_b1a1_smoke_compare_v3 import compare_smoke


PACKET_VERSION = "v2-core-stage-b1a1e-focus-input-r1-candidate"
MANIFEST_VERSION = "v2-core-stage-b1a1e-focus-manifest-r1-candidate"
SOURCE_SMOKE_MANIFEST = "stage_b1a1_smoke_execution_manifest_candidate_r4.json"
SOURCE_PACKET_DIRECTORY = "stage_b1a1_input_packets_candidate_r2"
SOURCE_OPTION_MANIFEST = "stage_b1a1_evidence_options_manifest_candidate_r1.json"
SOURCE_OPTION_DIRECTORY = "stage_b1a1_evidence_options_candidate_r1"
OUTPUT_DIRECTORY = "stage_b1a1e_input_packets_candidate_r1"
OUTPUT_MANIFEST = "stage_b1a1e_input_manifest_candidate_r1.json"
SELECTION_FILE = "stage_b1a1e_calibration_selection_candidate_r1.json"
MAX_SAME_SCALE_TARGETS = 3
MAX_OTHER_SCALE_TARGETS = 1


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _assessment_index(output: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["candidate_id"]: row for row in output["candidate_assessments"]}


def _eligible(row: dict[str, Any]) -> bool:
    return all(row[field]["result"] == "PASS" for field in ATOMIC_FIELDS)


def _r4_output(run_dir: Path, round_number: int, review_id: str) -> dict[str, Any]:
    return load_json(
        run_dir / f"round_{round_number}" / "stage_b1a1" / f"{review_id}.json"
    )


def select_focus_candidates(
    *, smoke_manifest: dict[str, Any], run_dir: Path
) -> dict[str, Any]:
    rows = []
    for case in smoke_manifest["rows"]:
        review_id = str(case["review_id"])
        rounds = [
            _assessment_index(_r4_output(run_dir, round_number, review_id))
            for round_number in (1, 2, 3)
        ]
        candidate_ids = sorted(rounds[0])
        unstable = []
        stable_eligible = []
        stable_ineligible = []
        for candidate_id in candidate_ids:
            result_signatures = [
                tuple(rows_by_id[candidate_id][field]["result"] for field in ATOMIC_FIELDS)
                for rows_by_id in rounds
            ]
            eligibility = [_eligible(rows_by_id[candidate_id]) for rows_by_id in rounds]
            if len(set(result_signatures)) > 1:
                unstable.append(candidate_id)
            elif all(eligibility):
                stable_eligible.append(candidate_id)
            else:
                stable_ineligible.append(candidate_id)
        controls = []
        if stable_eligible:
            controls.append(
                {"candidate_id": stable_eligible[0], "selection_role": "STABLE_ELIGIBLE_CONTROL"}
            )
        if stable_ineligible:
            controls.append(
                {"candidate_id": stable_ineligible[0], "selection_role": "STABLE_INELIGIBLE_CONTROL"}
            )
        selected = [
            {"candidate_id": candidate_id, "selection_role": "R4_UNSTABLE_BOUNDARY"}
            for candidate_id in unstable
        ] + controls
        selected.sort(key=lambda row: row["candidate_id"])
        rows.append(
            {
                "review_id": review_id,
                "selected": selected,
                "unstable_count": len(unstable),
                "control_count": len(controls),
            }
        )
    return {
        "selection_version": "v2-core-stage-b1a1e-calibration-selection-r1",
        "status": "CALIBRATION_SELECTION_NOT_AI_INPUT（校準選取、不得作AI輸入）",
        "uses_prior_calibration_smoke_outputs": True,
        "future_performance_used": False,
        "identity_used": False,
        "sealed_labels_used": False,
        "rows": rows,
    }


def _select_options(
    catalog: dict[str, Any], selected_ids: set[str]
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for option in catalog["evidence_options"]:
        candidate_id = str(option["candidate_id"])
        if candidate_id not in selected_ids:
            continue
        grouped.setdefault(candidate_id, {}).setdefault(str(option["atom_name"]), []).append(option)

    selected_options = []
    for candidate_id in sorted(selected_ids):
        atoms = grouped[candidate_id]
        directional = atoms.get("directional_coherence", [])
        if len(directional) != 1:
            raise ValueError(f"expected one directional option for {candidate_id}")
        selected_options.extend(directional)

        structural = sorted(
            atoms.get("structural_challenge_or_break", []),
            key=lambda row: int(row["evidence_rank"]),
        )
        same = [row for row in structural if row["target"]["same_scale_objective"]]
        other = [row for row in structural if not row["target"]["same_scale_objective"]]
        chosen = [
            *same[:MAX_SAME_SCALE_TARGETS],
            *other[:MAX_OTHER_SCALE_TARGETS],
        ]
        if not chosen:
            raise ValueError(f"no structural target option for {candidate_id}")
        for new_rank, option in enumerate(chosen, start=1):
            selected_options.append({**option, "focus_evidence_rank": new_rank})
    return selected_options


def build_focus_packet(
    *, source_packet: dict[str, Any], option_catalog: dict[str, Any], selected_ids: set[str]
) -> dict[str, Any]:
    segments = [
        row for row in source_packet["focus_segments"] if row["candidate_id"] in selected_ids
    ]
    segments.sort(key=lambda row: row["candidate_id"])
    if {row["candidate_id"] for row in segments} != selected_ids:
        raise ValueError(f"selected segment mismatch for {source_packet['review_id']}")
    options = _select_options(option_catalog, selected_ids)
    used_refs = {
        ref for option in options for ref in option["source_evidence_refs"]
    }
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
    smoke_manifest_path = artifact_dir / SOURCE_SMOKE_MANIFEST
    smoke_manifest = load_json(smoke_manifest_path)
    run_dir = artifact_dir / smoke_manifest["run_directory"]
    smoke = compare_smoke(
        manifest_path=smoke_manifest_path,
        artifact_dir=artifact_dir,
        run_dir=run_dir,
    )
    if smoke["status"] != "SMOKE_FEASIBILITY_FAILED（Smoke可行性失敗）":
        raise ValueError("R4 completed feasibility failure required")

    selection = select_focus_candidates(smoke_manifest=smoke_manifest, run_dir=run_dir)
    selection_path = artifact_dir / SELECTION_FILE
    write_new_or_identical(selection_path, canonical_bytes(selection))
    selection_rows = {row["review_id"]: row for row in selection["rows"]}

    option_manifest = load_json(artifact_dir / SOURCE_OPTION_MANIFEST)
    option_rows = {row["review_id"]: row for row in option_manifest["rows"]}
    source_rows = {
        row["review_id"]: row
        for row in load_json(artifact_dir / "stage_b1a1_input_manifest_candidate_r2.json")[
            "rows"
        ]
    }

    output_dir = artifact_dir / OUTPUT_DIRECTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    total_candidates = 0
    total_options = 0
    for smoke_row in smoke_manifest["rows"]:
        review_id = str(smoke_row["review_id"])
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
        packet = build_focus_packet(
            source_packet=load_json(source_path),
            option_catalog=load_json(option_path),
            selected_ids=selected_ids,
        )
        output_path = output_dir / f"{review_id}.json"
        write_new_or_identical(output_path, canonical_bytes(packet))
        total_candidates += len(packet["selected_focus_segments"])
        total_options += len(packet["evidence_options"])
        rows.append(
            {
                "review_id": review_id,
                "as_of": packet["as_of"],
                "input_packet_file": output_path.name,
                "input_packet_sha256": sha256_path(output_path),
                "selected_candidate_count": len(packet["selected_focus_segments"]),
                "evidence_option_count": len(packet["evidence_options"]),
                "source_packet_sha256": sha256_path(source_path),
                "source_option_catalog_sha256": sha256_path(option_path),
            }
        )

    rows.sort(key=lambda row: row["review_id"])
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "status": "FOCUSED_INPUTS_COMPLETE_NOT_READY_FOR_AI（聚焦輸入完成、尚不可執行AI）",
        "case_count": len(rows),
        "selected_candidate_count": total_candidates,
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
        "source_smoke_manifest_file": SOURCE_SMOKE_MANIFEST,
        "source_smoke_manifest_sha256": sha256_path(smoke_manifest_path),
        "selection_file": SELECTION_FILE,
        "selection_sha256": sha256_path(selection_path),
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
    print(
        json.dumps(
            {
                key: manifest[key]
                for key in (
                    "status",
                    "case_count",
                    "selected_candidate_count",
                    "evidence_option_count",
                    "formal_ai_calls",
                    "ready_for_formal_ai",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
