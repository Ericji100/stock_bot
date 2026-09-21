from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import hybrid_v3_mature_s1_track_v3 as track
from scripts.hybrid_v3_sharding_v2 import canonical_sha256, file_sha256


def test_v3_required_graph_adds_native_v3_track_and_evaluator() -> None:
    required = set(track.REQUIRED_COMPONENTS)
    assert "scripts/hybrid_v3_mature_s1_track_v1.py" in required
    assert "scripts/hybrid_v3_mature_s1_track_v2.py" in required
    assert "scripts/hybrid_v3_mature_s1_track_v3.py" in required
    assert "scripts/hybrid_v3_mature_s1_consistency_v1.py" in required
    assert "scripts/hybrid_v3_mature_s1_consistency_v2.py" in required
    assert "scripts/hybrid_v3_mature_s1_consistency_v3.py" in required
    assert "config/hybrid_v3_mature_s1_stage_v2.json" in required
    assert "scripts/hybrid_v3_atomic_packets_v2.py" in required
    assert "config/hybrid_v3_mature_s1_stage_v2.json" in required


def test_missing_native_v3_evaluator_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    for relative in track.REQUIRED_COMPONENTS:
        if relative.endswith("hybrid_v3_mature_s1_consistency_v3.py"):
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n", encoding="utf-8")
    with pytest.raises(track.MatureS1TrackV3Error, match="required freeze component missing"):
        track.build_component_graph(root)


def test_published_v2_rejection_is_technical_and_keeps_performance_sealed() -> None:
    path = track.DEFAULT_V2_TRACK / "preflight_rejection.json"
    if not path.exists():
        pytest.skip("V2 immutable rejection is published by the V3 builder preflight")
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["status"] == "PREFLIGHT_REJECTED_TECHNICAL_ONLY"
    assert value["technical_only"] is True
    assert value["ai_executed"] is False
    assert value["performance_unsealed"] is False
    assert value["performance_remains_sealed"] is True
    assert value["strategy_or_gate_change"] is False
    assert value["rejection_sha256"] == canonical_sha256(
        {key: child for key, child in value.items() if key != "rejection_sha256"}
    )


def test_integrity_complete_track_reuses_exact_v1_packets_and_pins_all_contracts(tmp_path: Path) -> None:
    if not (track.ROOT / "scripts/hybrid_v3_mature_s1_consistency_v3.py").exists():
        pytest.skip("native consistency V3 is delivered by the parallel transport task")
    output = tmp_path / "research_track_v3_integrity_complete"
    result = track.prepare(output_dir=output)
    assert result["cases"] == 36
    assert result["case_keys_are_new"] is True
    assert result["future_or_performance_visible"] is False
    assert (output / "primary_packets.jsonl").read_bytes() == (
        track.DEFAULT_SOURCE_TRACK / "primary_packets.jsonl"
    ).read_bytes()
    freeze = json.loads((output / "research_execution_freeze.json").read_text(encoding="utf-8"))
    assert freeze["performance_visible"] is False
    assert freeze["old_ai_output_visible"] is False
    assert freeze["stage_protocol_sha256"] == file_sha256(
        track.ROOT / "config/hybrid_v3_mature_s1_stage_v2.json"
    )
    assert {row["name"] for row in freeze["v2_components"]} == {
        "launcher", "reviewer", "runner", "policy", "prompt", "schema", "protocol"
    }
    assert {row["name"] for row in freeze["stage_components"]} >= {
        "track_v1", "track_v2", "track_v3",
        "consistency_v1", "consistency_v2", "consistency_v3", "stage_protocol",
    }
    graph = freeze["component_graph"]
    assert graph["graph_sha256"] == canonical_sha256(
        {key: child for key, child in graph.items() if key != "graph_sha256"}
    )
    manifest = json.loads((output / "primary_packets.manifest.json").read_text(encoding="utf-8"))
    assert manifest["sampling_metadata_inside_packet"] is False
    assert manifest["identity_visible_inside_packet"] is False
    assert manifest["future_or_performance_visible_inside_packet"] is False
    assert manifest["selection_plan_sha256"] == file_sha256(output / "selection_plan.json")
    assert all(row["sampling_focus"] in row["eligible_stage_focuses"] for row in manifest["mapping"])
    assignment = json.loads((output / "primary_cases/assignment.manifest.json").read_text(encoding="utf-8"))
    assert assignment["coverage"] == {
        "expected": 36, "covered": 36, "missing": 0, "overlap": 0, "unexpected": 0,
    }
