from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import hybrid_v4_s1_consistency_v1 as consistency
from scripts.hybrid_v3_sharding_v2 import (
    build_case_key,
    build_run_case_key,
    canonical_sha256,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_acceptance_requires_all_three_rates_and_both_class_counts() -> None:
    passed = consistency.evaluate_acceptance(
        schema_causal_rate=1.0,
        permission_rate=0.95,
        scenario_left_right_rate=0.90,
        unanimous_trade=3,
        unanimous_nontrade=3,
    )
    assert passed["status"] == "PASS"
    assert passed["status_zh"] == "驗收通過"
    assert all(passed["checks"].values())


@pytest.mark.parametrize(
    ("schema", "permission", "scenario", "trades", "nontrades"),
    [
        (0.999, 1.0, 1.0, 3, 3),
        (1.0, 0.949, 1.0, 3, 3),
        (1.0, 1.0, 0.899, 3, 3),
        (1.0, 1.0, 1.0, 2, 3),
        (1.0, 1.0, 1.0, 3, 2),
    ],
)
def test_acceptance_fails_every_frozen_boundary(
    schema: float, permission: float, scenario: float, trades: int, nontrades: int,
) -> None:
    result = consistency.evaluate_acceptance(
        schema_causal_rate=schema,
        permission_rate=permission,
        scenario_left_right_rate=scenario,
        unanimous_trade=trades,
        unanimous_nontrade=nontrades,
    )
    assert result["status"] == "FAIL"
    assert result["passed"] is False


def test_disagreement_overlay_is_unknown_wait_and_no_trade() -> None:
    policy_decision = {
        "permission": "TRADE",
        "route": "V2_CORE",
        "scenario": "MATURE_TREND_PULLBACK",
        "derived_structure": {"stage": "EARLY", "left_right_phase": "NONE"},
    }
    status, status_zh, decision, stage = consistency.conservative_decision(
        policy_decision, has_disagreement=True
    )
    assert status == "UNKNOWN"
    assert "不一致" in status_zh
    assert decision["permission"] == "WAIT"
    assert decision["route"] == "NO_TRADE"
    assert decision["reason_codes"] == [
        "THREE_RUN_DISAGREEMENT_CONSERVATIVE_UNKNOWN"
    ]
    assert stage == "WAIT"


def test_unanimous_overlay_uses_v4_s1_stage_permission() -> None:
    policy_decision = {
        "permission": "TRADE",
        "route": "V2_CORE",
        "scenario": "MATURE_TREND_PULLBACK",
        "derived_structure": {"stage": "MIDDLE", "left_right_phase": "NONE"},
    }
    status, _status_zh, decision, stage = consistency.conservative_decision(
        policy_decision, has_disagreement=False
    )
    assert status == "UNANIMOUS"
    assert decision["route"] == "V2_CORE"
    assert stage == "TRADE"


@pytest.mark.parametrize(
    "forbidden",
    [
        "stock_code",
        "stock_name",
        "ticker",
        "company_name",
        "security_name",
        "isin",
        "outcome",
        "mfe",
        "mae",
        "pnl",
        "profit",
        "return_after",
        "forward_return",
        "future_return",
        "future_open",
        "future_high",
        "future_low",
        "future_close",
        "exit_date",
        "exit_price",
        "realized_return",
        "unrealized_return",
        "winner",
        "next_20d_return",
        "profit_factor",
        "_preflight_symbolic_only",
        "action_signature_sha256",
        "symbolic_witness_sha256",
        "symbolic_reachability",
        "symbolic_classification",
        "eligible_sampling_strata",
        "eligible_stage_focuses",
        "primary_sampling_focus",
        "sampling_stratum",
        "sampling_focus",
        "symbolic_witness",
        "future_adjusted_close",
        "forward_5d_alpha",
        "largest_winner_contribution",
        "trade_mfe",
        "trade_mae",
        "trade_pnl",
    ],
)
def test_identity_future_performance_and_sampling_fields_are_rejected(
    forbidden: str,
) -> None:
    clean = {
        "review_id": "R-1",
        "anonymous_stock_id": "S-1",
        "as_of": "2023-06-01",
    }
    assert consistency._packet_from_source_row(clean, 0) == clean
    packet = {**clean, "nested": [{"deeper": {forbidden: "forbidden"}}]}
    with pytest.raises(consistency.V4S1ConsistencyError, match="entered packet"):
        consistency._packet_from_source_row(packet, 0)


@pytest.fixture
def case_record() -> dict:
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
        "model": "fixture-model",
        "reasoning_effort": "fixture-effort",
    }
    return {**identity, "case_key": build_case_key(**identity), "packet": packet}


