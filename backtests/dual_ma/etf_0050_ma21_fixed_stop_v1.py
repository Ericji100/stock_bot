"""Run the isolated 0050 MA21-breakout / MACD(21,55,55) experiment."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from .etf_0050_base_v1 import (
    MAIN_SCENARIO_BPS,
    SLIPPAGE_SCENARIOS_BPS,
    ETF0050Error,
    StrategyRunSlice,
    StrategySpec,
    tree_hash,
)
from .etf_0050_formal_v1 import _jsonable
from .etf_0050_formal_v2 import (
    CONTINUOUS_CAPITAL,
    _artifact_rows,
    _verify_artifacts,
    _write_csv,
    _write_json,
    replay_cash_safe_strategy,
    simulate_buy_hold_cash_safe,
    validate_cash_safe_metric_semantics,
)
from .etf_0050_long_term_monitor_v1 import run_five_long_term_interfaces
from .etf_0050_ten_year_same_method_v1 import (
    CUTOFF,
    FORMAL_START,
    PACKAGE_ROOT,
    REPORT_TEMPLATE,
    YEARS,
    _d,
    build_point_in_time_maps_parallel,
    load_ten_year_package,
    manifest_factory,
    normalize_value,
)
from .fugle_source import sha256_file
from .models import BacktestResult, StrategyKind
from . import etf_0050_ma21_macd_v2 as accepted_v21
from .paths import dev_run, report_path


RUNNER_VERSION = "dual-ma-0050-ma21-fixed-stop-macd-21-55-55-v1.1"
STRATEGY_ID = "MA21_FIXED_INITIAL_STOP_MACD_21_55_55"
STRATEGY_LABEL = "突破 MA21＋固定初始防守＋MACD(21,55,55) 停利"
NEW_SPEC = StrategySpec(STRATEGY_ID, STRATEGY_LABEL, StrategyKind.LONG, "CUSTOM")
CURRENT_STRATEGY_ID = accepted_v21.STRATEGY_ID
METHOD_ORDER = (
    "A_LONG", CURRENT_STRATEGY_ID, STRATEGY_ID,
    "BUY_HOLD_CASH", "BUY_HOLD_REINVEST",
)
MACD_FAST = 21
MACD_SLOW = 55
MACD_SIGNAL = 55

ACCEPTED_RUN_ROOT = dev_run("02-etf-0050-2017-2026-ma21-macd-21-55-55-v2-1")
ACCEPTED_REPORT_ROOT = report_path("etf", "0050", "experiments", "2017-2026-ma21-macd-21-55-55-v2-1")
OUTPUT_ROOT_DEFAULT = dev_run("02-etf-0050-2017-2026-ma21-fixed-stop-macd-v1-1")
REPORT_ROOT_DEFAULT = report_path("etf", "0050", "experiments", "2017-2026-ma21-fixed-stop-macd-v1-1")
ACCEPTED_RUN_MANIFEST_SHA256 = (
    "07CC7634C77F6766200C1661638EC1676480FB3CE14E6453F7C4694D6A4001BD"
)
ACCEPTED_REPORT_ARTIFACT_SHA256 = (
    "169B865303CC495F2E5B862550764AE5DFABC8A891E02AE4E55FCAD74C3F0172"
)
PROTECTED_PATHS = (
    dev_run("02-etf-0050-2017-2026-same-method-v2"),
    report_path("etf", "0050", "2017-2026-base-v2"),
    dev_run("02-etf-0050-2017-2026-ma21-macd-21-55-55-v1"),
    report_path("etf", "0050", "experiments", "2017-2026-ma21-macd-21-55-55-v1"),
    dev_run("02-etf-0050-2017-2026-ma21-macd-21-55-55-v2"),
    report_path("etf", "0050", "experiments", "2017-2026-ma21-macd-21-55-55-v2"),
    ACCEPTED_RUN_ROOT, ACCEPTED_REPORT_ROOT,
    dev_run("02-etf-0050-2017-2026-ma21-fixed-stop-macd-v1"),
    report_path("etf", "0050", "experiments", "2017-2026-ma21-fixed-stop-macd-v1"),
)


def _ema(previous: Decimal | None, value: Decimal, span: int) -> Decimal:
    if previous is None:
        return value
    alpha = Decimal(2) / Decimal(span + 1)
    return value * alpha + previous * (Decimal(1) - alpha)


def build_indicator_rows(
    raw_rows: Sequence[Mapping[str, Any]],
    events: Sequence[Any],
) -> list[dict[str, Any]]:
    """Build causal daily technical values in the package's action-adjusted coordinate."""

    factors: dict[date, Decimal] = {}
    for event in events:
        factor = Decimal("1")
        for value in (
            event.cash_price_factor,
            event.share_price_factor,
            event.rights_price_factor,
        ):
            if value is not None:
                factor *= _d(value)
        factors[event.event_date] = factors.get(event.event_date, Decimal("1")) * factor

    closes: list[Decimal] = []
    ema_fast: Decimal | None = None
    ema_slow: Decimal | None = None
    signal: Decimal | None = None
    output: list[dict[str, Any]] = []
    for raw in raw_rows:
        day = date.fromisoformat(str(raw["date"]))
        factor = factors.get(day, Decimal("1"))
        if factor != 1:
            closes = [value * factor for value in closes]
            if ema_fast is not None:
                ema_fast *= factor
            if ema_slow is not None:
                ema_slow *= factor
            if signal is not None:
                signal *= factor
        close = _d(raw["close"])
        low = _d(raw["low"])
        closes.append(close)
        ema_fast = _ema(ema_fast, close, MACD_FAST)
        ema_slow = _ema(ema_slow, close, MACD_SLOW)
        dif = ema_fast - ema_slow
        signal = _ema(signal, dif, MACD_SIGNAL)
        output.append(
            {
                "date": day,
                "technical_close": close,
                "technical_low": low,
                "raw_close": close,
                "price_rebase_factor": factor,
                "sma21": (
                    sum(closes[-21:], Decimal("0")) / Decimal("21")
                    if len(closes) >= 21
                    else None
                ),
                "ema21": ema_fast,
                "ema55": ema_slow,
                "dif": dif,
                "signal": signal,
                "histogram": dif - signal,
            }
        )
    return output


