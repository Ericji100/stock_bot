"""Independent adversarial tests for the MATURE S1 V3 integrity evaluator."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import hybrid_v3_mature_s1_consistency_v3 as v3
from scripts.hybrid_v3_sharding_v2 import (
    build_case_key,
    build_run_case_key,
    canonical_sha256,
    file_sha256,
)


FOCUS_QUOTAS = {
    "MATURE_OBJECTIVE_PROXY": 12,
    "COMPETING_MACRO_ONLY": 4,
    "COMPETING_FRESH_ONLY": 4,
    "COMPETING_BEAR_ONLY": 4,
    "MACRO_DEFENSE_REMOVE": 4,
    "WAIT_NO_CAUSAL_STOP": 4,
    "WAIT_MATERIAL_CONFLICT": 4,
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _self_hashed(value: dict, field: str) -> dict:
    result = copy.deepcopy(value)
    result[field] = canonical_sha256(result)
    return result


def _verify_kwargs(tmp_path: Path) -> dict[str, Path]:
    return {
        "source": tmp_path / "packets.jsonl",
        "source_manifest": tmp_path / "packets.manifest.json",
        "track_manifest": tmp_path / "track.json",
        "assignment_manifest": tmp_path / "assignment.json",
        "execution_freeze": tmp_path / "freeze.json",
        "stage_protocol": tmp_path / "stage.json",
        "research_protocol": tmp_path / "protocol.json",
        "schema": tmp_path / "schema.json",
    }


@pytest.mark.parametrize(
    ("artifact", "old_version"),
    [
        ("track", "hybrid-v3-mature-s1-research-track-v1"),
        ("track", "hybrid-v3-mature-s1-research-track-v2-integrity"),
        ("freeze", "hybrid-v3-mature-s1-execution-freeze-v1"),
        ("freeze", "hybrid-v3-mature-s1-execution-freeze-v2-integrity"),
    ],
)
def test_verify_track_rejects_v1_and_v2_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
    old_version: str,
) -> None:
    track = _self_hashed(
        {"track_version": v3.TRACK_VERSION, "status": v3.track_v3.TRACK_STATUS},
        "track_manifest_sha256",
    )
    freeze = _self_hashed(
        {
            "freeze_version": v3.FREEZE_VERSION,
            "status": "FROZEN_IMMUTABLE_BEFORE_AI_EXECUTION",
        },
        "freeze_sha256",
    )
    if artifact == "track":
        track = _self_hashed(
            {"track_version": old_version, "status": v3.track_v3.TRACK_STATUS},
            "track_manifest_sha256",
        )
    else:
        freeze = _self_hashed(
            {
                "freeze_version": old_version,
                "status": "FROZEN_IMMUTABLE_BEFORE_AI_EXECUTION",
            },
            "freeze_sha256",
        )
    documents = iter([track, freeze, {}, {}, {}, {}])
    monkeypatch.setattr(v3, "_read_json", lambda _path: next(documents))

    with pytest.raises(v3.MatureS1ConsistencyV3Error):
        v3.verify_track(**_verify_kwargs(tmp_path))


def _sample() -> tuple[list[dict], dict, dict, dict]:
    focuses = [focus for focus, quota in FOCUS_QUOTAS.items() for _ in range(quota)]
    records: list[dict] = []
    mapping: list[dict] = []
    selected_rows: list[dict] = []
    for ordinal, focus in enumerate(focuses):
        packet = {
            "review_id": f"R-{ordinal:02d}",
            "anonymous_stock_id": f"S-{ordinal:02d}",
            "as_of": f"2023-{6 + ordinal % 6:02d}-01",
            "objective_facts": {},
        }
        packet_sha = canonical_sha256(packet)
        records.append(
            {
                "review_id": packet["review_id"],
                "anonymous_stock_id": packet["anonymous_stock_id"],
                "packet_sha256": packet_sha,
                "packet": packet,
            }
        )
        external = {
            "review_id": packet["review_id"],
            "anonymous_stock_id": packet["anonymous_stock_id"],
            "packet_sha256": packet_sha,
            "source_ordinal": 1000 + ordinal,
            "sampling_focus": focus,
            "eligible_stage_focuses": [focus],
        }
        mapping.append({"selected_ordinal": ordinal, **external})
        selected_rows.append(copy.deepcopy(external))
    stage = {
        "consistency_sample": {
            "cases": 36,
            "runs": 3,
            "one_case_per_anonymous_stock": True,
            "minimum_distinct_months": 6,
            "maximum_cases_per_month": 9,
            "focus_order": list(FOCUS_QUOTAS),
            "quotas": FOCUS_QUOTAS,
        }
    }
    packet_manifest = {
        "sampling_metadata_inside_packet": False,
        "identity_visible_inside_packet": False,
        "future_or_performance_visible_inside_packet": False,
        "mapping": mapping,
    }
    selection = {"rows": selected_rows}
    return records, stage, packet_manifest, selection


def _replace_packet_identity(
    records: list[dict], packet_manifest: dict, selection: dict, ordinal: int
) -> None:
    record = records[ordinal]
    packet_sha = canonical_sha256(record["packet"])
    record["packet_sha256"] = packet_sha
    for row in (packet_manifest["mapping"][ordinal], selection["rows"][ordinal]):
        row["anonymous_stock_id"] = record["anonymous_stock_id"]
        row["packet_sha256"] = packet_sha


def test_sample_fixture_is_accepted_before_adversarial_mutation() -> None:
    records, stage, packet_manifest, selection = _sample()

    result = v3.validate_sample_invariants(records, stage, packet_manifest, selection)

    assert result["status"] == "PASS"
    assert result["focus_counts"] == FOCUS_QUOTAS


@pytest.mark.parametrize(
    "defect", ["quota", "duplicate_stock", "month", "ineligible_focus"]
)
def test_sample_invariants_reject_quota_stock_month_and_focus(defect: str) -> None:
    records, stage, packet_manifest, selection = _sample()
    if defect == "quota":
        for row in (packet_manifest["mapping"][0], selection["rows"][0]):
            row["sampling_focus"] = "COMPETING_MACRO_ONLY"
            row["eligible_stage_focuses"] = [
                "MATURE_OBJECTIVE_PROXY",
                "COMPETING_MACRO_ONLY",
            ]
    elif defect == "duplicate_stock":
        records[1]["anonymous_stock_id"] = records[0]["anonymous_stock_id"]
        records[1]["packet"]["anonymous_stock_id"] = records[0]["anonymous_stock_id"]
        _replace_packet_identity(records, packet_manifest, selection, 1)
    elif defect == "month":
        for ordinal, record in enumerate(records):
            record["packet"]["as_of"] = "2023-06-01"
            _replace_packet_identity(records, packet_manifest, selection, ordinal)
    else:
        for row in (packet_manifest["mapping"][0], selection["rows"][0]):
            row["eligible_stage_focuses"] = ["COMPETING_MACRO_ONLY"]

    with pytest.raises(v3.MatureS1ConsistencyV3Error):
        v3.validate_sample_invariants(records, stage, packet_manifest, selection)


def _record() -> dict:
    packet = {
        "review_id": "R-00",
        "anonymous_stock_id": "S-00",
        "as_of": "2023-06-01",
    }
    identity = {
        "source_manifest_sha256": _sha("source"),
        "source_ordinal": 0,
        "review_id": packet["review_id"],
        "packet_sha256": canonical_sha256(packet),
        "protocol_sha256": _sha("protocol"),
        "prompt_sha256": _sha("prompt"),
        "schema_sha256": _sha("schema"),
        "execution_contract_sha256": _sha("freeze"),
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
    }
    return {
        **identity,
        "case_key": build_case_key(**identity),
        "packet": packet,
    }


def _audit(reviewer_sha: str) -> dict:
    return {
        "adapter_version": v3.reviewer_v3.ADAPTER_VERSION,
        "adapter_status": v3.reviewer_v3.ADAPTER_STATUS,
        "adapter_code_sha256": reviewer_sha,
        "dynamic_transport_schema_sha256": _sha("transport"),
        "rendered_prompt_sha256": _sha("rendered"),
        "raw_transport_output_sha256": _sha("raw"),
    }


def _run_fixture(tmp_path: Path, reviewer_sha: str) -> tuple[dict, Path, Path, Path]:
    record = _record()
    run_root = tmp_path / "r1"
    run_key = build_run_case_key(record["case_key"], 1)
    case_path = run_root / "s00" / "cases" / f"{run_key}.json"
    attempt_path = run_root / "s00" / "attempts" / run_key / "attempt_001.json"
    case_path.parent.mkdir(parents=True)
    attempt_path.parent.mkdir(parents=True)
    output = {"fixture": True}
    audit = _audit(reviewer_sha)
    attempt = {
        "runner_version": v3.RUNNER_VERSION,
        "case_key": record["case_key"],
        "run_case_key": run_key,
        "run_number": 1,
        "attempt": 1,
        "status": "VALIDATED",
        "technical_failure": False,
        "started_at": "2026-09-08T00:00:00+00:00",
        "finished_at": "2026-09-08T00:00:01+00:00",
        "elapsed_seconds": 1.0,
        "output_sha256": canonical_sha256(output),
        "reviewer_audit_sha256": canonical_sha256(audit),
        "reviewer_audit": audit,
        "output": output,
    }
    envelope = {
        "runner_version": v3.RUNNER_VERSION,
        "status": "VALID",
        "run_number": 1,
        "source_ordinal": record["source_ordinal"],
        "review_id": record["review_id"],
        "case_key": record["case_key"],
        "run_case_key": run_key,
        "packet_sha256": record["packet_sha256"],
        "source_manifest_sha256": record["source_manifest_sha256"],
        "protocol_sha256": record["protocol_sha256"],
        "prompt_sha256": record["prompt_sha256"],
        "schema_sha256": record["schema_sha256"],
        "execution_contract_sha256": record["execution_contract_sha256"],
        "model": record["model"],
        "reasoning_effort": record["reasoning_effort"],
        "successful_attempt": 1,
        "output_sha256": attempt["output_sha256"],
        "reviewer_audit_sha256": attempt["reviewer_audit_sha256"],
        "reviewer_audit": audit,
        "output": output,
    }
    case_path.write_text(json.dumps(envelope), encoding="utf-8")
    attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
    return record, run_root, case_path, attempt_path


@pytest.mark.parametrize(
    "defect",
    [
        "envelope_field",
        "envelope_filename",
        "envelope_hash",
        "missing_attempt",
        "attempt_field",
        "multiple_success_attempts",
        "trailing_attempt",
        "reviewer_adapter_hash",
    ],
)
def test_run_artifacts_reject_forged_envelopes_attempts_and_reviewer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    monkeypatch.setattr(v3, "EXPECTED_CASES", 1)
    monkeypatch.setattr(v3, "EXPECTED_RUNS", 1)
    reviewer_sha = file_sha256(Path(v3.reviewer_v3.__file__))
    record, run_root, case_path, attempt_path = _run_fixture(tmp_path, reviewer_sha)
    envelope = json.loads(case_path.read_text(encoding="utf-8"))
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    if defect == "envelope_field":
        envelope["forged"] = True
        case_path.write_text(json.dumps(envelope), encoding="utf-8")
    elif defect == "envelope_filename":
        case_path.rename(case_path.with_name(f"{'0' * 64}.json"))
    elif defect == "envelope_hash":
        envelope["output_sha256"] = "0" * 64
        case_path.write_text(json.dumps(envelope), encoding="utf-8")
    elif defect == "missing_attempt":
        attempt_path.unlink()
    elif defect == "attempt_field":
        attempt["forged"] = True
        attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
    elif defect == "multiple_success_attempts":
        envelope["successful_attempt"] = 2
        case_path.write_text(json.dumps(envelope), encoding="utf-8")
        second = copy.deepcopy(attempt)
        second["attempt"] = 2
        attempt_path.with_name("attempt_002.json").write_text(
            json.dumps(second), encoding="utf-8"
        )
    elif defect == "trailing_attempt":
        attempt_path.with_name("attempt_002.json").write_text(
            json.dumps(attempt), encoding="utf-8"
        )
    else:
        for document in (envelope, attempt):
            document["reviewer_audit"]["adapter_code_sha256"] = "0" * 64
            document["reviewer_audit_sha256"] = canonical_sha256(
                document["reviewer_audit"]
            )
        case_path.write_text(json.dumps(envelope), encoding="utf-8")
        attempt_path.write_text(json.dumps(attempt), encoding="utf-8")

    with pytest.raises(v3.MatureS1ConsistencyV3Error):
        v3.validate_run_artifacts(
            [record], [run_root], reviewer_code_sha256=reviewer_sha
        )


def test_minimal_run_fixture_is_accepted_before_adversarial_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(v3, "EXPECTED_CASES", 1)
    monkeypatch.setattr(v3, "EXPECTED_RUNS", 1)
    reviewer_sha = file_sha256(Path(v3.reviewer_v3.__file__))
    record, run_root, _case_path, _attempt_path = _run_fixture(tmp_path, reviewer_sha)

    summaries, runs = v3.validate_run_artifacts(
        [record], [run_root], reviewer_code_sha256=reviewer_sha
    )

    assert summaries[0]["status"] == "PASS"
    assert summaries[0]["attempt_artifacts"] == 1
    assert set(runs[0]) == {record["review_id"]}


def test_cli_returns_two_and_does_not_publish_ledger_on_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = {
        "status": "REPEATABILITY_FAIL",
        "acceptance": {"passed": False},
        "report_sha256": _sha("report"),
    }
    published: dict[str, bytes] = {}
    monkeypatch.setattr(v3, "evaluate", lambda **_kwargs: (copy.deepcopy(report), []))
    monkeypatch.setattr(v3, "render_markdown", lambda *_args: "failed\n")
    monkeypatch.setattr(
        v3, "_publish", lambda path, payload: published.__setitem__(Path(path).name, payload)
    )
    output_json = tmp_path / "report.json"
    output_md = tmp_path / "report.md"
    ledger = tmp_path / "ledger.jsonl"
    exit_code = v3.main(
        [
            "--source", str(tmp_path / "source"),
            "--source-manifest", str(tmp_path / "source-manifest"),
            "--track-manifest", str(tmp_path / "track"),
            "--assignment-manifest", str(tmp_path / "assignment"),
            "--execution-freeze", str(tmp_path / "freeze"),
            "--stage-protocol", str(tmp_path / "stage"),
            "--research-protocol", str(tmp_path / "protocol"),
            "--schema", str(tmp_path / "schema"),
            "--run-root", str(tmp_path / "r1"),
            "--output-json", str(output_json),
            "--output-md", str(output_md),
            "--merged-ledger", str(ledger),
        ]
    )

    assert exit_code == 2
    assert set(published) == {"report.json", "report.md"}
    assert b"REPEATABILITY_FAIL" in published["report.json"]
    assert published["report.md"] == b"failed\n"
    assert "ledger.jsonl" not in published
