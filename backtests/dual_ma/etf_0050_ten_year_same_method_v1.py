"""Run the locked 0050 2017-2026 same-method continuous-account study."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.dual_ma_etf_0050_ten_year_package_v1 import technical_rows_as_of
from stock_ai_bot.data_sources.causal_price_contract import (
    ActionKind,
    CorporateActionEvent,
    CorporateActionType,
)

from .contracts import COST_MODEL, EXECUTION_MODEL, SPEC_SHA256, STRATEGY_VERSION, RunManifest
from .etf_0050_base_v1 import (
    MAIN_SCENARIO_BPS,
    SLIPPAGE_SCENARIOS_BPS,
    SYMBOL,
    CashDividendEvent,
    ETF0050Error,
    SplitEvent,
    StrategyRunSlice,
    tree_hash,
    validate_primary_split_evidence,
)
from .etf_0050_formal_v1 import PackageData, _jsonable
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
from .etf_0050_long_term_monitor_v1 import (
    LIFECYCLE_VERSION,
    run_five_long_term_interfaces,
)
from .fugle_source import sha256_file
from .h2_ma144_upward_entry_2022h2 import GateEvaluation, build_ma144_gate_map
from .models import Bar
from .paths import REPOSITORY_ROOT, dev_run, report_path


PACKAGE_ROOT = dev_run("04-etf-0050-research-package-2016-2026-v1")
OUTPUT_ROOT_DEFAULT = dev_run("02-etf-0050-2017-2026-same-method-v2")
REPORT_ROOT_DEFAULT = report_path("etf", "0050", "2017-2026-base-v2")
V1_RUN_ROOT = dev_run("02-etf-0050-2017-2026-same-method-v1")
V1_REPORT_ROOT = report_path("etf", "0050", "2017-2026-base-v1")
BASE_V4_ROOT = report_path("etf", "0050", "base-v4")
OLD_RUN_ROOT = dev_run("02-etf-0050-long-term-monitor-v1")
OLD_V3_PACKAGE_ROOT = dev_run("03-etf-research-package-v3")
CONTRACT = REPOSITORY_ROOT / "docs/strategies/dual_ma/0050-2017-2026-ten-year-same-method-v1.md"
REPORT_TEMPLATE = Path(__file__).resolve().parents[2] / "docs/strategies/dual_ma/standard-backtest-report-v2.md"
EXPECTED = {
    "contract": "B1A30A8E57025007FAD59E6578C4F408C020AAA03AA27673842A1C2F7828E663",
    "report_template": "9576B92D4CE5BE8C46ED08173DA1416F744EE8A1D73AA25277C943600FAE4B3A",
    "ready": "D55FD56FFC01F3F130A83FCDC26E4D5A02ACCC617E815528C3364AAF27E5F9DC",
    "artifact_manifest": "71D10A719A60F4D03D6D4CCBF3CC54FDCECC251037A61EF319D895B82DB1AD57",
    "readback": "6EECBB678EDEF323F2CCFEB9C4CB9F85622D937FFC4F350C05E32E2ADDD948B3",
}
FORMAL_START = date(2017, 1, 3)
CUTOFF = date(2026, 9, 30)
COMPAT_START = date(2022, 1, 3)
YEARS = tuple(range(2017, 2027))
ADOPTED_CORRECTION = "CAUSAL_TECHNICAL_CONTRACT_V2_POINT_IN_TIME_MULTIPLICATIVE_ACTION_FACTORS"
CACHE_PATH = Path(tempfile.gettempdir()) / "stock_ai_bot_0050_causal_maps_b1a30a8e_v1.json"
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


def _sha(path: Path) -> str:
    return sha256_file(path).upper()


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _bar(row: Mapping[str, Any]) -> Bar:
    return Bar.from_values(
        date.fromisoformat(str(row["date"])),
        _d(row["open"]),
        _d(row["high"]),
        _d(row["low"]),
        _d(row["close"]),
    )


def _event(row: Mapping[str, Any]) -> CorporateActionEvent:
    payload = dict(row)
    payload["action_type"] = CorporateActionType(payload["action_type"])
    payload["action_kind"] = ActionKind(payload["action_kind"])
    for key in (
        "effective_date",
        "ex_date",
        "available_on",
        "announcement_date",
        "entitlement_date",
        "payment_date",
        "halt_date",
        "resume_date",
    ):
        if payload.get(key):
            payload[key] = date.fromisoformat(payload[key])
    payload["fetched_at"] = datetime.fromisoformat(payload["fetched_at"])
    result = CorporateActionEvent(**payload)
    result.validate()
    return result


def validate_inputs(package_root: Path) -> None:
    checks = {
        "contract": CONTRACT,
        "report_template": REPORT_TEMPLATE,
        "ready": package_root / "READY_FOR_02.json",
        "artifact_manifest": package_root / "artifact_manifest.json",
        "readback": package_root / "readback.json",
    }
    mismatches = {
        key: {"expected": EXPECTED[key], "actual": _sha(path)}
        for key, path in checks.items()
        if not path.is_file() or _sha(path) != EXPECTED[key]
    }
    if mismatches:
        raise ETF0050Error(f"locked input hash mismatch: {mismatches}")
    ready = _load(package_root / "READY_FOR_02.json")
    readback = _load(package_root / "readback.json")
    if ready.get("ready") is not True or readback.get("status") != "PASS":
        raise ETF0050Error("research package is not ready/readback PASS")


def load_ten_year_package(
    package_root: Path,
) -> tuple[PackageData, list[dict[str, Any]], tuple[CorporateActionEvent, ...]]:
    validate_inputs(package_root)
    marker = _load(package_root / "READY_FOR_02.json")
    raw_rows = _load(package_root / "data/0050/daily_raw.json")["rows"]
    events = tuple(_event(row) for row in _load(package_root / "events/0050_corporate_action_events.json"))
    if len(raw_rows) != 2612 or len(events) != 22:
        raise ETF0050Error("ten-year package row/event count mismatch")
    raw_bars = tuple(_bar(row) for row in raw_rows)
    if raw_bars[0].date != date(2016, 1, 4) or raw_bars[-1].date != CUTOFF:
        raise ETF0050Error("ten-year package coverage mismatch")
    split_doc = _load(package_root / "events/0050_split.json")
    split = SplitEvent(
        effective_date=date.fromisoformat(split_doc["milestones"]["resumption_base_date"]),
        ratio=_d(split_doc["ratio"]),
        official_pre_split_reference=Decimal("188.65"),
        official_post_split_reference=_d(split_doc["official_reference_price"]),
        exact_post_split_reference=_d(split_doc["exact_reference_price"]),
        tick_size=_d(split_doc["tick_policy"]["tick_size"]),
        primary_source_url=split_doc["lineage"]["result"]["url"],
        primary_source_sha256=split_doc["lineage"]["result"]["sha256"],
    )
    validate_primary_split_evidence(split)
    dividend_doc = _load(package_root / "events/0050_dividends.json")
    dividends = tuple(
        CashDividendEvent(
            date.fromisoformat(row["ex_date"]),
            date.fromisoformat(row["payment_date"]),
            _d(row["amount_per_unit_original"]),
        )
        for row in dividend_doc["events"]
    )
    if len(dividends) != 21:
        raise ETF0050Error("dividend count mismatch")
    package = PackageData(
        marker=marker,
        raw_bars=raw_bars,
        signal_bars=raw_bars,
        sessions=tuple(bar.date for bar in raw_bars),
        raw_by_date={bar.date: bar for bar in raw_bars},
        factors_by_date={},
        split=split,
        dividends=dividends,
        package_hash=tree_hash(package_root),
        source_master_hash=sha256_file(package_root / "sources/source_master_table.json"),
    )
    return package, raw_rows, events


def build_point_in_time_maps(
    *,
    raw_rows: list[dict[str, Any]],
    events: Sequence[CorporateActionEvent],
    official_sessions: Sequence[date],
) -> tuple[dict[date, GateEvaluation], dict[date, Decimal | None], list[dict[str, Any]]]:
    """Call the locked adapter for every as-of and retain only causal day-T facts."""

    gate_map: dict[date, GateEvaluation] = {}
    extension: dict[date, Decimal | None] = {}
    audit: list[dict[str, Any]] = []
    fetched_at = events[0].fetched_at
    rebased: list[dict[str, Decimal]] = []
    factors_by_day: dict[date, Decimal] = {}
    for event in events:
        factor = Decimal("1")
        if event.cash_price_factor is not None:
            factor *= _d(event.cash_price_factor)
        if event.share_price_factor is not None:
            factor *= _d(event.share_price_factor)
        if event.rights_price_factor is not None:
            factor *= _d(event.rights_price_factor)
        factors_by_day[event.event_date] = factors_by_day.get(event.event_date, Decimal("1")) * factor

    for raw in raw_rows:
        as_of = raw["date"]
        day = date.fromisoformat(as_of)
        factor = factors_by_day.get(day)
        if factor is not None:
            for prior in rebased:
                for field in ("open", "high", "low", "close"):
                    prior[field] *= factor
        rebased.append({field: _d(raw[field]) for field in ("open", "high", "low", "close")})
        snapshot = technical_rows_as_of(raw_rows, list(events), as_of, fetched_at)
        if len(snapshot) != len(rebased) or snapshot[-1]["trade_date"] != as_of:
            raise ETF0050Error(f"causal adapter coverage mismatch at {as_of}")
        for expected, actual in zip(rebased, snapshot):
            for field in ("open", "high", "low", "close"):
                if abs(expected[field] - _d(actual[f"technical_{field}"])) > Decimal("0.00000001"):
                    raise ETF0050Error(f"causal rebase mismatch at {as_of}/{actual['trade_date']}/{field}")
        dates = [date.fromisoformat(row["trade_date"]) for row in snapshot]
        closes = [_d(row["technical_close"]) for row in snapshot]
        gate_map[day] = build_ma144_gate_map(
            dates=dates,
            closes=closes,
            official_sessions=official_sessions,
        )[day]
        extension[day] = (
            sum(closes[-21:], Decimal("0")) / Decimal("21")
            if len(closes) >= 21
            else None
        )
        tail = json.dumps(
            [str(value) for value in closes[-144:]],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        audit.append(
            {
                "as_of": day,
                "adapter_called": True,
                "row_count": len(snapshot),
                "same_day_technical_close": snapshot[-1]["technical_close"],
                "same_day_raw_close": raw["close"],
                "tail_144_sha256": hashlib.sha256(tail).hexdigest(),
                "ma144_gate_state": gate_map[day].state,
                "sma21": extension[day],
            }
        )
    return gate_map, extension, audit


_WORKER_ROWS: list[dict[str, Any]] = []
_WORKER_EVENTS: tuple[CorporateActionEvent, ...] = ()
_WORKER_SESSIONS: tuple[date, ...] = ()


def _init_causal_worker(
    rows: list[dict[str, Any]],
    events: tuple[CorporateActionEvent, ...],
    sessions: tuple[date, ...],
) -> None:
    global _WORKER_ROWS, _WORKER_EVENTS, _WORKER_SESSIONS
    _WORKER_ROWS = rows
    _WORKER_EVENTS = events
    _WORKER_SESSIONS = sessions


def _causal_worker(index: int) -> dict[str, Any]:
    raw = _WORKER_ROWS[index]
    as_of = raw["date"]
    day = date.fromisoformat(as_of)
    snapshot = technical_rows_as_of(
        _WORKER_ROWS, list(_WORKER_EVENTS), as_of, _WORKER_EVENTS[0].fetched_at
    )
    if len(snapshot) != index + 1 or snapshot[-1]["trade_date"] != as_of:
        raise ETF0050Error(f"causal adapter coverage mismatch at {as_of}")
    if _d(snapshot[-1]["technical_close"]) != _d(raw["close"]):
        raise ETF0050Error(f"same-day causal close differs from raw at {as_of}")
    dates = [date.fromisoformat(row["trade_date"]) for row in snapshot]
    closes = [_d(row["technical_close"]) for row in snapshot]
    gate = build_ma144_gate_map(
        dates=dates, closes=closes, official_sessions=_WORKER_SESSIONS
    )[day]
    extension = sum(closes[-21:], Decimal("0")) / 21 if len(closes) >= 21 else None
    tail = json.dumps(
        [str(value) for value in closes[-144:]],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "as_of": as_of,
        "gate": {
            "state": gate.state,
            "reference_date": gate.reference_date,
            "sma_at_signal": gate.sma_at_signal,
            "sma_at_reference": gate.sma_at_reference,
            "reason": gate.reason,
        },
        "sma21": extension,
        "audit": {
            "as_of": day,
            "adapter_called": True,
            "row_count": len(snapshot),
            "same_day_technical_close": snapshot[-1]["technical_close"],
            "same_day_raw_close": raw["close"],
            "tail_144_sha256": hashlib.sha256(tail).hexdigest(),
            "ma144_gate_state": gate.state,
            "sma21": extension,
        },
    }


def build_point_in_time_maps_parallel(
    *,
    raw_rows: list[dict[str, Any]],
    events: Sequence[CorporateActionEvent],
    official_sessions: Sequence[date],
    cache_path: Path = CACHE_PATH,
) -> tuple[dict[date, GateEvaluation], dict[date, Decimal | None], list[dict[str, Any]]]:
    if cache_path.is_file():
        cached = _load(cache_path)
        if cached.get("row_count") == len(raw_rows) and cached.get("last_date") == raw_rows[-1]["date"]:
            results = cached["results"]
        else:
            results = []
    else:
        results = []
    if not results:
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=4,
            initializer=_init_causal_worker,
            initargs=(raw_rows, tuple(events), tuple(official_sessions)),
        ) as pool:
            results = list(pool.map(_causal_worker, range(len(raw_rows)), chunksize=8))
        cache_path.write_text(
            json.dumps(
                _jsonable({"row_count": len(raw_rows), "last_date": raw_rows[-1]["date"], "results": results}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    gates: dict[date, GateEvaluation] = {}
    extension: dict[date, Decimal | None] = {}
    audit: list[dict[str, Any]] = []
    for result in results:
        day = date.fromisoformat(result["as_of"])
        gate = result["gate"]
        gates[day] = GateEvaluation(
            gate["state"],
            None if gate["reference_date"] in (None, "") else date.fromisoformat(str(gate["reference_date"])),
            None if gate["sma_at_signal"] in (None, "") else _d(gate["sma_at_signal"]),
            None if gate["sma_at_reference"] in (None, "") else _d(gate["sma_at_reference"]),
            gate["reason"],
        )
        extension[day] = None if result["sma21"] in (None, "") else _d(result["sma21"])
        audit.append(result["audit"])
    return gates, extension, audit


def build_incremental_maps_for_debug(
    *,
    raw_rows: list[dict[str, Any]],
    events: Sequence[CorporateActionEvent],
    official_sessions: Sequence[date],
) -> tuple[dict[date, GateEvaluation], dict[date, Decimal | None]]:
    """Fast equivalent used to diagnose the engine after adapter verification."""

    factors_by_day: dict[date, Decimal] = {}
    for event in events:
        factor = Decimal("1")
        for value in (event.cash_price_factor, event.share_price_factor, event.rights_price_factor):
            if value is not None:
                factor *= _d(value)
        factors_by_day[event.event_date] = factors_by_day.get(event.event_date, Decimal("1")) * factor
    dates: list[date] = []
    closes: list[Decimal] = []
    gates: dict[date, GateEvaluation] = {}
    extension: dict[date, Decimal | None] = {}
    for raw in raw_rows:
        day = date.fromisoformat(raw["date"])
        if day in factors_by_day:
            closes = [value * factors_by_day[day] for value in closes]
        dates.append(day)
        closes.append(_d(raw["close"]))
        gates[day] = build_ma144_gate_map(
            dates=dates, closes=closes, official_sessions=official_sessions
        )[day]
        extension[day] = sum(closes[-21:], Decimal("0")) / 21 if len(closes) >= 21 else None
    return gates, extension


def manifest_factory(package: PackageData, git_commit: str):
    def factory(matrix_id: str, bps: int) -> RunManifest:
        return RunManifest(
            run_id=f"0050-2017-2026-same-method-v2-{bps}bps",
            strategy_version=STRATEGY_VERSION,
            spec_sha256=SPEC_SHA256,
            git_commit=git_commit,
            universe_id="ETF-0050-EVERY-TRADING-DAY",
            data_snapshot_id="0050-research-package-2016-2026-v1",
            data_start=package.raw_bars[0].date,
            data_end=package.raw_bars[-1].date,
            data_hash=package.package_hash,
            execution_model=EXECUTION_MODEL,
            cost_model=COST_MODEL,
            matrix_id=matrix_id,
            slippage_bps=bps,
            strategy_parameters={"kd_exit": False, "single_position": True},
            metadata={"causal_adapter_every_as_of": True, "research_package_ready": True},
        )
    return factory


def entry_audit(run: StrategyRunSlice, *, bps: int, years: Sequence[int]) -> list[dict[str, Any]]:
    by_year: dict[int, Counter[str]] = {}
    cycles: Counter[int] = Counter()
    for decision in run.decisions:
        year = decision["date"].year
        for event in decision.get("events") or ():
            if event.get("type") == "ETF_REGIME_CYCLE_STARTED":
                cycles[year] += 1
            if event.get("type") == "ENTRY_DECISION":
                by_year.setdefault(year, Counter())[str(event.get("status") or "UNKNOWN")] += 1
    output = []
    periods: list[tuple[str, Counter[str], int]] = [
        (str(year), by_year.get(year, Counter()), cycles[year]) for year in years
    ]
    total = Counter()
    for year in years:
        total.update(by_year.get(year, Counter()))
    periods.append((f"{years[0]}-{years[-1]}", total, sum(cycles[year] for year in years)))
    for period, counts, regime_cycles in periods:
        known = sum(counts[status] for status in KNOWN_STATUSES)
        output.append({
            "strategy_id": run.spec.strategy_id,
            "strategy_label": run.spec.label,
            "slippage_bps": bps,
            "period": period,
            "regime_cycles": regime_cycles,
            "candidate_decisions": sum(counts.values()),
            "filled": counts["FILLED"],
            "already_holding": counts["ADD_REJECTED_SINGLE_ENTRY_VARIANT"],
            "same_day_reentry_blocked": counts["REENTRY_REJECTED_SAME_DAY_AS_EXIT"],
            "old_regime_lock_blocked": counts["REENTRY_REJECTED_REGIME_INVALIDATED_EPISODE"],
            "ma144_not_up": counts["ENTRY_GATE_MA144_NOT_UP"],
            "risk_width_below_7pct": counts["ENTRY_GATE_RISK_WIDTH_BELOW"],
            "extension_below_5pct": counts["ENTRY_GATE_EXTENSION_BELOW_5PCT"],
            "input_undetermined": counts["ENTRY_GATE_UNDETERMINED"] + counts["ENTRY_GATE_EXTENSION_UNDETERMINED"],
            "other_rejected": sum(counts.values()) - known,
        })
    return output


def run_scenarios(
    *,
    package: PackageData,
    gate_map: Mapping[date, GateEvaluation],
    extension_sma_map: Mapping[date, Decimal | None],
    events: Sequence[CorporateActionEvent],
    git_commit: str,
    start: date,
    scenarios: Sequence[int],
) -> dict[str, list[dict[str, Any]]]:
    cash_factors = {
        event.event_date: _d(event.cash_price_factor)
        for event in events
        if event.cash_price_factor is not None
    }
    output = {key: [] for key in ("metrics", "details", "transactions", "curves", "actions", "audit")}
    for bps in scenarios:
        runs = run_five_long_term_interfaces(
            signal_bars=package.signal_bars,
            raw_execution_bars=package.raw_bars,
            split_events=(package.split,),
            gate_map=gate_map,
            official_sessions=package.sessions,
            manifest_factory=manifest_factory(package, git_commit),
            slippage_bps=bps,
            backtest_start=start,
            backtest_end=CUTOFF,
            split_signal_coordinate_already_adjusted=False,
            signal_price_factors_by_date=cash_factors,
            extension_sma_map=extension_sma_map,
        )
        for run in runs:
            replay = replay_cash_safe_strategy(
                package=package,
                run=run,
                start=start,
                end=CUTOFF,
                slippage_bps=bps,
                initial_capital=CONTINUOUS_CAPITAL,
                budget_mode="all_available",
            )
            output["metrics"].append(replay["metrics"])
            for key, source in (("details", "details"), ("transactions", "transactions"), ("curves", "curve")):
                output[key].extend({"slippage_bps": bps, "strategy_id": run.spec.strategy_id, **row} for row in replay[source])
            output["actions"].extend({"slippage_bps": bps, "strategy_id": run.spec.strategy_id, "action_kind": "DIVIDEND", **row} for row in replay["dividends"])
            output["actions"].extend({"slippage_bps": bps, "strategy_id": run.spec.strategy_id, "action_kind": "SPLIT", **row} for row in replay["splits"])
            output["audit"].extend(entry_audit(run, bps=bps, years=tuple(range(start.year, CUTOFF.year + 1))))
        for reinvest in (False, True):
            replay = simulate_buy_hold_cash_safe(
                package=package,
                start=start,
                end=CUTOFF,
                slippage_bps=bps,
                initial_capital=CONTINUOUS_CAPITAL,
                reinvest=reinvest,
            )
            sid = replay["metrics"]["strategy_id"]
            output["metrics"].append(replay["metrics"])
            for key, source in (("details", "details"), ("transactions", "transactions"), ("curves", "curve")):
                output[key].extend({"slippage_bps": bps, "strategy_id": sid, **row} for row in replay[source])
            output["actions"].extend({"slippage_bps": bps, "strategy_id": sid, "action_kind": "DIVIDEND", **row} for row in replay["dividends"])
            output["actions"].extend({"slippage_bps": bps, "strategy_id": sid, "action_kind": "SPLIT", **row} for row in replay["splits"])
    return output


def normalize_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except Exception:
        return str(value)


def compatibility_check(replay: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    old_transactions = list(csv.DictReader((BASE_V4_ROOT / "transactions.csv").open("r", encoding="utf-8-sig", newline="")))
    new_transactions = replay["transactions"]
    execution_fields = [
        "slippage_bps", "strategy_id", "date", "role", "status", "leg_id",
        "reference_price", "fill_price", "shares", "gross", "commission", "tax",
        "cash_amount", "exit_reason",
    ]
    metadata_fields = ["initial_stop_raw", "initial_r"]
    old_rows = [[normalize_value(row.get(field)) for field in execution_fields] for row in old_transactions]
    new_rows = [[normalize_value(_jsonable(row.get(field))) for field in execution_fields] for row in new_transactions]
    execution_transaction_exact = old_rows == new_rows
    metadata_mismatches = []
    for index, (old, new) in enumerate(zip(old_transactions, new_transactions)):
        for field in metadata_fields:
            if normalize_value(old.get(field)) != normalize_value(_jsonable(new.get(field))):
                metadata_mismatches.append({
                    "row": index,
                    "strategy_id": old["strategy_id"],
                    "date": old["date"],
                    "field": field,
                    "accepted": old.get(field),
                    "replay": _jsonable(new.get(field)),
                })
    old_summary = {
        row["strategy_id"]: row
        for row in csv.DictReader((BASE_V4_ROOT / "scenario-summary.csv").open("r", encoding="utf-8-sig", newline=""))
    }
    ending_mismatches = []
    for row in replay["metrics"]:
        old = old_summary[row["strategy_id"]]
        if normalize_value(row["ending_equity"]) != normalize_value(old["ending_equity"]):
            ending_mismatches.append({
                "strategy_id": row["strategy_id"],
                "accepted": old["ending_equity"],
                "replay": row["ending_equity"],
            })
    old_annual = list(csv.DictReader((BASE_V4_ROOT / "calendar-year-summary.csv").open("r", encoding="utf-8-sig", newline="")))
    curve_by_strategy: dict[str, list[dict[str, Any]]] = {}
    for row in replay["curves"]:
        curve_by_strategy.setdefault(row["strategy_id"], []).append(row)
    annual_mismatches = []
    for old in old_annual:
        strategy_id = old["strategy_id"]
        year = int(old["year"])
        year_rows = [row for row in curve_by_strategy[strategy_id] if row["date"].year == year]
        ending = year_rows[-1]["equity"]
        if normalize_value(ending) != normalize_value(old["ending_equity"]):
            annual_mismatches.append({
                "strategy_id": strategy_id,
                "year": year,
                "field": "ending_equity",
                "accepted": old["ending_equity"],
                "replay": ending,
            })
    exact_core = execution_transaction_exact and not annual_mismatches and not ending_mismatches
    status = "PASS_EXACT_CORE_WITH_ADOPTED_METADATA_CORRECTION" if exact_core else "FAIL"
    if status == "FAIL":
        raise ETF0050Error(
            "2022 compatibility failed outside the named causal technical metadata correction"
        )
    return {
        "status": status,
        "required_exact_basis": "accepted base-v4",
        "execution_transaction_exact": execution_transaction_exact,
        "accepted_transaction_rows": len(old_rows),
        "replay_transaction_rows": len(new_rows),
        "annual_ending_equity_exact": not annual_mismatches,
        "annual_rows_checked": len(old_annual),
        "annual_mismatches": annual_mismatches,
        "final_equity_exact": not ending_mismatches,
        "ending_equity_mismatches": ending_mismatches,
        "technical_risk_metadata_exact": not metadata_mismatches,
        "technical_risk_metadata_mismatch_count": len(metadata_mismatches),
        "technical_risk_metadata_mismatch_sample": metadata_mismatches[:10],
        "named_adopted_data_correction": ADOPTED_CORRECTION if metadata_mismatches else None,
        "fail_closed_rule": "execution ledger, annual ending assets and final assets must remain exact; only stop/R metadata may differ under the named Phase-1 causal correction",
    }


def normalize_roc_day(value: str) -> date:
    year, month, day = (int(part) for part in value.split("/"))
    return date(year + 1911, month, day)


def fetch_benchmark(raw_root: Path) -> list[dict[str, Any]]:
    raw_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    seen: set[date] = set()
    for year in YEARS:
        last_month = 9 if year == CUTOFF.year else 12
        for month in range(1, last_month + 1):
            pin = f"{year:04d}{month:02d}01"
            query = urllib.parse.urlencode({"response": "json", "date": pin})
            url = f"https://www.twse.com.tw/indicesReport/MFI94U?{query}"
            request = urllib.request.Request(url, headers={"User-Agent": "stock_ai_bot-0050-ten-year-v1"})
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
            path = raw_root / f"{year:04d}{month:02d}.json"
            path.write_bytes(body)
            payload = json.loads(body.decode("utf-8-sig"))
            if payload.get("stat") != "OK" or payload.get("fields") != ["日　期", "發行量加權股價報酬指數"]:
                raise ETF0050Error(f"benchmark schema changed: {pin}")
            for raw_day, raw_value in payload.get("data", []):
                day = normalize_roc_day(raw_day)
                if FORMAL_START <= day <= CUTOFF:
                    if day in seen:
                        raise ETF0050Error(f"duplicate benchmark date: {day}")
                    seen.add(day)
                    rows.append({
                        "date": day,
                        "index_value": _d(str(raw_value).replace(",", "")),
                        "source_url": url,
                        "raw_path": f"benchmark-raw/{path.name}",
                        "raw_sha256": sha256_file(path),
                    })
    rows.sort(key=lambda row: row["date"])
    if not rows or rows[0]["date"] != FORMAL_START or rows[-1]["date"] != CUTOFF:
        raise ETF0050Error("benchmark coverage mismatch")
    base = rows[0]["index_value"]
    for row in rows:
        row["equivalent_500k"] = CONTINUOUS_CAPITAL * row["index_value"] / base
    return rows


def run_study(
    *,
    package_root: Path = PACKAGE_ROOT,
    output_root: Path = OUTPUT_ROOT_DEFAULT,
    report_root: Path = REPORT_ROOT_DEFAULT,
) -> Mapping[str, Any]:
    if output_root.exists() or report_root.exists():
        raise FileExistsError("refusing to overwrite versioned ten-year output")
    repo = Path(__file__).resolve().parents[2]
    git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True, encoding="utf-8").strip()
    protected_paths = (
        OLD_V3_PACKAGE_ROOT,
        BASE_V4_ROOT,
        OLD_RUN_ROOT,
        V1_RUN_ROOT,
        V1_REPORT_ROOT,
    )
    protected = {str(path): tree_hash(path) for path in protected_paths}
    package, raw_rows, events = load_ten_year_package(package_root)
    gate_map, extension_sma_map, causal_audit = build_point_in_time_maps_parallel(
        raw_rows=raw_rows,
        events=events,
        official_sessions=package.sessions,
    )
    all_rows = run_scenarios(
        package=package,
        gate_map=gate_map,
        extension_sma_map=extension_sma_map,
        events=events,
        git_commit=git_commit,
        start=FORMAL_START,
        scenarios=SLIPPAGE_SCENARIOS_BPS,
    )
    compat_rows = run_scenarios(
        package=package,
        gate_map=gate_map,
        extension_sma_map=extension_sma_map,
        events=events,
        git_commit=git_commit,
        start=COMPAT_START,
        scenarios=(MAIN_SCENARIO_BPS,),
    )
    compatibility = compatibility_check(compat_rows)
    main = {
        key: [row for row in rows if int(row.get("slippage_bps", MAIN_SCENARIO_BPS)) == MAIN_SCENARIO_BPS]
        for key, rows in all_rows.items()
    }
    if len(all_rows["metrics"]) != 21 or len(main["metrics"]) != 7:
        raise ETF0050Error("scenario/method count mismatch")
    if any(_d(row["minimum_cash"]) < 0 for row in all_rows["metrics"]):
        raise ETF0050Error("negative cash survived replay")
    if any(int(row["old_regime_lock_blocked"]) for row in all_rows["audit"]):
        raise ETF0050Error("old regime lock survived replay")
    for row in all_rows["metrics"]:
        validate_cash_safe_metric_semantics(row)
    buy_hold_details = [
        row for row in all_rows["details"] if str(row["strategy_id"]).startswith("BUY_HOLD_")
    ]
    buy_hold_return_formula_pass = all(
        _d(row["return_rate_or_estimate"])
        == _d(row["net_pnl_or_estimate"]) / _d(row["buy_total"])
        for row in buy_hold_details
    )
    if not buy_hold_return_formula_pass:
        raise ETF0050Error("buy-and-hold trade return must use actual buy_total")
    with tempfile.TemporaryDirectory(prefix="0050-ten-year-v2-") as temp_name:
        stage = Path(temp_name)
        details = stage / "details"
        mappings = (
            ("all_scenarios_metrics.csv", all_rows["metrics"]),
            ("all_scenarios_trade_details.csv", all_rows["details"]),
            ("all_scenarios_transactions.csv", all_rows["transactions"]),
            ("all_scenarios_daily_equity.csv", all_rows["curves"]),
            ("all_scenarios_corporate_actions.csv", all_rows["actions"]),
            ("all_scenarios_signal_audit.csv", all_rows["audit"]),
            ("continuous_500k_metrics.csv", main["metrics"]),
            ("continuous_500k_trade_details.csv", main["details"]),
            ("continuous_500k_transactions.csv", main["transactions"]),
            ("continuous_500k_daily_equity.csv", main["curves"]),
            ("continuous_500k_corporate_actions.csv", main["actions"]),
            ("continuous_500k_signal_audit.csv", main["audit"]),
            ("causal-adapter-as-of-audit.csv", causal_audit),
        )
        for name, rows in mappings:
            _write_csv(details / name, rows)
        _write_json(details / "compatibility-replay.json", compatibility)
        benchmark = fetch_benchmark(stage / "benchmark-raw")
        _write_csv(stage / "benchmark.csv", benchmark)
        protected_after = {str(path): tree_hash(path) for path in protected_paths}
        main_open_shares = {
            str(row["strategy_id"]): int(row["final_shares"])
            for row in main["metrics"]
            if int(row["open_rounds"]) > 0
        }
        qa = {
            "status": "PASS",
            "final_checkpoint": "STOP_WAIT_00_QA",
            "causal_adapter_calls": len(causal_audit),
            "causal_adapter_calls_expected": len(package.sessions),
            "formal_period": {"from": FORMAL_START, "through": CUTOFF},
            "warmup_only_before_formal": True,
            "scenario_method_rows": len(all_rows["metrics"]),
            "main_method_rows": len(main["metrics"]),
            "minimum_cash_nonnegative": True,
            "final_shares_open_position_invariant": True,
            "buy_hold_return_formula": "net_pnl_or_estimate / actual buy_total",
            "buy_hold_return_formula_pass": buy_hold_return_formula_pass,
            "legacy_cost_fields_basis": "actual incurred only",
            "cutoff_sale_cost_reserve_fields_separate": True,
            "main_20bps_open_position_final_shares": main_open_shares,
            "split_transaction_count": sum(
                row.get("date") == package.split.effective_date and row.get("role") in {"SPLIT", "CORPORATE_ACTION"}
                for row in all_rows["transactions"]
            ),
            "compatibility": compatibility,
            "protected_tree_hashes_before": protected,
            "protected_tree_hashes_after": protected_after,
            "protected_trees_unchanged": protected == protected_after,
        }
        if not qa["protected_trees_unchanged"] or qa["split_transaction_count"]:
            raise ETF0050Error("protected output or split transaction invariant failed")
        _write_json(details / "qa-recompute.json", qa)
        excluded = {"manifest.json", "readback.json", "checkpoint.json", "QA_READY.json"}
        artifacts = _artifact_rows(stage, excluded)
        manifest = {
            "schema_version": "dual-ma-0050-2017-2026-same-method-manifest-v2",
            "status": "STOP_WAIT_00_QA",
            "git_commit": git_commit,
            "contract_sha256": EXPECTED["contract"],
            "report_template_sha256": EXPECTED["report_template"],
            "package_root": str(package_root),
            "package_tree_sha256": package.package_hash,
            "period": {"warmup_from": package.sessions[0], "formal_from": FORMAL_START, "through": CUTOFF},
            "initial_capital": CONTINUOUS_CAPITAL,
            "annual_reset": False,
            "slippage_scenarios_bps": SLIPPAGE_SCENARIOS_BPS,
            "main_slippage_bps": MAIN_SCENARIO_BPS,
            "causal_adapter": "scripts/dual_ma_etf_0050_ten_year_package_v1.py::technical_rows_as_of",
            "causal_adapter_calls": len(causal_audit),
            "artifacts": artifacts,
        }
        _write_json(stage / "manifest.json", manifest)
        mismatches = _verify_artifacts(stage, artifacts)
        _write_json(stage / "readback.json", {"status": "PASS" if not mismatches else "FAIL", "mismatches": mismatches})
        if mismatches:
            raise ETF0050Error(f"artifact readback failed: {mismatches}")
        _write_json(stage / "QA_READY.json", {"status": "STOP_WAIT_00_QA", "qa_recompute": "details/qa-recompute.json", "compatibility_status": compatibility["status"]})
        _write_json(stage / "checkpoint.json", {"status": "STOP_WAIT_00_QA", "manifest_sha256": sha256_file(stage / "manifest.json"), "readback_sha256": sha256_file(stage / "readback.json"), "safe_next_step": "00 independent ten-year QA only"})
        output_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, output_root)

    from .etf_0050_standard_report_v3 import build as build_report
    report_result = build_report(
        source_root=output_root,
        benchmark_root=output_root,
        output_root=report_root,
        years=YEARS,
        period_start=FORMAL_START,
        period_end=CUTOFF,
        source_qa_filename="QA_READY.json",
        final_status="STOP_WAIT_00_QA",
    )
    return {
        "status": "STOP_WAIT_00_QA",
        "run_root": str(output_root),
        "report_root": str(report_root),
        "manifest_sha256": sha256_file(output_root / "manifest.json"),
        "report_manifest_sha256": report_result["artifact_manifest_sha256"],
        "compatibility_status": compatibility["status"],
        "causal_adapter_calls": len(causal_audit),
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
