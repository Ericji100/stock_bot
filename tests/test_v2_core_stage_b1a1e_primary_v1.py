from __future__ import annotations

import json
from pathlib import Path

from scripts.v2_core_stage_b1a1e_primary_input_v1 import ARTIFACT_DIR, CASE_COUNT, generate_all
from scripts.v2_core_stage_b1a1e_primary_manifest_v1 import build_manifest
from scripts.v2_core_stage_b1a1e_smoke_compare_v1 import compare_smoke


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def test_r3_primary_selection_is_independent_and_has_no_auxiliary_candidates():
    manifest = generate_all(ARTIFACT_DIR)
    selection = _load(ARTIFACT_DIR / "stage_b1a1e_primary_selection_candidate_r3.json")
    excluded = set(selection["excluded_prior_review_ids"])
    selected_reviews = {row["review_id"] for row in selection["rows"]}
    assert len(selected_reviews) == CASE_COUNT
    assert selected_reviews.isdisjoint(excluded)
    assert selection["uses_prior_ai_answers"] is False
    assert manifest["auxiliary_standalone_candidate_forbidden"] is True
    for row in selection["rows"]:
        assert row["selected_candidates"]
        assert {candidate["scale"] for candidate in row["selected_candidates"]} <= {
            "LARGE",
            "SMALL",
        }


def test_r3_packets_and_manifest_are_blind_and_preflight_empty(tmp_path: Path):
    input_manifest = generate_all(ARTIFACT_DIR)
    for row in input_manifest["rows"]:
        packet = _load(
            ARTIFACT_DIR
            / input_manifest["input_packet_directory"]
            / row["input_packet_file"]
        )
        assert {segment["scale"] for segment in packet["selected_focus_segments"]} <= {
            "LARGE",
            "SMALL",
        }
        constraints = packet["review_constraints"]
        assert constraints["auxiliary_standalone_candidate_forbidden"] is True
        assert constraints["selection_uses_prior_ai_answers"] is False
        assert constraints["future_performance_blind"] is True
    manifest = build_manifest(ARTIFACT_DIR)
    assert manifest["expected_case_rounds"] == CASE_COUNT * 3
    assert manifest["stop_remaining_percent_lte"] == 20
    report = compare_smoke(
        manifest_path=ARTIFACT_DIR
        / "stage_b1a1e_primary_execution_manifest_candidate_r3.json",
        artifact_dir=ARTIFACT_DIR,
        run_dir=tmp_path,
    )
    assert report["completed_case_rounds"] == 0
    assert report["metrics_published"] is False
