from __future__ import annotations

import json

from scripts.v2_core_objective_candidate_catalog_v1 import sha256_path
from scripts.v2_core_stage_b1a1_evidence_options_v1 import (
    ARTIFACT_DIR,
    OUTPUT_DIRECTORY,
    build_evidence_option_catalog,
    generate_all,
    load_json,
)


def test_all_48_option_catalogs_are_hash_bound_blind_and_not_ai_ready():
    manifest = generate_all(ARTIFACT_DIR)
    assert manifest["case_count"] == 48
    assert manifest["formal_ai_calls"] == 0
    assert manifest["ready_for_formal_ai"] is False
    assert manifest["future_performance_used"] is False
    assert manifest["identity_used"] is False
    assert manifest["sealed_labels_used"] is False
    assert manifest["course_judgement_generated_by_program"] is False
    assert manifest["total_evidence_options"] > 0
    assert set(manifest["atom_option_counts"]) == {
        "directional_coherence",
        "structural_challenge_or_break",
        "invalidation_traceability",
        "as_of_relation_link",
    }
    for row in manifest["rows"]:
        path = ARTIFACT_DIR / OUTPUT_DIRECTORY / row["catalog_file"]
        assert sha256_path(path) == row["catalog_sha256"]
        catalog = load_json(path)
        assert catalog["as_of"] == row["as_of"]
        assert catalog["source_packet_sha256"] == row["source_packet_sha256"]
        assert all(option["not_course_judgement"] for option in catalog["evidence_options"])


def test_options_use_only_existing_as_of_evidence_and_one_path_bundle_per_segment():
    manifest = generate_all(ARTIFACT_DIR)
    for row in manifest["rows"]:
        packet = load_json(
            ARTIFACT_DIR / "stage_b1a1_input_packets_candidate_r2" / row["source_packet_file"]
        )
        catalog = load_json(ARTIFACT_DIR / OUTPUT_DIRECTORY / row["catalog_file"])
        valid_refs = {item["ref"] for item in packet["evidence_catalog"]}
        directional_counts = {segment["candidate_id"]: 0 for segment in packet["focus_segments"]}
        for option in catalog["evidence_options"]:
            assert set(option["source_evidence_refs"]) <= valid_refs
            if option["atom_name"] == "directional_coherence":
                directional_counts[option["candidate_id"]] += 1
            if option["atom_name"] == "structural_challenge_or_break":
                target = option["target"]
                assert target["target_source_date"] < next(
                    segment["start_date"]
                    for segment in packet["focus_segments"]
                    if segment["candidate_id"] == option["candidate_id"]
                )
                assert target["target_confirmation_date"] <= next(
                    segment["observed_through"]
                    for segment in packet["focus_segments"]
                    if segment["candidate_id"] == option["candidate_id"]
                )
        assert set(directional_counts.values()) == {1}


def test_generation_is_deterministic_and_contains_no_performance_fields():
    first = generate_all(ARTIFACT_DIR)
    second = generate_all(ARTIFACT_DIR)
    assert first == second
    rendered = json.dumps(first, ensure_ascii=False).lower()
    assert "mfe" not in rendered
    assert "mae" not in rendered
    assert "profit" not in rendered


def test_single_catalog_ids_are_unique_and_no_course_verdict_is_generated():
    manifest = generate_all(ARTIFACT_DIR)
    row = manifest["rows"][0]
    packet_path = (
        ARTIFACT_DIR / "stage_b1a1_input_packets_candidate_r2" / row["source_packet_file"]
    )
    packet = load_json(packet_path)
    catalog = build_evidence_option_catalog(packet, sha256_path(packet_path))
    ids = [option["evidence_option_id"] for option in catalog["evidence_options"]]
    assert len(ids) == len(set(ids))
    rendered = json.dumps(catalog, ensure_ascii=False)
    assert '"result"' not in rendered
    assert '"verdict"' not in rendered
