"""Versioned technical correction for MATURE S1 V3 consistency evaluation.

V4 changes no trading, sampling, model, prompt, schema, protocol, or gate
semantics.  It only permits a conservative no-op merge when a frozen packet
has an exactly empty four-part question manifest and all three already-valid
outputs are canonically identical.  Existing V3 artifacts remain immutable.
"""
from __future__ import annotations

import argparse
import copy
from collections import defaultdict
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from . import hybrid_v3_mature_s1_consistency_v3 as v3
    from .hybrid_v3_atomic_policy_v2 import expected_question_manifest
    from .hybrid_v3_atomic_policy_v3 import reduce_atomic_v3, validate_atomic
    from .hybrid_v3_consistency_v2 import _atomic_verdicts, critical_question_ids
    from .hybrid_v3_conservative_merge_v1 import conservative_merge_atomic_outputs
    from .hybrid_v3_sharding_v2 import canonical_json_bytes, canonical_sha256, file_sha256
    from .hybrid_v3_triplicate_smoke_audit import decision_signature, result_map
except ImportError:  # pragma: no cover - direct script execution
    from scripts import hybrid_v3_mature_s1_consistency_v3 as v3
    from scripts.hybrid_v3_atomic_policy_v2 import expected_question_manifest
    from scripts.hybrid_v3_atomic_policy_v3 import reduce_atomic_v3, validate_atomic
    from scripts.hybrid_v3_consistency_v2 import _atomic_verdicts, critical_question_ids
    from scripts.hybrid_v3_conservative_merge_v1 import conservative_merge_atomic_outputs
    from scripts.hybrid_v3_sharding_v2 import canonical_json_bytes, canonical_sha256, file_sha256
    from scripts.hybrid_v3_triplicate_smoke_audit import decision_signature, result_map


ROOT = Path(__file__).resolve().parents[1]
REPORT_VERSION = "hybrid-v3-mature-s1-consistency-v4-empty-manifest-technical-correction"
RECEIPT_VERSION = "hybrid-v3-mature-s1-consistency-v4-technical-correction-receipt-v1"
RECEIPT_STATUS = "IMMUTABLE_TECHNICAL_CORRECTION_INPUTS_PINNED"
EMPTY_REASON = "EMPTY_FROZEN_MANIFEST_EXACT"
EXPECTED_CASES = v3.EXPECTED_CASES
EXPECTED_RUNS = v3.EXPECTED_RUNS


