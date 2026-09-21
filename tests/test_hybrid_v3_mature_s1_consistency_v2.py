import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import hybrid_v3_mature_s1_consistency_v2 as v2
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


def _sample(*, concentrated_month: bool = False):
    focuses = [focus for focus, quota in FOCUS_QUOTAS.items() for _ in range(quota)]
    records = []
    mapping = []
    formal = {
        "source_manifest_sha256": _sha("source"),
        "protocol_sha256": _sha("protocol"),
        "prompt_sha256": _sha("prompt"),
        "schema_sha256": _sha("schema"),
        "execution_contract_sha256": _sha("execution"),
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
    }
    for index, focus in enumerate(focuses):
        month = 6 if concentrated_month else 6 + index % 6
        packet = {
            "review_id": f"R-{index:02d}",
            "anonymous_stock_id": f"S-{index:02d}",
            "as_of": f"2023-{month:02d}-01",
            "objective_facts": {},
        }
        packet_sha = canonical_sha256(packet)
        case_key = build_case_key(
            source_ordinal=index,
            review_id=packet["review_id"],
            packet_sha256=packet_sha,
            **formal,
        )
        records.append(
            {
                "source_ordinal": index,
                "review_id": packet["review_id"],
                "anonymous_stock_id": packet["anonymous_stock_id"],
                "packet_sha256": packet_sha,
                "case_key": case_key,
                "packet": packet,
                **formal,
            }
        )
        mapping.append(
            {
                "selected_ordinal": index,
                "source_ordinal": 1000 + index,
                "review_id": packet["review_id"],
                "anonymous_stock_id": packet["anonymous_stock_id"],
                "packet_sha256": packet_sha,
                "sampling_focus": focus,
                "eligible_stage_focuses": [focus],
            }
        )
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
    manifest = {
        "sampling_metadata_inside_packet": False,
        "identity_visible_inside_packet": False,
        "future_or_performance_visible_inside_packet": False,
        "mapping": mapping,
    }
    return records, stage, manifest


def _audit(reviewer_sha: str) -> dict:
    return {
        "adapter_version": v2.reviewer_v3.ADAPTER_VERSION,
        "adapter_status": v2.reviewer_v3.ADAPTER_STATUS,
        "adapter_code_sha256": reviewer_sha,
        "dynamic_transport_schema_sha256": _sha("transport"),
        "rendered_prompt_sha256": _sha("rendered"),
        "raw_transport_output_sha256": _sha("raw"),
    }


