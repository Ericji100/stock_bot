from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from scripts.v2_core_objective_candidate_catalog_v1 import (
    ARTIFACT_DIR,
    OUTPUT_DIRECTORY as SEGMENT_DIRECTORY,
    canonical_bytes,
    sha256_path,
)
from scripts.v2_core_objective_focus_catalog_v1 import (
    OUTPUT_DIRECTORY,
    SCHEMA_FILE,
    SEGMENT_MANIFEST_FILE,
    RELATION_MANIFEST_FILE,
    build_focus_catalog,
    validate_focus_catalog,
)
from scripts.v2_core_objective_relation_catalog_v1 import (
    OUTPUT_DIRECTORY as RELATION_DIRECTORY,
)


def _schema() -> dict:
    return json.loads((ARTIFACT_DIR / SCHEMA_FILE).read_text(encoding="utf-8"))


def _catalog_pair(review_id: str | None = None) -> tuple[dict, str, dict, str]:
    segment_manifest = json.loads((ARTIFACT_DIR / SEGMENT_MANIFEST_FILE).read_text(encoding="utf-8"))
    relation_manifest = json.loads((ARTIFACT_DIR / RELATION_MANIFEST_FILE).read_text(encoding="utf-8"))
    segment_row = next(
        row for row in segment_manifest["rows"]
        if review_id is None or row["review_id"] == review_id
    )
    relation_row = next(row for row in relation_manifest["rows"] if row["review_id"] == segment_row["review_id"])
    segment_path = ARTIFACT_DIR / SEGMENT_DIRECTORY / segment_row["catalog_file"]
    relation_path = ARTIFACT_DIR / RELATION_DIRECTORY / relation_row["relation_catalog_file"]
    return (
        json.loads(segment_path.read_text(encoding="utf-8")),
        sha256_path(segment_path),
        json.loads(relation_path.read_text(encoding="utf-8")),
        sha256_path(relation_path),
    )


def test_focus_schema_is_valid() -> None:
    Draft202012Validator.check_schema(_schema())


def test_focus_catalog_is_deterministic_and_objective_only() -> None:
    segment, segment_hash, relation, relation_hash = _catalog_pair()
    first = build_focus_catalog(segment, segment_hash, relation, relation_hash)
    second = build_focus_catalog(segment, segment_hash, relation, relation_hash)
    assert canonical_bytes(first) == canonical_bytes(second)
    assert len(first["focus_segments"]) <= 20
    assert len(first["focus_relation_ids"]) <= 16
    assert len(first["focus_stop_ids"]) <= 12
    assert first["generator_contract"]["future_performance_used"] is False
    assert first["generator_contract"]["sealed_labels_used"] is False
    assert first["generator_contract"]["identity_used"] is False
    assert first["generator_contract"]["ai_course_judgement_included"] is False
    validate_focus_catalog(first, segment, relation, _schema())


def test_all_48_focus_catalogs_cover_both_directions_and_multiple_scales() -> None:
    segment_manifest = json.loads((ARTIFACT_DIR / SEGMENT_MANIFEST_FILE).read_text(encoding="utf-8"))
    schema = _schema()
    built = []
    for row in segment_manifest["rows"]:
        segment, segment_hash, relation, relation_hash = _catalog_pair(row["review_id"])
        focus = build_focus_catalog(segment, segment_hash, relation, relation_hash)
        validate_focus_catalog(focus, segment, relation, schema)
        segment_map = {item["candidate_id"]: item for item in segment["objective_segment_candidates"]}
        focused = [segment_map[item["segment_id"]] for item in focus["focus_segments"]]
        assert {item["direction"] for item in focused} == {"UP", "DOWN"}
        assert {item["scale"] for item in focused} >= {"LARGE", "SMALL", "AUXILIARY"}
        assert any(item["status"] == "FORMING" for item in focused)
        built.append(focus)
    assert len(built) == 48


def test_persisted_focus_manifest_revalidates_all_hashes() -> None:
    manifest_path = ARTIFACT_DIR / "objective_focus_catalog_manifest_r1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema_path = ARTIFACT_DIR / manifest["focus_schema_file"]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert manifest["case_count"] == 48
    assert manifest["formal_ai_calls"] == 0
    assert manifest["future_performance_used"] is False
    assert manifest["sealed_labels_used"] is False
    assert manifest["identity_used"] is False
    assert manifest["course_judgement_generated_by_program"] is False
    assert manifest["focus_catalog_directory"] == OUTPUT_DIRECTORY
    assert manifest["focus_schema_sha256"] == sha256_path(schema_path)
    for row in manifest["rows"]:
        segment_path = ARTIFACT_DIR / manifest["segment_catalog_directory"] / row["source_segment_catalog_file"]
        relation_path = ARTIFACT_DIR / manifest["relation_catalog_directory"] / row["source_relation_catalog_file"]
        focus_path = ARTIFACT_DIR / manifest["focus_catalog_directory"] / row["focus_catalog_file"]
        assert sha256_path(segment_path) == row["source_segment_catalog_sha256"]
        assert sha256_path(relation_path) == row["source_relation_catalog_sha256"]
        assert sha256_path(focus_path) == row["focus_catalog_sha256"]
        segment = json.loads(segment_path.read_text(encoding="utf-8"))
        relation = json.loads(relation_path.read_text(encoding="utf-8"))
        focus = json.loads(focus_path.read_text(encoding="utf-8"))
        validate_focus_catalog(focus, segment, relation, schema)
