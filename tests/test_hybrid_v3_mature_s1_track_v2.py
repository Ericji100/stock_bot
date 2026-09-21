from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import hybrid_v3_mature_s1_track_v2 as track
from scripts.hybrid_v3_sharding_v2 import canonical_json_bytes, canonical_sha256, file_sha256


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _jsonl(rows: list[dict]) -> bytes:
    return b"".join(canonical_json_bytes(row) + b"\n" for row in rows)


def _fake_repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    for relative in track.REQUIRED_COMPONENTS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".py":
            path.write_text("from __future__ import annotations\n", encoding="utf-8")
        else:
            path.write_text("{}\n", encoding="utf-8")

    # Exercise transitive discovery in addition to the mandatory anchor list.
    transitive = root / "scripts/transitive_only.py"
    transitive.write_text("VALUE = 1\n", encoding="utf-8")
    (root / "scripts/hybrid_v3_codex_reviewer_v2.py").write_text(
        "from . import transitive_only\n", encoding="utf-8"
    )

    source = root / "reports/source/research_track_v1"
    packets = [
        {
            "review_id": f"D-{index:02d}",
            "anonymous_stock_id": f"S-{index:02d}",
            "as_of": f"2023-{6 + index % 6:02d}-{1 + index // 6:02d}",
            "evidence": [],
        }
        for index in range(track.EXPECTED_CASES)
    ]
    packet_payload = _jsonl(packets)
    packet_path = source / "primary_packets.jsonl"
    packet_path.parent.mkdir(parents=True, exist_ok=True)
    packet_path.write_bytes(packet_payload)
    mapping = [
        {
            "selected_ordinal": index,
            "review_id": packet["review_id"],
            "anonymous_stock_id": packet["anonymous_stock_id"],
            "packet_sha256": canonical_sha256(packet),
        }
        for index, packet in enumerate(packets)
    ]
    packet_manifest_core = {
        "status": "LOCKED_OUTCOME_BLIND",
        "rows": track.EXPECTED_CASES,
        "packets_canonical_jsonl_sha256": file_sha256(packet_path),
        "mapping": mapping,
    }
    packet_manifest = {
        **packet_manifest_core,
        "manifest_sha256": canonical_sha256(packet_manifest_core),
    }
    packet_manifest_path = source / "primary_packets.manifest.json"
    _write_json(packet_manifest_path, packet_manifest)

    freeze_path = source / "research_execution_freeze.json"
    _write_json(freeze_path, {"status": "FROZEN_IMMUTABLE_BEFORE_AI_EXECUTION"})
    selection_rows = [
        {
            "selected_ordinal": index,
            "review_id": packet["review_id"],
            "anonymous_stock_id": packet["anonymous_stock_id"],
            "sampling_focus": "MATURE_OBJECTIVE_PROXY",
            "eligible_stage_focuses": ["MATURE_OBJECTIVE_PROXY"],
        }
        for index, packet in enumerate(packets)
    ]
    selection = {
        "status": "LOCKED_OUTCOME_BLIND",
        "cases": track.EXPECTED_CASES,
        "rows": selection_rows,
        "rows_sha256": canonical_sha256(selection_rows),
    }
    selection_path = source / "selection_plan.json"
    _write_json(selection_path, selection)
    parent_cases = []
    for index, packet in enumerate(packets):
        parent_cases.append({**packet, "case_key": f"{index + 1:064x}"})
    shard_path = source / "primary_cases/shard_00.jsonl"
    shard_path.parent.mkdir(parents=True, exist_ok=True)
    shard_path.write_bytes(_jsonl(parent_cases))
    _write_json(
        source / "primary_cases/assignment.manifest.json",
        {
            "shards": [
                {
                    "path": str(shard_path),
                    "rows": len(parent_cases),
                    "sha256": file_sha256(shard_path),
                }
            ]
        },
    )
    track_core = {
        "track_version": "hybrid-v3-mature-s1-research-track-v1",
        "status": track.TRACK_STATUS,
        "cases": track.EXPECTED_CASES,
        "runs": 3,
        "outcome_blind": True,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "performance_must_remain_sealed_until_prior_gates_pass": True,
        "packet_path": str(packet_path),
        "packet_sha256": file_sha256(packet_path),
        "packet_manifest_path": str(packet_manifest_path),
        "packet_manifest_sha256": file_sha256(packet_manifest_path),
        "execution_freeze_path": str(freeze_path),
        "execution_freeze_sha256": file_sha256(freeze_path),
        "selection_plan_path": str(selection_path),
        "selection_plan_sha256": file_sha256(selection_path),
    }
    _write_json(
        source / "research_track_manifest.json",
        {**track_core, "track_manifest_sha256": canonical_sha256(track_core)},
    )
    return root, source


def test_required_graph_names_every_integrity_component() -> None:
    required = set(track.REQUIRED_COMPONENTS)
    assert {
        "scripts/hybrid_v3_mature_s1_track_v1.py",
        "scripts/hybrid_v3_mature_s1_track_v2.py",
        "scripts/hybrid_v3_mature_s1_consistency_v1.py",
        "scripts/hybrid_v3_mature_s1_consistency_v2.py",
        "scripts/hybrid_v3_atomic_runner_v2.py",
        "scripts/hybrid_v3_sharding_v2.py",
        "scripts/hybrid_v3_codex_reviewer_v3.py",
        "scripts/hybrid_v3_codex_reviewer_v2.py",
        "scripts/hybrid_v3_atomic_policy_v3.py",
        "scripts/hybrid_v3_atomic_policy_v2.py",
        "scripts/hybrid_v3_atomic_packets_v2.py",
        "scripts/hybrid_v3_conservative_merge_v1.py",
        "scripts/hybrid_v3_consistency_v2.py",
        "scripts/hybrid_v3_consistency_v3.py",
        "scripts/hybrid_v3_triplicate_smoke_audit.py",
        "scripts/hybrid_v3_codex_launcher_v2.py",
        "config/hybrid_semantic_prompt_v3.md",
        "config/hybrid_atomic_semantics_v2.schema.json",
        "config/hybrid_monitoring_research_protocol_v1.json",
        "config/hybrid_v3_mature_s1_stage_v2.json",
    } <= required