def _frozen_entry(
    *, day: date, leg_id: str, raw_close: Decimal,
    technical_close: Decimal, stop: Decimal,
) -> dict[str, Any]:
    return {
        "date": day,
        "role": "MOTHER",
        "status": "FILLED",
        "leg_id": leg_id,
        "reference_price": raw_close,
        "entry_reference_close": technical_close,
        "initial_stop": stop,
        "exit_reason": None,
    }


def build_ma21_macd_run(
    *,
    indicator_rows: Sequence[Mapping[str, Any]],
    raw_by_date: Mapping[date, Any],
    start: date = FORMAL_START,
    end: date = CUTOFF,
    slippage_bps: int = 0,
) -> tuple[StrategyRunSlice, list[dict[str, Any]], list[dict[str, Any]]]:
    """Freeze causal entry/exit decisions; cash sizing remains in the accepted replayer."""

    transactions: list[dict[str, Any]] = []
    opportunities: list[dict[str, Any]] = []
    macd_audit: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    phase_low: Decimal | None = None
    previous: Mapping[str, Any] | None = None
    episode = 0

    for row in indicator_rows:
        day = row["date"]
        if day > end:
            break
        factor = _d(row["price_rebase_factor"])
        if factor != 1:
            if phase_low is not None:
                phase_low *= factor
            if active is not None:
                active["stop"] *= factor

        sma21 = row["sma21"]
        histogram = _d(row["histogram"])
        death_cross = bool(
            previous is not None
            and _d(previous["histogram"]) >= 0
            and histogram < 0
        )
        exited_today = False
        day_events: list[dict[str, Any]] = []

        if day >= start and active is not None:
            stop_hit = _d(row["technical_close"]) < _d(active["stop"])
            if not active["armed"] and histogram >= 0:
                active["armed"] = True
                active["armed_date"] = day
                macd_audit.append(
                    {
                        "episode_id": active["episode_id"],
                        "leg_id": active["leg_id"],
                        "date": day,
                        "event": "MACD_ARMED",
                        "histogram": histogram,
                        "valid": True,
                        "reason": "買進後已觀察到柱狀值大於或等於零",
                    }
                )
            if stop_hit:
                exit_reason = "INITIAL_PROTECTIVE_STOP"
                transactions.append(
                    {
                        "date": day,
                        "role": "EXIT",
                        "status": "FILLED",
                        "leg_id": active["leg_id"],
                        "reference_price": raw_by_date[day].close,
                        "exit_reason": exit_reason,
                    }
                )
                macd_audit.append(
                    {
                        "episode_id": active["episode_id"],
                        "leg_id": active["leg_id"],
                        "date": day,
                        "event": (
                            "INITIAL_STOP_PRIORITY_OVER_MACD" if death_cross
                            else "INITIAL_STOP_EXIT"
                        ),
                        "histogram": histogram,
                        "previous_histogram": previous["histogram"],
                        "valid": True,
                        "reason": "固定初始防守優先" if death_cross else "收盤跌破固定初始防守價",
                        "stop": active["stop"],
                    }
                )
                day_events.append({"type": "EXIT", "reason": exit_reason})
                active = None
                exited_today = True
            elif death_cross and active["armed"]:
                exit_reason = "MACD_RED_TO_GREEN"
                transactions.append(
                    {
                        "date": day,
                        "role": "EXIT",
                        "status": "FILLED",
                        "leg_id": active["leg_id"],
                        "reference_price": raw_by_date[day].close,
                        "exit_reason": exit_reason,
                    }
                )
                macd_audit.append(
                    {
                        "episode_id": active["episode_id"],
                        "leg_id": active["leg_id"],
                        "date": day,
                        "event": "VALID_RED_TO_GREEN_EXIT",
                        "histogram": histogram,
                        "previous_histogram": previous["histogram"],
                        "valid": True,
                        "reason": "前日柱狀值大於或等於零，當日小於零",
                    }
                )
                day_events.append({"type": "EXIT", "reason": exit_reason})
                active = None
                exited_today = True

        breakout = bool(
            day >= start
            and previous is not None
            and previous.get("sma21") is not None
            and sma21 is not None
            and _d(previous["technical_close"]) <= _d(previous["sma21"])
            and _d(row["technical_close"]) > _d(sma21)
        )
        if breakout:
            candidate_stop = min(
                value for value in (
                    phase_low,
                    _d(previous["technical_low"]),
                    _d(row["technical_low"]),
                )
                if value is not None
            )
            status = "成交"
            reason = "新的由下往上突破 MA21"
            leg_id = ""
            if exited_today:
                status = "未成交"
                reason = "同日已賣出，不得重新進場"
            elif active is not None:
                status = "未成交"
                reason = "已有持股，不重複進場"
            else:
                episode += 1
                leg_id = f"MA21-MACD-L{episode}"
                episode_id = f"MA21-MACD-E{episode}"
                effective_stop = candidate_stop
                transactions.append(
                    _frozen_entry(
                        day=day,
                        leg_id=leg_id,
                        raw_close=raw_by_date[day].close,
                        technical_close=_d(row["technical_close"]),
                        stop=candidate_stop,
                    )
                )
                active = {
                    "episode_id": episode_id,
                    "leg_id": leg_id,
                    "entry_date": day,
                    "stop": effective_stop,
                    "armed": histogram >= 0,
                    "armed_date": day if histogram >= 0 else None,
                }
                macd_audit.append(
                    {
                        "episode_id": episode_id,
                        "leg_id": leg_id,
                        "date": day,
                        "event": "ENTRY",
                        "histogram": histogram,
                        "entry_histogram_state": "大於或等於零" if histogram >= 0 else "小於零",
                        "armed_on_entry": histogram >= 0,
                        "armed_date": active["armed_date"],
                        "valid": True,
                        "reason": (
                            "買進時已可等待下一次紅翻綠"
                            if histogram >= 0
                            else "須先觀察到柱狀值大於或等於零"
                        ),
                        "initial_stop": candidate_stop,
                        "inherited_dow_stop": None,
                        "effective_stop": effective_stop,
                    }
                )
                day_events.append({"type": "ENTRY", "leg_id": leg_id})
            opportunities.append(
                {
                    "date": day,
                    "strategy_id": STRATEGY_ID,
                    "technical_close": row["technical_close"],
                    "sma21": sma21,
                    "previous_technical_close": previous["technical_close"],
                    "previous_sma21": previous["sma21"],
                    "histogram": histogram,
                    "structural_stop": candidate_stop,
                    "execution_status": status,
                    "not_filled_reason": "" if status == "成交" else reason,
                    "leg_id": leg_id,
                }
            )
            phase_low = None
        elif sma21 is not None and _d(row["technical_close"]) <= _d(sma21):
            phase_low = min(
                value for value in (phase_low, _d(row["technical_low"]))
                if value is not None
            )
        elif sma21 is not None:
            phase_low = None

        if day >= start:
            decisions.append(
                {
                    "date": day,
                    "strategy": StrategyKind.LONG.value,
                    "close": row["technical_close"],
                    "sma21": sma21,
                    "histogram": histogram,
                    "events": day_events,
                }
            )
        previous = row

    if active is not None:
        macd_audit.append(
            {
                "episode_id": active["episode_id"],
                "leg_id": active["leg_id"],
                "date": end,
                "event": "OPEN_AT_CUTOFF",
                "histogram": previous["histogram"] if previous else None,
                "valid": True,
                "reason": "截至資料截止日仍持有，以截止日收盤估算",
            }
        )

    result = object.__new__(BacktestResult)
    run = StrategyRunSlice(
        NEW_SPEC,
        result,
        tuple(decisions),
        tuple(transactions),
        (),
    )
    return run, opportunities, macd_audit


