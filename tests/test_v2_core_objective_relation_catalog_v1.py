from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from scripts.v2_core_objective_candidate_catalog_v1 import (
    ARTIFACT_DIR,
    OUTPUT_DIRECTORY as SEGMENT_DIRECTORY,
    canonical_bytes,
    sha256_path,
)
from scripts.v2_core_objective_relation_catalog_v1 import (
    OUTPUT_DIRECTORY,
    SCHEMA_FILE,
    SEGMENT_MANIFEST_FILE,
    build_relations,
    validate_relation_catalog,
)


def _schema() -> dict:
    return json.loads((ARTIFACT_DIR / SCHEMA_FILE).read_text(encoding="utf-8"))


def _first_segment_catalog() -> tuple[dict, str]:
    manifest = json.loads((ARTIFACT_DIR / SEGMENT_MANIFEST_FILE).read_text(encoding="utf-8"))
    row = manifest["rows"][0]
    path = ARTIFACT_DIR / SEGMENT_DIRECTORY / row["catalog_file"]
    return json.loads(path.read_text(encoding="utf-8")), sha256_path(path)


def test_relation_schema_is_valid() -> None:
    Draft202012Validator.check_schema(_schema())


def test_relation_catalog_is_deterministic_and_not_course_judgement() -> None:
    segment_catalog, segment_hash = _first_segment_catalog()
    first = build_relations(segment_catalog, segment_hash)
    second = build_relations(segment_catalog, segment_hash)
    assert canonical_bytes(first) == canonical_bytes(second)
    assert first["generator_contract"] == {
        "candidate_only": True,
        "ai_course_relation_judgement_included": False,
        "future_performance_used": False,
        "sealed_labels_used": False,
        "identity_used": False,
        "source_segment_catalog_unchanged": True,
    }
    assert len(first["prompt_shortlist_relation_ids"]) <= 30
    assert all(row["not_course_relation_judgement"] for row in first["objective_relation_candidates"])
    validate_relation_catalog(first, segment_catalog, _schema())


def test_all_48_segment_catalogs_build_causal_relation_candidates() -> None:
    manifest = json.loads((ARTIFACT_DIR / SEGMENT_MANIFEST_FILE).read_text(encoding="utf-8"))
    schema = _schema()
    built = []
    for row in manifest["rows"]:
        path = ARTIFACT_DIR / SEGMENT_DIRECTORY / row["catalog_file"]
        before = sha256_path(path)
        assert before == row["catalog_sha256"]
        segment_catalog = json.loads(path.read_text(encoding="utf-8"))
        relation_catalog = build_relations(segment_catalog, before)
        validate_relation_catalog(relation_catalog, segment_catalog, schema)
        assert sha256_path(path) == before
        assert relation_catalog["as_of"] == segment_catalog["as_of"]
        built.append(relation_catalog)
    assert len(built) == 48
    assert sum(len(row["objective_relation_candidates"]) for row in built) > 0


def test_persisted_relation_manifest_revalidates_all_hashes() -> None:
    manifest_path = ARTIFACT_DIR / "objective_relation_catalog_manifest_r1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema_path = ARTIFACT_DIR / manifest["relation_schema_file"]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert manifest["case_count"] == 48
    assert manifest["formal_ai_calls"] == 0
    assert manifest["future_performance_used"] is False
    assert manifest["sealed_labels_used"] is False
    assert manifest["identity_used"] is False
    assert manifest["course_relation_judgement_generated_by_program"] is False
    assert manifest["relation_catalog_directory"] == OUTPUT_DIRECTORY
    assert manifest["relation_schema_sha256"] == sha256_path(schema_path)
    for row in manifest["rows"]:
        segment_path = ARTIFACT_DIR / manifest["source_segment_catalog_directory"] / row["source_segment_catalog_file"]
        relation_path = ARTIFACT_DIR / manifest["relation_catalog_directory"] / row["relation_catalog_file"]
        assert sha256_path(segment_path) == row["source_segment_catalog_sha256"]
        assert sha256_path(relation_path) == row["relation_catalog_sha256"]
        segment_catalog = json.loads(segment_path.read_text(encoding="utf-8"))
        relation_catalog = json.loads(relation_path.read_text(encoding="utf-8"))
        validate_relation_catalog(relation_catalog, segment_catalog, schema)
