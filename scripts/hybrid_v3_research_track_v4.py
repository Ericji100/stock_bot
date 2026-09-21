"""Abort Candidate4 repeatability cleanly and freeze exact-evidence track V4.

The V3 strategy, policy, prompt, packet universe, holdout allocation, model,
and reasoning effort stay unchanged.  This script records the first failed
Candidate4 execution as immutable research evidence, then pins a new launcher
and reviewer whose only changes are packet-specific evidence enums and bounded
retry of model-produced output-validation defects.
"""
from __future__ import annotations

from collections import Counter
import argparse
import json
from pathlib import Path
from typing import Any, Mapping

try:
    from .hybrid_v3_sharding_v2 import (
        _publish_immutable,
        build_case_records,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
        read_jsonl,
        source_content_sha256,
        write_shards,
    )
except ImportError:  # pragma: no cover - direct script execution
    from scripts.hybrid_v3_sharding_v2 import (
        _publish_immutable,
        build_case_records,
        canonical_json_bytes,
        canonical_sha256,
        file_sha256,
        read_jsonl,
        source_content_sha256,
        write_shards,
    )


ROOT = Path(__file__).resolve().parents[1]
TRACK_VERSION = "hybrid-v3-research-track-v4-exact-evidence"
FREEZE_VERSION = "hybrid-v3-research-execution-freeze-v4-exact-evidence"
ABORT_VERSION = "hybrid-v3-candidate4-abort-audit-v1"
EXPECTED_MODEL = "gpt-5.6-sol"
EXPECTED_REASONING = "xhigh"


class TrackV4Error(ValueError):
    """The failed baseline or the superseding frozen inputs are not exact."""


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise TrackV4Error(f"expected JSON object: {path}")
    return value


def _publish_json(path: Path, value: Mapping[str, Any]) -> None:
    _publish_immutable(Path(path), canonical_json_bytes(value) + b"\n")


def _component_rows() -> list[dict[str, Any]]:
    components = (
        ("launcher", "scripts/hybrid_v3_codex_launcher_v2.py"),
        ("reviewer", "scripts/hybrid_v3_codex_reviewer_v3.py"),
        ("runner", "scripts/hybrid_v3_atomic_runner_v2.py"),
        ("policy", "scripts/hybrid_v3_atomic_policy_v3.py"),
        ("prompt", "config/hybrid_semantic_prompt_v3.md"),
        ("schema", "config/hybrid_atomic_semantics_v2.schema.json"),
        ("protocol", "config/hybrid_monitoring_research_protocol_v1.json"),
    )
    rows: list[dict[str, Any]] = []
    for name, relative in components:
        path = ROOT / relative
        if not path.is_file():
            raise TrackV4Error(f"missing V4 component: {path}")
        rows.append(
            {
                "name": name,
                "relative_path": relative,
                "status": "FINAL",
                "sha256": file_sha256(path),
            }
        )
    return rows


