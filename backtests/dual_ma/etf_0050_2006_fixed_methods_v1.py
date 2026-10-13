"""Run the accepted five 0050 methods from 2006 through 2026-09-30.

This is the Phase-2 consumer of the independently accepted 2005--2026 data
package.  It keeps one continuous TWD 500,000 account, evaluates 0/10/20 bps,
and performs a separate 2016-warmup/2017-start compatibility replay against
the accepted fixed-stop v1.1 run.  It never rewrites an accepted artifact.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from stock_ai_bot.data_sources.etf_snapshot_registry import (
    EtfSnapshotRegistryError,
    resolve_snapshot,
)

from . import etf_0050_ma21_fixed_stop_report_v1 as accepted_report_builder
from . import etf_0050_ma21_fixed_stop_v1 as fixed
from . import etf_0050_ma21_macd_v2 as moving
from . import etf_0050_standard_report_v3 as standard
from .contracts import COST_MODEL, EXECUTION_MODEL, SPEC_SHA256, STRATEGY_VERSION, RunManifest
from .etf_0050_2006_data_package_v1 import file_sha256
from .etf_0050_base_v1 import (
    MAIN_SCENARIO_BPS,
    SLIPPAGE_SCENARIOS_BPS,
    CashDividendEvent,
    ETF0050Error,
    SplitEvent,
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
    validate_cash_safe_metric_semantics,
)
from .etf_0050_ten_year_same_method_v1 import _d, _event, normalize_roc_day, normalize_value
from .fugle_source import sha256_file
from .h2_ma144_upward_entry_2022h2 import (
    GATE_BACK_SESSIONS,
    GATE_SMA_WINDOW,
    GateEvaluation,
    _evaluate_ma144_gate,
)
from .models import Bar
from .paths import REPOSITORY_ROOT, dev_run, report_path


try:
    PACKAGE_ROOT = resolve_snapshot("0050").package_root
except EtfSnapshotRegistryError:
    # Backward-compatible fallback for machines that have not installed the
    # shared ETF registry yet. Both locations are independently hash-gated.
    PACKAGE_ROOT = dev_run("02-etf-0050-research-package-2005-2026-v1")
OUTPUT_ROOT_DEFAULT = dev_run("02-etf-0050-2006-2026-fixed-methods-v1")
LEGACY_REPORT_ROOT = report_path("etf", "0050", "experiments", "2006-2026-fixed-methods-v1")
REPORT_ROOT_DEFAULT = report_path("etf", "0050", "experiments", "2006-2026-fixed-methods-v1-1")
ACCEPTED_RUN_ROOT = dev_run("02-etf-0050-2017-2026-ma21-fixed-stop-macd-v1-1")
ACCEPTED_REPORT_ROOT = report_path("etf", "0050", "experiments", "2017-2026-ma21-fixed-stop-macd-v1-1")
ADOPTED_DATA_ROOT = dev_run("04-etf-0050-research-package-2016-2026-v1")
ACCEPTED_BENCHMARK_RAW_ROOT = dev_run("02-etf-0050-2017-2026-same-method-v2") / "benchmark-raw"
CONTRACT = REPOSITORY_ROOT / "docs/strategies/dual_ma/0050-2006-2026-data-and-backtest-v1.md"
REPORT_TEMPLATE = Path(__file__).resolve().parents[2] / "docs/strategies/dual_ma/standard-backtest-report-v2.md"

FORMAL_START = date(2006, 1, 2)
COMPAT_WARMUP_START = date(2016, 1, 4)
COMPAT_START = date(2017, 1, 3)
CUTOFF = date(2026, 9, 30)
YEARS = tuple(range(2006, 2027))
METHOD_ORDER = fixed.METHOD_ORDER
RUNNER_VERSION = "dual-ma-0050-2006-2026-fixed-methods-v1"
REPORT_VERSION = "dual-ma-0050-2006-2026-fixed-methods-standard-report-v1"
ERRATA_REPORT_VERSION = "dual-ma-0050-2006-2026-fixed-methods-standard-report-v1.1"

EXPECTED = {
    "contract": "5103731DBAC279202C0D2D8018BD35AD048900192A3BCA5796D7F4572686FAFA",
    "template": "9576B92D4CE5BE8C46ED08173DA1416F744EE8A1D73AA25277C943600FAE4B3A",
    "package_gate": "E6EC8E504C1DF108CD05C4472E3EF4624910E342842FEE4FDE8CAB040679311F",
    "package_manifest": "86D91332D1974CE54B0ED76F623EE253919DF3A7C5AA9CCB454A7516560F6CBB",
    "package_readback": "44ED3A1F34380852558F83F33E7BA5814193AE53F3AC04352BFDF56AB95D07A8",
    "accepted_run_manifest": "616C2CF2955BD44A8F7F62C8DCA1A70BA162E994034FA7780F777FFA1BAF9A47",
    "accepted_report_manifest": "CA2F0CA9EB287E00B4B33840BD5D3CAE543D40ED59378B57A8846AAF2331BBCE",
    "adopted_data_ready": "D55FD56FFC01F3F130A83FCDC26E4D5A02ACCC617E815528C3364AAF27E5F9DC",
    "adopted_data_manifest": "71D10A719A60F4D03D6D4CCBF3CC54FDCECC251037A61EF319D895B82DB1AD57",
    "phase2_run_manifest": "C57DEA6132FD41C49C2AB9DB2F54D51060A764A17F3DDDBFF9F0CCB4B78C1430",
    "legacy_report_manifest": "D11E42300E103A94012BF01515BADB761D913F92B319FC5498203C65A4C21C2F",
}

PROTECTED_PATHS = (
    PACKAGE_ROOT,
    ADOPTED_DATA_ROOT,
    ACCEPTED_RUN_ROOT,
    ACCEPTED_REPORT_ROOT,
    dev_run("02-etf-0050-2017-2026-same-method-v2"),
    report_path("etf", "0050", "2017-2026-base-v2"),
    LEGACY_REPORT_ROOT,
)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def validate_inputs(package_root: Path = PACKAGE_ROOT) -> None:
    checks = {
        "contract": CONTRACT,
        "template": REPORT_TEMPLATE,
        "package_gate": package_root / "STOP_WAIT_00_DATA_QA.json",
        "package_manifest": package_root / "artifact_manifest.json",
        "package_readback": package_root / "readback.json",
        "accepted_run_manifest": ACCEPTED_RUN_ROOT / "manifest.json",
        "accepted_report_manifest": ACCEPTED_REPORT_ROOT / "artifact-manifest.json",
        "adopted_data_ready": ADOPTED_DATA_ROOT / "READY_FOR_02.json",
        "adopted_data_manifest": ADOPTED_DATA_ROOT / "artifact_manifest.json",
    }
    mismatches = {
        name: {"expected": EXPECTED[name], "actual": None if not path.is_file() else file_sha256(path)}
        for name, path in checks.items()
        if not path.is_file() or file_sha256(path) != EXPECTED[name]
    }
    if mismatches:
        raise ETF0050Error(f"locked input hash mismatch: {mismatches}")
    gate = _load(package_root / "STOP_WAIT_00_DATA_QA.json")
    readback = _load(package_root / "readback.json")
    coverage = _load(package_root / "coverage.json")
    if gate.get("status") != "STOP_WAIT_00_DATA_QA" or gate.get("data_complete_candidate") is not True:
        raise ETF0050Error("Phase-1 package is not a complete data candidate")
    if gate.get("ready_for_performance") is not False:
        raise ETF0050Error("unexpected Phase-1 performance gate state")
    if readback.get("status") != "PASS" or coverage.get("status") != "PASS" or coverage.get("named_gaps"):
        raise ETF0050Error("Phase-1 package readback/coverage is not clean")


def load_package(
    package_root: Path = PACKAGE_ROOT,
    *,
    slice_from: date | None = None,
) -> tuple[PackageData, list[dict[str, Any]], tuple[Any, ...]]:
    validate_inputs(package_root)
    marker = _load(package_root / "STOP_WAIT_00_DATA_QA.json")
    raw_rows = list(_load(package_root / "data/0050/daily_raw.json")["rows"])
    event_rows = list(_load(package_root / "events/0050_corporate_action_events.json"))
    dividend_rows = list(_load(package_root / "events/0050_dividends.json")["events"])
    if slice_from is not None:
        raw_rows = [row for row in raw_rows if date.fromisoformat(row["date"]) >= slice_from]
        event_rows = [row for row in event_rows if date.fromisoformat(row["effective_date"]) >= slice_from]
        dividend_rows = [row for row in dividend_rows if date.fromisoformat(row["ex_date"]) >= slice_from]
    events = tuple(_event(row) for row in event_rows)
    raw_bars = tuple(
        Bar.from_values(
            date.fromisoformat(row["date"]), _d(row["open"]), _d(row["high"]),
            _d(row["low"]), _d(row["close"]),
        )
        for row in raw_rows
    )
    expected_start = slice_from or date(2005, 1, 3)
    if not raw_bars or raw_bars[0].date != expected_start or raw_bars[-1].date != CUTOFF:
        raise ETF0050Error("package coverage mismatch")
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
    dividends = tuple(
        CashDividendEvent(
            date.fromisoformat(row["ex_date"]),
            date.fromisoformat(row["payment_date"]),
            _d(row["amount_per_unit_original"]),
        )
        for row in dividend_rows
    )
    package = PackageData(
        marker=marker,
        raw_bars=raw_bars,
        signal_bars=raw_bars,
        sessions=tuple(bar.date for bar in raw_bars),
        raw_by_date={bar.date: bar for bar in raw_bars},
        factors_by_date={},
        split=split,
        dividends=dividends,
        package_hash=file_sha256(package_root / "artifact_manifest.json"),
        source_master_hash=sha256_file(package_root / "sources/source_master_table.json"),
    )
    return package, raw_rows, events


def build_causal_maps(
    *, raw_rows: Sequence[Mapping[str, Any]], events: Sequence[Any],
    official_sessions: Sequence[date],
) -> tuple[dict[date, GateEvaluation], dict[date, Decimal | None], list[dict[str, Any]]]:
    factors: dict[date, Decimal] = {}
    for event in events:
        factor = Decimal("1")
        for value in (event.cash_price_factor, event.share_price_factor, event.rights_price_factor):
            if value is not None:
                factor *= _d(value)
        factors[event.event_date] = factors.get(event.event_date, Decimal("1")) * factor
    dates: list[date] = []
    closes: list[Decimal] = []
    prefix = [Decimal("0")]
    official_position = {day: index for index, day in enumerate(official_sessions)}
    gates: dict[date, GateEvaluation] = {}
    sma21: dict[date, Decimal | None] = {}
    audit: list[dict[str, Any]] = []
    for raw in raw_rows:
        day = date.fromisoformat(str(raw["date"]))
        factor = factors.get(day, Decimal("1"))
        if factor != 1:
            closes = [value * factor for value in closes]
            prefix = [value * factor for value in prefix]
        dates.append(day)
        closes.append(_d(raw["close"]))
        prefix.append(prefix[-1] + closes[-1])
        gate = _evaluate_ma144_gate(
            signal_position=len(dates) - 1,
            signal_date=day,
            dates=dates,
            prefix_sums=prefix,
            official_position=official_position,
            official_sessions=official_sessions,
            back=GATE_BACK_SESSIONS,
            window=GATE_SMA_WINDOW,
        )
        value = sum(closes[-21:], Decimal("0")) / Decimal("21") if len(closes) >= 21 else None
        gates[day] = gate
        sma21[day] = value
        tail = json.dumps([str(item) for item in closes[-144:]], separators=(",", ":")).encode("utf-8")
        audit.append({
            "as_of": day, "row_count": len(closes), "same_day_technical_close": closes[-1],
            "same_day_raw_close": raw["close"], "tail_144_sha256": hashlib.sha256(tail).hexdigest(),
            "ma144_gate_state": gate.state, "sma21": value,
            "construction": "ACCEPTED_CAUSAL_INCREMENTAL_EVENT_REBASE",
        })
    return gates, sma21, audit


def manifest_factory(package: PackageData, git_commit: str):
    def factory(matrix_id: str, bps: int) -> RunManifest:
        return RunManifest(
            run_id=f"0050-2006-2026-fixed-methods-v1-{bps}bps",
            strategy_version=STRATEGY_VERSION,
            spec_sha256=SPEC_SHA256,
            git_commit=git_commit,
            universe_id="ETF-0050-EVERY-TRADING-DAY",
            data_snapshot_id="0050-research-package-2005-2026-v1",
            data_start=package.raw_bars[0].date,
            data_end=package.raw_bars[-1].date,
            data_hash=package.package_hash,
            execution_model=EXECUTION_MODEL,
            cost_model=COST_MODEL,
            matrix_id=matrix_id,
            slippage_bps=bps,
            strategy_parameters={"kd_exit": False, "single_position": True},
            metadata={"continuous_account_from": FORMAL_START.isoformat(), "research_package_00_accepted": True},
        )
    return factory


@contextmanager
def configured_period(start: date, package: PackageData, git_commit: str) -> Iterator[None]:
    original = {
        "fixed_start": fixed.FORMAL_START,
        "moving_start": moving.FORMAL_START,
        "fixed_builder": fixed.build_ma21_macd_run,
        "moving_builder": moving.build_ma21_macd_run,
        "moving_manifest": moving.manifest_factory,
    }
    fixed_builder = fixed.build_ma21_macd_run
    moving_builder = moving.build_ma21_macd_run

    def fixed_at_period(**kwargs: Any):
        kwargs.setdefault("start", start)
        kwargs.setdefault("end", CUTOFF)
        return fixed_builder(**kwargs)

    def moving_at_period(**kwargs: Any):
        kwargs.setdefault("start", start)
        kwargs.setdefault("end", CUTOFF)
        return moving_builder(**kwargs)

    fixed.FORMAL_START = start
    moving.FORMAL_START = start
    fixed.build_ma21_macd_run = fixed_at_period
    moving.build_ma21_macd_run = moving_at_period
    moving.manifest_factory = lambda _package, _commit: manifest_factory(package, git_commit)
    try:
        yield
    finally:
        fixed.FORMAL_START = original["fixed_start"]
        moving.FORMAL_START = original["moving_start"]
        fixed.build_ma21_macd_run = original["fixed_builder"]
        moving.build_ma21_macd_run = original["moving_builder"]
        moving.manifest_factory = original["moving_manifest"]


def run_methods(
    *, package: PackageData, raw_rows: Sequence[Mapping[str, Any]], events: Sequence[Any],
    git_commit: str, start: date,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    gates, extension, causal_audit = build_causal_maps(
        raw_rows=raw_rows, events=events, official_sessions=package.sessions,
    )
    with configured_period(start, package, git_commit):
        output, fixed_opportunities, fixed_audit = fixed.run_all_methods(
            package=package, raw_rows=raw_rows, events=events, gate_map=gates,
            extension_sma_map=extension, git_commit=git_commit,
        )
        indicators = moving.build_indicator_rows(raw_rows, events)
        _run, moving_opportunities, moving_audit = moving.build_ma21_macd_run(
            indicator_rows=indicators, raw_by_date=package.raw_by_date,
            start=start, end=CUTOFF, slippage_bps=MAIN_SCENARIO_BPS,
        )
    fixed_opportunities = fixed._finalize_opportunities(fixed_opportunities, output)
    moving_opportunities = moving._finalize_opportunities(moving_opportunities, output)
    opportunities = [
        {"strategy_id": moving.STRATEGY_ID, **row} for row in moving_opportunities
    ] + [{"strategy_id": fixed.STRATEGY_ID, **row} for row in fixed_opportunities]
    macd_audit = [
        {"strategy_id": moving.STRATEGY_ID, **row} for row in moving_audit
    ] + [{"strategy_id": fixed.STRATEGY_ID, **row} for row in fixed_audit]
    return output, opportunities, macd_audit, causal_audit


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def _normalized(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> list[list[str]]:
    return [[normalize_value(_jsonable(row.get(field))) for field in fields] for row in rows]


def compatibility_check(output: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    all_files = {
        "metrics": "all_scenarios_metrics.csv",
        "details": "all_scenarios_trade_details.csv",
        "transactions": "all_scenarios_transactions.csv",
        "curves": "all_scenarios_daily_equity.csv",
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
            accepted_path = ACCEPTED_RUN_ROOT / ("details" if scope == "all_scenarios" else "") / name
            fields, accepted = _read_csv(accepted_path)
            replay = list(output[key]) if scope == "all_scenarios" else [
                row for row in output[key]
                if int(row.get("slippage_bps", MAIN_SCENARIO_BPS)) == MAIN_SCENARIO_BPS
            ]
            exact = _normalized(accepted, fields) == _normalized(replay, fields)
            check_id = scope + "/" + name
            checks[check_id] = {"accepted_rows": len(accepted), "replay_rows": len(replay), "exact": exact}
            if not exact:
                raise ETF0050Error("2017 compatibility mismatch: " + check_id)
    return {
        "status": "PASS_EXACT", "start": COMPAT_START, "through": CUTOFF,
        "warmup_from": COMPAT_WARMUP_START, "initial_capital": CONTINUOUS_CAPITAL,
        "annual_reset": False, "checks": checks,
        "accepted_run_manifest_sha256": EXPECTED["accepted_run_manifest"],
        "accepted_report_artifact_manifest_sha256": EXPECTED["accepted_report_manifest"],
        "note": "This isolated replay starts a fresh TWD 500,000 account in 2017; it does not compare the 2017 slice of the 2006-start account.",
    }


def fetch_benchmark(raw_root: Path) -> list[dict[str, Any]]:
    raw_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    seen: set[date] = set()
    for year in YEARS:
        last_month = 9 if year == CUTOFF.year else 12
        for month in range(1, last_month + 1):
            pin = f"{year:04d}{month:02d}01"
            params = {"response": "json", "date": pin}
            url = "https://www.twse.com.tw/indicesReport/MFI94U?" + urllib.parse.urlencode(params)
            raw_path = raw_root / (pin[:6] + ".raw")
            receipt_path = raw_root / (pin[:6] + ".receipt.json")
            if raw_path.is_file() and receipt_path.is_file():
                body = raw_path.read_bytes()
                receipt = _load(receipt_path)
                if receipt.get("sha256") != sha256_file(raw_path) or receipt.get("http_status") != 200:
                    raise ETF0050Error("benchmark receipt mismatch: " + pin)
            else:
                adopted_raw = ACCEPTED_BENCHMARK_RAW_ROOT / (pin[:6] + ".json")
                if year >= 2017 and adopted_raw.is_file():
                    shutil.copy2(adopted_raw, raw_path)
                    body = raw_path.read_bytes()
                    _write_json(receipt_path, {
                        "url": "https://www.twse.com.tw/indicesReport/MFI94U", "params": params,
                        "full_url": url, "http_status": 200, "status": "REUSED_FROM_ACCEPTED_RUN",
                        "accepted_source_path": str(adopted_raw),
                        "accepted_source_sha256": sha256_file(adopted_raw),
                        "reused_at_utc": datetime.now(timezone.utc).isoformat(),
                        "bytes": len(body), "sha256": sha256_file(raw_path),
                    })
                else:
                    user_agents = (
                        "Mozilla/5.0",
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
                        "curl/8.10.1",
                        "stock_ai_bot-research/0050-benchmark-v1",
                    )
                    last_error: Exception | None = None
                    for attempt in range(len(user_agents)):
                        request = urllib.request.Request(
                            url,
                            headers={
                                "User-Agent": user_agents[(year * 12 + month + attempt) % len(user_agents)],
                                "Accept": "application/json",
                                "Referer": "https://www.twse.com.tw/",
                            },
                        )
                        try:
                            with urllib.request.urlopen(request, timeout=30) as response:
                                status = int(getattr(response, "status", 200))
                                body = response.read()
                            last_error = None
                            break
                        except urllib.error.HTTPError as exc:
                            failure_body = exc.read() or b""
                            failure = {
                                "pin": pin, "attempt": attempt + 1, "http_status": exc.code,
                                "url": url, "body_bytes": len(failure_body),
                                "body_sha256": hashlib.sha256(failure_body).hexdigest(),
                                "failed_at_utc": datetime.now(timezone.utc).isoformat(),
                            }
                            failure_dir = raw_root / "failures"
                            failure_dir.mkdir(parents=True, exist_ok=True)
                            _write_json(failure_dir / f"{pin}#{attempt + 1}.json", failure)
                            last_error = exc
                            time.sleep(0.25)
                    if last_error is not None:
                        raise ETF0050Error("benchmark fetch failed after official retries: " + pin) from last_error
                    if status != 200 or not body:
                        raise ETF0050Error("benchmark fetch failed: " + pin)
                    raw_path.write_bytes(body)
                    _write_json(receipt_path, {
                        "url": "https://www.twse.com.tw/indicesReport/MFI94U", "params": params,
                        "full_url": url, "http_status": status, "status": "FETCHED",
                        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                        "bytes": len(body), "sha256": sha256_file(raw_path),
                    })
                    time.sleep(0.05)
            payload = json.loads(body.decode("utf-8-sig"))
            if payload.get("stat") != "OK" or len(payload.get("fields") or []) != 2:
                raise ETF0050Error("benchmark schema changed: " + pin)
            for raw_day, raw_value in payload.get("data") or []:
                day = normalize_roc_day(raw_day.strip())
                if FORMAL_START <= day <= CUTOFF:
                    if day in seen:
                        raise ETF0050Error("duplicate benchmark date: " + str(day))
                    seen.add(day)
                    rows.append({
                        "date": day, "index_value": _d(str(raw_value).replace(",", "")),
                        "source_url": url, "raw_path": "benchmark-raw/" + raw_path.name,
                        "raw_sha256": sha256_file(raw_path),
                    })
    rows.sort(key=lambda row: row["date"])
    if not rows or rows[0]["date"] != FORMAL_START or rows[-1]["date"] != CUTOFF:
        raise ETF0050Error("benchmark coverage mismatch")
    base_value = rows[0]["index_value"]
    for row in rows:
        row["equivalent_500k"] = CONTINUOUS_CAPITAL * row["index_value"] / base_value
    return rows


def _report_build(source_root: Path, report_root: Path) -> dict[str, Any]:
    compatibility = _load(source_root / "details/compatibility-replay.json")
    original = {
        "formal": accepted_report_builder.FORMAL_START,
        "cutoff": accepted_report_builder.CUTOFF,
        "years": accepted_report_builder.YEARS,
        "version": accepted_report_builder.REPORT_VERSION,
        "parity": accepted_report_builder._baseline_report_parity,
        "render": accepted_report_builder._render,
    }
    original_render = accepted_report_builder._render

    def parity(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return compatibility

    limitation = (
        "\n\n## 2005～2009 資料來源限制\n\n"
        "2005～2009 每日未還原 OHLCV 來自 FinMind 公開端點；TWSE STOCK_DAY 不提供該期間日線，"
        "因此第一方驗證限於 FMSRFK 的 60 個月最高、最低、成交量與成交額。這不等於逐日第一方交叉驗證。"
        "本研究未使用付費 token，未用還原價成交，也未用未驗證資料補值。\n"
    )

    def render(**kwargs: Any):
        documents = list(original_render(**kwargs))
        for index, text in enumerate(documents):
            text = text.replace("十年比較", "2006～2026 比較")
            text = text.replace("0050 十年回測", "0050 2006～2026 回測")
            text = text.replace("MACD 停利2006～2026", "MACD 停利｜2006～2026")
            text = text.replace("## 四組方法總覽", "## 五組方法總覽")
            text = text.replace("本研究使用同一筆 500,000 元本金，從 2017-01-03", "本研究使用同一筆 500,000 元本金，從 2006-01-02")
            if index in {0, 1, 2, 3}:
                text += limitation
            documents[index] = text
        return tuple(documents)

    accepted_report_builder.FORMAL_START = FORMAL_START
    accepted_report_builder.CUTOFF = CUTOFF
    accepted_report_builder.YEARS = YEARS
    accepted_report_builder.REPORT_VERSION = REPORT_VERSION
    accepted_report_builder._baseline_report_parity = parity
    accepted_report_builder._render = render
    try:
        return accepted_report_builder.build(source_root=source_root, output_root=report_root)
    finally:
        accepted_report_builder.FORMAL_START = original["formal"]
        accepted_report_builder.CUTOFF = original["cutoff"]
        accepted_report_builder.YEARS = original["years"]
        accepted_report_builder.REPORT_VERSION = original["version"]
        accepted_report_builder._baseline_report_parity = original["parity"]
        accepted_report_builder._render = original["render"]


def _report_build_errata(source_root: Path, report_root: Path) -> dict[str, Any]:
    """Create the v1.1 report-only errata without touching run or v1 report."""

    if report_root.exists():
        raise FileExistsError("refusing to overwrite versioned v1.1 report")
    if file_sha256(source_root / "manifest.json") != EXPECTED["phase2_run_manifest"]:
        raise ETF0050Error("Phase-2 run manifest pin changed")
    if file_sha256(LEGACY_REPORT_ROOT / "artifact-manifest.json") != EXPECTED["legacy_report_manifest"]:
        raise ETF0050Error("v1 report manifest pin changed")
    run_readback = _load(source_root / "readback.json")
    if run_readback.get("status") != "PASS" or run_readback.get("mismatches"):
        raise ETF0050Error("Phase-2 run readback is not clean")
    protected_before = {
        "run": tree_hash(source_root),
        "legacy_report": tree_hash(LEGACY_REPORT_ROOT),
        "phase1_package": tree_hash(PACKAGE_ROOT),
    }
    with tempfile.TemporaryDirectory(prefix="0050-2006-2026-report-v1-1-") as temp_name:
        temp = Path(temp_name)
        base_report = temp / "base"
        stage = temp / "stage"
        _report_build(source_root, base_report)
        shutil.copytree(base_report, stage)

        copies = {
            "all-scenarios-summary.csv": "all_scenarios_metrics.csv",
            "all-scenarios-trade-details.csv": "all_scenarios_trade_details.csv",
            "all-scenarios-transactions.csv": "all_scenarios_transactions.csv",
            "all-scenarios-daily-equity.csv": "all_scenarios_daily_equity.csv",
            "all-scenarios-corporate-actions.csv": "all_scenarios_corporate_actions.csv",
        }
        for target, source in copies.items():
            shutil.copy2(source_root / "details" / source, stage / target)

        attachment_section = "\n".join(
            [
                "",
                "## 全情境敏感度附件",
                "",
                "主文與圖表固定使用較保守的 20 bps。以下附件同時列出 0／10／20 bps；0 bps 只是理想化的成本敏感度下界，不代表可實現績效。",
                "",
                "- [五法 × 0／10／20 bps 完整摘要](all-scenarios-summary.csv)",
                "- [全情境完整逐筆交易](all-scenarios-trade-details.csv)",
                "- [全情境全部成交事件](all-scenarios-transactions.csv)",
                "- [全情境每日資產](all-scenarios-daily-equity.csv)",
                "- [全情境公司行動](all-scenarios-corporate-actions.csv)",
                "- [移動與固定防線兩策略的完整 MACD／停損事件](macd-event-audit.csv)",
                "",
            ]
        )
        for name in ("README.md", "report.md"):
            path = stage / name
            path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n" + attachment_section, encoding="utf-8", newline="\n")

        summaries = standard.read_csv(stage / "scenario-summary.csv")
        timing = [row for row in summaries if row["method_kind"] == "擇時策略"]
        best_timing = max(timing, key=lambda row: standard.dec(row["cagr"]))
        shallowest = min(summaries, key=lambda row: abs(standard.dec(row["max_asset_drawdown"])))
        highest = max(summaries, key=lambda row: standard.dec(row["cagr"]))
        deepest = max(summaries, key=lambda row: abs(standard.dec(row["max_asset_drawdown"])))
        risk_table = standard.md_table(
            ("方法", "年化報酬", "全期最大回落", "報酬／回落", "平均資金閒置"),
            (
                (
                    row["strategy_label"], standard.pct(row["cagr"]),
                    standard.pct(row["max_asset_drawdown"]),
                    f"{standard.dec(row['return_to_drawdown']):.2f}",
                    standard.pct(row["idle_cash_ratio"]),
                )
                for row in summaries
            ),
        )
        risk_appendix = "\n".join(
            [
                "",
                "## 五法全期風險與資金使用對照",
                "",
                "下表均為主文 20 bps 情境；年化報酬越高不代表風險越低，必須同時查看最大回落、報酬相對回落與平均資金閒置。",
                "",
                risk_table,
                "",
                f"- 三個擇時方法中，年化報酬最高的是「{best_timing['strategy_label']}」；它並不是全體方法中回落最小者。",
                f"- 全體回落較淺的是「{shallowest['strategy_label']}」；較高的現金閒置是降低曝險與報酬的共同代價。",
                f"- 全體年化報酬最高的是「{highest['strategy_label']}」，同時全期回落最深的是「{deepest['strategy_label']}」。這說明長期持有的高報酬伴隨較大的帳面波動。",
                "- 2022、2025 與 2026 的逐年比較列在上一節；2026 僅統計至 9 月 30 日。",
                "",
            ]
        )
        risk_path = stage / "benchmark-risk-report.md"
        risk_path.write_text(risk_path.read_text(encoding="utf-8").rstrip() + "\n" + risk_appendix, encoding="utf-8", newline="\n")

        input_manifest = _load(stage / "input-manifest.json")
        input_manifest.update(
            {
                "report_version": ERRATA_REPORT_VERSION,
                "errata_of": str(LEGACY_REPORT_ROOT),
                "errata_of_artifact_manifest_sha256": EXPECTED["legacy_report_manifest"],
                "report_only": True,
                "performance_recomputed": False,
                "main_scenario_bps": 20,
                "sensitivity_scenarios_bps": [0, 10, 20],
                "zero_bps_policy": "IDEALIZED_SENSITIVITY_ONLY_NOT_REALIZABLE_PERFORMANCE_CLAIM",
                "added_attachments": sorted(copies),
            }
        )
        standard.write_json(stage / "input-manifest.json", input_manifest)

        broken_links = []
        for markdown in stage.glob("*.md"):
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", markdown.read_text(encoding="utf-8")):
                if not (stage / target).is_file():
                    broken_links.append(markdown.name + " -> " + target)
        if broken_links:
            raise ETF0050Error(f"v1.1 report broken links: {broken_links}")
        protected_after = {
            "run": tree_hash(source_root),
            "legacy_report": tree_hash(LEGACY_REPORT_ROOT),
            "phase1_package": tree_hash(PACKAGE_ROOT),
        }
        if protected_before != protected_after:
            raise ETF0050Error("report-only errata changed protected artifacts")

        excluded = {"artifact-manifest.json", "readback.json", "checkpoint.json"}
        artifacts = standard.artifact_rows(stage, excluded)
        standard.write_json(
            stage / "artifact-manifest.json",
            {
                "status": "STOP_WAIT_00_QA",
                "report_version": ERRATA_REPORT_VERSION,
                "artifact_count": len(artifacts),
                "annual_rows": len(standard.read_csv(stage / "calendar-year-summary.csv")),
                "daily_rows": len(standard.read_csv(stage / "daily-equity.csv")),
                "trade_rows": len(standard.read_csv(stage / "trade-details.csv")),
                "all_scenario_rows": len(standard.read_csv(stage / "all-scenarios-summary.csv")),
                "protected_trees_unchanged": True,
                "artifacts": artifacts,
            },
        )
        mismatches = [
            item["path"] for item in artifacts
            if standard.sha256(stage / item["path"]) != item["sha256"]
            or (stage / item["path"]).stat().st_size != int(item["bytes"])
        ]
        standard.write_json(stage / "readback.json", {"status": "PASS" if not mismatches else "FAIL", "mismatches": mismatches})
        if mismatches:
            raise ETF0050Error(f"v1.1 report artifact readback failed: {mismatches}")
        standard.write_json(
            stage / "checkpoint.json",
            {
                "status": "STOP_WAIT_00_QA",
                "artifact_manifest_sha256": standard.sha256(stage / "artifact-manifest.json").upper(),
                "readback_sha256": standard.sha256(stage / "readback.json").upper(),
                "safe_next_step": "00 independent report-only errata QA",
            },
        )
        report_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, report_root)
    return {
        "status": "STOP_WAIT_00_QA",
        "output_root": str(report_root),
        "artifact_manifest_sha256": standard.sha256(report_root / "artifact-manifest.json").upper(),
        "readback_sha256": standard.sha256(report_root / "readback.json").upper(),
        "all_scenario_rows": 15,
        "legacy_report_unchanged": True,
    }


def run_study(
    *, package_root: Path = PACKAGE_ROOT, output_root: Path = OUTPUT_ROOT_DEFAULT,
    report_root: Path = REPORT_ROOT_DEFAULT,
) -> Mapping[str, Any]:
    if output_root.exists() or report_root.exists():
        raise FileExistsError("refusing to overwrite versioned Phase-2 output")
    validate_inputs(package_root)
    repo = Path(__file__).resolve().parents[2]
    git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True, encoding="utf-8").strip()
    protected_before = {str(path): tree_hash(path) for path in PROTECTED_PATHS}

    package, raw_rows, events = load_package(package_root)
    output, opportunities, macd_audit, causal_audit = run_methods(
        package=package, raw_rows=raw_rows, events=events,
        git_commit=git_commit, start=FORMAL_START,
    )
    compat_package, compat_rows, compat_events = load_package(package_root, slice_from=COMPAT_WARMUP_START)
    compat_output, _compat_opportunities, _compat_macd, _compat_causal = run_methods(
        package=compat_package, raw_rows=compat_rows, events=compat_events,
        git_commit=git_commit, start=COMPAT_START,
    )
    compatibility = compatibility_check(compat_output)

    if len(output["metrics"]) != 15:
        raise ETF0050Error("five methods x three scenarios expected")
    for row in output["metrics"]:
        validate_cash_safe_metric_semantics(row)
        if _d(row["minimum_cash"]) < 0:
            raise ETF0050Error("negative cash survived replay")
    if {int(row["slippage_bps"]) for row in output["metrics"]} != {0, 10, 20}:
        raise ETF0050Error("slippage scenario mismatch")
    main = {
        key: [row for row in rows if int(row.get("slippage_bps", MAIN_SCENARIO_BPS)) == MAIN_SCENARIO_BPS]
        for key, rows in output.items()
    }
    if tuple(row["strategy_id"] for row in main["metrics"]) != METHOD_ORDER:
        raise ETF0050Error("method order mismatch")
    if any(row["strategy_id"] == fixed.STRATEGY_ID and str(row.get("event", "")).startswith("DOW_") for row in macd_audit):
        raise ETF0050Error("fixed-stop strategy inherited a moving Dow defense")

    protected_after_replay = {str(path): tree_hash(path) for path in PROTECTED_PATHS}
    if protected_before != protected_after_replay:
        raise ETF0050Error("protected old artifact changed during replay")

    benchmark_cache = output_root.parent / ("." + output_root.name + "-benchmark-bootstrap")
    benchmark = fetch_benchmark(benchmark_cache)
    with tempfile.TemporaryDirectory(prefix="0050-2006-2026-fixed-methods-v1-") as temp_name:
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
            ("scenario-summary.csv", main["metrics"]),
            ("trade-details.csv", main["details"]),
            ("transactions.csv", main["transactions"]),
            ("daily-equity.csv", main["curves"]),
            ("corporate-actions.csv", main["actions"]),
        ):
            _write_csv(stage / name, rows)
        _write_csv(stage / "signal-opportunity-audit.csv", opportunities)
        _write_csv(stage / "macd-event-audit.csv", macd_audit)
        _write_csv(stage / "fixed-initial-stop-macd-event-audit.csv", [row for row in macd_audit if row["strategy_id"] == fixed.STRATEGY_ID])
        _write_csv(details / "causal-technical-audit.csv", causal_audit)
        _write_json(details / "compatibility-replay.json", compatibility)
        shutil.copytree(benchmark_cache, stage / "benchmark-raw")
        _write_csv(stage / "benchmark.csv", benchmark)

        protected_after = {str(path): tree_hash(path) for path in PROTECTED_PATHS}
        qa = {
            "status": "PASS", "final_checkpoint": "STOP_WAIT_00_QA",
            "method_order": METHOD_ORDER, "scenario_method_rows": len(output["metrics"]),
            "main_method_rows": len(main["metrics"]), "initial_capital": CONTINUOUS_CAPITAL,
            "formal_period": {"from": FORMAL_START, "through": CUTOFF},
            "warmup_2005_excluded": True, "annual_reset": False,
            "minimum_cash_nonnegative": True, "compatibility": compatibility,
            "fixed_stop_has_no_dow_events": True,
            "split_transaction_count": sum(
                row.get("date") == package.split.effective_date and row.get("role") in {"SPLIT", "CORPORATE_ACTION"}
                for row in output["transactions"]
            ),
            "protected_tree_hashes_before": protected_before,
            "protected_tree_hashes_after": protected_after,
            "protected_trees_unchanged": protected_before == protected_after,
            "source_limitation_2005_2009": "FinMind public daily raw; TWSE first-party validation is monthly high/low/volume/amount, not daily",
            "paid_finmind_token_used": False,
            "rules_changed_after_results": False,
            "performance_used_to_tune_parameters": False,
        }
        if qa["split_transaction_count"] or not qa["protected_trees_unchanged"]:
            raise ETF0050Error("split/protected artifact invariant failed")
        _write_json(details / "qa-recompute.json", qa)
        _write_json(stage / "input-manifest.json", {
            "status": "PASS", "runner_version": RUNNER_VERSION, "git_commit": git_commit,
            "contract": str(CONTRACT), "contract_sha256": EXPECTED["contract"],
            "report_template": str(REPORT_TEMPLATE), "report_template_sha256": EXPECTED["template"],
            "package_root": str(package_root), "package_manifest_sha256": EXPECTED["package_manifest"],
            "package_gate_sha256": EXPECTED["package_gate"], "package_readback_sha256": EXPECTED["package_readback"],
            "phase1_00_acceptance": {"status": "ACCEPT", "related_tests_passed": 91, "artifact_mismatches": 0},
            "period": {"warmup_from": package.sessions[0], "formal_from": FORMAL_START, "through": CUTOFF},
            "initial_capital": CONTINUOUS_CAPITAL, "annual_reset": False,
            "methods": METHOD_ORDER, "slippage_scenarios_bps": SLIPPAGE_SCENARIOS_BPS,
            "main_slippage_bps": MAIN_SCENARIO_BPS,
            "compatibility_replay": "details/compatibility-replay.json",
            "source_limitation_2005_2009": qa["source_limitation_2005_2009"],
        })
        excluded = {"manifest.json", "readback.json", "checkpoint.json", "QA_READY.json"}
        artifacts = _artifact_rows(stage, excluded)
        _write_json(stage / "manifest.json", {
            "schema_version": RUNNER_VERSION + "-manifest", "status": "STOP_WAIT_00_QA",
            "git_commit": git_commit, "period": {"start": FORMAL_START, "through": CUTOFF},
            "initial_capital": CONTINUOUS_CAPITAL, "annual_reset": False,
            "methods": METHOD_ORDER, "slippage_scenarios_bps": SLIPPAGE_SCENARIOS_BPS,
            "main_slippage_bps": MAIN_SCENARIO_BPS, "artifacts": artifacts,
        })
        mismatches = _verify_artifacts(stage, artifacts)
        _write_json(stage / "readback.json", {"status": "PASS" if not mismatches else "FAIL", "mismatches": mismatches})
        if mismatches:
            raise ETF0050Error("run artifact readback failed")
        _write_json(stage / "QA_READY.json", {
            "status": "STOP_WAIT_00_QA", "compatibility_status": compatibility["status"],
            "ready_for_claimed_acceptance": False,
        })
        _write_json(stage / "checkpoint.json", {
            "status": "STOP_WAIT_00_QA", "manifest_sha256": sha256_file(stage / "manifest.json").upper(),
            "readback_sha256": sha256_file(stage / "readback.json").upper(),
            "safe_next_step": "00 independent Phase-2 QA only",
            "exact_rerun_command": "python -m backtests.dual_ma.etf_0050_2006_fixed_methods_v1 all",
            "exact_report_resume_command": "python -m backtests.dual_ma.etf_0050_2006_fixed_methods_v1 report-only",
        })
        output_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(stage, output_root)

    report_result = _report_build_errata(output_root, report_root)
    if benchmark_cache.name != "." + output_root.name + "-benchmark-bootstrap":
        raise ETF0050Error("unsafe benchmark bootstrap cleanup target")
    shutil.rmtree(benchmark_cache)
    return {
        "status": "STOP_WAIT_00_QA", "run_root": str(output_root), "report_root": str(report_root),
        "run_manifest_sha256": sha256_file(output_root / "manifest.json").upper(),
        "run_readback_sha256": sha256_file(output_root / "readback.json").upper(),
        "report_artifact_manifest_sha256": report_result["artifact_manifest_sha256"],
        "report_readback_sha256": report_result["readback_sha256"],
        "compatibility_status": compatibility["status"],
    }


def validate_only(
    output_root: Path = OUTPUT_ROOT_DEFAULT, report_root: Path = REPORT_ROOT_DEFAULT,
) -> dict[str, Any]:
    results: dict[str, Any] = {"network_used": False, "writes_performed": False}
    for label, root, manifest_name in (
        ("run", output_root, "manifest.json"),
        ("report", report_root, "artifact-manifest.json"),
    ):
        manifest = _load(root / manifest_name)
        mismatches = []
        for item in manifest.get("artifacts") or []:
            relative = item.get("path") or item.get("relative_path")
            path = root / relative
            if not path.is_file() or sha256_file(path) != item["sha256"] or path.stat().st_size != int(item["bytes"]):
                mismatches.append(relative)
        results[label] = {"artifact_count": len(manifest.get("artifacts") or []), "mismatches": mismatches}
    broken_links = []
    for markdown in report_root.glob("*.md"):
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", markdown.read_text(encoding="utf-8")):
            if not (report_root / target).is_file():
                broken_links.append(markdown.name + " -> " + target)
    results["broken_links"] = broken_links
    results["status"] = "PASS" if not results["run"]["mismatches"] and not results["report"]["mismatches"] and not broken_links else "FAIL"
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("all", "report-only", "validate-only"), default="all")
    parser.add_argument("--package-root", type=Path, default=PACKAGE_ROOT)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT_DEFAULT)
    parser.add_argument("--report-root", type=Path, default=REPORT_ROOT_DEFAULT)
    args = parser.parse_args(argv)
    if args.mode == "all":
        result = run_study(package_root=args.package_root, output_root=args.output_root, report_root=args.report_root)
    elif args.mode == "report-only":
        if args.report_root.exists():
            raise FileExistsError("refusing to overwrite report")
        result = _report_build_errata(args.output_root, args.report_root)
        if isinstance(result.get("output_root"), Path):
            result = {**result, "output_root": str(result["output_root"])}
    else:
        result = validate_only(args.output_root, args.report_root)
    print(json.dumps(_jsonable(result), ensure_ascii=True, sort_keys=True))
    return 0 if result.get("status") in {"PASS", "STOP_WAIT_00_QA"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