def _append_replay(
    output: dict[str, list[dict[str, Any]]],
    replay: Mapping[str, Any],
    *, bps: int,
) -> None:
    metrics = dict(replay["metrics"])
    output["metrics"].append(metrics)
    sid = str(metrics["strategy_id"])
    for key, source in (
        ("details", "details"),
        ("transactions", "transactions"),
        ("curves", "curve"),
    ):
        output[key].extend(
            {"slippage_bps": bps, "strategy_id": sid, **row}
            for row in replay[source]
        )
    output["actions"].extend(
        {"slippage_bps": bps, "strategy_id": sid, "action_kind": "DIVIDEND", **row}
        for row in replay["dividends"]
    )
    output["actions"].extend(
        {"slippage_bps": bps, "strategy_id": sid, "action_kind": "SPLIT", **row}
        for row in replay["splits"]
    )


def run_all_methods(
    *, package: Any, raw_rows: Sequence[Mapping[str, Any]], events: Sequence[Any],
    gate_map: Mapping[date, Any], extension_sma_map: Mapping[date, Decimal | None],
    git_commit: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    accepted_output, _accepted_opportunities, _accepted_audit = accepted_v21.run_all_methods(
        package=package, raw_rows=raw_rows, events=events, gate_map=gate_map,
        extension_sma_map=extension_sma_map, git_commit=git_commit,
    )
    indicators = build_indicator_rows(raw_rows, events)
    opportunities: list[dict[str, Any]] = []
    macd_audit: list[dict[str, Any]] = []
    fixed_output = {key: [] for key in ("metrics", "details", "transactions", "curves", "actions")}
    for bps in SLIPPAGE_SCENARIOS_BPS:
        new_run, scenario_opportunities, scenario_macd_audit = build_ma21_macd_run(
            indicator_rows=indicators, raw_by_date=package.raw_by_date,
            slippage_bps=bps,
        )
        if bps == MAIN_SCENARIO_BPS:
            opportunities = scenario_opportunities
            macd_audit = scenario_macd_audit
        replay = replay_cash_safe_strategy(
            package=package, run=new_run, start=FORMAL_START, end=CUTOFF,
            slippage_bps=bps, initial_capital=CONTINUOUS_CAPITAL,
            budget_mode="all_available",
        )
        _append_replay(fixed_output, replay, bps=bps)
    output = {key: [] for key in fixed_output}
    for key in output:
        source = [*accepted_output[key], *fixed_output[key]]
        for bps in SLIPPAGE_SCENARIOS_BPS:
            for sid in METHOD_ORDER:
                output[key].extend(
                    row for row in source
                    if int(row.get("slippage_bps", bps)) == bps
                    and row["strategy_id"] == sid
                )
    stops = {
        (int(row["slippage_bps"]), row["strategy_id"], row.get("leg_id", "")): row.get("initial_stop_raw")
        for row in output["transactions"]
        if row.get("role") == "MOTHER"
    }
    for row in output["details"]:
        row["initial_stop"] = stops.get(
            (int(row["slippage_bps"]), row["strategy_id"], row.get("leg_id", ""))
        )
    return output, opportunities, macd_audit


def _csv_bytes(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _jsonable(row.get(field)) for field in fields})
    return stream.getvalue().encode("utf-8")