def build_abort_audit(
    *,
    old_track_dir: Path,
    old_run_root: Path,
) -> dict[str, Any]:
    old_track_dir = Path(old_track_dir).resolve()
    old_run_root = Path(old_run_root).resolve()
    old_freeze_path = old_track_dir / "research_execution_freeze.json"
    old_track_path = old_track_dir / "research_track_manifest.json"
    old_freeze = _read_json(old_freeze_path)
    old_track = _read_json(old_track_path)
    if old_freeze.get("freeze_version") != "hybrid-v3-research-execution-freeze-v3-candidate4":
        raise TrackV4Error("abort audit source is not Candidate4 execution freeze V3")
    if old_track.get("track_version") != "hybrid-v3-research-track-v3-candidate4":
        raise TrackV4Error("abort audit source is not Candidate4 research track V3")

    attempt_rows: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(old_run_root.glob("r*/s*/attempts/*/attempt_*.json")):
        attempt_rows.append((path, _read_json(path)))
    statuses = Counter(str(row.get("status")) for _, row in attempt_rows)
    failures = [(path, row) for path, row in attempt_rows if row.get("status") != "VALIDATED"]
    if len(failures) != 1 or failures[0][1].get("status") != "CONTRACT_ERROR":
        raise TrackV4Error("Candidate4 abort audit requires exactly one CONTRACT_ERROR")
    failure_path, failure = failures[0]
    expected_error = "unknown evidence ref: REL_CANDIDATE:R-b57731032559c12d"
    if failure.get("error") != expected_error:
        raise TrackV4Error("Candidate4 failure reason changed")

    valid_counts: dict[str, int] = {}
    common = 0
    total_valid = 0
    for shard in range(4):
        counts: list[int] = []
        for run in range(1, 4):
            case_dir = old_run_root / f"r{run}" / f"s{shard}" / "cases"
            count = len(list(case_dir.glob("*.json"))) if case_dir.exists() else 0
            valid_counts[f"s{shard}_run{run}"] = count
            counts.append(count)
            total_valid += count
        common += min(counts)

    core = {
        "audit_version": ABORT_VERSION,
        "status": "ABORTED_REPEATABILITY_SCHEMA_CAUSAL_GATE_FAIL",
        "classification": "OUTCOME_BLIND_RESEARCH_EXECUTION_FAILURE_NO_PERFORMANCE",
        "strategy_or_gate_change": False,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "performance_evaluated": False,
        "candidate4_execution_freeze_sha256": file_sha256(old_freeze_path),
        "candidate4_track_manifest_sha256": file_sha256(old_track_path),
        "valid_envelopes_before_abort": total_valid,
        "three_run_common_cases_before_abort": common,
        "valid_counts": valid_counts,
        "attempt_status_counts": dict(sorted(statuses.items())),
        "failure": {
            "attempt_path": str(failure_path.resolve()),
            "attempt_sha256": file_sha256(failure_path),
            "run_number": failure.get("run_number"),
            "case_key": failure.get("case_key"),
            "run_case_key": failure.get("run_case_key"),
            "attempt": failure.get("attempt"),
            "status": failure.get("status"),
            "technical_failure": failure.get("technical_failure"),
            "elapsed_seconds": failure.get("elapsed_seconds"),
            "error_type": failure.get("error_type"),
            "error": failure.get("error"),
            "root_cause": (
                "MODEL_ABBREVIATED_AN_EXISTING_RELATION_CANDIDATE_REF; "
                "STATIC_TRANSPORT_PATTERN_ALLOWED_THE_ALIAS; EXACT_PACKET_VALIDATOR_REJECTED_IT"
            ),
        },
        "gate_effect": {
            "schema_and_causal_threshold": 1.0,
            "mathematically_passable": False,
            "performance_must_remain_sealed": True,
        },
        "disposition": {
            "reuse_candidate4_ai_outputs": False,
            "preserve_candidate4_artifacts": True,
            "required_next_action": "NEW_VERSIONED_EXECUTION_CONTRACT_AND_FULL_THREE_RUN_RESTART",
        },
    }
    return {**core, "report_sha256": canonical_sha256(core)}


def render_abort_markdown(report: Mapping[str, Any]) -> str:
    failure = report["failure"]
    return "\n".join(
        [
            "# Candidate4 三輪一致性中止稽核",
            "",
            f"- 狀態：`{report['status']}`",
            "- 性質：盲化研究執行失敗；未讀取股票身分、未來行情或績效。",
            f"- 中止前有效輸出：{report['valid_envelopes_before_abort']}／360",
            f"- 中止前三輪共同案例：{report['three_run_common_cases_before_abort']}／120",
            f"- 失敗輪次：第{failure['run_number']}輪",
            f"- 失敗狀態：`{failure['status']}`",
            f"- 錯誤：`{failure['error']}`",
            "",
            "## 判定",
            "",
            "AI把封包中存在的`RELATION_CANDIDATE:…`縮寫成不存在的`REL_CANDIDATE:…`。靜態傳輸schema只限制大寫前綴格式，因此由最終封包精確驗證攔截。這使100% schema／因果門檻已無法通過，本輪依凍結協定中止，績效不得解封。",
            "",
            "Candidate4所有有效與失敗產物均保留；新輪不得沿用其AI答案，只可沿用未改變的盲化封包與抽樣。",
            "",
            f"報告SHA-256：`{report['report_sha256']}`",
            "",
        ]
    )


