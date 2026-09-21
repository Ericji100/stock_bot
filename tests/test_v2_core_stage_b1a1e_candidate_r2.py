from __future__ import annotations

from pathlib import Path

from scripts.v2_core_stage_b1a1e_candidate_validator_v1 import (
    load_json,
    validate_stage_b1a1e_response,
)
from scripts.v2_core_stage_b1a1e_focus_input_v2 import (
    ARTIFACT_DIR,
    OUTPUT_DIRECTORY,
    generate_all,
)
from scripts.v2_core_stage_b1a1e_smoke_compare_v1 import compare_smoke
from scripts.v2_core_stage_b1a1e_smoke_manifest_v2 import build_manifest


def _insufficient_response(packet):
    options = {}
    for option in packet["evidence_options"]:
        options.setdefault(option["candidate_id"], {}).setdefault(
            option["atom_name"], []
        ).append(option)
    rows = []
    for segment in packet["selected_focus_segments"]:
        candidate_id = segment["candidate_id"]
        directional = options[candidate_id]["directional_coherence"][0]
        rows.append(
            {
                "candidate_id": candidate_id,
                "directional_path": {
                    "evidence_option_id": directional["evidence_option_id"],
                    "judgement": "INSUFFICIENT",
                    "reason_code": "REQUIRED_EVIDENCE_INSUFFICIENT",
                },
                "structural_targets": [
                    {
                        "evidence_option_id": option["evidence_option_id"],
                        "same_level_meaningful_target": {
                            "judgement": "INSUFFICIENT",
                            "reason_code": "REQUIRED_EVIDENCE_INSUFFICIENT",
                        },
                        "challenge_or_break_realized": {
                            "judgement": (
                                "INSUFFICIENT"
                                if option["target"]["price_reached_or_crossed_objective"]
                                else "CONTRADICTS"
                            ),
                            "reason_code": (
                                "REQUIRED_EVIDENCE_INSUFFICIENT"
                                if option["target"]["price_reached_or_crossed_objective"]
                                else "VISIBLE_EVIDENCE_CONTRADICTS"
                            ),
                        },
                    }
                    for option in options[candidate_id]["structural_challenge_or_break"]
                ],
            }
        )
    return {
        "schema_version": "v2-core-stage-b1a1e-focus-r2-candidate",
        "candidate_perceptions": rows,
    }


def test_r2_schema_and_validator_accept_complete_fixed_options():
    manifest = generate_all(ARTIFACT_DIR)
    row = manifest["rows"][0]
    packet = load_json(ARTIFACT_DIR / OUTPUT_DIRECTORY / row["input_packet_file"])
    schema = load_json(ARTIFACT_DIR / "v2_core_stage_b1a1e.schema.candidate_r2.json")
    result = validate_stage_b1a1e_response(
        response=_insufficient_response(packet), schema=schema, packet=packet
    )
    assert result["status"] == "VALID"
    assert result["submitted_candidate_count"] == row["selected_candidate_count"]


def test_r2_manifest_is_hash_bound_and_preflight_is_incomplete(tmp_path: Path):
    manifest = build_manifest(ARTIFACT_DIR)
    assert manifest["status"] == "READY_FOR_FORMAL_AI_SMOKE（可執行正式AI Smoke）"
    assert manifest["expected_case_rounds"] == 12
    report = compare_smoke(
        manifest_path=ARTIFACT_DIR
        / "stage_b1a1e_smoke_execution_manifest_candidate_r2.json",
        artifact_dir=ARTIFACT_DIR,
        run_dir=tmp_path,
    )
    assert report["status"] == "NOT_READY_AI_RUNS_INCOMPLETE（AI三輪尚未完成）"
    assert report["completed_case_rounds"] == 0
    assert report["metrics_published"] is False