def _accepted_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def baseline_parity(output: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    if sha256_file(ACCEPTED_RUN_ROOT / "manifest.json").upper() != ACCEPTED_RUN_MANIFEST_SHA256:
        raise ETF0050Error("accepted 0050 run manifest pin changed")
    if sha256_file(ACCEPTED_REPORT_ROOT / "artifact-manifest.json").upper() != ACCEPTED_REPORT_ARTIFACT_SHA256:
        raise ETF0050Error("accepted 0050 report artifact manifest pin changed")
    accepted_readback = json.loads(
        (ACCEPTED_RUN_ROOT / "readback.json").read_text(encoding="utf-8-sig")
    )
    if accepted_readback.get("status") != "PASS" or accepted_readback.get("mismatches"):
        raise ETF0050Error("accepted 0050 run readback is not clean")

    files = {
        "metrics": "scenario-summary.csv",
        "details": "trade-details.csv",
        "transactions": "transactions.csv",
        "curves": "daily-equity.csv",
        "actions": "corporate-actions.csv",
    }
    methods = {"A_LONG", CURRENT_STRATEGY_ID, "BUY_HOLD_CASH", "BUY_HOLD_REINVEST"}
    checks: dict[str, Any] = {}
    for key, name in files.items():
        fields, accepted = _accepted_rows(ACCEPTED_RUN_ROOT / name)
        accepted = [row for row in accepted if row["strategy_id"] in methods]
        replay = [
            row
            for row in output[key]
            if str(row["strategy_id"]) in methods
            and int(row.get("slippage_bps", MAIN_SCENARIO_BPS))
            == MAIN_SCENARIO_BPS
        ]
        accepted_normalized = [
            [normalize_value(row.get(field)) for field in fields] for row in accepted
        ]
        replay_normalized = [
            [normalize_value(_jsonable(row.get(field))) for field in fields] for row in replay
        ]
        exact = accepted_normalized == replay_normalized
        byte_exact = _csv_bytes(accepted, fields) == _csv_bytes(replay, fields)
        checks[key] = {
            "accepted_rows": len(accepted),
            "replay_rows": len(replay),
            "numeric_and_text_exact": exact,
            "canonical_csv_byte_exact": byte_exact,
        }
        if not exact or not byte_exact:
            raise ETF0050Error(f"accepted baseline parity failed: {key}")
    return {
        "status": "PASS",
        "accepted_run_manifest_sha256": ACCEPTED_RUN_MANIFEST_SHA256,
        "accepted_report_artifact_manifest_sha256": ACCEPTED_REPORT_ARTIFACT_SHA256,
        "methods": sorted(methods),
        "files": checks,
    }


def _finalize_opportunities(
    opportunities: Sequence[Mapping[str, Any]],
    replay: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    filled = {
        str(row["leg_id"])
        for row in replay["transactions"]
        if int(row["slippage_bps"]) == MAIN_SCENARIO_BPS
        and row["strategy_id"] == STRATEGY_ID
        and row["role"] == "MOTHER"
    }
    output = []
    for row in opportunities:
        item = dict(row)
        if item["execution_status"] == "成交" and item["leg_id"] not in filled:
            item["execution_status"] = "未成交"
            item["not_filled_reason"] = "可用現金或風險上限不足"
        output.append(item)
    return output


def run_study(
    *, package_root: Path = PACKAGE_ROOT, output_root: Path = OUTPUT_ROOT_DEFAULT,
    report_root: Path = REPORT_ROOT_DEFAULT,
) -> Mapping[str, Any]:
    if output_root.exists() or report_root.exists():
        raise FileExistsError("refusing to overwrite versioned MA21/MACD output")
    repo = Path(__file__).resolve().parents[2]
    git_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, encoding="utf-8"
    ).strip()
    protected_paths = PROTECTED_PATHS
    protected_before = {str(path): tree_hash(path) for path in protected_paths}
    package, raw_rows, events = load_ten_year_package(package_root)
    gate_map, extension_sma_map, causal_audit = build_point_in_time_maps_parallel(
        raw_rows=raw_rows, events=events, official_sessions=package.sessions
    )
    output, opportunities, macd_audit = run_all_methods(
        package=package, raw_rows=raw_rows, events=events, gate_map=gate_map,
        extension_sma_map=extension_sma_map, git_commit=git_commit,
    )
    for row in output["metrics"]:
        validate_cash_safe_metric_semantics(row)
    parity = baseline_parity(output)
    main = {
        key: [
            row for row in rows
            if int(row.get("slippage_bps", MAIN_SCENARIO_BPS)) == MAIN_SCENARIO_BPS
        ]
        for key, rows in output.items()
    }
    opportunities = _finalize_opportunities(opportunities, output)
    protected_after = {str(path): tree_hash(path) for path in protected_paths}
    if protected_before != protected_after:
        raise ETF0050Error("accepted 0050 run/report changed during experiment")

    with tempfile.TemporaryDirectory(prefix="0050-ma21-fixed-stop-v1-1-") as temp_name:
        stage = Path(temp_name)
        details = stage / "details"
        all_mappings = (
            ("all_scenarios_metrics.csv", output["metrics"]),
            ("all_scenarios_trade_details.csv", output["details"]),
            ("all_scenarios_transactions.csv", output["transactions"]),
            ("all_scenarios_daily_equity.csv", output["curves"]),
            ("all_scenarios_corporate_actions.csv", output["actions"]),
        )
        main_mappings = (
            ("scenario-summary.csv", main["metrics"]),
            ("trade-details.csv", main["details"]),
            ("transactions.csv", main["transactions"]),
            ("daily-equity.csv", main["curves"]),
            ("corporate-actions.csv", main["actions"]),
        )
        for name, rows in all_mappings:
            _write_csv(details / name, rows)
        for name, rows in main_mappings:
            _write_csv(stage / name, rows)
        _write_csv(stage / "signal-opportunity-audit.csv", opportunities)
        _write_csv(stage / "macd-event-audit.csv", macd_audit)
        _write_csv(stage / "fixed-initial-stop-macd-event-audit.csv", macd_audit)
        _write_csv(details / "causal-adapter-as-of-audit.csv", causal_audit)
        _write_json(details / "baseline-parity.json", parity)
        shutil.copy2(ACCEPTED_RUN_ROOT / "benchmark.csv", stage / "benchmark.csv")
        _write_json(
            stage / "input-manifest.json",
            {
                "status": "PASS",
                "runner_version": RUNNER_VERSION,
                "git_commit": git_commit,
                "period": {"start": FORMAL_START, "cutoff": CUTOFF},
                "initial_capital": CONTINUOUS_CAPITAL,
                "annual_reset": False,
                "package_root": str(package_root),
                "package_tree_sha256": package.package_hash,
                "accepted_run_root": str(ACCEPTED_RUN_ROOT),
                "accepted_run_manifest_sha256": ACCEPTED_RUN_MANIFEST_SHA256,
                "accepted_report_root": str(ACCEPTED_REPORT_ROOT),
                "accepted_report_artifact_manifest_sha256": ACCEPTED_REPORT_ARTIFACT_SHA256,
                "report_template": str(REPORT_TEMPLATE),
                "report_template_sha256": sha256_file(REPORT_TEMPLATE).upper(),
                "benchmark_sha256": sha256_file(stage / "benchmark.csv").upper(),
                "strategy": {
                    "entry": "new close>SMA21 while prior close<=prior SMA21",
                    "stop": "entry-time candidate initial stop fixed for the episode; corporate-action scaling only; close break first priority",
                    "macd": {"fast": 21, "slow": 55, "signal": 55, "ema_adjust": False},
                    "macd_exit": "prior histogram>=0 and current histogram<0 after episode armed",
                    "same_day_reentry": False,
                    "cutoff_force_exit": False,
                },
            },
        )
        qa = {
            "status": "PASS",
            "final_checkpoint": "STOP_WAIT_00_QA",
            "method_order": METHOD_ORDER,
            "main_scenario_rows": len(main["metrics"]),
            "main_strategy_trade_rows": sum(
                row["strategy_id"] == STRATEGY_ID for row in main["details"]
            ),
            "main_strategy_open_rows": sum(
                row["strategy_id"] == STRATEGY_ID
                and row["status_zh"] != "已賣出"
                for row in main["details"]
            ),
            "baseline_parity": parity,
            "protected_tree_hashes_before": protected_before,
            "protected_tree_hashes_after": protected_after,
            "protected_trees_unchanged": protected_before == protected_after,
            "rules_changed_after_results": False,
            "main_live_deployment_touched": False,
        }
        _write_json(details / "qa-recompute.json", qa)
        excluded = {"manifest.json", "readback.json", "checkpoint.json", "QA_READY.json"}
        artifacts = _artifact_rows(stage, excluded)
        manifest = {
            "schema_version": "dual-ma-0050-ma21-fixed-stop-experiment-manifest-v1.1",
            "status": "STOP_WAIT_00_QA",
            "runner_version": RUNNER_VERSION,
            "git_commit": git_commit,
            "period": {"start": FORMAL_START, "through": CUTOFF},
            "initial_capital": CONTINUOUS_CAPITAL,
            "methods": METHOD_ORDER,
            "slippage_scenarios_bps": SLIPPAGE_SCENARIOS_BPS,
            "main_slippage_bps": MAIN_SCENARIO_BPS,
            "artifacts": artifacts,
        }
        _write_json(stage / "manifest.json", manifest)
        mismatches = _verify_artifacts(stage, artifacts)
        _write_json(stage / "readback.json", {"status": "PASS" if not mismatches else "FAIL", "mismatches": mismatches})
        if mismatches:
            raise ETF0050Error(f"run artifact readback failed: {mismatches}")
        _write_json(stage / "QA_READY.json", {"status": "STOP_WAIT_00_QA", "baseline_parity": "PASS"})
        _write_json(
            stage / "checkpoint.json",
            {
                "status": "STOP_WAIT_00_QA",
                "manifest_sha256": sha256_file(stage / "manifest.json").upper(),
                "readback_sha256": sha256_file(stage / "readback.json").upper(),
                "safe_next_step": "00 independent rule, parity, accounting and report QA only",
            },
        )
        output_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, output_root)

    from .etf_0050_ma21_fixed_stop_report_v1 import build as build_report

    report = build_report(source_root=output_root, output_root=report_root)
    return {
        "status": "STOP_WAIT_00_QA",
        "run_root": str(output_root),
        "report_root": str(report_root),
        "run_manifest_sha256": sha256_file(output_root / "manifest.json").upper(),
        "run_readback_sha256": sha256_file(output_root / "readback.json").upper(),
        "report_artifact_manifest_sha256": report["artifact_manifest_sha256"],
        "report_readback_sha256": report["readback_sha256"],
        "baseline_parity": "PASS",
        "new_strategy_trade_rows": qa["main_strategy_trade_rows"],
        "new_strategy_open_rows": qa["main_strategy_open_rows"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, default=PACKAGE_ROOT)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT_DEFAULT)
    parser.add_argument("--report-root", type=Path, default=REPORT_ROOT_DEFAULT)
    args = parser.parse_args(argv)
    print(json.dumps(_jsonable(run_study(package_root=args.package_root, output_root=args.output_root, report_root=args.report_root)), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