def prepare(
    *,
    old_track_dir: Path,
    old_run_root: Path,
    output_dir: Path,
    abort_json: Path,
    abort_md: Path,
) -> dict[str, Any]:
    old_track_dir = Path(old_track_dir).resolve()
    output_dir = Path(output_dir).resolve()
    old_freeze_path = old_track_dir / "research_execution_freeze.json"
    old_track_path = old_track_dir / "research_track_manifest.json"
    old_freeze = _read_json(old_freeze_path)
    old_track = _read_json(old_track_path)

    abort = build_abort_audit(old_track_dir=old_track_dir, old_run_root=old_run_root)
    _publish_json(abort_json, abort)
    _publish_immutable(abort_md, render_abort_markdown(abort).encode("utf-8"))

    if old_freeze.get("strategy_or_gate_change") is not False:
        raise TrackV4Error("superseded Candidate4 freeze unexpectedly changed strategy or gates")
    execution = old_freeze.get("execution_contract") or {}
    if execution.get("model") != EXPECTED_MODEL or execution.get("reasoning_effort") != EXPECTED_REASONING:
        raise TrackV4Error("superseded Candidate4 model/reasoning changed")

    freeze = {
        **old_freeze,
        "freeze_version": FREEZE_VERSION,
        "track_version": TRACK_VERSION,
        "supersedes_execution_freeze_sha256": file_sha256(old_freeze_path),
        "supersession_reason": [
            "ENUMERATE_EXACT_PACKET_EVIDENCE_REFS_IN_DYNAMIC_TRANSPORT_SCHEMA",
            "RETRY_ONLY_MODEL_PRODUCED_OUTPUT_VALIDATION_FAILURES_WITHIN_MAX_ATTEMPTS",
        ],
        "contract_repair_only": True,
        "strategy_or_gate_change": False,
        "candidate4_abort_audit_sha256": file_sha256(abort_json),
        "candidate4_abort_report_sha256": abort["report_sha256"],
        "transport_hardening": {
            "packet_specific_evidence_ref_enum": True,
            "model_output_validation_is_retryable": True,
            "frozen_input_or_hash_error_is_retryable": False,
            "max_attempts": 3,
            "first_attempt_failures_remain_auditable": True,
        },
        "v2_components": _component_rows(),
        "execution_contract": {
            "model": EXPECTED_MODEL,
            "reasoning_effort": EXPECTED_REASONING,
        },
    }
    freeze_path = output_dir / "research_execution_freeze.json"
    _publish_json(freeze_path, freeze)

    primary = next(
        (row for row in old_track.get("blocks", []) if row.get("block_id") == "PRIMARY"),
        None,
    )
    if not isinstance(primary, dict):
        raise TrackV4Error("superseded track has no PRIMARY block")
    source_path = Path(str(primary["packet_path"])).resolve()
    source_manifest_path = Path(str(primary["manifest_path"])).resolve()
    if file_sha256(source_path) != primary.get("packet_sha256"):
        raise TrackV4Error("inherited PRIMARY packets changed")
    if file_sha256(source_manifest_path) != primary.get("manifest_sha256"):
        raise TrackV4Error("inherited PRIMARY manifest changed")
    packets = read_jsonl(source_path)
    if len(packets) != 120:
        raise TrackV4Error("inherited PRIMARY block must contain 120 packets")
    components = {row["name"]: row for row in freeze["v2_components"]}
    records = build_case_records(
        packets,
        source_manifest_sha256=source_content_sha256(packets),
        protocol_sha256=components["protocol"]["sha256"],
        prompt_sha256=components["prompt"]["sha256"],
        schema_sha256=components["schema"]["sha256"],
        execution_contract_sha256=file_sha256(freeze_path),
        model=EXPECTED_MODEL,
        reasoning_effort=EXPECTED_REASONING,
    )
    assignment = write_shards(records, output_dir / "primary_cases", shard_count=4)
    assignment_path = output_dir / "primary_cases/assignment.manifest.json"

    track_core = {
        "track_version": TRACK_VERSION,
        "status": "FROZEN_OUTCOME_BLIND_RESEARCH_ONLY",
        "classification": "RESEARCH_BACKTEST_NOT_COURSE_VALIDATED",
        "course_fidelity_claim_allowed": False,
        "outcome_blind": True,
        "model": EXPECTED_MODEL,
        "reasoning_effort": EXPECTED_REASONING,
        "strategy_or_gate_change": False,
        "supersedes_track_manifest_sha256": file_sha256(old_track_path),
        "candidate4_abort_audit_path": str(Path(abort_json).resolve()),
        "candidate4_abort_audit_sha256": file_sha256(abort_json),
        "execution_freeze_path": str(freeze_path.resolve()),
        "execution_freeze_sha256": file_sha256(freeze_path),
        "candidate4_source_manifest_sha256": old_track.get("candidate4_source_manifest_sha256"),
        "candidate4_holdout_plan_sha256": old_track.get("candidate4_holdout_plan_sha256"),
        "blocks": old_track.get("blocks"),
        "primary_case_assignment": {
            "path": str(assignment_path.resolve()),
            "sha256": file_sha256(assignment_path),
            "assignment_sha256": assignment["assignment_sha256"],
            "rows": assignment["expected_rows"],
            "shards": assignment["shard_count"],
        },
    }
    track = {**track_core, "track_manifest_sha256": canonical_sha256(track_core)}
    track_path = output_dir / "research_track_manifest.json"
    _publish_json(track_path, track)
    return {
        "status": track["status"],
        "track_version": TRACK_VERSION,
        "strategy_or_gate_change": False,
        "abort_report_sha256": abort["report_sha256"],
        "execution_freeze_path": str(freeze_path),
        "execution_freeze_sha256": file_sha256(freeze_path),
        "track_manifest_path": str(track_path),
        "track_manifest_sha256": track["track_manifest_sha256"],
        "assignment_path": str(assignment_path),
        "assignment_sha256": assignment["assignment_sha256"],
        "rows": assignment["expected_rows"],
        "shards": assignment["shard_count"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-track-dir", type=Path, required=True)
    parser.add_argument("--old-run-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--abort-json", type=Path, required=True)
    parser.add_argument("--abort-md", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(
        old_track_dir=args.old_track_dir,
        old_run_root=args.old_run_root,
        output_dir=args.output_dir,
        abort_json=args.abort_json,
        abort_md=args.abort_md,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
