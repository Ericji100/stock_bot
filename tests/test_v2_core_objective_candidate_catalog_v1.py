from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from scripts.v2_core_objective_candidate_catalog_v1 import (
    ARTIFACT_DIR,
    CATALOG_VERSION,
    OUTPUT_DIRECTORY,
    SCHEMA_FILE,
    build_catalog,
    canonical_bytes,
    sha256_path,
    validate_catalog,
)


REVIEW_ID = "FP-004ab6003b74044860aa6f03"


def _inputs() -> tuple[dict, str, dict]:
    packet_path = ARTIFACT_DIR / "feasibility_probe_packets_r2" / f"{REVIEW_ID}.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    schema = json.loads((ARTIFACT_DIR / SCHEMA_FILE).read_text(encoding="utf-8"))
    return packet, sha256_path(packet_path), schema


def test_candidate_catalog_schema_is_valid() -> None:
    _, _, schema = _inputs()
    Draft202012Validator.check_schema(schema)


def test_catalog_is_deterministic_and_contains_only_objective_candidates() -> None:
    packet, packet_hash, schema = _inputs()
    first = build_catalog(packet, packet_hash, schema)
    second = build_catalog(packet, packet_hash, schema)
    assert canonical_bytes(first) == canonical_bytes(second)
    assert first["catalog_version"] == CATALOG_VERSION
    assert first["source_packet_sha256"] == packet_hash
    assert first["generator_contract"] == {
        "candidate_only": True,
        "ai_course_judgement_included": False,
        "future_performance_used": False,
        "sealed_labels_used": False,
        "identity_used": False,
        "source_packet_unchanged": True,
        "shortlist_uses_outcomes": False,
    }
    assert first["objective_segment_candidates"]
    assert first["objective_stop_candidates"]
    assert len(first["prompt_shortlist_segment_ids"]) <= 40
    assert len(first["prompt_shortlist_stop_ids"]) <= 20
    assert all(row["not_course_anchor_judgement"] for row in first["objective_segment_candidates"])
    assert all(row["not_course_stop_judgement"] for row in first["objective_stop_candidates"])
    assert all(row["not_course_trigger_judgement"] for row in first["objective_trigger_candidates"])


def test_catalog_is_asof_causal_and_shortlists_only_existing_ids() -> None:
    packet, packet_hash, schema = _inputs()
    catalog = build_catalog(packet, packet_hash, schema)
    as_of = catalog["as_of"]
    segment_ids = {row["candidate_id"] for row in catalog["objective_segment_candidates"]}
    stop_ids = {row["candidate_id"] for row in catalog["objective_stop_candidates"]}
    assert set(catalog["prompt_shortlist_segment_ids"]) <= segment_ids
    assert set(catalog["prompt_shortlist_stop_ids"]) <= stop_ids
    for row in catalog["objective_segment_candidates"]:
        assert row["start_date"] <= as_of
        assert row["observed_through"] <= as_of
        assert row["confirmed_end_date"] is None or row["confirmed_end_date"] <= as_of
        assert row["not_course_anchor_judgement"] is True
    for row in catalog["objective_stop_candidates"]:
        assert row["source_date"] <= as_of
        assert row["confirmed_on"] <= as_of
    for row in catalog["objective_trigger_candidates"]:
        assert row["event_date"] == as_of


def test_forming_segments_never_claim_a_confirmed_endpoint() -> None:
    packet, packet_hash, schema = _inputs()
    catalog = build_catalog(packet, packet_hash, schema)
    forming = [row for row in catalog["objective_segment_candidates"] if row["status"] == "FORMING"]
    assert forming
    assert all(row["confirmed_end_date"] is None for row in forming)
    assert all(row["confirmed_end_price"] is None for row in forming)


def test_all_48_public_probe_packets_build_without_sealed_or_future_data() -> None:
    manifest = json.loads((ARTIFACT_DIR / "feasibility_probe_manifest.json").read_text(encoding="utf-8"))
    schema = json.loads((ARTIFACT_DIR / SCHEMA_FILE).read_text(encoding="utf-8"))
    packet_dir = ARTIFACT_DIR / manifest["packet_directory"]
    built = []
    for item in manifest["rows"]:
        packet_path = packet_dir / item["packet_file"]
        before = sha256_path(packet_path)
        assert before == item["packet_sha256"]
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        catalog = build_catalog(packet, before, schema)
        assert sha256_path(packet_path) == before
        assert catalog["generator_contract"]["future_performance_used"] is False
        assert catalog["generator_contract"]["sealed_labels_used"] is False
        assert catalog["generator_contract"]["identity_used"] is False
        built.append(catalog)
    assert len(built) == 48
    assert all(catalog["objective_segment_candidates"] for catalog in built)
    assert all(catalog["objective_stop_candidates"] for catalog in built)


def test_persisted_manifest_revalidates_all_48_catalogs_and_hashes() -> None:
    manifest_path = ARTIFACT_DIR / "objective_candidate_catalog_manifest_r1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_manifest_path = ARTIFACT_DIR / manifest["source_manifest_file"]
    schema_path = ARTIFACT_DIR / manifest["catalog_schema_file"]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert manifest["case_count"] == 48
    assert manifest["formal_ai_calls"] == 0
    assert manifest["future_performance_used"] is False
    assert manifest["sealed_labels_used"] is False
    assert manifest["identity_used"] is False
    assert manifest["course_judgement_generated_by_program"] is False
    assert manifest["source_manifest_sha256"] == sha256_path(source_manifest_path)
    assert manifest["catalog_schema_sha256"] == sha256_path(schema_path)
    assert manifest["catalog_directory"] == OUTPUT_DIRECTORY
    for row in manifest["rows"]:
        source_path = ARTIFACT_DIR / manifest["source_packet_directory"] / row["source_packet_file"]
        catalog_path = ARTIFACT_DIR / manifest["catalog_directory"] / row["catalog_file"]
        assert sha256_path(source_path) == row["source_packet_sha256"]
        assert sha256_path(catalog_path) == row["catalog_sha256"]
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        validate_catalog(catalog, schema)
        assert catalog["source_packet_sha256"] == row["source_packet_sha256"]
