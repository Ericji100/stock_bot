from __future__ import annotations

import json
from pathlib import Path

from scripts.v2_core_stage_b1a_input_packet_v1 import (
    ARTIFACT_DIR,
    FOCUS_CATALOG_DIRECTORY,
    FOCUS_MANIFEST_FILE,
    OUTPUT_DIRECTORY,
    SEGMENT_CATALOG_DIRECTORY,
    SOURCE_PACKET_DIRECTORY,
    build_stage_b1a_input,
    generate_all,
    load_json,
    sha256_path,
)


def _first_inputs():
    focus_manifest = load_json(ARTIFACT_DIR / FOCUS_MANIFEST_FILE)
    row = focus_manifest["rows"][0]
    source_path = ARTIFACT_DIR / SOURCE_PACKET_DIRECTORY / f"{row['review_id']}.json"
    segment_path = ARTIFACT_DIR / SEGMENT_CATALOG_DIRECTORY / row["source_segment_catalog_file"]
    focus_path = ARTIFACT_DIR / FOCUS_CATALOG_DIRECTORY / row["focus_catalog_file"]
    return (
        load_json(source_path),
        source_path,
        load_json(segment_path),
        segment_path,
        load_json(focus_path),
        focus_path,
    )


def test_build_input_is_deterministic_and_blind():
    source, source_path, segments, segment_path, focus, focus_path = _first_inputs()
    kwargs = {
        "source_packet": source,
        "source_packet_sha256": sha256_path(source_path),
        "segment_catalog": segments,
        "segment_catalog_sha256": sha256_path(segment_path),
        "focus_catalog": focus,
        "focus_catalog_sha256": sha256_path(focus_path),
    }
    first = build_stage_b1a_input(**kwargs)
    second = build_stage_b1a_input(**kwargs)
    assert first == second
    rendered = json.dumps(first, ensure_ascii=False).lower()
    assert "mfe" not in rendered
    assert "mae" not in rendered
    assert "profit" not in rendered
    assert first["review_constraints"]["identity_blind"] is True
    assert first["daily_structure_context_to_as_of"][-1]["date"] == first["as_of"]
    assert 1 <= len(first["focus_segments"]) <= 20


def test_focus_ids_and_full_segment_ids_match_exactly():
    source, source_path, segments, segment_path, focus, focus_path = _first_inputs()
    packet = build_stage_b1a_input(
        source_packet=source,
        source_packet_sha256=sha256_path(source_path),
        segment_catalog=segments,
        segment_catalog_sha256=sha256_path(segment_path),
        focus_catalog=focus,
        focus_catalog_sha256=sha256_path(focus_path),
    )
    assert {row["candidate_id"] for row in packet["focus_segments"]} == {
        row["segment_id"] for row in focus["focus_segments"]
    }


def test_all_48_persisted_inputs_are_hash_bound_and_as_of_clean():
    manifest = generate_all(ARTIFACT_DIR)
    assert manifest["case_count"] == 48
    assert manifest["formal_ai_calls"] == 0
    assert manifest["ready_for_formal_ai"] is False
    assert manifest["future_performance_used"] is False
    assert manifest["identity_used"] is False
    assert manifest["sealed_labels_used"] is False
    assert (ARTIFACT_DIR / "stage_b1a_input_manifest_candidate_r1.md").exists()
    for row in manifest["rows"]:
        path = ARTIFACT_DIR / OUTPUT_DIRECTORY / row["input_packet_file"]
        packet = load_json(path)
        assert sha256_path(path) == row["input_packet_sha256"]
        assert packet["as_of"] == row["latest_visible_date"]
        assert packet["daily_structure_context_to_as_of"][-1]["date"] == packet["as_of"]
        assert len(packet["focus_segments"]) == row["focus_segment_count"]
