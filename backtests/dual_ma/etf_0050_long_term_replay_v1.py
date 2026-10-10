"""Publish the corrected continuous-regime 0050 replay for standard reporting."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from .etf_0050_base_v1 import (
    MAIN_SCENARIO_BPS,
    PACKAGE_ROOT_V3,
    SLIPPAGE_SCENARIOS_BPS,
    ETF0050Error,
    StrategyRunSlice,
    tree_hash,
)
from .etf_0050_formal_v1 import (
    END,
    START,
    _jsonable,
    build_manifest_factory,
    load_package,
)
from .etf_0050_formal_v2 import (
    CONTINUOUS_CAPITAL,
    _artifact_rows,
    _verify_artifacts,
    _write_csv,
    _write_json,
    replay_cash_safe_strategy,
    simulate_buy_hold_cash_safe,
)
from .etf_0050_long_term_monitor_v1 import (
    LIFECYCLE_VERSION,
    run_five_long_term_interfaces,
)
from .fugle_source import sha256_file
from .h2_ma144_upward_entry_2022h2 import build_ma144_gate_map
from .paths import dev_run, report_path


OUTPUT_ROOT_DEFAULT = dev_run("02-etf-0050-long-term-monitor-v1")
PREVIOUS_ACCEPTED_ROOT = report_path("etf", "0050", "base-v2.2")

KNOWN_STATUSES = (
    "FILLED",
    "ADD_REJECTED_SINGLE_ENTRY_VARIANT",
    "REENTRY_REJECTED_SAME_DAY_AS_EXIT",
    "REENTRY_REJECTED_REGIME_INVALIDATED_EPISODE",
    "ENTRY_GATE_MA144_NOT_UP",
    "ENTRY_GATE_RISK_WIDTH_BELOW",
    "ENTRY_GATE_EXTENSION_BELOW_5PCT",
    "ENTRY_GATE_UNDETERMINED",
    "ENTRY_GATE_EXTENSION_UNDETERMINED",
)


def entry_opportunity_audit(
    run: StrategyRunSlice, *, slippage_bps: int
) -> list[dict[str, Any]]:
    """Count every complete entry candidate and its final disposition."""

    by_year: dict[int, Counter[str]] = {}
    cycle_by_year: Counter[int] = Counter()
    for decision in run.decisions:
        year = decision["date"].year
        counter = by_year.setdefault(year, Counter())
        for event in decision.get("events") or ():
            if event.get("type") == "ETF_REGIME_CYCLE_STARTED":
                cycle_by_year[year] += 1
            if event.get("type") == "ENTRY_DECISION":
                counter[str(event.get("status") or "UNKNOWN")] += 1
    rows: list[dict[str, Any]] = []
    total = Counter()
    for year in range(2022, 2027):
        counts = by_year.get(year, Counter())
        total.update(counts)
        rows.append(
            _audit_row(
                run,
                slippage_bps=slippage_bps,
                period=str(year),
                counts=counts,
                regime_cycles=cycle_by_year[year],
            )
        )
    rows.append(
        _audit_row(
            run,
            slippage_bps=slippage_bps,
            period="2022-2026",
            counts=total,
            regime_cycles=sum(cycle_by_year.values()),
        )
    )
    return rows


def _audit_row(
    run: StrategyRunSlice,
    *,
    slippage_bps: int,
    period: str,
    counts: Counter[str],
    regime_cycles: int,
) -> dict[str, Any]:
    known = sum(counts[status] for status in KNOWN_STATUSES)
    return {
        "strategy_id": run.spec.strategy_id,
        "strategy_label": run.spec.label,
        "slippage_bps": slippage_bps,
        "period": period,
        "regime_cycles": regime_cycles,
        "candidate_decisions": sum(counts.values()),
        "filled": counts["FILLED"],
        "already_holding": counts["ADD_REJECTED_SINGLE_ENTRY_VARIANT"],
        "same_day_reentry_blocked": counts["REENTRY_REJECTED_SAME_DAY_AS_EXIT"],
        "old_regime_lock_blocked": counts[
            "REENTRY_REJECTED_REGIME_INVALIDATED_EPISODE"
        ],
        "ma144_not_up": counts["ENTRY_GATE_MA144_NOT_UP"],
        "risk_width_below_7pct": counts["ENTRY_GATE_RISK_WIDTH_BELOW"],
        "extension_below_5pct": counts["ENTRY_GATE_EXTENSION_BELOW_5PCT"],
        "input_undetermined": (
            counts["ENTRY_GATE_UNDETERMINED"]
            + counts["ENTRY_GATE_EXTENSION_UNDETERMINED"]
        ),
        "other_rejected": sum(counts.values()) - known,
    }


def run_replay(
    *,
    package_root: Path = PACKAGE_ROOT_V3,
    output_root: Path = OUTPUT_ROOT_DEFAULT,
    repo_root: Path | None = None,
) -> Mapping[str, Any]:
    if output_root.exists():
        raise FileExistsError(f"refusing to overwrite versioned output: {output_root}")
    repo_root = repo_root or Path(__file__).resolve().parents[2]
    package = load_package(package_root)
    git_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True, encoding="utf-8"
    ).strip()
    gate_map = build_ma144_gate_map(
        dates=[bar.date for bar in package.signal_bars],
        closes=[bar.close for bar in package.signal_bars],
        official_sessions=package.sessions,
    )
    previous_hash_before = tree_hash(PREVIOUS_ACCEPTED_ROOT)
    all_metrics: list[dict[str, Any]] = []
    all_details: list[dict[str, Any]] = []
    all_transactions: list[dict[str, Any]] = []
    all_curves: list[dict[str, Any]] = []
    all_actions: list[dict[str, Any]] = []
    all_audit: list[dict[str, Any]] = []

    for bps in SLIPPAGE_SCENARIOS_BPS:
        runs = run_five_long_term_interfaces(
            signal_bars=package.signal_bars,
            raw_execution_bars=package.raw_bars,
            split_events=(package.split,),
            gate_map=gate_map,
            official_sessions=package.sessions,
            manifest_factory=build_manifest_factory(
                package=package, year=2022, git_commit=git_commit
            ),
            slippage_bps=bps,
            backtest_start=START,
            backtest_end=END,
            split_signal_coordinate_already_adjusted=True,
        )
        for run in runs:
            replay = replay_cash_safe_strategy(
                package=package,
                run=run,
                start=START,
                end=END,
                slippage_bps=bps,
                initial_capital=CONTINUOUS_CAPITAL,
                budget_mode="all_available",
            )
            all_metrics.append(replay["metrics"])
            all_details.extend(
                {"slippage_bps": bps, **row} for row in replay["details"]
            )
            all_transactions.extend(
                {
                    "slippage_bps": bps,
                    "strategy_id": run.spec.strategy_id,
                    **row,
                }
                for row in replay["transactions"]
            )
            all_curves.extend(
                {
                    "slippage_bps": bps,
                    "strategy_id": run.spec.strategy_id,
                    **row,
                }
                for row in replay["curve"]
            )
            all_actions.extend(
                {
                    "slippage_bps": bps,
                    "strategy_id": run.spec.strategy_id,
                    "action_kind": "DIVIDEND",
                    **row,
                }
                for row in replay["dividends"]
            )
            all_actions.extend(
                {
                    "slippage_bps": bps,
                    "strategy_id": run.spec.strategy_id,
                    "action_kind": "SPLIT",
                    **row,
                }
                for row in replay["splits"]
            )
            all_audit.extend(entry_opportunity_audit(run, slippage_bps=bps))

        for reinvest in (False, True):
            replay = simulate_buy_hold_cash_safe(
                package=package,
                start=START,
                end=END,
                slippage_bps=bps,
                initial_capital=CONTINUOUS_CAPITAL,
                reinvest=reinvest,
            )
            sid = replay["metrics"]["strategy_id"]
            all_metrics.append(replay["metrics"])
            all_details.extend(
                {"slippage_bps": bps, **row} for row in replay["details"]
            )
            all_transactions.extend(
                {"slippage_bps": bps, "strategy_id": sid, **row}
                for row in replay["transactions"]
            )
            all_curves.extend(
                {"slippage_bps": bps, "strategy_id": sid, **row}
                for row in replay["curve"]
            )
            all_actions.extend(
                {
                    "slippage_bps": bps,
                    "strategy_id": sid,
                    "action_kind": "DIVIDEND",
                    **row,
                }
                for row in replay["dividends"]
            )
            all_actions.extend(
                {
                    "slippage_bps": bps,
                    "strategy_id": sid,
                    "action_kind": "SPLIT",
                    **row,
                }
                for row in replay["splits"]
            )

    main = MAIN_SCENARIO_BPS
    main_metrics = [row for row in all_metrics if row["slippage_bps"] == main]
    main_details = [row for row in all_details if row["slippage_bps"] == main]
    main_transactions = [row for row in all_transactions if row["slippage_bps"] == main]
    main_curves = [row for row in all_curves if row["slippage_bps"] == main]
    main_actions = [row for row in all_actions if row["slippage_bps"] == main]
    main_audit = [row for row in all_audit if row["slippage_bps"] == main]
    if len(all_metrics) != 21 or len(main_metrics) != 7:
        raise ETF0050Error("continuous scenario method count mismatch")
    if any(Decimal(str(row["minimum_cash"])) < 0 for row in all_metrics):
        raise ETF0050Error("negative cash survived long-term replay")
    if any(
        int(row["old_regime_lock_blocked"]) != 0
        for row in all_audit
        if row["period"] == "2022-2026"
    ):
        raise ETF0050Error("old single-episode regime lock survived ETF lifecycle fix")

    with tempfile.TemporaryDirectory(prefix="0050-long-term-v1-") as temp_name:
        stage = Path(temp_name)
        details = stage / "details"
        _write_csv(details / "all_scenarios_metrics.csv", all_metrics)
        _write_csv(details / "all_scenarios_trade_details.csv", all_details)
        _write_csv(details / "all_scenarios_transactions.csv", all_transactions)
        _write_csv(details / "all_scenarios_daily_equity.csv", all_curves)
        _write_csv(details / "all_scenarios_corporate_actions.csv", all_actions)
        _write_csv(details / "all_scenarios_signal_audit.csv", all_audit)
        _write_csv(details / "continuous_500k_metrics.csv", main_metrics)
        _write_csv(details / "continuous_500k_trade_details.csv", main_details)
        _write_csv(details / "continuous_500k_transactions.csv", main_transactions)
        _write_csv(details / "continuous_500k_daily_equity.csv", main_curves)
        _write_csv(details / "continuous_500k_corporate_actions.csv", main_actions)
        _write_csv(details / "continuous_500k_signal_audit.csv", main_audit)
        _write_json(
            details / "qa-recompute.json",
            {
                "status": "PASS",
                "lifecycle_version": LIFECYCLE_VERSION,
                "scenario_method_rows": len(all_metrics),
                "main_method_rows": len(main_metrics),
                "negative_cash_rows": sum(
                    Decimal(str(row["minimum_cash"])) < 0 for row in all_metrics
                ),
                "old_regime_lock_rejections": sum(
                    int(row["old_regime_lock_blocked"])
                    for row in all_audit
                    if row["period"] == "2022-2026"
                ),
                "previous_accepted_tree_sha256_before": previous_hash_before,
                "previous_accepted_tree_sha256_after": tree_hash(PREVIOUS_ACCEPTED_ROOT),
            },
        )
        artifacts = _artifact_rows(
            stage, {"manifest.json", "readback.json", "checkpoint.json"}
        )
        manifest = {
            "schema_version": "dual-ma-0050-long-term-monitor-manifest-v1",
            "status": "STOP_WAIT_INDEPENDENT_QA",
            "git_commit": git_commit,
            "lifecycle_version": LIFECYCLE_VERSION,
            "package_root": str(package_root),
            "package_tree_sha256": package.package_hash,
            "period": {"from": START, "through": END},
            "initial_capital": CONTINUOUS_CAPITAL,
            "slippage_scenarios_bps": SLIPPAGE_SCENARIOS_BPS,
            "main_slippage_bps": MAIN_SCENARIO_BPS,
            "annual_reset": False,
            "artifacts": artifacts,
        }
        _write_json(stage / "manifest.json", manifest)
        mismatches = _verify_artifacts(stage, artifacts)
        _write_json(
            stage / "readback.json",
            {
                "status": "PASS" if not mismatches else "FAIL",
                "artifact_hashes_verified": not mismatches,
                "mismatches": mismatches,
            },
        )
        if mismatches:
            raise ETF0050Error(f"long-term artifact readback failed: {mismatches}")
        _write_json(
            stage / "checkpoint.json",
            {
                "status": "STOP_WAIT_INDEPENDENT_QA",
                "manifest_sha256": sha256_file(stage / "manifest.json"),
                "readback_sha256": sha256_file(stage / "readback.json"),
                "safe_next_step": "independent lifecycle, accounting, split and opportunity QA",
            },
        )
        output_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, output_root)

    return {
        "status": "STOP_WAIT_INDEPENDENT_QA",
        "output_root": str(output_root),
        "manifest_sha256": sha256_file(output_root / "manifest.json"),
        "readback_sha256": sha256_file(output_root / "readback.json"),
        "scenario_method_rows": len(all_metrics),
        "main_method_rows": len(main_metrics),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, default=PACKAGE_ROOT_V3)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT_DEFAULT)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            _jsonable(
                run_replay(
                    package_root=args.package_root,
                    output_root=args.output_root,
                    repo_root=Path(__file__).resolve().parents[2],
                )
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