def _reviewer_audit(reviewer_sha: str) -> dict:
    return {
        "adapter_version": consistency.reviewer_v3.ADAPTER_VERSION,
        "adapter_status": consistency.reviewer_v3.ADAPTER_STATUS,
        "adapter_code_sha256": reviewer_sha,
        "dynamic_transport_schema_sha256": _sha("transport"),
        "rendered_prompt_sha256": _sha("rendered"),
        "raw_transport_output_sha256": _sha("raw"),
    }


def _write_run(
    root: Path, record: dict, run_number: int, reviewer_sha: str,
) -> tuple[Path, Path]:
    run_key = build_run_case_key(record["case_key"], run_number)
    output = {"fixture": True, "run": run_number}
    audit = _reviewer_audit(reviewer_sha)
    output_sha = canonical_sha256(output)
    audit_sha = canonical_sha256(audit)
    envelope = {
        "runner_version": consistency.RUNNER_VERSION,
        "status": "VALID",
        "run_number": run_number,
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
        "output_sha256": output_sha,
        "reviewer_audit_sha256": audit_sha,
        "reviewer_audit": audit,
        "output": output,
    }
    attempt = {
        "runner_version": consistency.RUNNER_VERSION,
        "case_key": record["case_key"],
        "run_case_key": run_key,
        "run_number": run_number,
        "attempt": 1,
        "status": "VALIDATED",
        "technical_failure": False,
        "started_at": "2026-09-08T00:00:00+00:00",
        "finished_at": "2026-09-08T00:00:01+00:00",
        "elapsed_seconds": 1.0,
        "output_sha256": output_sha,
        "reviewer_audit_sha256": audit_sha,
        "reviewer_audit": audit,
        "output": output,
    }
    case_path = root / "s00" / "cases" / f"{run_key}.json"
    attempt_path = root / "s00" / "attempts" / run_key / "attempt_001.json"
    case_path.parent.mkdir(parents=True)
    attempt_path.parent.mkdir(parents=True)
    case_path.write_text(json.dumps(envelope), encoding="utf-8")
    attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
    return case_path, attempt_path


def test_three_run_artifacts_require_exact_hashes_coverage_and_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case_record: dict,
) -> None:
    monkeypatch.setattr(consistency, "EXPECTED_CASES", 1)
    monkeypatch.setattr(consistency, "validate_atomic", lambda _packet, _output: [])
    reviewer_sha = _sha("reviewer")
    roots = [tmp_path / f"run{i}" for i in range(1, 4)]
    for number, root in enumerate(roots, 1):
        _write_run(root, case_record, number, reviewer_sha)

    summaries, runs = consistency.validate_run_artifacts(
        [case_record], roots, reviewer_sha256=reviewer_sha
    )
    assert [row["cases"] for row in summaries] == [1, 1, 1]
    assert all(row["status_zh"] == "完整覆蓋通過" for row in summaries)
    assert all(set(run) == {"R-00"} for run in runs)


@pytest.mark.parametrize("tamper", ["output", "attempt", "coverage"])
def test_run_artifact_tampering_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case_record: dict, tamper: str,
) -> None:
    monkeypatch.setattr(consistency, "EXPECTED_CASES", 1)
    monkeypatch.setattr(consistency, "validate_atomic", lambda _packet, _output: [])
    reviewer_sha = _sha("reviewer")
    roots = [tmp_path / f"run{i}" for i in range(1, 4)]
    artifacts = [
        _write_run(root, case_record, number, reviewer_sha)
        for number, root in enumerate(roots, 1)
    ]
    case_path, attempt_path = artifacts[0]
    if tamper == "output":
        envelope = json.loads(case_path.read_text(encoding="utf-8"))
        envelope["output"]["forged"] = True
        case_path.write_text(json.dumps(envelope), encoding="utf-8")
    elif tamper == "attempt":
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        attempt["forged"] = True
        attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
    else:
        case_path.unlink()

    with pytest.raises(consistency.V4S1ConsistencyError):
        consistency.validate_run_artifacts(
            [case_record], roots, reviewer_sha256=reviewer_sha
        )


def test_markdown_has_bilingual_status_and_sealed_performance() -> None:
    metric = {"exact": 36, "total": 36, "rate": 1.0, "threshold": 1.0, "passed": True}
    report = {
        "status": "REPEATABILITY_PASS",
        "status_zh": "三輪一致性驗收通過",
        "expected_cases": 36,
        "run_count": 3,
        "metrics": {
            "schema_and_causal_fields": metric,
            "material_permission": {**metric, "threshold": 0.95},
            "scenario_and_left_right_phase": {**metric, "threshold": 0.90},
            "all_atomic_answers_pooled": {"rate": 1.0},
            "critical_atomic_answers_pooled": {"rate": 1.0},
            "unanimous_s1_trade_cases": 3,
            "unanimous_s1_nontrade_cases": 33,
        },
        "acceptance": {"passed": True},
        "report_sha256": _sha("report"),
    }
    rendered = consistency.render_markdown(report)
    assert "REPEATABILITY_PASS" in rendered
    assert "三輪一致性驗收通過" in rendered
    assert "SEALED" in rendered
    assert "不可解封績效" in rendered


