from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import hybrid_v3_mature_s1_incident_v1 as incident


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _make_fixture(tmp_path: Path, *, expected_cases: int = 2, expected_runs: int = 2):
    track_root = tmp_path / "research_track_v1"
    run_root = tmp_path / "run_root"
    track_root.mkdir()
    run_root.mkdir()

    component_files = {}
    for name in ("protocol", "prompt", "schema"):
        path = track_root / f"{name}.txt"
        path.write_text(f"frozen-{name}\n", encoding="utf-8")
        component_files[name] = path

    selection_path = track_root / "selection_plan.json"
    packet_path = track_root / "primary_packets.jsonl"
    packet_manifest_path = track_root / "primary_packets.manifest.json"
    freeze_path = track_root / "research_execution_freeze.json"
    assignment_path = track_root / "primary_cases/assignment.manifest.json"
    _write_json(selection_path, {"status": "LOCKED_OUTCOME_BLIND"})
    packet_path.write_text("{}\n", encoding="utf-8")
    _write_json(
        packet_manifest_path,
        {
            "status": "LOCKED_OUTCOME_BLIND",
            "rows": expected_cases,
            "sampling_metadata_inside_packet": False,
            "identity_visible_inside_packet": False,
            "future_or_performance_visible_inside_packet": False,
        },
    )
    _write_json(assignment_path, {"status": "LOCKED", "rows": expected_cases})
    freeze = {
        "outcome_blind": True,
        "identity_visible": False,
        "future_data_visible": False,
        "performance_visible": False,
        "old_ai_output_visible": False,
        "strategy_or_gate_change": False,
        "v2_components": [
            {
                "name": name,
                "relative_path": path.relative_to(track_root).as_posix(),
                "sha256": incident.file_sha256(path),
            }
            for name, path in component_files.items()
        ],
        "stage_components": [],
    }
    _write_json(freeze_path, freeze)

    manifest_core = {
        "track_version": incident.TRACK_VERSION,
        "status": incident.TRACK_STATUS,
        "classification": "RESEARCH_BACKTEST_NOT_COURSE_VALIDATED",
        "stage": "MATURE_TREND_PULLBACK_V2_CORE_ONLY",
        "outcome_blind": True,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "model": "test-model",
        "reasoning_effort": "test-effort",
        "cases": expected_cases,
        "runs": expected_runs,
        "selection_plan_path": str(selection_path),
        "selection_plan_sha256": incident.file_sha256(selection_path),
        "packet_path": str(packet_path),
        "packet_sha256": incident.file_sha256(packet_path),
        "packet_manifest_path": str(packet_manifest_path),
        "packet_manifest_sha256": incident.file_sha256(packet_manifest_path),
        "execution_freeze_path": str(freeze_path),
        "execution_freeze_sha256": incident.file_sha256(freeze_path),
        "assignment_manifest_path": str(assignment_path),
        "assignment_manifest_sha256": incident.file_sha256(assignment_path),
        "performance_must_remain_sealed_until_prior_gates_pass": True,
    }
    manifest = {**manifest_core, "track_manifest_sha256": incident.canonical_sha256(manifest_core)}
    _write_json(track_root / "research_track_manifest.json", manifest)
    return track_root, run_root, manifest, component_files


def _add_validated_case(
    run_root: Path,
    manifest: dict,
    component_files: dict[str, Path],
    *,
    run_number: int = 1,
    shard_number: int = 0,
    source_ordinal: int = 0,
) -> tuple[Path, Path]:
    output = {
        "schema_version": "test",
        "causal_attestation": {
            "used_future_data": False,
            "identity_visible": False,
            "performance_visible": False,
        },
    }
    reviewer_audit = {
        "adapter_version": "test",
        "adapter_status": "FINAL",
        "adapter_code_sha256": "1" * 64,
    }
    packet_sha256 = hashlib.sha256(f"packet-{source_ordinal}".encode()).hexdigest()
    identity = {
        "source_manifest_sha256": manifest["packet_sha256"],
        "source_ordinal": source_ordinal,
        "review_id": f"R-{source_ordinal}",
        "packet_sha256": packet_sha256,
        "protocol_sha256": incident.file_sha256(component_files["protocol"]),
        "prompt_sha256": incident.file_sha256(component_files["prompt"]),
        "schema_sha256": incident.file_sha256(component_files["schema"]),
        "execution_contract_sha256": manifest["execution_freeze_sha256"],
        "model": manifest["model"],
        "reasoning_effort": manifest["reasoning_effort"],
    }
    case_key = incident.canonical_sha256(identity)
    run_case_key = incident.canonical_sha256({"case_key": case_key, "run_number": run_number})
    output_sha256 = incident.canonical_sha256(output)
    reviewer_sha256 = incident.canonical_sha256(reviewer_audit)
    attempt = {
        "runner_version": incident.EXPECTED_RUNNER_VERSION,
        "case_key": case_key,
        "run_case_key": run_case_key,
        "run_number": run_number,
        "attempt": 1,
        "status": "VALIDATED",
        "technical_failure": False,
        "output_sha256": output_sha256,
        "reviewer_audit_sha256": reviewer_sha256,
        "reviewer_audit": reviewer_audit,
        "output": output,
    }
    envelope = {
        "runner_version": incident.EXPECTED_RUNNER_VERSION,
        "status": "VALID",
        "run_number": run_number,
        **identity,
        "case_key": case_key,
        "run_case_key": run_case_key,
        "successful_attempt": 1,
        "output_sha256": output_sha256,
        "reviewer_audit_sha256": reviewer_sha256,
        "reviewer_audit": reviewer_audit,
        "output": output,
    }
    base = run_root / f"r{run_number}" / f"s{shard_number}"
    attempt_path = base / "attempts" / run_case_key / "attempt_001.json"
    envelope_path = base / "cases" / f"{run_case_key}.json"
    _write_json(attempt_path, attempt)
    _write_json(envelope_path, envelope)
    return envelope_path, attempt_path