def test_missing_required_component_fails_closed(tmp_path: Path) -> None:
    root, _ = _fake_repo(tmp_path)
    (root / "scripts/hybrid_v3_mature_s1_consistency_v2.py").unlink()
    with pytest.raises(track.MatureS1TrackV2Error, match="required freeze component missing"):
        track.build_component_graph(root)


def test_prepare_rekeys_and_reshards_same_locked_36_cases(tmp_path: Path) -> None:
    root, source = _fake_repo(tmp_path)
    output = root / "reports/output/research_track_v2_integrity"
    result = track.prepare(
        root=root,
        source_track_dir=source,
        output_dir=output,
        shard_count=4,
    )

    assert result["cases"] == 36
    assert result["case_keys_are_new"] is True
    assert result["outcome_blind"] is True
    assert result["identity_visible"] is False
    assert result["future_or_performance_visible"] is False

    freeze = json.loads((output / "research_execution_freeze.json").read_text())
    graph = freeze["component_graph"]
    paths = {node["relative_path"] for node in graph["nodes"]}
    assert set(track.REQUIRED_COMPONENTS) <= paths
    assert "scripts/transitive_only.py" in paths
    assert graph["graph_sha256"] == canonical_sha256(
        {key: value for key, value in graph.items() if key != "graph_sha256"}
    )
    assert {row["name"] for row in freeze["v2_components"]} == {
        "launcher", "reviewer", "runner", "policy", "prompt", "schema", "protocol"
    }
    assert {row["name"] for row in freeze["stage_components"]} >= {
        "track_v1", "track_v2", "consistency_v1", "consistency_v2", "stage_protocol"
    }
    assert freeze["stage_protocol_sha256"] == file_sha256(
        root / "config/hybrid_v3_mature_s1_stage_v2.json"
    )

    copied = (output / "primary_packets.jsonl").read_bytes()
    assert copied == (source / "primary_packets.jsonl").read_bytes()
    assert (output / "selection_plan.json").read_bytes() == canonical_json_bytes(
        json.loads((source / "selection_plan.json").read_text(encoding="utf-8"))
    ) + b"\n"
    packet_manifest = json.loads(
        (output / "primary_packets.manifest.json").read_text(encoding="utf-8")
    )
    assert packet_manifest["sampling_metadata_inside_packet"] is False
    assert packet_manifest["identity_visible_inside_packet"] is False
    assert packet_manifest["future_or_performance_visible_inside_packet"] is False
    assert packet_manifest["selection_plan_sha256"] == file_sha256(
        output / "selection_plan.json"
    )
    assert all(row["sampling_focus"] for row in packet_manifest["mapping"])
    assert all(row["eligible_stage_focuses"] for row in packet_manifest["mapping"])
    assignment = json.loads(
        (output / "primary_cases/assignment.manifest.json").read_text(encoding="utf-8")
    )
    assert assignment["coverage"] == {
        "expected": 36,
        "covered": 36,
        "missing": 0,
        "overlap": 0,
        "unexpected": 0,
    }
    new_keys = {
        row["case_key"]
        for shard in assignment["shards"]
        for row in (
            json.loads(line)
            for line in Path(shard["path"]).read_text(encoding="utf-8").splitlines()
            if line
        )
    }
    assert len(new_keys) == 36
    assert not new_keys.intersection({f"{index + 1:064x}" for index in range(36)})


def test_source_packet_with_ai_output_is_rejected(tmp_path: Path) -> None:
    root, source = _fake_repo(tmp_path)
    packet_path = source / "primary_packets.jsonl"
    packets = [
        json.loads(line)
        for line in packet_path.read_text(encoding="utf-8").splitlines()
    ]
    packets[0]["ai_output"] = {"permission": "TRADE"}
    packet_path.write_bytes(_jsonl(packets))
    # Repair outer hashes so the semantic prohibition, not a stale hash, is tested.
    manifest_path = source / "primary_packets.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["packets_canonical_jsonl_sha256"] = file_sha256(packet_path)
    manifest["mapping"][0]["packet_sha256"] = canonical_sha256(packets[0])
    manifest["manifest_sha256"] = canonical_sha256(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    _write_json(manifest_path, manifest)
    parent = json.loads(
        (source / "research_track_manifest.json").read_text(encoding="utf-8")
    )
    parent["packet_sha256"] = file_sha256(packet_path)
    parent["packet_manifest_sha256"] = file_sha256(manifest_path)
    parent["track_manifest_sha256"] = canonical_sha256(
        {key: value for key, value in parent.items() if key != "track_manifest_sha256"}
    )
    _write_json(source / "research_track_manifest.json", parent)
    with pytest.raises(track.MatureS1TrackV2Error, match="output or outcome"):
        track.prepare(root=root, source_track_dir=source, output_dir=root / "out")
