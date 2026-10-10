"""Isolated seven-method 0050 comparison, 2006 through 2026-09-30."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import etf_0050_2006_fixed_methods_v1 as accepted
from . import etf_0050_ma21_fixed_stop_v1 as fixed
from . import etf_0050_ma21_ma144_regime_v1 as regime
from .etf_0050_base_v1 import MAIN_SCENARIO_BPS, SLIPPAGE_SCENARIOS_BPS, ETF0050Error, tree_hash
from .etf_0050_formal_v1 import _jsonable
from .etf_0050_formal_v2 import (
    CONTINUOUS_CAPITAL, _artifact_rows, _verify_artifacts, _write_csv, _write_json,
    replay_cash_safe_strategy, validate_cash_safe_metric_semantics,
)
from .etf_0050_ten_year_same_method_v1 import _d, normalize_value
from .fugle_source import sha256_file
from .paths import DUAL_MA_REPORT_ROOT, dev_run, report_path


RUNNER_VERSION = "dual-ma-0050-2006-2026-seven-methods-dynamic-exit-v1.5"
OUTPUT_ROOT_DEFAULT = dev_run("02-etf-0050-2006-2026-seven-methods-v1-5")
REPORT_ROOT_DEFAULT = report_path("etf", "0050", "experiments", "2006-2026-seven-methods-complete-report-v1-6")
PREVIOUS_RUN_ROOT = dev_run("02-etf-0050-2006-2026-seven-methods-v1")
PREVIOUS_REPORT_ROOT = report_path("etf", "0050", "experiments", "2006-2026-seven-methods-v1")
PREVIOUS_V11_RUN_ROOT = dev_run("02-etf-0050-2006-2026-seven-methods-v1-1")
PREVIOUS_V11_REPORT_ROOT = report_path("etf", "0050", "experiments", "2006-2026-seven-methods-v1-1")
PREVIOUS_V12_RUN_ROOT = dev_run("02-etf-0050-2006-2026-seven-methods-v1-2")
PREVIOUS_V12_REPORT_ROOT = report_path("etf", "0050", "experiments", "2006-2026-seven-methods-v1-2")
ACCEPTED_RUN_ROOT = dev_run("02-etf-0050-2006-2026-seven-methods-v1-3")
ACCEPTED_REPORT_ROOT = report_path("etf", "0050", "experiments", "2006-2026-seven-methods-complete-report-v1-4")
REJECTED_V15_REPORT_ROOT = report_path("etf", "0050", "experiments", "2006-2026-seven-methods-complete-report-v1-5")
REJECTED_V15_REPORT_MANIFEST = "6AD603631B6501AECD3A891202B5F9519F6F091FEA5F3364F18518F7C6DC983A"
CATALOG = DUAL_MA_REPORT_ROOT / "README.md"
METHOD_ORDER = (
    "A_LONG", fixed.CURRENT_STRATEGY_ID, fixed.STRATEGY_ID,
    "BUY_HOLD_CASH", "BUY_HOLD_REINVEST", regime.MOVING_ID, regime.FIXED_ID,
)
OLD_METHODS = METHOD_ORDER[:5]
PROTECTED_PATHS = (
    accepted.PACKAGE_ROOT, ACCEPTED_RUN_ROOT, accepted.LEGACY_REPORT_ROOT,
    ACCEPTED_REPORT_ROOT, PREVIOUS_RUN_ROOT, PREVIOUS_REPORT_ROOT,
    PREVIOUS_V11_RUN_ROOT, PREVIOUS_V11_REPORT_ROOT,
    PREVIOUS_V12_RUN_ROOT, PREVIOUS_V12_REPORT_ROOT,
    accepted.OUTPUT_ROOT_DEFAULT, accepted.REPORT_ROOT_DEFAULT, accepted.LEGACY_REPORT_ROOT,
)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def _normalized(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> list[list[str]]:
    return [[normalize_value(_jsonable(row.get(field))) for field in fields] for row in rows]


def _old_five_parity(output: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    all_files = {
        "metrics": "all_scenarios_metrics.csv", "details": "all_scenarios_trade_details.csv",
        "transactions": "all_scenarios_transactions.csv", "curves": "all_scenarios_daily_equity.csv",
        "actions": "all_scenarios_corporate_actions.csv",
    }
    main_files = {
        "metrics": "scenario-summary.csv", "details": "trade-details.csv",
        "transactions": "transactions.csv", "curves": "daily-equity.csv",
        "actions": "corporate-actions.csv",
    }
    checks: dict[str, Any] = {}
    for scope, files in (("all_scenarios", all_files), ("main_20bps", main_files)):
        for key, name in files.items():
            path = ACCEPTED_RUN_ROOT / ("details" if scope == "all_scenarios" else "") / name
            fields, expected = _read_csv(path)
            expected = [row for row in expected if row["strategy_id"] in OLD_METHODS]
            actual = [row for row in output[key] if row["strategy_id"] in OLD_METHODS]
            if scope == "main_20bps":
                actual = [row for row in actual if int(row.get("slippage_bps", MAIN_SCENARIO_BPS)) == MAIN_SCENARIO_BPS]
            exact = _normalized(actual, fields) == _normalized(expected, fields)
            checks[f"{scope}/{name}"] = {
                "expected_rows": len(expected), "actual_rows": len(actual), "exact": exact,
            }
            if not exact:
                raise ETF0050Error(f"accepted five-method parity failed: {scope}/{name}")
    return {
        "status": "PASS_EXACT", "accepted_run_root": str(ACCEPTED_RUN_ROOT),
        "accepted_manifest_sha256": sha256_file(ACCEPTED_RUN_ROOT / "manifest.json").upper(),
        "checks": checks,
    }


def run_methods(
    *, package: Any, raw_rows: Sequence[Mapping[str, Any]], events: Sequence[Any],
    git_commit: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    old, old_opportunities, old_events, causal = accepted.run_methods(
        package=package, raw_rows=raw_rows, events=events,
        git_commit=git_commit, start=accepted.FORMAL_START,
    )
    indicators = regime.build_indicator_rows(raw_rows, events)
    additions = {key: [] for key in ("metrics", "details", "transactions", "curves", "actions")}
    daily_audit: list[dict[str, Any]] = []
    event_audit: list[dict[str, Any]] = []
    for bps in SLIPPAGE_SCENARIOS_BPS:
        for strategy_id, moving_stop in ((regime.MOVING_ID, True), (regime.FIXED_ID, False)):
            frozen, daily, audit = regime.build_regime_run(
                indicator_rows=indicators, raw_by_date=package.raw_by_date,
                strategy_id=strategy_id, moving_stop=moving_stop,
                start=accepted.FORMAL_START, end=accepted.CUTOFF, slippage_bps=bps,
            )
            replay = replay_cash_safe_strategy(
                package=package, run=frozen, start=accepted.FORMAL_START, end=accepted.CUTOFF,
                slippage_bps=bps, initial_capital=CONTINUOUS_CAPITAL,
                budget_mode="all_available",
            )
            fixed._append_replay(additions, replay, bps=bps)
            if bps == MAIN_SCENARIO_BPS:
                daily_audit.extend(daily)
                event_audit.extend(audit)
    output = {key: [] for key in additions}
    for key in output:
        source = [*old[key], *additions[key]]
        for bps in SLIPPAGE_SCENARIOS_BPS:
            for strategy_id in METHOD_ORDER:
                output[key].extend(
                    row for row in source
                    if int(row.get("slippage_bps", bps)) == bps
                    and row["strategy_id"] == strategy_id
                )
    stops = {
        (int(row["slippage_bps"]), row["strategy_id"], row.get("leg_id", "")): row.get("initial_stop_raw")
        for row in output["transactions"] if row.get("role") == "MOTHER"
    }
    for row in output["details"]:
        row["initial_stop"] = stops.get((int(row["slippage_bps"]), row["strategy_id"], row.get("leg_id", "")))
    funded_entries = {
        (row["strategy_id"], row["date"])
        for row in output["transactions"]
        if int(row.get("slippage_bps", MAIN_SCENARIO_BPS)) == MAIN_SCENARIO_BPS
        and row.get("role") == "MOTHER"
        and row["strategy_id"] in {regime.MOVING_ID, regime.FIXED_ID}
    }
    for row in daily_audit:
        if row.get("entry_status") == "FILLED" and (row["strategy_id"], row["date"]) not in funded_entries:
            row["entry_status"] = "SKIPPED_CASH_OR_RISK"
    return output, old_opportunities, [*old_events, *event_audit], [*causal, *daily_audit]


def run_study(
    *, package_root: Path = accepted.PACKAGE_ROOT,
    output_root: Path = OUTPUT_ROOT_DEFAULT,
    report_root: Path = REPORT_ROOT_DEFAULT,
) -> Mapping[str, Any]:
    if output_root.exists() or report_root.exists():
        raise FileExistsError("refusing to overwrite versioned seven-method output")
    accepted.validate_inputs(package_root)
    repo = Path(__file__).resolve().parents[2]
    git_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, encoding="utf-8",
    ).strip()
    protected_before = {str(path): tree_hash(path) for path in PROTECTED_PATHS}
    package, raw_rows, events = accepted.load_package(package_root)
    output, old_opportunities, event_audit, combined_audit = run_methods(
        package=package, raw_rows=raw_rows, events=events, git_commit=git_commit,
    )
    parity = _old_five_parity(output)
    if len(output["metrics"]) != 21:
        raise ETF0050Error("seven methods x three scenarios expected")
    for row in output["metrics"]:
        validate_cash_safe_metric_semantics(row)
        if _d(row["minimum_cash"]) < 0:
            raise ETF0050Error("negative cash survived replay")
    main = {
        key: [row for row in rows if int(row.get("slippage_bps", MAIN_SCENARIO_BPS)) == MAIN_SCENARIO_BPS]
        for key, rows in output.items()
    }
    if tuple(row["strategy_id"] for row in main["metrics"]) != METHOD_ORDER:
        raise ETF0050Error("seven-method order mismatch")
    daily_audit = [row for row in combined_audit if row.get("strategy_id") in {regime.MOVING_ID, regime.FIXED_ID}]
    causal_audit = [row for row in combined_audit if row.get("strategy_id") not in {regime.MOVING_ID, regime.FIXED_ID}]
    new_events = [row for row in event_audit if row.get("strategy_id") in {regime.MOVING_ID, regime.FIXED_ID}]
    macd_events = [row for row in new_events if row.get("event") in {
        "ENTRY", "ENTRY_SKIPPED_INVALID_STOP", "MACD_ARMED",
        "VALID_RED_TO_GREEN_EXIT", "OPEN_AT_CUTOFF",
    }]
    priority_events = [row for row in new_events if row.get("event") in {
        "STOP_PRIORITY_OVER_MACD", "STRUCTURAL_STOP_EXIT", "INITIAL_STOP_EXIT",
        "VALID_RED_TO_GREEN_EXIT",
        "DOW_DEFENSE_ESTABLISHED", "DOW_DEFENSE_RAISED", "DOW_DEFENSE_APPLIED_TO_LEG",
        "DOW_DEFENSE_INVALIDATED", "CORPORATE_ACTION_REBASE",
        "STOP_ENFORCEMENT_INACTIVE_LOW_REGIME", "STOP_EXIT_SUPPRESSED_LOW_REGIME",
        "STOP_ENFORCEMENT_REACTIVATED_GE_REGIME",
    }]
    dynamic_exit_events = [row for row in new_events if row.get("event") in {
        "STOP_ENFORCEMENT_INACTIVE_LOW_REGIME", "STOP_EXIT_SUPPRESSED_LOW_REGIME",
        "STOP_ENFORCEMENT_REACTIVATED_GE_REGIME", "VALID_RED_TO_GREEN_EXIT",
        "STOP_PRIORITY_OVER_MACD", "STRUCTURAL_STOP_EXIT", "INITIAL_STOP_EXIT",
    }]
    protected_after_replay = {str(path): tree_hash(path) for path in PROTECTED_PATHS}
    if protected_before != protected_after_replay:
        raise ETF0050Error("accepted artifact changed during seven-method replay")
    produced_at = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix="0050-seven-methods-v1-") as temp_name:
        stage = Path(temp_name)
        details = stage / "details"
        for name, rows in (
            ("all_scenarios_metrics.csv", output["metrics"]),
            ("all_scenarios_trade_details.csv", output["details"]),
            ("all_scenarios_transactions.csv", output["transactions"]),
            ("all_scenarios_daily_equity.csv", output["curves"]),
            ("all_scenarios_corporate_actions.csv", output["actions"]),
        ):
            _write_csv(details / name, rows)
        for name, rows in (
            ("scenario-summary.csv", main["metrics"]), ("trade-details.csv", main["details"]),
            ("transactions.csv", main["transactions"]), ("daily-equity.csv", main["curves"]),
            ("corporate-actions.csv", main["actions"]),
        ):
            _write_csv(stage / name, rows)
        _write_csv(stage / "signal-opportunity-audit.csv", old_opportunities)
        _write_csv(stage / "new-strategy-regime-entry-audit.csv", daily_audit)
        _write_csv(stage / "new-strategy-macd-armed-cross-audit.csv", macd_events)
        _write_csv(stage / "new-strategy-stop-exit-priority-audit.csv", priority_events)
        _write_csv(stage / "dynamic-regime-exit-audit.csv", dynamic_exit_events)
        _write_csv(stage / "macd-event-audit.csv", event_audit)
        _write_csv(details / "causal-technical-audit.csv", causal_audit)
        _write_json(details / "accepted-five-method-parity.json", parity)
        shutil.copy2(ACCEPTED_RUN_ROOT / "benchmark.csv", stage / "benchmark.csv")
        shutil.copytree(ACCEPTED_RUN_ROOT / "benchmark-raw", stage / "benchmark-raw")
        protected_after = {str(path): tree_hash(path) for path in PROTECTED_PATHS}
        if protected_before != protected_after:
            raise ETF0050Error("accepted artifact changed while staging output")
        qa = {
            "status": "PASS", "final_checkpoint": "STOP_WAIT_00_QA",
            "produced_at_utc": produced_at, "method_order": METHOD_ORDER,
            "scenario_method_rows": len(output["metrics"]), "main_method_rows": len(main["metrics"]),
            "initial_capital": CONTINUOUS_CAPITAL,
            "formal_period": {"from": accepted.FORMAL_START, "through": accepted.CUTOFF},
            "warmup_2005_excluded": True, "annual_reset": False,
            "minimum_cash_nonnegative": True, "accepted_five_method_parity": parity,
            "protected_tree_hashes_before": protected_before,
            "protected_tree_hashes_after": protected_after,
            "protected_trees_unchanged": protected_before == protected_after,
            "rules_changed_after_results": False, "performance_used_to_tune_parameters": False,
        }
        _write_json(details / "qa-recompute.json", qa)
        _write_json(stage / "input-manifest.json", {
            "status": "PASS", "runner_version": RUNNER_VERSION,
            "produced_at_utc": produced_at, "git_commit": git_commit,
            "package_root": str(package_root),
            "package_manifest_sha256": accepted.EXPECTED["package_manifest"],
            "accepted_five_method_run_root": str(ACCEPTED_RUN_ROOT),
            "accepted_five_method_manifest_sha256": parity["accepted_manifest_sha256"],
            "period": {"warmup_from": package.sessions[0], "formal_from": accepted.FORMAL_START, "through": accepted.CUTOFF},
            "initial_capital": CONTINUOUS_CAPITAL, "annual_reset": False,
            "methods": METHOD_ORDER, "slippage_scenarios_bps": SLIPPAGE_SCENARIOS_BPS,
            "main_slippage_bps": MAIN_SCENARIO_BPS,
            "macd": {"fast": 21, "slow": 55, "signal": 55},
            "source_limitation_2005_2009": "FinMind public daily raw; TWSE first-party validation is monthly high/low/volume/amount, not daily",
        })
        excluded = {"manifest.json", "readback.json", "checkpoint.json", "QA_READY.json"}
        artifacts = _artifact_rows(stage, excluded)
        _write_json(stage / "manifest.json", {
            "schema_version": RUNNER_VERSION + "-manifest", "status": "STOP_WAIT_00_QA",
            "produced_at_utc": produced_at, "git_commit": git_commit,
            "period": {"start": accepted.FORMAL_START, "through": accepted.CUTOFF},
            "initial_capital": CONTINUOUS_CAPITAL, "annual_reset": False,
            "methods": METHOD_ORDER, "slippage_scenarios_bps": SLIPPAGE_SCENARIOS_BPS,
            "main_slippage_bps": MAIN_SCENARIO_BPS, "artifacts": artifacts,
        })
        mismatches = _verify_artifacts(stage, artifacts)
        _write_json(stage / "readback.json", {"status": "PASS" if not mismatches else "FAIL", "mismatches": mismatches})
        if mismatches:
            raise ETF0050Error("seven-method run readback failed")
        _write_json(stage / "QA_READY.json", {
            "status": "STOP_WAIT_00_QA", "ready_for_claimed_acceptance": False,
            "accepted_five_method_parity": "PASS_EXACT",
        })
        _write_json(stage / "checkpoint.json", {
            "status": "STOP_WAIT_00_QA",
            "manifest_sha256": sha256_file(stage / "manifest.json").upper(),
            "readback_sha256": sha256_file(stage / "readback.json").upper(),
            "safe_next_step": "00 independent seven-method strategy/report QA only",
        })
        output_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, output_root)
    from .etf_0050_2006_seven_methods_complete_report_v1 import build as build_report
    run_manifest_sha256 = sha256_file(output_root / "manifest.json").upper()
    report = build_report(
        source_root=output_root, output_root=report_root,
        expected_run_manifest=run_manifest_sha256,
        sealed_report_root=REJECTED_V15_REPORT_ROOT,
        expected_sealed_report_manifest=REJECTED_V15_REPORT_MANIFEST,
        report_version="dual-ma-0050-2006-2026-seven-methods-dynamic-exit-complete-report-v1.6",
        version_label="v1.6",
        source_run_strategy_replay_performed=True,
        source_run_data_or_strategy_changed=True,
    )
    return {
        "status": "STOP_WAIT_00_QA", "run_root": str(output_root),
        "report_root": str(report_root),
        "run_manifest_sha256": sha256_file(output_root / "manifest.json").upper(),
        "run_readback_sha256": sha256_file(output_root / "readback.json").upper(),
        "report_artifact_manifest_sha256": report["artifact_manifest_sha256"],
        "report_readback_sha256": report["readback_sha256"],
        "accepted_five_method_parity": "PASS_EXACT",
    }


def validate_only(
    *, output_root: Path = OUTPUT_ROOT_DEFAULT, report_root: Path = REPORT_ROOT_DEFAULT,
) -> Mapping[str, Any]:
    run_manifest = json.loads((output_root / "manifest.json").read_text(encoding="utf-8-sig"))
    report_manifest = json.loads((report_root / "artifact-manifest.json").read_text(encoding="utf-8-sig"))
    run_bad = _verify_artifacts(output_root, run_manifest["artifacts"])
    report_bad = [item["path"] for item in report_manifest["artifacts"] if sha256_file(report_root / item["path"]) != item["sha256"]]
    protected = {str(path): tree_hash(path) for path in PROTECTED_PATHS}
    expected = json.loads((output_root / "details" / "qa-recompute.json").read_text(encoding="utf-8-sig"))["protected_tree_hashes_before"]
    return {
        "status": "PASS" if not run_bad and not report_bad and protected == expected else "FAIL",
        "run_artifacts": len(run_manifest["artifacts"]), "run_mismatches": run_bad,
        "report_artifacts": len(report_manifest["artifacts"]), "report_mismatches": report_bad,
        "protected_trees_unchanged": protected == expected,
        "network_used": False, "writes_performed": False,
    }


def report_only(
    *, output_root: Path = OUTPUT_ROOT_DEFAULT, report_root: Path = REPORT_ROOT_DEFAULT,
) -> Mapping[str, Any]:
    if report_root.exists():
        raise FileExistsError("refusing to overwrite versioned seven-method report")
    run_manifest = json.loads((output_root / "manifest.json").read_text(encoding="utf-8-sig"))
    mismatches = _verify_artifacts(output_root, run_manifest["artifacts"])
    if mismatches:
        raise ETF0050Error(f"cannot resume report from invalid run: {mismatches}")
    from .etf_0050_2006_seven_methods_complete_report_v1 import build as build_report
    report = build_report(
        source_root=output_root, output_root=report_root,
        expected_run_manifest=sha256_file(output_root / "manifest.json").upper(),
        sealed_report_root=REJECTED_V15_REPORT_ROOT,
        expected_sealed_report_manifest=REJECTED_V15_REPORT_MANIFEST,
        report_version="dual-ma-0050-2006-2026-seven-methods-dynamic-exit-complete-report-v1.6",
        version_label="v1.6",
        source_run_strategy_replay_performed=True,
        source_run_data_or_strategy_changed=True,
    )
    return {
        "status": "STOP_WAIT_00_QA", "run_root": str(output_root),
        "report_root": str(report_root),
        "run_manifest_sha256": sha256_file(output_root / "manifest.json").upper(),
        "report_artifact_manifest_sha256": report["artifact_manifest_sha256"],
        "report_readback_sha256": report["readback_sha256"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("all", "report-only", "validate-only"), nargs="?", default="all")
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT_DEFAULT)
    parser.add_argument("--report-root", type=Path, default=REPORT_ROOT_DEFAULT)
    args = parser.parse_args(argv)
    if args.mode == "validate-only":
        result = validate_only(output_root=args.output_root, report_root=args.report_root)
    elif args.mode == "report-only":
        result = report_only(output_root=args.output_root, report_root=args.report_root)
    else:
        result = run_study(output_root=args.output_root, report_root=args.report_root)
    print(json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"PASS", "STOP_WAIT_00_QA"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