def _write_run_artifacts(tmp_path: Path, records: list[dict], reviewer_sha: str) -> list[Path]:
    roots = []
    for run_number in range(1, 4):
        root = tmp_path / f"r{run_number}"
        roots.append(root)
        for index, record in enumerate(records):
            shard = root / f"s{index % 4:02d}"
            run_case_key = build_run_case_key(record["case_key"], run_number)
            case_path = shard / "cases" / f"{run_case_key}.json"
            attempt_path = shard / "attempts" / run_case_key / "attempt_001.json"
            case_path.parent.mkdir(parents=True, exist_ok=True)
            attempt_path.parent.mkdir(parents=True, exist_ok=True)
            output = {"fixture": record["review_id"]}
            audit = _audit(reviewer_sha)
            attempt = {
                "runner_version": v2.RUNNER_VERSION,
                "case_key": record["case_key"],
                "run_case_key": run_case_key,
                "run_number": run_number,
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
                "runner_version": v2.RUNNER_VERSION,
                "status": "VALID",
                "run_number": run_number,
                "source_ordinal": record["source_ordinal"],
                "review_id": record["review_id"],
                "case_key": record["case_key"],
                "run_case_key": run_case_key,
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
            attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
            case_path.write_text(json.dumps(envelope), encoding="utf-8")
    return roots


def _freeze(tmp_path: Path, reviewer_sha: str) -> Path:
    path = tmp_path / "freeze.json"
    path.write_text(
        json.dumps(
            {
                "v2_components": [
                    {
                        "name": "reviewer",
                        "relative_path": "scripts/hybrid_v3_codex_reviewer_v3.py",
                        "status": "FINAL",
                        "sha256": reviewer_sha,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_sample_invariants_recompute_exact_quotas_stocks_months_and_focus() -> None:
    records, stage, manifest = _sample()
    result = v2.validate_sample_invariants(records, stage, manifest)
    assert result["status"] == "PASS"
    assert result["cases"] == result["unique_anonymous_stocks"] == 36
    assert result["focus_counts"] == FOCUS_QUOTAS
    assert result["distinct_months"] == 6
    assert result["maximum_cases_in_one_month"] == 6


@pytest.mark.parametrize("defect", ["duplicate_stock", "ineligible_focus", "month_concentration"])
def test_sample_invariants_fail_closed(defect: str) -> None:
    records, stage, manifest = _sample(concentrated_month=defect == "month_concentration")
    if defect == "duplicate_stock":
        records[1]["anonymous_stock_id"] = records[0]["anonymous_stock_id"]
    elif defect == "ineligible_focus":
        manifest["mapping"][0]["eligible_stage_focuses"] = ["COMPETING_MACRO_ONLY"]
    with pytest.raises(v2.MatureS1ConsistencyV2Error):
        v2.validate_sample_invariants(records, stage, manifest)


def test_run_artifacts_validate_runner_filename_attempt_and_reviewer(tmp_path: Path) -> None:
    records, _, _ = _sample()
    reviewer_sha = file_sha256(Path(v2.reviewer_v3.__file__))
    roots = _write_run_artifacts(tmp_path, records, reviewer_sha)
    result = v2.validate_run_artifacts(records, roots, reviewer_code_sha256=reviewer_sha)
    assert [row["cases"] for row in result] == [36, 36, 36]
    assert [row["attempt_artifacts"] for row in result] == [36, 36, 36]


@pytest.mark.parametrize("defect", ["filename", "attempt", "reviewer"])
def test_run_artifacts_fail_closed_on_provenance_defects(tmp_path: Path, defect: str) -> None:
    records, _, _ = _sample()
    reviewer_sha = file_sha256(Path(v2.reviewer_v3.__file__))
    roots = _write_run_artifacts(tmp_path, records, reviewer_sha)
    record = records[0]
    run_key = build_run_case_key(record["case_key"], 1)
    case_path = roots[0] / "s00" / "cases" / f"{run_key}.json"
    attempt_path = roots[0] / "s00" / "attempts" / run_key / "attempt_001.json"
    if defect == "filename":
        case_path.rename(case_path.with_name(f"{'0' * 64}.json"))
    elif defect == "attempt":
        attempt_path.unlink()
    else:
        envelope = json.loads(case_path.read_text(encoding="utf-8"))
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        for value in (envelope, attempt):
            value["reviewer_audit"]["adapter_version"] = "not-reviewer-v3"
            value["reviewer_audit_sha256"] = canonical_sha256(value["reviewer_audit"])
        case_path.write_text(json.dumps(envelope), encoding="utf-8")
        attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
    with pytest.raises(v2.MatureS1ConsistencyV2Error):
        v2.validate_run_artifacts(records, roots, reviewer_code_sha256=reviewer_sha)


def test_raw_permission_route_diagnostics_exposes_stage_masking() -> None:
    rows = []
    for index in range(36):
        decisions = [
            {"permission": "WAIT", "route": None},
            {"permission": "WAIT", "route": None},
            {"permission": "WAIT", "route": None},
        ]
        if index == 0:
            decisions[0] = {"permission": "TRADE", "route": "NEAR_PASS_MACRO_COPY"}
        rows.append(
            {
                "review_id": f"R-{index:02d}",
                "stage_permission_exact": True,
                "run_decisions": decisions,
            }
        )
    report = {"per_case": rows}
    result = v2.raw_permission_route_diagnostics(report)
    assert result["raw_permission"]["exact"] == 35
    assert result["raw_route"]["exact"] == 35
    assert result["raw_permission_and_route"]["exact"] == 35
    assert result["stage_agreement_masked_raw_pair_disagreement_cases"] == 1
    assert report["per_case"][0]["raw_permission_route_exact"] is False


def test_evaluate_rejects_bad_envelope_before_calling_v1_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records, stage, manifest = _sample()
    reviewer_sha = file_sha256(Path(v2.reviewer_v3.__file__))
    roots = _write_run_artifacts(tmp_path, records, reviewer_sha)
    run_key = build_run_case_key(records[0]["case_key"], 1)
    case_path = roots[0] / "s00" / "cases" / f"{run_key}.json"
    case_path.rename(case_path.with_name(f"{'0' * 64}.json"))
    manifest_path = tmp_path / "packet-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    freeze_path = _freeze(tmp_path, reviewer_sha)
    monkeypatch.setattr(v2.v1, "verify_track", lambda **_: (records, stage, {}, {}))
    called = False

    def forbidden_metrics(**_):
        nonlocal called
        called = True
        raise AssertionError("V1 metrics must not run")

    monkeypatch.setattr(v2.v1, "evaluate", forbidden_metrics)
    with pytest.raises(v2.MatureS1ConsistencyV2Error):
        v2.evaluate(
            source=tmp_path / "source.jsonl",
            source_manifest=manifest_path,
            track_manifest=tmp_path / "track.json",
            assignment_manifest=tmp_path / "assignment.json",
            execution_freeze=freeze_path,
            stage_protocol=tmp_path / "stage.json",
            research_protocol=tmp_path / "protocol.json",
            schema=tmp_path / "schema.json",
            run_roots=roots,
        )
    assert called is False


def test_cli_returns_nonzero_for_repeatability_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = {
        "status": "REPEATABILITY_FAIL",
        "acceptance": {"passed": False},
        "report_sha256": _sha("report"),
    }
    monkeypatch.setattr(v2, "evaluate", lambda **_: (copy.deepcopy(report), []))
    monkeypatch.setattr(v2, "render_markdown", lambda *_: "failed\n")
    output_json = tmp_path / "report.json"
    output_md = tmp_path / "report.md"
    exit_code = v2.main(
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
            "--run-root", str(tmp_path / "r2"),
            "--run-root", str(tmp_path / "r3"),
            "--output-json", str(output_json),
            "--output-md", str(output_md),
            "--merged-ledger", str(tmp_path / "ledger.jsonl"),
        ]
    )
    assert exit_code == 2
    assert json.loads(output_json.read_text(encoding="utf-8"))["status"] == "REPEATABILITY_FAIL"
    assert output_md.read_text(encoding="utf-8") == "failed\n"
    assert not (tmp_path / "ledger.jsonl").exists()