class MatureS1ConsistencyV4Error(ValueError):
    """The narrow V4 correction contract cannot be proven."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MatureS1ConsistencyV4Error(f"cannot read JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise MatureS1ConsistencyV4Error(f"expected JSON object: {path}")
    return value


def _self_hash(value: Mapping[str, Any], field: str) -> bool:
    expected = value.get(field)
    core = {key: child for key, child in value.items() if key != field}
    return isinstance(expected, str) and expected == canonical_sha256(core)


def _artifact_pin(path: Path) -> dict[str, Any]:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise MatureS1ConsistencyV4Error(f"pinned artifact is missing: {resolved}")
    return {"path": str(resolved), "sha256": file_sha256(resolved)}


def _run_tree_manifest(run_root: Path, run_number: int) -> dict[str, Any]:
    root = Path(run_root).resolve()
    if not root.is_dir():
        raise MatureS1ConsistencyV4Error(f"run root is missing: {root}")
    rows: list[dict[str, Any]] = []
    case_files = attempt_files = 0
    for shard in sorted(root.glob("s*"), key=lambda path: path.name):
        if not shard.is_dir():
            continue
        for category in ("cases", "attempts"):
            directory = shard / category
            if not directory.exists():
                continue
            if not directory.is_dir() or directory.is_symlink():
                raise MatureS1ConsistencyV4Error("cases/attempts tree is not a real directory")
            for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
                if path.is_dir():
                    continue
                if path.is_symlink() or not path.is_file():
                    raise MatureS1ConsistencyV4Error("run tree contains a non-regular file")
                relative = path.relative_to(root).as_posix()
                rows.append(
                    {
                        "relative_path": relative,
                        "bytes": path.stat().st_size,
                        "sha256": file_sha256(path),
                    }
                )
                if category == "cases" and path.parent == directory and path.suffix == ".json":
                    case_files += 1
                if category == "attempts" and path.name.startswith("attempt_") and path.suffix == ".json":
                    attempt_files += 1
    if case_files != EXPECTED_CASES or attempt_files < EXPECTED_CASES:
        raise MatureS1ConsistencyV4Error(
            f"run tree coverage is not exact: cases={case_files} attempts={attempt_files}"
        )
    core = {
        "run_number": int(run_number),
        "run_root": str(root),
        "case_files": case_files,
        "attempt_files": attempt_files,
        "files": rows,
        "files_sha256": canonical_sha256(rows),
    }
    return {**core, "run_tree_sha256": canonical_sha256(core)}


def build_technical_correction_receipt(
    *,
    source: Path,
    source_manifest: Path,
    track_manifest: Path,
    assignment_manifest: Path,
    execution_freeze: Path,
    stage_protocol: Path,
    research_protocol: Path,
    schema: Path,
    run_roots: Sequence[Path],
) -> dict[str, Any]:
    """Build, but do not publish, a receipt for an already completed V3 run set."""

    records, _stage, _protocol, _focus, track_validation = v3.verify_track(
        source=source,
        source_manifest=source_manifest,
        track_manifest=track_manifest,
        assignment_manifest=assignment_manifest,
        execution_freeze=execution_freeze,
        stage_protocol=stage_protocol,
        research_protocol=research_protocol,
        schema=schema,
    )
    reviewer_sha = v3._reviewer_code_sha256(execution_freeze)
    v3.validate_run_artifacts(records, run_roots, reviewer_code_sha256=reviewer_sha)
    run_trees = [
        _run_tree_manifest(path, number)
        for number, path in enumerate(run_roots, 1)
    ]
    pins = {
        "v3_track_manifest": _artifact_pin(track_manifest),
        "v3_execution_freeze": _artifact_pin(execution_freeze),
        "v3_source_manifest": _artifact_pin(source_manifest),
        "v3_assignment_manifest": _artifact_pin(assignment_manifest),
        "v3_evaluator": _artifact_pin(Path(v3.__file__)),
        "v4_evaluator": _artifact_pin(Path(__file__)),
    }
    core = {
        "receipt_version": RECEIPT_VERSION,
        "status": RECEIPT_STATUS,
        "classification": "TECHNICAL_EVALUATOR_CORRECTION_ONLY",
        "correction": "ALLOW_EXACT_EMPTY_FROZEN_MANIFEST_VACUOUS_PASSTHROUGH",
        "pins": pins,
        "run_trees": run_trees,
        "run_trees_sha256": canonical_sha256(run_trees),
        "track_validation": track_validation,
        "strategy_changed": False,
        "gate_changed": False,
        "outputs_changed": False,
        "sample_changed": False,
        "model_changed": False,
        "prompt_changed": False,
        "ai_rerun": False,
        "performance_sealed": True,
    }
    return {**core, "receipt_sha256": canonical_sha256(core)}


def publish_technical_correction_receipt(path: Path, receipt: Mapping[str, Any]) -> str:
    """Publish a receipt immutably; formal callers should retain the returned file hash."""

    if not _self_hash(receipt, "receipt_sha256"):
        raise MatureS1ConsistencyV4Error("technical correction receipt self-hash is invalid")
    payload = canonical_json_bytes(dict(receipt)) + b"\n"
    try:
        v3.track_v3._publish_immutable(Path(path), payload)
    except Exception as exc:
        raise MatureS1ConsistencyV4Error("refusing to overwrite technical correction receipt") from exc
    return hashlib.sha256(payload).hexdigest()


def verify_technical_correction_receipt(
    *,
    receipt_path: Path,
    expected_receipt_file_sha256: str,
    source: Path,
    source_manifest: Path,
    track_manifest: Path,
    assignment_manifest: Path,
    execution_freeze: Path,
    stage_protocol: Path,
    research_protocol: Path,
    schema: Path,
    run_roots: Sequence[Path],
) -> dict[str, Any]:
    """Require an externally supplied file hash and recompute every receipt pin."""

    receipt_path = Path(receipt_path).resolve()
    if (
        len(str(expected_receipt_file_sha256)) != 64
        or not receipt_path.is_file()
        or file_sha256(receipt_path) != str(expected_receipt_file_sha256).lower()
    ):
        raise MatureS1ConsistencyV4Error("external technical correction receipt hash differs")
    receipt = _read_json(receipt_path)
    if (
        receipt.get("receipt_version") != RECEIPT_VERSION
        or receipt.get("status") != RECEIPT_STATUS
        or not _self_hash(receipt, "receipt_sha256")
    ):
        raise MatureS1ConsistencyV4Error("technical correction receipt is invalid")
    rebuilt = build_technical_correction_receipt(
        source=source,
        source_manifest=source_manifest,
        track_manifest=track_manifest,
        assignment_manifest=assignment_manifest,
        execution_freeze=execution_freeze,
        stage_protocol=stage_protocol,
        research_protocol=research_protocol,
        schema=schema,
        run_roots=run_roots,
    )
    if receipt != rebuilt:
        raise MatureS1ConsistencyV4Error("technical correction receipt pins differ")
    return receipt


def _empty_frozen_question_manifest(packet: Mapping[str, Any]) -> bool:
    manifest = packet.get("question_manifest")
    required = {
        "anchor_candidates",
        "relation_candidates",
        "stop_candidates",
        "global_question_ids",
    }
    return (
        isinstance(manifest, Mapping)
        and set(manifest) == required
        and all(isinstance(manifest[key], list) and not manifest[key] for key in required)
    )


def _not_applicable_rate() -> dict[str, Any]:
    return {
        "exact": 0,
        "total": 0,
        "rate": None,
        "applicable": False,
        "status": "N/A",
        "reason": EMPTY_REASON,
    }


def merge_case_outputs_v4(
    packet: Mapping[str, Any],
    outputs: Sequence[Mapping[str, Any]],
    *,
    critical_ids: set[str],
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Apply the narrow empty-manifest exception or the unchanged V3 merge."""

    if len(outputs) != EXPECTED_RUNS:
        raise MatureS1ConsistencyV4Error("V4 merge requires exactly three outputs")
    maps = [_atomic_verdicts(output) for output in outputs]
    empty = [not mapping for mapping in maps]
    if any(empty):
        if not all(empty):
            raise MatureS1ConsistencyV4Error("partial-empty atomic manifests are forbidden")
        if not _empty_frozen_question_manifest(packet):
            raise MatureS1ConsistencyV4Error("empty atoms lack an exactly empty frozen manifest")
        if packet.get("question_manifest") != expected_question_manifest(dict(packet)):
            raise MatureS1ConsistencyV4Error(
                "empty frozen manifest differs from deterministic atomic policy"
            )
        errors = [validate_atomic(dict(packet), dict(output)) for output in outputs]
        if any(errors):
            raise MatureS1ConsistencyV4Error(f"empty-manifest atomic validation failed: {errors}")
        output_hashes = [canonical_sha256(dict(output)) for output in outputs]
        if len(set(output_hashes)) != 1:
            raise MatureS1ConsistencyV4Error("empty-manifest full outputs are not canonically identical")
        signatures = [decision_signature(reduce_atomic_v3(dict(packet), dict(output))) for output in outputs]
        if len({canonical_sha256(signature) for signature in signatures}) != 1:
            raise MatureS1ConsistencyV4Error("empty-manifest decision signatures differ")
        return copy.deepcopy(dict(outputs[0])), {
            "merge_version": REPORT_VERSION,
            "merge_status": "VACUOUS_PASSTHROUGH",
            "technical_correction": EMPTY_REASON,
            "critical_atom_fields": 0,
            "critical_atom_exact_fields": 0,
            "critical_atom_rate": None,
            "disagreement_paths": [],
            "critical_disagreement_paths": [],
            "critical_by_question": {},
        }, True

    keys = set().union(*(set(mapping) for mapping in maps))
    if keys and not any(key[2] in critical_ids for key in keys):
        raise MatureS1ConsistencyV4Error(
            "non-empty atoms do not match frozen critical question IDs"
        )
    try:
        merged, metrics = conservative_merge_atomic_outputs(outputs, critical_ids=critical_ids)
    except Exception as exc:
        raise MatureS1ConsistencyV4Error("unchanged conservative merge failed") from exc
    return merged, metrics, False