def test_cli_failure_publishes_json_and_markdown_but_not_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = {
        "status": "REPEATABILITY_FAIL",
        "status_zh": "三輪一致性驗收未通過",
        "acceptance": {"passed": False},
        "report_sha256": _sha("report"),
    }
    published: dict[str, bytes] = {}
    monkeypatch.setattr(consistency, "evaluate", lambda **_kwargs: (copy.deepcopy(report), []))
    monkeypatch.setattr(consistency, "render_markdown", lambda *_args: "failed\n")
    monkeypatch.setattr(
        consistency, "_publish",
        lambda path, payload: published.__setitem__(Path(path).name, payload),
    )
    common = [
        "--source", str(tmp_path / "source"),
        "--source-manifest", str(tmp_path / "source-manifest"),
        "--track-manifest", str(tmp_path / "track"),
        "--assignment-manifest", str(tmp_path / "assignment"),
        "--execution-freeze", str(tmp_path / "freeze"),
        "--stage-protocol", str(tmp_path / "stage"),
        "--research-protocol", str(tmp_path / "protocol"),
        "--prompt", str(tmp_path / "prompt"),
        "--schema", str(tmp_path / "schema"),
        "--policy", str(tmp_path / "policy"),
    ]
    roots = sum((["--run-root", str(tmp_path / f"r{i}")] for i in range(1, 4)), [])
    outputs = [
        "--output-json", str(tmp_path / "report.json"),
        "--output-md", str(tmp_path / "report.md"),
        "--merged-ledger", str(tmp_path / "ledger.jsonl"),
    ]
    assert consistency.main([*common, *roots, *outputs]) == 2
    assert set(published) == {"report.json", "report.md"}
    assert b"REPEATABILITY_FAIL" in published["report.json"]


def test_verify_frozen_track_accepts_native_v4_builder_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import hybrid_v4_s1_track_v1 as track

    focuses = [
        focus
        for focus, count in track.EXPECTED_QUOTAS.items()
        for _ in range(count)
    ]
    selected = []
    packets = []
    for ordinal, focus in enumerate(focuses):
        packet = {
            "review_id": f"R-{ordinal:02d}",
            "anonymous_stock_id": f"S-{ordinal:02d}",
            "as_of": f"2023-{6 + ordinal % 6:02d}-01",
        }
        packets.append(packet)
        selected.append({
            "source_ordinal": ordinal,
            "review_id": packet["review_id"],
            "anonymous_stock_id": packet["anonymous_stock_id"],
            "as_of": packet["as_of"],
            "packet_sha256": canonical_sha256(packet),
            "sampling_focus": focus,
            "eligible_stage_focuses": [focus],
        })
    monkeypatch.setattr(
        track, "scan_source",
        lambda *_args: (copy.deepcopy(selected), {"review_points": 36}, []),
    )
    monkeypatch.setattr(track, "allocate", lambda *_args: copy.deepcopy(selected))
    monkeypatch.setattr(track, "extract_packets", lambda *_args: copy.deepcopy(packets))
    output = tmp_path / "v4_track"
    track.prepare(
        source_path=track.SOURCE,
        source_manifest_path=track.SOURCE_MANIFEST,
        # scan_source is isolated above; use a stable repository-local file so
        # this contract test does not depend on a generated preflight artifact.
        preflight_path=track.STAGE_PROTOCOL,
        output_dir=output,
        shard_count=4,
    )
    records, stage, protocol, integrity = consistency.verify_frozen_track(
        source=output / "primary_packets.jsonl",
        source_manifest=output / "primary_packets.manifest.json",
        track_manifest=output / "research_track_manifest.json",
        assignment_manifest=output / "primary_cases/assignment.manifest.json",
        execution_freeze=output / "research_execution_freeze.json",
        stage_protocol=track.STAGE_PROTOCOL,
        research_protocol=track.RESEARCH_PROTOCOL,
        prompt=track.PROMPT,
        schema=track.SCHEMA,
        policy=track.V4_S1_POLICY,
    )
    assert len(records) == 36
    assert stage["repeatability_acceptance"] == consistency.THRESHOLDS
    assert protocol["status"] == "FINAL_RESEARCH_LOCKED"
    assert integrity["status"] == "PASS"