def test_build_incident_counts_actual_validated_files_and_preserves_inputs(tmp_path: Path) -> None:
    track_root, run_root, manifest, components = _make_fixture(tmp_path)
    _add_validated_case(run_root, manifest, components)
    before = {
        path.relative_to(tmp_path).as_posix(): incident.file_sha256(path)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    output_dir = tmp_path / "research_track_v2_integrity"
    result = incident.build_incident(
        track_root=track_root,
        run_root=run_root,
        output_dir=output_dir,
        created_at_utc="2026-09-08T00:00:00+00:00",
    )

    report = json.loads((output_dir / "incident.json").read_text(encoding="utf-8"))
    assert result["status"] == report["status"] == "ABORTED_TECHNICAL_AUDIT_FAIL"
    assert report["observed_runner_state"]["validated_envelopes"] == 1
    assert report["observed_runner_state"]["validated_attempts"] == 1
    assert report["observed_runner_state"]["expected_validated_envelopes"] == 4
    assert report["observed_runner_state"]["missing_validated_envelopes"] == 3
    assert report["performance_unsealed"] is False
    assert report["strategy_change"] is False
    assert report["formal_reuse_allowed"] is False
    assert report["old_outputs_deleted"] is False
    assert report["old_outputs_overwritten"] is False
    core = dict(report)
    supplied = core.pop("incident_sha256")
    assert supplied == incident.canonical_sha256(core)
    assert "正式重用：禁止" in (output_dir / "incident.md").read_text(encoding="utf-8")

    after = {
        path.relative_to(tmp_path).as_posix(): incident.file_sha256(path)
        for root in (track_root, run_root)
        for path in root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_incident_refuses_to_overwrite_any_existing_output(tmp_path: Path) -> None:
    track_root, run_root, manifest, components = _make_fixture(tmp_path)
    _add_validated_case(run_root, manifest, components)
    output_dir = tmp_path / "research_track_v2_integrity"
    output_dir.mkdir()
    marker = output_dir / "keep.txt"
    marker.write_text("unchanged", encoding="utf-8")

    with pytest.raises(incident.IncidentIntegrityError, match="overwrite output directory"):
        incident.build_incident(track_root=track_root, run_root=run_root, output_dir=output_dir)
    assert marker.read_text(encoding="utf-8") == "unchanged"


def test_incident_fails_closed_on_tampered_validated_attempt(tmp_path: Path) -> None:
    track_root, run_root, manifest, components = _make_fixture(tmp_path)
    _, attempt_path = _add_validated_case(run_root, manifest, components)
    tampered = json.loads(attempt_path.read_text(encoding="utf-8"))
    tampered["output"]["schema_version"] = "tampered"
    _write_json(attempt_path, tampered)
    output_dir = tmp_path / "research_track_v2_integrity"

    with pytest.raises(incident.IncidentIntegrityError, match="output hash mismatch"):
        incident.build_incident(track_root=track_root, run_root=run_root, output_dir=output_dir)
    assert not output_dir.exists()


def test_incident_refuses_to_abort_a_complete_run(tmp_path: Path) -> None:
    track_root, run_root, manifest, components = _make_fixture(
        tmp_path, expected_cases=1, expected_runs=1
    )
    _add_validated_case(run_root, manifest, components)
    output_dir = tmp_path / "research_track_v2_integrity"

    with pytest.raises(incident.IncidentIntegrityError, match="not partial"):
        incident.build_incident(track_root=track_root, run_root=run_root, output_dir=output_dir)
    assert not output_dir.exists()