def evaluate(
    *,
    source: Path,
    source_manifest: Path,
    track_manifest: Path,
    assignment_manifest: Path,
    execution_freeze: Path,
    stage_protocol: Path,
    research_protocol: Path,
    schema: Path,
    run_roots: Sequence[Path],
    technical_correction_receipt: Path,
    expected_receipt_file_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Evaluate only after the external V4 correction receipt is verified."""

    receipt = verify_technical_correction_receipt(
        receipt_path=technical_correction_receipt,
        expected_receipt_file_sha256=expected_receipt_file_sha256,
        source=source,
        source_manifest=source_manifest,
        track_manifest=track_manifest,
        assignment_manifest=assignment_manifest,
        execution_freeze=execution_freeze,
        stage_protocol=stage_protocol,
        research_protocol=research_protocol,
        schema=schema,
        run_roots=run_roots,
    )
    records, stage, protocol, focus_by_review, track_validation = v3.verify_track(
        source=source,
        source_manifest=source_manifest,
        track_manifest=track_manifest,
        assignment_manifest=assignment_manifest,
        execution_freeze=execution_freeze,
        stage_protocol=stage_protocol,
        research_protocol=research_protocol,
        schema=schema,
    )
    reviewer_sha = v3._reviewer_code_sha256(execution_freeze)
    run_validation, runs = v3.validate_run_artifacts(
        records, run_roots, reviewer_code_sha256=reviewer_sha
    )
    critical_ids = critical_question_ids(protocol, _read_json(schema))

    schema_exact = permission_exact = scenario_lr_exact = 0
    pooled_exact = pooled_total = critical_exact_total = critical_total = 0
    unanimous_trade = unanimous_nontrade = empty_handled = 0
    run_permissions: list[list[str]] = [[] for _ in range(EXPECTED_RUNS)]
    run_scenarios: list[list[str]] = [[] for _ in range(EXPECTED_RUNS)]
    focus_totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"cases": 0, "permission_exact": 0, "scenario_lr_exact": 0}
    )
    per_case: list[dict[str, Any]] = []
    merged_ledger: list[dict[str, Any]] = []
    for record in records:
        review_id = str(record["review_id"])
        packet = record["packet"]
        envelopes = [run[review_id] for run in runs]
        outputs = [envelope["output"] for envelope in envelopes]
        errors = [validate_atomic(packet, output) for output in outputs]
        if any(errors):
            raise MatureS1ConsistencyV4Error(
                f"atomic validation failed for {review_id}: {errors}"
            )
        schema_exact += 1
        signatures = [decision_signature(reduce_atomic_v3(packet, output)) for output in outputs]
        stage_values = [v3.stage_permission(signature) for signature in signatures]
        permission_ok = len(set(stage_values)) == 1
        scenario_lr_ok = (
            len({signature.get("scenario") for signature in signatures}) == 1
            and len({signature.get("left_right_phase") for signature in signatures}) == 1
        )
        permission_exact += int(permission_ok)
        scenario_lr_exact += int(scenario_lr_ok)
        if permission_ok and stage_values[0] == "TRADE":
            unanimous_trade += 1
        if permission_ok and stage_values[0] != "TRADE":
            unanimous_nontrade += 1
        for index, value in enumerate(stage_values):
            run_permissions[index].append(value)
            run_scenarios[index].append(str(signatures[index].get("scenario")))

        maps = [result_map(output) for output in outputs]
        paths = sorted(set().union(*(set(mapping) for mapping in maps)))
        if any(set(mapping) != set(paths) for mapping in maps):
            raise MatureS1ConsistencyV4Error(f"atomic manifest differs for {review_id}")
        critical_paths = [path for path in paths if path.rsplit(".", 1)[-1] in critical_ids]
        atom_exact = sum(len({mapping[path] for mapping in maps}) == 1 for path in paths)
        critical_atom_exact = sum(
            len({mapping[path] for mapping in maps}) == 1 for path in critical_paths
        )
        pooled_exact += atom_exact
        pooled_total += len(paths)
        critical_exact_total += critical_atom_exact
        critical_total += len(critical_paths)

        merged, merge_metrics, handled = merge_case_outputs_v4(
            packet, outputs, critical_ids=critical_ids
        )
        merged_errors = validate_atomic(packet, merged)
        if merged_errors:
            raise MatureS1ConsistencyV4Error(
                f"merged output invalid for {review_id}: {merged_errors}"
            )
        empty_handled += int(handled)
        merged_signature = decision_signature(reduce_atomic_v3(packet, merged))
        focus = focus_by_review[review_id]
        focus_totals[focus]["cases"] += 1
        focus_totals[focus]["permission_exact"] += int(permission_ok)
        focus_totals[focus]["scenario_lr_exact"] += int(scenario_lr_ok)
        case_row = {
            "review_id": review_id,
            "sampling_focus": focus,
            "atom_agreement": _not_applicable_rate() if handled else v3._rate(atom_exact, len(paths)),
            "critical_atom_agreement": (
                _not_applicable_rate()
                if handled
                else v3._rate(critical_atom_exact, len(critical_paths))
            ),
            "empty_frozen_manifest_handling": EMPTY_REASON if handled else None,
            "stage_permission_exact": permission_ok,
            "scenario_left_right_exact": scenario_lr_ok,
            "run_decisions": signatures,
            "run_stage_permissions": stage_values,
            "conservative_merge_decision": merged_signature,
            "conservative_merge_stage_permission": v3.stage_permission(merged_signature),
            "critical_disagreement_count": len(merge_metrics["critical_disagreement_paths"]),
        }
        per_case.append(case_row)
        merged_ledger.append(
            {
                "review_id": review_id,
                "sampling_focus": focus,
                "packet_sha256": record["packet_sha256"],
                "merge_policy": "UNANIMOUS_ELSE_UNKNOWN",
                "technical_correction": EMPTY_REASON if handled else None,
                "zero_atom_manifest": handled,
                "merge_mode": (
                    "IDENTICAL_ZERO_ATOM_IDENTITY" if handled else "UNANIMOUS_ELSE_UNKNOWN"
                ),
                "atomic_semantics": merged,
                "full_v3_decision": merged_signature,
                "s1_stage_permission": v3.stage_permission(merged_signature),
                "critical_disagreement_paths": merge_metrics["critical_disagreement_paths"],
            }
        )

    thresholds = stage.get("repeatability_acceptance") or {}
    schema_metric = v3._rate(schema_exact, EXPECTED_CASES, float(thresholds["schema_and_causal_fields"]))
    permission_metric = v3._rate(permission_exact, EXPECTED_CASES, float(thresholds["material_permission"]))
    scenario_metric = v3._rate(
        scenario_lr_exact, EXPECTED_CASES, float(thresholds["scenario_and_left_right_phase"])
    )
    checks = {
        "schema_and_causal_fields": bool(schema_metric["passed"]),
        "material_permission": bool(permission_metric["passed"]),
        "scenario_and_left_right_phase": bool(scenario_metric["passed"]),
        "minimum_unanimous_s1_trade_cases": unanimous_trade
        >= int(thresholds["minimum_unanimous_s1_trade_cases"]),
        "minimum_unanimous_s1_nontrade_cases": unanimous_nontrade
        >= int(thresholds["minimum_unanimous_s1_nontrade_cases"]),
    }
    passed = all(checks.values())
    focus_metrics = {
        focus: {
            "cases": values["cases"],
            "material_permission": v3._rate(values["permission_exact"], values["cases"]),
            "scenario_and_left_right_phase": v3._rate(values["scenario_lr_exact"], values["cases"]),
        }
        for focus, values in sorted(focus_totals.items())
    }
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "metrics_semantics": v3.METRICS_SEMANTICS,
        "status": "REPEATABILITY_PASS" if passed else "REPEATABILITY_FAIL",
        "classification": "OUTCOME_BLIND_MATURE_S1_REPEATABILITY_ONLY",
        "technical_correction": {
            "receipt_sha256": receipt["receipt_sha256"],
            "receipt_file_sha256": str(expected_receipt_file_sha256).lower(),
            "reason": EMPTY_REASON,
            "handled_cases": empty_handled,
            "ai_rerun": False,
            "performance_sealed": True,
            "strategy_or_gate_change": False,
        },
        "course_correctness_evaluated": False,
        "performance_evaluated": False,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "target_scenario": v3.TARGET_SCENARIO,
        "target_route": v3.TARGET_ROUTE,
        "expected_cases": EXPECTED_CASES,
        "run_count": EXPECTED_RUNS,
        "model": records[0]["model"],
        "reasoning_effort": records[0]["reasoning_effort"],
        "fail_closed_integrity": {"status": "PASS", "track": track_validation, "runs": run_validation},
        "metrics": {
            "schema_and_causal_fields": schema_metric,
            "material_permission": permission_metric,
            "scenario_and_left_right_phase": scenario_metric,
            "all_atomic_answers_pooled": v3._rate(pooled_exact, pooled_total),
            "critical_atomic_answers_pooled": v3._rate(critical_exact_total, critical_total),
            "empty_frozen_manifest_cases": empty_handled,
            "unanimous_s1_trade_cases": unanimous_trade,
            "unanimous_s1_nontrade_cases": unanimous_nontrade,
        },
        "acceptance": {"checks": checks, "passed": passed},
        "stage_permission_distributions_by_run": [
            {"run_number": index + 1, "counts": v3._counter(values)}
            for index, values in enumerate(run_permissions)
        ],
        "scenario_distributions_by_run": [
            {"run_number": index + 1, "counts": v3._counter(values)}
            for index, values in enumerate(run_scenarios)
        ],
        "sampling_focus_metrics": focus_metrics,
        "per_case": per_case,
        "raw_v3_permission_route_diagnostics": v3._raw_diagnostics(per_case),
        "next_step": (
            "LOCK_COMMON_LEDGER_AND_RUN_OUTCOME_BLIND_COURSE_FIDELITY_AUDIT"
            if passed
            else "KEEP_PERFORMANCE_SEALED_AND_VERSION_BEFORE_RETEST"
        ),
        "limitations": list(v3.evaluate.__doc__ and [
            "REPEATABILITY_IS_NOT_COURSE_CORRECTNESS",
            "REPEATABILITY_IS_NOT_TRADING_PERFORMANCE",
            "OTHER_SIX_V3_ROUTES_REMAIN_SUSPENDED",
        ]),
    }
    report["report_sha256"] = canonical_sha256(report)
    return report, merged_ledger


def render_markdown(report: Mapping[str, Any], ledger_path: Path | None = None) -> str:
    text = v3.render_markdown(report, ledger_path).replace(
        "# V3 情境1三輪一致性報告（integrity-complete）",
        "# V3 情境1三輪一致性報告（V4 technical correction）",
        1,
    )
    marker = "- 性質：結果盲化一致性驗收；不是課程正確性或交易績效。"
    disclosure = (
        marker
        + f"\n- 技術修正：`{EMPTY_REASON}`，處理 "
        + f"{report['technical_correction']['handled_cases']} 例；AI 未重跑，績效仍封存。"
    )
    return text.replace(marker, disclosure, 1)


def _publish(path: Path, payload: bytes) -> None:
    try:
        v3.track_v3._publish_immutable(Path(path), payload)
    except Exception as exc:
        raise MatureS1ConsistencyV4Error(f"refusing to overwrite immutable artifact: {path}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--track-manifest", type=Path, required=True)
    parser.add_argument("--assignment-manifest", type=Path, required=True)
    parser.add_argument("--execution-freeze", type=Path, required=True)
    parser.add_argument("--stage-protocol", type=Path, required=True)
    parser.add_argument("--research-protocol", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, action="append", required=True)
    parser.add_argument("--technical-correction-receipt", type=Path, required=True)
    parser.add_argument("--expected-receipt-file-sha256", required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--merged-ledger", type=Path, required=True)
    args = parser.parse_args(argv)
    report, ledger = evaluate(
        source=args.source,
        source_manifest=args.source_manifest,
        track_manifest=args.track_manifest,
        assignment_manifest=args.assignment_manifest,
        execution_freeze=args.execution_freeze,
        stage_protocol=args.stage_protocol,
        research_protocol=args.research_protocol,
        schema=args.schema,
        run_roots=args.run_root,
        technical_correction_receipt=args.technical_correction_receipt,
        expected_receipt_file_sha256=args.expected_receipt_file_sha256,
    )
    ledger_path: Path | None = None
    if report["acceptance"]["passed"]:
        ledger_payload = v3._canonical_jsonl(ledger)
        _publish(args.merged_ledger, ledger_payload)
        ledger_path = args.merged_ledger
        report["merged_ledger"] = {
            "status": "LOCKED_OUTCOME_BLIND_MATURE_S1_COMMON_LEDGER",
            "rows": len(ledger),
            "path": str(args.merged_ledger.resolve()),
            "sha256": hashlib.sha256(ledger_payload).hexdigest(),
        }
        report.pop("report_sha256", None)
        report["report_sha256"] = canonical_sha256(report)
    _publish(args.output_json, canonical_json_bytes(report) + b"\n")
    _publish(args.output_md, render_markdown(report, ledger_path).encode("utf-8"))
    print(json.dumps({"status": report["status"], "report_sha256": report["report_sha256"]}))
    return 0 if report["acceptance"]["passed"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
