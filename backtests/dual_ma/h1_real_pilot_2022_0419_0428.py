"""H1 real-data pilot wiring: 2022-04-19..04-28 eight-program selection events.

This module is the 0 -> 1 wiring step that connects the 2022 H1 eight-program
selection sample produced by the 01 selection worktree to the already frozen
life-cycle, engine and cost programs.  It answers one question: for the real
D selection events of 2022-04-19 .. 2022-04-28, what does the frozen
v0.4.1-frozen machinery produce, and which named evidence is still missing
before a six-matrix cost result exists.

Frozen programs this module reuses without modification:

* life cycle: approx_l10_episode_lifecycle_smoke.regime_values and
  .episode_windows implement the D -> E monitoring episode ruling of
  docs/strategies/dual_ma/2022-h1-selection-episode-lifecycle-v1.md;
* engine: engine.DualMABacktestEngine with the frozen RunManifest contract;
  signal and fill both use the signal-day close (EOD_CLOSE_PROXY_V1);
* cost: approx_h1_event_cost_adapter.adapt_events (the fail-closed adapter)
  and approx_h1_cost_report_core.build_report (the six-matrix ledger).

Two of the four known adapter wiring blockers are closed here with a thin,
inspectable wiring step:

3. economic_key not materialised in LEG raw -- the canonical four-column
   economic key is materialised into a derived leg copy through the frozen B
   sidecar (h1_economic_signal_sidecar.build_sidecar), never inside the
   source LEG rows and never derived from an engine field.
4. run-level official session calendar -- the frozen official TWSE/TPEx
   calendar is bound at run level and turned into the adapter's MarketEvidence
   (official sessions, per-symbol bar dates, official closes).

The other two blockers are left exactly as C3 / C4 found them and are never
worked around:

1. the price basis of the frozen Fugle vendor snapshot is
   UNADJUSTED_VENDOR_CLOSE_PROXY while the adapter's PRICE_BASIS_ALLOWED only
   accepts UNADJUSTED_OFFICIAL_CLOSE_PROXY; the frozen label is passed through
   verbatim, so every leg holds HOLD_MISSING_PRICE_BASIS_EVIDENCE;
2. no dated per-symbol InstrumentIdentity source exists, so no leg row carries
   instrument_type and the next unresolved blocker is
   HOLD_MISSING_INSTRUMENT_TYPE.  02 never infers it from market, industry,
   code shape or an engine default.

Because of (1) and (2) this run publishes no cost result: the six-matrix x
slippage grid is reported as an all-HOLD ledger with named counts.  The
published trade table is deliberately restricted to instrument-independent
fields (dates, reference closes, fill prices, share counts); every engine money
column that depends on the engine's instrument parameter is recorded as an
adapter input and withheld from the trade table.

2022 H1 is the development sample: 2023 / 2024 data is never read, no formal
performance label is produced, and no rule, sample or selection program is
modified.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .approx_h1_cost_report_core import LegInput, build_report, canonical_bytes
from .approx_h1_event_cost_adapter import (
    HOLD_MISSING_INSTRUMENT_TYPE,
    HOLD_MISSING_PRICE_BASIS,
    MarketEvidence,
    PRICE_BASIS_ALLOWED,
    adapt_events,
)
from .approx_l10_episode_lifecycle_smoke import (
    EpisodeWindow,
    episode_windows,
    regime_values,
)
from .contracts import (
    COST_MODEL,
    EXECUTION_MODEL,
    SPEC_SHA256,
    STRATEGY_VERSION,
    RunManifest,
)
from .engine import MATRIX_GROUPS, DualMABacktestEngine
from .h1_economic_signal_sidecar import (
    build_sidecar,
    classification_counts_json,
    economic_signals_jsonl,
    holds_json,
    load_lifecycle_registry,
    read_engine_events,
)
from .models import AddVariant, Bar, ExitVariant, InstrumentType, StrategyKind


MODULE_VERSION = "dual-ma-h1-real-pilot-2022-0419-0428-v1"
RUN_ID_DEFAULT = "h1-real-pilot-2022-0419-0428-v1"
MANIFEST_SCHEMA_VERSION = "dual-ma-h1-real-pilot-manifest-v1"
READBACK_SCHEMA_VERSION = "dual-ma-h1-real-pilot-readback-v1"
EPISODE_REGISTRY_SCHEMA_VERSION = "dual-ma-h1-real-pilot-episode-windows-v1"
TRADE_TABLE_SCHEMA_VERSION = "dual-ma-h1-real-pilot-trade-rows-v1"
EXCEPTION_TABLE_SCHEMA_VERSION = "dual-ma-h1-real-pilot-exception-rows-v1"
COST_GRID_SCHEMA_VERSION = "dual-ma-h1-real-pilot-cost-grid-v1"

# The eight official sessions the 01 worktree actually produced selections for.
SELECTION_DATES: tuple[str, ...] = (
    "2022-04-19",
    "2022-04-20",
    "2022-04-21",
    "2022-04-22",
    "2022-04-25",
    "2022-04-26",
    "2022-04-27",
    "2022-04-28",
)

PILOT_RUN_BY_DATE: dict[str, str] = {
    "2022-04-19": "run-2026-10-03-d2022-04-19-v2-isolated",
    "2022-04-20": "run-2026-10-03-d2022-04-20-21-v1-isolated",
    "2022-04-21": "run-2026-10-03-d2022-04-20-21-v1-isolated",
    "2022-04-22": "run-2026-10-03-d2022-04-22-28-v1-isolated",
    "2022-04-25": "run-2026-10-03-d2022-04-22-28-v1-isolated",
    "2022-04-26": "run-2026-10-03-d2022-04-22-28-v1-isolated",
    "2022-04-27": "run-2026-10-03-d2022-04-22-28-v1-isolated",
    "2022-04-28": "run-2026-10-03-d2022-04-22-28-v1-isolated",
}

PILOT_ROOT_RELATIVE = "data/dual_ma/current_bot_all_eight_pilot"
PILOT_REPORT_NAME = "pilot_report.json"
PROGRAM_ORDER: tuple[str, ...] = (
    "financial",
    "chip_1",
    "chip_2",
    "chip_3",
    "chip_4",
    "technical",
    "curated",
    "laoxiao",
)

CALENDAR_RELATIVE_PATH = (
    "data/dual_ma/v2/official_trading_calendar/2021-2022/"
    "dual-ma-official-trading-calendar#a468084f0a95f961.json"
)
CALENDAR_RAW_SHA256 = "7994c6ea97506622744a76b1b59003e74221da40516fee47f90e9987fbc4f8e8"
CALENDAR_CONTENT_SHA256 = (
    "a468084f0a95f961147fa829b16d3d64fd67ad4a06cd151e1c6599dd79d833f6"
)
CALENDAR_OPEN_UNION_COUNT = 490

PRICE_SNAPSHOT_RELATIVE_PATH = (
    "02-causal-backtest/2021-2022-fugle-v2-universe-extension-v1"
)
PRICE_SNAPSHOT_MANIFEST_NAME = "snapshot_manifest.json"
PRICE_SNAPSHOT_CONTENT_SHA256 = (
    "58e8701de0124704efcf92e742b10c6e18391c6a196829ac95f07e3f57e0f654"
)

C1_BINDING_RELATIVE_PATH = (
    "02-causal-backtest/h1-price-binding-manifest-v4/price_binding_manifest.json"
)
C1_BINDING_FILE_SHA256 = (
    "018ee51585528d0121f06b930f9127242f912083cc4d52d69cae2150702a13c4"
)
C1_DECLARED_PRICE_BASIS = "UNADJUSTED_VENDOR_CLOSE_PROXY"

SLIPPAGE_SCENARIOS: tuple[int, ...] = (0, 10, 20)
MATRIX_IDS: tuple[str, ...] = ("M1", "M2", "M3", "M4", "M5", "M6")
STRATEGY_ORDER: tuple[str, ...] = (
    StrategyKind.MEDIUM.value,
    StrategyKind.LONG.value,
)

# The frozen engine requires an InstrumentType argument.  It is passed the
# engine's own existing default so the engine can size positions; it is never
# written into any adapter evidence row and never reported as an instrument
# claim.
ENGINE_INSTRUMENT_PARAMETER = InstrumentType.STOCK
ENGINE_INSTRUMENT_IS_NOT_AN_INSTRUMENT_CLAIM = True

# Thin-wiring closure of the two C3/C4 blockers that need no adjudication.
RUN_LEVEL_BLOCKERS_RESOLVED_BY_THIS_BATCH: tuple[str, ...] = (
    "HOLD_ECONOMIC_KEY_NOT_IN_LEG_RAW_FIELD",
    "HOLD_OFFICIAL_SESSION_CALENDAR_UNAVAILABLE",
)
# The two blockers that still need 00 adjudication; never worked around here.
RUN_LEVEL_BLOCKERS_STILL_OPEN: tuple[str, ...] = (
    "HOLD_INSTRUMENT_TYPE",
    "HOLD_PRICE_BASIS_NOT_ADAPTER_ACCEPTED",
)

# Fields the frozen engine marks as excluded; carried forward as markers only.
LEG_PERFORMANCE_FIELDS_EXCLUDED: tuple[str, ...] = ("net_pnl",)
TRANSACTION_PERFORMANCE_FIELDS_EXCLUDED: tuple[str, ...] = ("net_pnl", "gross_pnl")
TRANSACTION_UNAVAILABLE_ENGINE_FIELDS: tuple[str, ...] = ("price", "gross_amount")

# The published trade table carries only instrument-independent columns: a
# STOCK / ETF / BENEFICIARY sweep over the spot-check windows reproduces the same
# dates, reference closes, fill prices and share counts for every leg.
TRADE_TABLE_FIELDS: tuple[str, ...] = (
    "code",
    "symbol",
    "strategy",
    "matrix_id",
    "slippage_bps",
    "leg_id",
    "leg_number",
    "episode_ordinal",
    "source_selection_date",
    "monitor_effective_from",
    "regime_window_start",
    "regime_window_end",
    "entry_date",
    "entry_reference_close",
    "entry_fill_price",
    "shares",
    "initial_stop",
    "exit_date",
    "exit_reason",
    "exit_reference_close",
    "exit_fill_price",
)

# Fields the frozen adapter needs on a LEG row; they include the engine money
# columns the adapter contract requires, which are never used as a result here.
ADAPTER_LEG_FIELDS: tuple[str, ...] = (
    "leg_id",
    "symbol",
    "strategy",
    "leg_number",
    "entry_date",
    "entry_reference_close",
    "entry_fill_price",
    "shares",
    "initial_stop",
    "initial_r",
    "buy_total",
    "exit_date",
    "exit_reason",
    "exit_reference_close",
    "exit_fill_price",
    "sell_net",
    "episode_id",
)

ADAPTER_TRANSACTION_FIELDS: tuple[str, ...] = (
    "date",
    "role",
    "leg_id",
    "symbol",
    "strategy",
    "shares",
    "cash_amount",
)


class RealPilotError(RuntimeError):
    """A structural problem with a declared frozen input."""


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def _iso_or_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _jsonable(value: Any) -> Any:
    """Local mirror of the frozen cost core's canonical JSON projection."""

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _json_bytes(value: Any) -> bytes:
    return canonical_bytes(value) + b"\n"


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    chunks = [
        json.dumps(
            _jsonable(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        for row in rows
    ]
    return (("\n".join(chunks) + "\n") if chunks else "").encode("utf-8")


@dataclass(frozen=True)
class FrozenExpectations:
    """Hashes the caller expects to find; None disables one check.

    The defaults are the real frozen artifacts.  Synthetic fixtures pass their
    own values so the same code path stays testable.
    """

    calendar_raw_sha256: str | None = CALENDAR_RAW_SHA256
    calendar_content_sha256: str | None = CALENDAR_CONTENT_SHA256
    calendar_open_union_count: int | None = CALENDAR_OPEN_UNION_COUNT
    price_snapshot_content_sha256: str | None = PRICE_SNAPSHOT_CONTENT_SHA256
    c1_binding_file_sha256: str | None = C1_BINDING_FILE_SHA256
    c1_price_basis: str | None = C1_DECLARED_PRICE_BASIS


FROZEN_EXPECTATIONS = FrozenExpectations()
ANY_EXPECTATIONS = FrozenExpectations(
    calendar_raw_sha256=None,
    calendar_content_sha256=None,
    calendar_open_union_count=None,
    price_snapshot_content_sha256=None,
    c1_binding_file_sha256=None,
    c1_price_basis=None,
)


# ---------------------------------------------------------------------------
# Frozen input readers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OfficialCalendar:
    path: Path
    raw_sha256: str
    content_sha256: str
    coverage_start: str
    coverage_end: str
    sessions: tuple[date, ...]

    def next_session(self, day: date) -> date | None:
        later = [item for item in self.sessions if item > day]
        return later[0] if later else None

    def sessions_between(self, start: date, end: date) -> tuple[date, ...]:
        return tuple(item for item in self.sessions if start <= item <= end)

    def as_binding(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "raw_sha256": self.raw_sha256,
            "content_sha256": self.content_sha256,
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "session_count": len(self.sessions),
            "first_session": self.sessions[0].isoformat() if self.sessions else None,
            "last_session": self.sessions[-1].isoformat() if self.sessions else None,
            "next_session_rule": (
                "first official session strictly after D over the frozen union "
                "calendar; no weekday rule and no ^TWII fallback"
            ),
        }


def load_official_calendar(
    path: str | Path, *, expectations: FrozenExpectations = FROZEN_EXPECTATIONS
) -> OfficialCalendar:
    path = Path(path)
    if not path.is_file():
        raise RealPilotError(f"official trading calendar is missing: {path}")
    payload = _read_json(path)
    if payload.get("schema_version") != "dual-ma-official-trading-calendar-v1":
        raise RealPilotError(
            f"unexpected calendar schema: {payload.get('schema_version')!r}"
        )
    open_dates = payload.get("open_dates_union")
    if not isinstance(open_dates, list) or not open_dates:
        raise RealPilotError("official trading calendar has no open_dates_union")
    raw_sha = sha256_file(path)
    if expectations.calendar_raw_sha256 and raw_sha != expectations.calendar_raw_sha256:
        raise RealPilotError(
            f"calendar raw sha256 {raw_sha} != expected {expectations.calendar_raw_sha256}"
        )
    content_sha = str(payload.get("content_sha256") or "")
    if (
        expectations.calendar_content_sha256
        and content_sha != expectations.calendar_content_sha256
    ):
        raise RealPilotError(
            f"calendar content sha256 {content_sha} != expected "
            f"{expectations.calendar_content_sha256}"
        )
    if (
        expectations.calendar_open_union_count is not None
        and len(open_dates) != expectations.calendar_open_union_count
    ):
        raise RealPilotError(
            f"calendar open_dates_union count {len(open_dates)} != expected "
            f"{expectations.calendar_open_union_count}"
        )
    sessions = tuple(sorted(date.fromisoformat(str(item)) for item in open_dates))
    if len(set(sessions)) != len(sessions):
        raise RealPilotError("calendar open_dates_union contains duplicate sessions")
    return OfficialCalendar(
        path=path,
        raw_sha256=raw_sha,
        content_sha256=content_sha,
        coverage_start=str(payload.get("coverage_start")),
        coverage_end=str(payload.get("coverage_end")),
        sessions=sessions,
    )


@dataclass(frozen=True)
class SelectionDateReport:
    report_date: str
    run_dir: str
    report_path: Path
    report_sha256: str
    manifest_path: Path
    manifest_file_sha256: str
    manifest_content_sha256: str
    program_status: Mapping[str, str]
    program_candidate_count: Mapping[str, int]
    available_groups: tuple[str, ...]
    missing_groups: tuple[str, ...]
    candidate_count: int
    code_programs: Mapping[str, tuple[str, ...]]
    universe_snapshot_id: str
    universe_snapshot_content_sha256: str
    universe_snapshot_file_sha256: str

    def as_coverage(self) -> dict[str, Any]:
        return {
            "report_date": self.report_date,
            "pilot_run_dir": self.run_dir,
            "pilot_report_sha256": self.report_sha256,
            "selection_manifest_file_sha256": self.manifest_file_sha256,
            "selection_manifest_content_sha256": self.manifest_content_sha256,
            "program_status": dict(self.program_status),
            "program_candidate_count": dict(self.program_candidate_count),
            "available_groups": list(self.available_groups),
            "missing_groups": list(self.missing_groups),
            "deduplicated_candidate_count": self.candidate_count,
            "universe_snapshot_id": self.universe_snapshot_id,
            "universe_snapshot_content_sha256": self.universe_snapshot_content_sha256,
        }


def _date_reports(run_dir: Path) -> list[Mapping[str, Any]]:
    payload = _read_json(run_dir / PILOT_REPORT_NAME)
    if isinstance(payload.get("dates"), list):
        return [item for item in payload["dates"] if isinstance(item, Mapping)]
    return [payload]


def _selection_manifest_path(run_dir: Path, report_date: str) -> Path:
    directory = run_dir / "selection_manifests" / report_date[:4] / report_date
    candidates = sorted(directory.glob("dual-ma-selection@*.json"))
    if not candidates:
        raise RealPilotError(f"no selection manifest for {report_date} under {directory}")
    if len(candidates) > 1:
        raise RealPilotError(f"ambiguous selection manifest for {report_date}: {candidates}")
    return candidates[0]


def _universe_snapshot_path(run_dir: Path, report_date: str) -> Path:
    directory = run_dir / "universe_snapshots" / report_date[:4] / report_date
    candidates = sorted(directory.glob("dual-ma-universe@*.json"))
    if not candidates:
        raise RealPilotError(f"no universe snapshot for {report_date} under {directory}")
    if len(candidates) > 1:
        raise RealPilotError(f"ambiguous universe snapshot for {report_date}: {candidates}")
    return candidates[0]


def load_pilot_selection_dates(
    pilot_root: str | Path,
    *,
    selection_dates: Sequence[str] = SELECTION_DATES,
    run_by_date: Mapping[str, str] | None = None,
) -> list[SelectionDateReport]:
    """Read the 01 eight-program daily selection products, read-only.

    ``run_by_date`` is an optional date -> pilot run directory override.
    When it is None the module's frozen eight-session map is used, so every
    existing caller (and therefore the accepted pilot run) is unchanged.  A
    caller may pass a wider map to read more selection dates out of more than
    one accepted 01 run without touching this module's defaults.
    """

    pilot_root = Path(pilot_root)
    run_map = PILOT_RUN_BY_DATE if run_by_date is None else run_by_date
    reports: list[SelectionDateReport] = []
    for report_date in selection_dates:
        try:
            run_name = run_map[report_date]
        except KeyError as error:
            raise RealPilotError(
                f"{report_date}: no pilot run is bound for this selection date"
            ) from error
        run_dir = pilot_root / run_name
        if not run_dir.is_dir():
            raise RealPilotError(f"pilot run directory is missing: {run_dir}")
        report_path = run_dir / PILOT_REPORT_NAME
        report_payload = None
        for item in _date_reports(run_dir):
            if str(item.get("report_date")) == report_date:
                report_payload = item
                break
        if report_payload is None:
            raise RealPilotError(f"{report_path} has no date block for {report_date}")
        group_rows = report_payload.get("groups") or []
        if not isinstance(group_rows, list) or not group_rows:
            raise RealPilotError(f"{report_date}: report has no groups block")
        status_by_key = {
            str(group.get("key")): str(group.get("status"))
            for group in group_rows
            if isinstance(group, Mapping)
        }
        count_by_key = {
            str(group.get("key")): int(group.get("candidate_count") or 0)
            for group in group_rows
            if isinstance(group, Mapping)
        }
        missing_keys = sorted(set(PROGRAM_ORDER) - set(status_by_key))
        if missing_keys:
            raise RealPilotError(
                f"{report_date}: groups block is missing {missing_keys}"
            )
        program_status = {key: status_by_key[key] for key in PROGRAM_ORDER}
        program_candidate_count = {key: count_by_key[key] for key in PROGRAM_ORDER}
        manifest_path = _selection_manifest_path(run_dir, report_date)
        manifest = _read_json(manifest_path)
        code_programs: OrderedDict[str, tuple[str, ...]] = OrderedDict()
        for candidate in manifest.get("candidates") or ():
            code = str(candidate.get("code"))
            code_programs[code] = tuple(
                str(item) for item in candidate.get("programs") or ()
            )
        snapshot_path = _universe_snapshot_path(run_dir, report_date)
        snapshot = _read_json(snapshot_path)
        reports.append(
            SelectionDateReport(
                report_date=report_date,
                run_dir=run_name,
                report_path=report_path,
                report_sha256=sha256_file(report_path),
                manifest_path=manifest_path,
                manifest_file_sha256=sha256_file(manifest_path),
                manifest_content_sha256=str(manifest.get("content_sha256") or ""),
                program_status=program_status,
                program_candidate_count=program_candidate_count,
                available_groups=tuple(
                    sorted({k for k, v in program_status.items() if v == "SUCCESS"})
                ),
                missing_groups=tuple(
                    sorted({k for k, v in program_status.items() if v != "SUCCESS"})
                ),
                candidate_count=len(code_programs),
                code_programs=dict(code_programs),
                universe_snapshot_id=str(snapshot.get("snapshot_id") or ""),
                universe_snapshot_content_sha256=str(
                    snapshot.get("content_sha256") or ""
                ),
                universe_snapshot_file_sha256=sha256_file(snapshot_path),
            )
        )
    return reports


@dataclass(frozen=True)
class SymbolResolution:
    symbol_by_code: Mapping[str, str]
    conflicting_codes: Mapping[str, tuple[str, ...]]
    unknown_codes: tuple[str, ...]
    universe_snapshot_sha256_by_date: Mapping[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": (
                "the 01 pilot run's own dual-ma-universe@<date> snapshot members "
                "(code -> symbol); no other file is consulted"
            ),
            "resolved_code_count": len(self.symbol_by_code),
            "conflicting_codes": {
                key: list(value) for key, value in sorted(self.conflicting_codes.items())
            },
            "unknown_codes": list(self.unknown_codes),
            "universe_snapshot_file_sha256_by_date": dict(
                self.universe_snapshot_sha256_by_date
            ),
        }


def resolve_symbols(
    pilot_root: str | Path, reports: Sequence[SelectionDateReport]
) -> SymbolResolution:
    pilot_root = Path(pilot_root)
    seen: dict[str, set[str]] = {}
    snapshot_hashes: dict[str, str] = {}
    for report in reports:
        snapshot_path = _universe_snapshot_path(
            pilot_root / report.run_dir, report.report_date
        )
        snapshot_hashes[report.report_date] = sha256_file(snapshot_path)
        for member in _read_json(snapshot_path).get("members") or ():
            seen.setdefault(str(member.get("code")), set()).add(str(member.get("symbol")))
    symbol_by_code: dict[str, str] = {}
    conflicting: dict[str, tuple[str, ...]] = {}
    unknown: list[str] = []
    for report in reports:
        for code in report.code_programs:
            if code in symbol_by_code or code in conflicting or code in unknown:
                continue
            candidates = seen.get(code)
            if not candidates:
                unknown.append(code)
            elif len(candidates) == 1:
                symbol_by_code[code] = next(iter(candidates))
            else:
                conflicting[code] = tuple(sorted(candidates))
    return SymbolResolution(
        symbol_by_code=symbol_by_code,
        conflicting_codes=conflicting,
        unknown_codes=tuple(sorted(unknown)),
        universe_snapshot_sha256_by_date=snapshot_hashes,
    )


@dataclass(frozen=True)
class FrozenPriceSnapshot:
    root: Path
    manifest_path: Path
    manifest_file_sha256: str
    content_sha256: str
    snapshot_id: str
    symbol_count: int
    bars_by_symbol: Mapping[str, tuple[Bar, ...]]
    csv_sha256_by_symbol: Mapping[str, str]
    first_session: date
    last_session: date

    def bar_dates(self, symbol: str) -> tuple[date, ...]:
        return tuple(bar.date for bar in self.bars_by_symbol.get(symbol, ()))

    def official_closes(self) -> dict[tuple[str, date], Any]:
        return {
            (symbol, bar.date): bar.close
            for symbol, bars in self.bars_by_symbol.items()
            for bar in bars
        }


def load_price_snapshot(
    root: str | Path, symbols: Sequence[str], *, expectations: FrozenExpectations
) -> FrozenPriceSnapshot:
    root = Path(root)
    manifest_path = root / PRICE_SNAPSHOT_MANIFEST_NAME
    if not manifest_path.is_file():
        raise RealPilotError(f"price snapshot manifest is missing: {manifest_path}")
    manifest = _read_json(manifest_path)
    content_sha = str(manifest.get("content_sha256") or "")
    if (
        expectations.price_snapshot_content_sha256
        and content_sha != expectations.price_snapshot_content_sha256
    ):
        raise RealPilotError(
            f"price snapshot content sha256 {content_sha} != expected "
            f"{expectations.price_snapshot_content_sha256}"
        )
    entry_by_symbol: dict[str, Mapping[str, Any]] = {}
    for entry in manifest.get("prices") or ():
        stem = str(entry.get("path") or "").rsplit("/", 1)[-1]
        if stem.endswith(".csv"):
            stem = stem[: -len(".csv")]
        entry_by_symbol[stem.replace("_", ".")] = entry
    bars_by_symbol: dict[str, tuple[Bar, ...]] = {}
    csv_hashes: dict[str, str] = {}
    first_dates: list[date] = []
    last_dates: list[date] = []
    for symbol in sorted(set(symbols)):
        entry = entry_by_symbol.get(symbol)
        if entry is None:
            continue
        csv_path = root / str(entry.get("path"))
        if not csv_path.is_file():
            continue
        digest = sha256_file(csv_path)
        declared = entry.get("sha256")
        if declared and digest != declared:
            raise RealPilotError(
                f"{csv_path.name} sha256 {digest} != snapshot manifest {declared}"
            )
        bars: list[Bar] = []
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                bars.append(
                    Bar.from_values(
                        date.fromisoformat(row["date"]),
                        row["open"],
                        row["high"],
                        row["low"],
                        row["close"],
                    )
                )
        bars_by_symbol[symbol] = tuple(bars)
        csv_hashes[symbol] = digest
        if bars:
            first_dates.append(bars[0].date)
            last_dates.append(bars[-1].date)
    if not bars_by_symbol:
        raise RealPilotError("no per-symbol price CSV could be read for the sample")
    return FrozenPriceSnapshot(
        root=root,
        manifest_path=manifest_path,
        manifest_file_sha256=sha256_file(manifest_path),
        content_sha256=content_sha,
        snapshot_id=str(manifest.get("snapshot_id") or ""),
        symbol_count=int(manifest.get("symbol_count") or len(entry_by_symbol)),
        bars_by_symbol=bars_by_symbol,
        csv_sha256_by_symbol=csv_hashes,
        first_session=min(first_dates),
        last_session=max(last_dates),
    )


@dataclass(frozen=True)
class PriceBasisBinding:
    path: Path
    file_sha256: str
    declared_price_basis: str
    accepted_by_adapter: bool
    availability_sha256: str
    corporate_action_ledger_sha256: str
    price_source_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "c1_binding_path": str(self.path),
            "c1_binding_file_sha256": self.file_sha256,
            "declared_price_basis": self.declared_price_basis,
            "adapter_price_basis_allowed": list(PRICE_BASIS_ALLOWED),
            "accepted_by_adapter": self.accepted_by_adapter,
            "availability_sha256": self.availability_sha256,
            "corporate_action_ledger_sha256": self.corporate_action_ledger_sha256,
            "price_source_sha256": self.price_source_sha256,
            "note": (
                "the frozen C1 vendor-proxy label is passed through verbatim; this "
                "batch neither relabels the basis nor widens the adapter allow-list"
            ),
        }


def load_price_basis_binding(
    path: str | Path, *, expectations: FrozenExpectations
) -> PriceBasisBinding:
    path = Path(path)
    if not path.is_file():
        raise RealPilotError(f"C1 price binding manifest is missing: {path}")
    digest = sha256_file(path)
    if (
        expectations.c1_binding_file_sha256
        and digest != expectations.c1_binding_file_sha256
    ):
        raise RealPilotError(
            f"C1 binding file sha256 {digest} != expected "
            f"{expectations.c1_binding_file_sha256}"
        )
    payload = _read_json(path)
    basis = str(payload.get("price_basis") or "")
    if expectations.c1_price_basis and basis != expectations.c1_price_basis:
        raise RealPilotError(
            f"C1 declared price basis {basis!r} != expected "
            f"{expectations.c1_price_basis!r}"
        )
    daily = (payload.get("sources") or {}).get("authoritative_daily_manifest") or {}
    by_role = {
        str(item.get("role")): str(item.get("sha256"))
        for item in (daily.get("source_bindings") or ())
    }
    return PriceBasisBinding(
        path=path,
        file_sha256=digest,
        declared_price_basis=basis,
        accepted_by_adapter=basis in PRICE_BASIS_ALLOWED,
        availability_sha256=by_role.get("availability_snapshot", ""),
        corporate_action_ledger_sha256=by_role.get("corporate_action_ledger", ""),
        price_source_sha256=str(payload.get("price_source_sha256") or ""),
    )


# ---------------------------------------------------------------------------
# Life-cycle planning and engine runs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedEpisode:
    code: str
    symbol: str
    strategy: str
    window: EpisodeWindow

    def registry_row(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "strategy": self.strategy,
            "episode_ordinal": self.window.ordinal,
            "source_selection_date": self.window.source_selection_date.isoformat(),
            "monitor_effective_from": self.window.monitor_effective_from.isoformat(),
            "regime_window_start": self.window.regime_window_start.isoformat(),
            "regime_window_end": self.window.regime_window_end.isoformat(),
        }


@dataclass(frozen=True)
class LifecyclePlan:
    episodes: tuple[PlannedEpisode, ...]
    selection_records: tuple[dict[str, Any], ...]
    no_price_codes: tuple[str, ...]
    conflicting_symbol_codes: tuple[str, ...]
    unknown_symbol_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "selection_event_count": len(self.selection_records),
            "activated_episode_count": len(self.episodes),
            "status_counts": dict(
                sorted(
                    Counter(str(row["status"]) for row in self.selection_records).items()
                )
            ),
            "reason_counts": dict(
                sorted(
                    Counter(str(row["reason"]) for row in self.selection_records).items()
                )
            ),
            "no_price_code_count": len(self.no_price_codes),
            "no_price_codes": list(self.no_price_codes),
            "conflicting_symbol_code_count": len(self.conflicting_symbol_codes),
            "conflicting_symbol_codes": list(self.conflicting_symbol_codes),
            "unknown_symbol_code_count": len(self.unknown_symbol_codes),
            "unknown_symbol_codes": list(self.unknown_symbol_codes),
        }


def build_lifecycle_plan(
    *,
    reports: Sequence[SelectionDateReport],
    resolution: SymbolResolution,
    snapshot: FrozenPriceSnapshot,
    calendar: OfficialCalendar,
    strategies: Sequence[str] = STRATEGY_ORDER,
    window_limit: int | None = None,
) -> LifecyclePlan:
    """Apply the frozen D -> E monitoring-episode ruling to the real sample."""

    selections_by_key: dict[tuple[str, str], set[str]] = {}
    for report in reports:
        for code, programs in report.code_programs.items():
            if not programs:
                continue
            for strategy in strategies:
                selections_by_key.setdefault((code, strategy), set()).add(
                    report.report_date
                )

    episodes: list[PlannedEpisode] = []
    records: list[dict[str, Any]] = []
    seen_conflicts = set(resolution.conflicting_codes)
    seen_unknown = set(resolution.unknown_codes)
    no_price: set[str] = set()

    for (code, strategy), dates in sorted(selections_by_key.items()):
        symbol = resolution.symbol_by_code.get(code)
        if symbol is None:
            records.append(
                {
                    "code": code,
                    "symbol": None,
                    "strategy": strategy,
                    "selection_date": min(dates),
                    "selection_dates": sorted(dates),
                    "effective_date": None,
                    "status": "HOLD",
                    "reason": (
                        "HOLD_CONFLICTING_SYMBOL_IDENTITY"
                        if code in seen_conflicts
                        else "HOLD_MISSING_SYMBOL_IDENTITY"
                    ),
                    "episode_ordinal": None,
                    "regime_evidence": {},
                }
            )
            continue
        bars = snapshot.bars_by_symbol.get(symbol)
        if not bars:
            no_price.add(code)
            records.append(
                {
                    "code": code,
                    "symbol": symbol,
                    "strategy": strategy,
                    "selection_date": min(dates),
                    "selection_dates": sorted(dates),
                    "effective_date": None,
                    "status": "HOLD",
                    "reason": "HOLD_MISSING_PRICE_SERIES",
                    "episode_ordinal": None,
                    "regime_evidence": {},
                }
            )
            continue

        kind = StrategyKind(strategy)
        detail = regime_values(bars, kind)
        regime = {day: verdict for day, (_, _, verdict) in detail.items()}
        pairs: list[tuple[date, date]] = []
        for selection_date in sorted(dates):
            parsed = date.fromisoformat(selection_date)
            effective = calendar.next_session(parsed)
            if effective is None:
                records.append(
                    {
                        "code": code,
                        "symbol": symbol,
                        "strategy": strategy,
                        "selection_date": selection_date,
                        "selection_dates": [selection_date],
                        "effective_date": None,
                        "status": "HOLD",
                        "reason": "HOLD_NO_NEXT_OFFICIAL_SESSION_AFTER_D",
                        "episode_ordinal": None,
                        "regime_evidence": {},
                    }
                )
                continue
            pairs.append((parsed, effective))
        windows, window_records = episode_windows(
            code=code,
            strategy=kind,
            selections=pairs,
            regime=regime,
            sessions=calendar.sessions,
            bar_dates=[bar.date for bar in bars],
            regime_detail=detail,
        )
        for record in window_records:
            records.append({**record, "symbol": symbol, "selection_dates": sorted(dates)})
        for window in windows:
            episodes.append(
                PlannedEpisode(code=code, symbol=symbol, strategy=strategy, window=window)
            )

    episodes.sort(
        key=lambda item: (
            item.code,
            item.strategy,
            item.window.monitor_effective_from,
            item.window.ordinal,
        )
    )
    if window_limit is not None:
        episodes = episodes[: int(window_limit)]
    return LifecyclePlan(
        episodes=tuple(episodes),
        selection_records=tuple(records),
        no_price_codes=tuple(sorted(no_price)),
        conflicting_symbol_codes=tuple(sorted(seen_conflicts)),
        unknown_symbol_codes=tuple(sorted(seen_unknown)),
    )


def episode_identity(base: Mapping[str, Any]) -> dict[str, Any]:
    """Causal D -> E provenance carried by every event of one episode."""

    return {
        "source_selection_date": base["episode_source_selection_date"],
        "membership_effective_date": base["monitor_effective_from"],
        "episode_key": "|".join(
            [
                str(base["symbol"]),
                str(base["strategy"]),
                "EP" + str(base["episode_ordinal"]),
                str(base["episode_source_selection_date"]),
            ]
        ),
    }


def _run_manifest(
    *,
    code: str,
    matrix_id: str,
    bars: Sequence[Bar],
    add_variant: AddVariant,
    exit_variant: ExitVariant,
    slippage_bps: int,
    run_id: str,
    runner_commit: str,
    snapshot_id: str,
    data_hash: str,
) -> RunManifest:
    return RunManifest(
        run_id=(
            f"{run_id}:{code}:{matrix_id}:{slippage_bps}:"
            f"{bars[0].date.isoformat()}:{bars[-1].date.isoformat()}"
        ),
        strategy_version=STRATEGY_VERSION,
        spec_sha256=SPEC_SHA256,
        git_commit=runner_commit,
        universe_id=snapshot_id,
        data_snapshot_id=snapshot_id,
        data_start=bars[0].date,
        data_end=bars[-1].date,
        data_hash=data_hash,
        execution_model=EXECUTION_MODEL,
        cost_model=COST_MODEL,
        matrix_id=matrix_id,
        slippage_bps=slippage_bps,
        strategy_parameters={
            "entry_variant": "ENTRY_RECLAIM_ONLY",
            "add_variant": add_variant.value,
            "exit_variant": exit_variant.value,
        },
        metadata={
            "source_code": code,
            "lifecycle_contract": (
                "docs/strategies/dual_ma/2022-h1-selection-episode-lifecycle-v1.md"
            ),
        },
    )


def _project(row: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {name: row.get(name) for name in fields}


def run_episode_window(
    *,
    episode: PlannedEpisode,
    bars: Sequence[Bar],
    slippage_scenarios: Sequence[int] = SLIPPAGE_SCENARIOS,
    run_id: str,
    runner_commit: str,
    snapshot_id: str,
    data_hash: str,
    instrument_parameter: InstrumentType = ENGINE_INSTRUMENT_PARAMETER,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Run the frozen engine once per matrix x slippage for one episode window.

    Emitted LEG / TRANSACTION rows carry the fields the frozen B sidecar and the
    frozen cost adapter read.  The engine's own excluded-field markers are kept
    so the sidecar records them instead of silently dropping them.
    """

    window = episode.window
    run_bars = [bar for bar in bars if bar.date <= window.regime_window_end]
    legs: list[dict[str, Any]] = []
    transactions: list[dict[str, Any]] = []
    warmup = sum(1 for bar in run_bars if bar.date < window.regime_window_start)
    stats = {
        "matrix_slippage_runs": 0,
        "leg_rows": 0,
        "transaction_rows": 0,
        "warmup_bars_before_window": warmup,
    }
    for add_variant in AddVariant:
        for exit_variant in ExitVariant:
            matrix_id = MATRIX_GROUPS[(add_variant, exit_variant)]
            for slippage_bps in slippage_scenarios:
                manifest = _run_manifest(
                    code=episode.code,
                    matrix_id=matrix_id,
                    bars=run_bars,
                    add_variant=add_variant,
                    exit_variant=exit_variant,
                    slippage_bps=slippage_bps,
                    run_id=run_id,
                    runner_commit=runner_commit,
                    snapshot_id=snapshot_id,
                    data_hash=data_hash,
                )
                result = DualMABacktestEngine(
                    add_variant, exit_variant, slippage_bps=slippage_bps
                ).run(
                    episode.symbol,
                    run_bars,
                    run_manifest=manifest,
                    instrument_type=instrument_parameter,
                    backtest_start=window.regime_window_start,
                    backtest_end=window.regime_window_end,
                )
                stats["matrix_slippage_runs"] += 1
                payload = result.to_dict()
                base = {
                    "code": episode.code,
                    "symbol": episode.symbol,
                    "strategy": episode.strategy,
                    "matrix_id": matrix_id,
                    "slippage_bps": slippage_bps,
                    "episode_ordinal": window.ordinal,
                    "episode_source_selection_date": (
                        window.source_selection_date.isoformat()
                    ),
                    "monitor_effective_from": window.monitor_effective_from.isoformat(),
                    "regime_window_start": window.regime_window_start.isoformat(),
                    "regime_window_end": window.regime_window_end.isoformat(),
                    "episode_ended_by_regime": window.ended_by_regime,
                    "warmup_bars_before_segment": warmup,
                }
                identity = episode_identity(base)
                pullback_by_leg: dict[str, Any] = {}
                for row in payload["transactions"]:
                    if row.get("strategy") != episode.strategy:
                        continue
                    leg_id = str(row.get("leg_id"))
                    if leg_id not in pullback_by_leg and row.get("pullback_event_id"):
                        pullback_by_leg[leg_id] = row.get("pullback_event_id")
                    transactions.append(
                        {
                            **base,
                            **identity,
                            "kind": "TRANSACTION",
                            "trade_date": row.get("date"),
                            "performance_fields_excluded": list(
                                TRANSACTION_PERFORMANCE_FIELDS_EXCLUDED
                            ),
                            "unavailable_engine_fields": list(
                                TRANSACTION_UNAVAILABLE_ENGINE_FIELDS
                            ),
                            **_project(row, ADAPTER_TRANSACTION_FIELDS),
                        }
                    )
                for row in payload["legs"]:
                    if row.get("strategy") != episode.strategy:
                        continue
                    leg_id = str(row.get("leg_id"))
                    legs.append(
                        {
                            **base,
                            **identity,
                            "kind": "LEG",
                            "trade_date": row.get("entry_date"),
                            "performance_fields_excluded": list(
                                LEG_PERFORMANCE_FIELDS_EXCLUDED
                            ),
                            "pullback_event_id": pullback_by_leg.get(leg_id),
                            "open_censored": row.get("exit_date") is None,
                            **_project(row, ADAPTER_LEG_FIELDS),
                        }
                    )
                stats["leg_rows"] += len(
                    [
                        row
                        for row in payload["legs"]
                        if row.get("strategy") == episode.strategy
                    ]
                )
                stats["transaction_rows"] += len(
                    [
                        row
                        for row in payload["transactions"]
                        if row.get("strategy") == episode.strategy
                    ]
                )
    return legs, transactions, stats


def run_all_episodes(
    *,
    plan: LifecyclePlan,
    snapshot: FrozenPriceSnapshot,
    run_id: str,
    runner_commit: str,
    progress: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    legs: list[dict[str, Any]] = []
    transactions: list[dict[str, Any]] = []
    stats = {
        "episode_windows_run": 0,
        "matrix_slippage_runs": 0,
        "leg_rows": 0,
        "transaction_rows": 0,
    }
    for index, episode in enumerate(plan.episodes, start=1):
        bars = snapshot.bars_by_symbol[episode.symbol]
        window_legs, window_transactions, window_stats = run_episode_window(
            episode=episode,
            bars=bars,
            run_id=run_id,
            runner_commit=runner_commit,
            snapshot_id=snapshot.snapshot_id,
            data_hash=snapshot.content_sha256,
        )
        legs.extend(window_legs)
        transactions.extend(window_transactions)
        stats["episode_windows_run"] += 1
        stats["matrix_slippage_runs"] += window_stats["matrix_slippage_runs"]
        stats["leg_rows"] += window_stats["leg_rows"]
        stats["transaction_rows"] += window_stats["transaction_rows"]
        if progress is not None and index % 25 == 0:
            progress(index, len(plan.episodes), stats)
    return legs, transactions, stats


def verify_trade_table_instrument_independence(
    *,
    episodes: Sequence[PlannedEpisode],
    snapshot: FrozenPriceSnapshot,
    run_id: str,
    runner_commit: str,
    limit: int = 3,
) -> dict[str, Any]:
    """Bounded sweep proving the published trade columns are instrument-free.

    The frozen engine takes an InstrumentType argument that changes position
    sizing, so this re-runs the sample's own episode windows under every
    InstrumentType and compares the published trade columns (dates, reference
    closes, fill prices, share counts).  It is a bounded spot check on real
    episodes, not a proof over the whole sample.
    """

    checks: list[dict[str, Any]] = []
    for episode in list(episodes)[: int(limit)]:
        bars = snapshot.bars_by_symbol[episode.symbol]
        per_instrument: dict[str, list[dict[str, Any]]] = {}
        for instrument in InstrumentType:
            legs, _, _ = run_episode_window(
                episode=episode,
                bars=bars,
                run_id=run_id,
                runner_commit=runner_commit,
                snapshot_id=snapshot.snapshot_id,
                data_hash=snapshot.content_sha256,
                instrument_parameter=instrument,
            )
            per_instrument[instrument.value] = build_trade_rows(legs)
        reference = per_instrument[ENGINE_INSTRUMENT_PARAMETER.value]
        differing = sorted(
            name
            for name, rows in per_instrument.items()
            if rows != reference
        )
        checks.append(
            {
                "code": episode.code,
                "symbol": episode.symbol,
                "strategy": episode.strategy,
                "episode_ordinal": episode.window.ordinal,
                "source_selection_date": (
                    episode.window.source_selection_date.isoformat()
                ),
                "monitor_effective_from": (
                    episode.window.monitor_effective_from.isoformat()
                ),
                "regime_window_end": episode.window.regime_window_end.isoformat(),
                "trade_row_count": len(reference),
                "instrument_types_compared": sorted(per_instrument),
                "differing_instrument_types": differing,
                "identical": not differing,
            }
        )
    return {
        "schema_version": "dual-ma-h1-real-pilot-instrument-sweep-v1",
        "checked_episode_count": len(checks),
        "all_identical": all(check["identical"] for check in checks),
        "trade_row_count_compared": sum(check["trade_row_count"] for check in checks),
        "trade_columns_compared": list(TRADE_TABLE_FIELDS) + ["open_censored"],
        "engine_parameter_reference": ENGINE_INSTRUMENT_PARAMETER.value,
        "checks": checks,
        "note": (
            "bounded spot check over real episode windows; it supports removing "
            "instrument-dependent money columns from the published trade table "
            "but is not a proof over every symbol and episode"
        ),
    }


# ---------------------------------------------------------------------------
# Thin wiring: B sidecar (blocker 3) + official calendar binding (blocker 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WiringResult:
    sidecar_rows: tuple[dict[str, Any], ...]
    sidecar_holds: tuple[dict[str, Any], ...]
    sidecar_counts: Mapping[str, Any]
    sidecar_payloads: Mapping[str, bytes]
    adapter_report: Mapping[str, Any]
    derived_legs: tuple[dict[str, Any], ...]
    price_basis: str
    market_evidence_counts: Mapping[str, int]
    next_unresolved_blocker: str


def build_market_evidence(
    *,
    snapshot: FrozenPriceSnapshot,
    calendar: OfficialCalendar,
    price_basis: str,
    binding: PriceBasisBinding,
    decision_cutoff: date,
) -> MarketEvidence:
    bar_dates_by_symbol = {
        symbol: tuple(bar.date for bar in bars)
        for symbol, bars in snapshot.bars_by_symbol.items()
    }
    return MarketEvidence(
        official_sessions=calendar.sessions,
        bar_dates_by_symbol=bar_dates_by_symbol,
        # No per-symbol suspension evidence is bound in this minimal round, so a
        # missing bar stays unexplained instead of being silently explained.
        explained_non_trading=frozenset(),
        official_closes=snapshot.official_closes(),
        price_basis=price_basis,
        corporate_action_ledger_sha256=binding.corporate_action_ledger_sha256,
        availability_sha256=binding.availability_sha256,
        price_source_sha256=binding.price_source_sha256 or snapshot.content_sha256,
        decision_cutoff=decision_cutoff,
    )


def wire_legs_and_adapt(
    *,
    leg_rows: Sequence[Mapping[str, Any]],
    transaction_rows: Sequence[Mapping[str, Any]],
    registry_index: Mapping[Any, Any],
    market: MarketEvidence,
) -> WiringResult:
    """Materialise the B sidecar, inject the key into a derived copy, then adapt."""

    build = build_sidecar(
        legs=leg_rows,
        transactions=transaction_rows,
        registry_index=registry_index,
    )
    key_by_identity: dict[tuple[Any, ...], str] = {}
    for row in build.rows:
        registry = row.get("lifecycle_registry_fields") or {}
        key_by_identity[
            (
                row["matrix_id"],
                int(row["slippage_bps"]),
                row["leg_id"],
                int(row["leg_number"]),
                registry.get("code"),
                registry.get("strategy"),
                registry.get("episode_ordinal"),
                row.get("engine_episode_id"),
            )
        ] = str(row["economic_key"])
    derived: list[dict[str, Any]] = []
    for row in leg_rows:
        identity = (
            row.get("matrix_id"),
            int(row.get("slippage_bps") or 0),
            str(row.get("leg_id")),
            int(row.get("leg_number") or 0),
            row.get("code"),
            row.get("strategy"),
            row.get("episode_ordinal"),
            row.get("episode_id"),
        )
        economic_key = key_by_identity.get(identity)
        if economic_key is None:
            continue
        derived.append({**dict(row), "economic_key": economic_key})

    adapter_report = adapt_events(
        leg_events=derived,
        transaction_events=transaction_rows,
        episodes=[],
        market=market,
    )
    hold_codes = sorted(
        {str(row.get("hold_code")) for row in adapter_report["holds"]}
    )
    next_unresolved = (
        HOLD_MISSING_INSTRUMENT_TYPE if HOLD_MISSING_PRICE_BASIS in hold_codes else ""
    )
    payloads = {
        "economic_signals.jsonl": economic_signals_jsonl(build.rows),
        "economic_signal_holds.json": holds_json(build),
        "sidecar_classification_counts.json": classification_counts_json(build),
    }
    return WiringResult(
        sidecar_rows=tuple(build.rows),
        sidecar_holds=tuple(build.holds),
        sidecar_counts=dict(build.counts),
        sidecar_payloads=payloads,
        adapter_report=adapter_report,
        derived_legs=tuple(derived),
        price_basis=market.price_basis,
        market_evidence_counts={
            "official_session_count": len(market.official_sessions),
            "symbol_bar_date_count": len(market.bar_dates_by_symbol),
            "official_close_count": len(market.official_closes),
            "explained_non_trading_count": len(market.explained_non_trading),
        },
        next_unresolved_blocker=next_unresolved,
    )


# ---------------------------------------------------------------------------
# Published tables
# ---------------------------------------------------------------------------


def build_trade_rows(leg_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Instrument-independent trade table; every money column is dropped."""

    rows: list[dict[str, Any]] = []
    for row in leg_rows:
        trade = {name: row.get(name) for name in TRADE_TABLE_FIELDS}
        trade["open_censored"] = row.get("exit_date") is None
        rows.append(trade)
    rows.sort(
        key=lambda item: (
            str(item["code"]),
            str(item["strategy"]),
            str(item["entry_date"]),
            str(item["matrix_id"]),
            int(item["slippage_bps"]),
            str(item["leg_id"]),
        )
    )
    return rows


def build_exception_rows(
    *,
    plan: LifecyclePlan,
    adapter_report: Mapping[str, Any],
    sidecar_holds: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Named, pre-existing reasons a selection event or a leg cannot be costed."""

    rows: list[dict[str, Any]] = []
    for record in plan.selection_records:
        if str(record["status"]) == "ACTIVATED":
            continue
        rows.append(
            {
                "stage": "LIFECYCLE_SELECTION_EVENT",
                "code": record.get("code"),
                "symbol": record.get("symbol"),
                "strategy": record.get("strategy"),
                "selection_date": _iso_or_none(record.get("selection_date")),
                "effective_date": _iso_or_none(record.get("effective_date")),
                "status": record.get("status"),
                "reason": record.get("reason"),
                "detail": "",
            }
        )
    for hold in sidecar_holds:
        rows.append(
            {
                "stage": "ECONOMIC_KEY_SIDECAR",
                "code": hold.get("symbol"),
                "symbol": hold.get("symbol"),
                "strategy": hold.get("strategy"),
                "selection_date": None,
                "effective_date": _iso_or_none(hold.get("entry_date")),
                "status": "HOLD",
                "reason": hold.get("code"),
                "detail": str(hold.get("detail") or ""),
            }
        )
    for hold in adapter_report["holds"]:
        mapping = hold.get("mapping") or {}
        rows.append(
            {
                "stage": "COST_ADAPTER",
                "code": None,
                "symbol": mapping.get("symbol"),
                "strategy": mapping.get("strategy"),
                "selection_date": mapping.get("source_selection_date"),
                "effective_date": None,
                "status": "HOLD",
                "reason": hold.get("hold_code"),
                "detail": str(hold.get("reason") or ""),
            }
        )
    return rows


def build_cost_grid(
    *,
    adapter_report: Mapping[str, Any],
    sidecar_rows: Sequence[Mapping[str, Any]],
    price_basis: str,
    cost_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Six matrices x 0/10/20 bps, every cell listed, computable and held apart."""

    leg_by_cell: Counter[tuple[str, int]] = Counter()
    hold_by_cell: Counter[tuple[str, int]] = Counter()
    hold_code_by_cell: dict[tuple[str, int], Counter[str]] = {}
    for row in adapter_report["mapping_table"]:
        mapping = row.get("mapping") or {}
        matrix_id = str(mapping.get("matrix_id"))
        slippage = int(mapping.get("slippage_bps") or 0)
        leg_by_cell[(matrix_id, slippage)] += 1
        if row.get("status") == "HOLD":
            hold_by_cell[(matrix_id, slippage)] += 1
            hold_code_by_cell.setdefault((matrix_id, slippage), Counter())[
                str(row.get("hold_code"))
            ] += 1

    costed_by_cell: Counter[tuple[str, int]] = Counter()
    for leg in cost_report.get("legs") or ():
        identity = leg.get("identity") or {}
        costed_by_cell[
            (str(identity.get("matrix_id")), int(leg.get("slippage_bps") or 0))
        ] += 1

    cells: list[dict[str, Any]] = []
    for matrix_id in MATRIX_IDS:
        for slippage in SLIPPAGE_SCENARIOS:
            key = (matrix_id, slippage)
            leg_count = leg_by_cell.get(key, 0)
            hold_count = hold_by_cell.get(key, 0)
            costed_count = costed_by_cell.get(key, 0)
            cells.append(
                {
                    "matrix_id": matrix_id,
                    "slippage_bps": slippage,
                    "leg_count": leg_count,
                    "costed_leg_count": costed_count,
                    "hold_leg_count": hold_count,
                    "computable": costed_count > 0,
                    "hold_codes": dict(
                        sorted((hold_code_by_cell.get(key) or Counter()).items())
                    ),
                    "withheld_reason": (
                        ""
                        if costed_count
                        else (
                            "every leg is held before any cost quantity is produced: "
                            "the frozen vendor price basis "
                            + repr(price_basis)
                            + " is not in the adapter allow-list, and no dated "
                            "instrument identity exists"
                        )
                    ),
                }
            )
    computable = [cell for cell in cells if cell["computable"]]
    held = [cell for cell in cells if not cell["computable"]]
    total_legs = sum(cell["leg_count"] for cell in cells)
    total_holds = sum(cell["hold_leg_count"] for cell in cells)
    return {
        "schema_version": COST_GRID_SCHEMA_VERSION,
        "matrix_ids": list(MATRIX_IDS),
        "slippage_scenarios": list(SLIPPAGE_SCENARIOS),
        "price_basis": price_basis,
        "cell_count": len(cells),
        "computable_cell_count": len(computable),
        "held_cell_count": len(held),
        "total_leg_count": total_legs,
        "total_costed_leg_count": sum(cell["costed_leg_count"] for cell in cells),
        "total_hold_leg_count": total_holds,
        "hold_coverage": (float(total_holds) / float(total_legs)) if total_legs else 0.0,
        "cells": cells,
        "computable_cells": computable,
        "held_cells": held,
        "cost_report_core_status_counts": dict(cost_report.get("status_counts") or {}),
        "cost_report_core_leg_count": len(cost_report.get("legs") or ()),
        "sidecar_emitted_leg_count": len(sidecar_rows),
        "note": (
            "no cost quantity is produced in this batch; the grid exists so the "
            "held set, the denominators and the two open blockers are explicit"
        ),
    }


def build_spot_checks(
    *,
    trade_rows: Sequence[Mapping[str, Any]],
    plan: LifecyclePlan,
    limit: int = 2,
) -> dict[str, Any]:
    """Real D -> E -> T sequences, or a real selected-but-not-traded reason."""

    checks: list[dict[str, Any]] = []
    ordered = sorted(
        trade_rows,
        key=lambda row: (str(row["entry_date"]), str(row["code"]), str(row["matrix_id"])),
    )
    for row in ordered[:limit]:
        checks.append(
            {
                "kind": "D_TO_E_TO_T",
                "code": row["code"],
                "symbol": row["symbol"],
                "strategy": row["strategy"],
                "matrix_id": row["matrix_id"],
                "slippage_bps": row["slippage_bps"],
                "selection_date_D": row["source_selection_date"],
                "monitor_effective_date_E": row["monitor_effective_from"],
                "entry_signal_date_T": row["entry_date"],
                "entry_reference_close": row["entry_reference_close"],
                "entry_fill_price": row["entry_fill_price"],
                "exit_date": row["exit_date"],
                "exit_reason": row["exit_reason"],
                "exit_reference_close": row["exit_reference_close"],
                "open_censored": row["open_censored"],
                "note": (
                    "signal and fill share the T session close (EOD_CLOSE_PROXY_V1); "
                    "monitoring started on E, entry happened later on T"
                ),
            }
        )
    if len(checks) < limit:
        for record in plan.selection_records:
            if str(record["status"]) == "ACTIVATED":
                continue
            checks.append(
                {
                    "kind": "SELECTED_BUT_NOT_TRADED",
                    "code": record.get("code"),
                    "symbol": record.get("symbol"),
                    "strategy": record.get("strategy"),
                    "selection_date_D": record.get("selection_date"),
                    "monitor_effective_date_E": record.get("effective_date"),
                    "status": record.get("status"),
                    "reason": record.get("reason"),
                    "note": (
                        "the selection event expired or was held before the engine "
                        "could open a monitored episode"
                    ),
                }
            )
            if len(checks) >= limit:
                break
    return {
        "schema_version": "dual-ma-h1-real-pilot-spot-checks-v1",
        "check_count": len(checks),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Run driver
# ---------------------------------------------------------------------------


def resolve_runner_commit(worktree: str | Path) -> dict[str, Any]:
    worktree = Path(worktree)
    try:
        head = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    except Exception:  # pragma: no cover - non-git checkouts
        return {"runner_commit": "UNRESOLVED", "worktree_dirty_entry_count": None}
    return {
        "runner_commit": head,
        "worktree_dirty_entry_count": len(status),
        "worktree_dirty_entries": status,
    }


def run_real_pilot(
    *,
    workdata_root: str | Path,
    output_root: str | Path | None = None,
    run_id: str = RUN_ID_DEFAULT,
    selection_dates: Sequence[str] = SELECTION_DATES,
    expectations: FrozenExpectations = FROZEN_EXPECTATIONS,
    runner_commit: str | None = None,
    window_limit: int | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    workdata_root = Path(workdata_root)
    output_root = (
        Path(output_root)
        if output_root is not None
        else workdata_root / "02-causal-backtest"
    )
    run_dir = output_root / run_id
    if run_dir.exists():
        raise RealPilotError(f"refusing to overwrite an existing run directory: {run_dir}")

    calendar = load_official_calendar(
        workdata_root / CALENDAR_RELATIVE_PATH, expectations=expectations
    )
    binding = load_price_basis_binding(
        workdata_root / C1_BINDING_RELATIVE_PATH, expectations=expectations
    )
    reports = load_pilot_selection_dates(
        workdata_root / PILOT_ROOT_RELATIVE, selection_dates=selection_dates
    )
    resolution = resolve_symbols(workdata_root / PILOT_ROOT_RELATIVE, reports)
    snapshot = load_price_snapshot(
        workdata_root / PRICE_SNAPSHOT_RELATIVE_PATH,
        list(resolution.symbol_by_code.values()),
        expectations=expectations,
    )
    plan = build_lifecycle_plan(
        reports=reports,
        resolution=resolution,
        snapshot=snapshot,
        calendar=calendar,
        window_limit=window_limit,
    )
    commit = runner_commit or resolve_runner_commit(
        Path(__file__).resolve().parents[2]
    )["runner_commit"]
    legs, transactions, engine_stats = run_all_episodes(
        plan=plan,
        snapshot=snapshot,
        run_id=run_id,
        runner_commit=commit,
        progress=progress,
    )

    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "episode_windows.json").write_bytes(
        _json_bytes(
            {
                "schema_version": EPISODE_REGISTRY_SCHEMA_VERSION,
                "run_id": run_id,
                "window_count": len(plan.episodes),
                "windows": [episode.registry_row() for episode in plan.episodes],
            }
        )
    )
    (run_dir / "engine_events.jsonl").write_bytes(_jsonl_bytes((*legs, *transactions)))
    registry_index, _ = load_lifecycle_registry(run_dir / "episode_windows.json")
    _, _, kind_counts = read_engine_events(run_dir / "engine_events.jsonl")

    market = build_market_evidence(
        snapshot=snapshot,
        calendar=calendar,
        price_basis=binding.declared_price_basis,
        binding=binding,
        decision_cutoff=snapshot.last_session,
    )
    wiring = wire_legs_and_adapt(
        leg_rows=legs,
        transaction_rows=transactions,
        registry_index=registry_index,
        market=market,
    )
    leg_inputs: list[LegInput] = list(wiring.adapter_report["leg_inputs"])
    cost_report = build_report(leg_inputs)

    trade_rows = build_trade_rows(legs)
    exception_rows = build_exception_rows(
        plan=plan,
        adapter_report=wiring.adapter_report,
        sidecar_holds=wiring.sidecar_holds,
    )
    cost_grid = build_cost_grid(
        adapter_report=wiring.adapter_report,
        sidecar_rows=wiring.sidecar_rows,
        price_basis=binding.declared_price_basis,
        cost_report=cost_report,
    )
    spot_checks = build_spot_checks(trade_rows=trade_rows, plan=plan)
    episodes_by_identity = {
        (item.code, item.strategy, item.window.ordinal): item for item in plan.episodes
    }
    sweep_episodes: list[PlannedEpisode] = []
    for row in sorted(
        trade_rows,
        key=lambda item: (
            str(item["entry_date"]),
            str(item["code"]),
            str(item["strategy"]),
        ),
    ):
        identity = (str(row["code"]), str(row["strategy"]), int(row["episode_ordinal"]))
        episode = episodes_by_identity.get(identity)
        if episode is None or episode in sweep_episodes:
            continue
        sweep_episodes.append(episode)
        if len(sweep_episodes) >= 3:
            break
    instrument_sweep = verify_trade_table_instrument_independence(
        episodes=sweep_episodes,
        snapshot=snapshot,
        run_id=run_id,
        runner_commit=commit,
    )

    first_day = date.fromisoformat(selection_dates[0])
    last_day = date.fromisoformat(selection_dates[-1])
    window_sessions = calendar.sessions_between(first_day, last_day)
    payloads: dict[str, bytes] = {
        "input_coverage.json": _json_bytes(
            {
                "schema_version": "dual-ma-h1-real-pilot-input-coverage-v1",
                "selection_dates_requested": list(selection_dates),
                "selection_dates_present": [report.report_date for report in reports],
                "official_session_count_between_first_and_last_selection": len(
                    window_sessions
                ),
                "official_sessions_between_first_and_last_selection": [
                    item.isoformat() for item in window_sessions
                ],
                "program_order": list(PROGRAM_ORDER),
                "dates": [report.as_coverage() for report in reports],
                "symbol_resolution": resolution.as_dict(),
                "price_snapshot": {
                    "snapshot_id": snapshot.snapshot_id,
                    "content_sha256": snapshot.content_sha256,
                    "manifest_file_sha256": snapshot.manifest_file_sha256,
                    "manifest_path": str(snapshot.manifest_path),
                    "declared_symbol_count": snapshot.symbol_count,
                    "loaded_symbol_count": len(snapshot.bars_by_symbol),
                    "first_session": snapshot.first_session.isoformat(),
                    "last_session": snapshot.last_session.isoformat(),
                },
                "calendar": calendar.as_binding(),
                "lifecycle": plan.as_dict(),
            }
        ),
        "selection_episode_ledger.json": _json_bytes(
            {
                "schema_version": "dual-ma-h1-real-pilot-selection-episode-ledger-v1",
                "selection_record_count": len(plan.selection_records),
                "records": list(plan.selection_records),
                "windows": [episode.registry_row() for episode in plan.episodes],
            }
        ),
        "trade_rows.jsonl": _jsonl_bytes(trade_rows),
        "exception_rows.jsonl": _jsonl_bytes(exception_rows),
        "cost_grid.json": _json_bytes(cost_grid),
        "cost_report_core.json": _json_bytes(cost_report),
        "adapter_hold_ledger.json": _json_bytes(
            {
                "adapter_version": wiring.adapter_report["adapter_version"],
                "leg_count": wiring.adapter_report["leg_count"],
                "mapped_count": wiring.adapter_report["mapped_count"],
                "hold_count": wiring.adapter_report["hold_count"],
                "hold_codes": wiring.adapter_report["hold_codes"],
                "rows": wiring.adapter_report["holds"],
            }
        ),
        "economic_signals.jsonl": wiring.sidecar_payloads["economic_signals.jsonl"],
        "economic_signal_holds.json": wiring.sidecar_payloads[
            "economic_signal_holds.json"
        ],
        "sidecar_classification_counts.json": wiring.sidecar_payloads[
            "sidecar_classification_counts.json"
        ],
        "price_basis_binding.json": _json_bytes(binding.as_dict()),
        "spot_checks.json": _json_bytes(spot_checks),
        "instrument_sweep.json": _json_bytes(instrument_sweep),
    }
    for name, blob in payloads.items():
        (run_dir / name).write_bytes(blob)

    artifact_hashes = {name: sha256_bytes(blob) for name, blob in sorted(payloads.items())}
    artifact_hashes["episode_windows.json"] = sha256_file(run_dir / "episode_windows.json")
    artifact_hashes["engine_events.jsonl"] = sha256_file(
        run_dir / "engine_events.jsonl"
    )

    hold_code_counts = Counter(
        str(row.get("hold_code")) for row in wiring.adapter_report["holds"]
    )
    coverage_gaps = {
        "missing_price_series_codes": list(plan.no_price_codes),
        "conflicting_symbol_identity_codes": list(plan.conflicting_symbol_codes),
        "unknown_symbol_identity_codes": list(plan.unknown_symbol_codes),
    }
    no_performance_leak = all(
        token not in str(name).lower()
        for row in (*legs, *transactions)
        for name in row
        for token in ("expectancy", "win_rate", "mfe", "mae", "profit")
    )
    gates = {
        "calendar_bound": "PASS" if calendar.sessions else "FAIL",
        "every_selection_date_present": (
            "PASS" if len(reports) == len(selection_dates) else "FAIL"
        ),
        "engine_events_kinds_expected": (
            "PASS" if set(kind_counts) <= {"LEG", "TRANSACTION"} else "FAIL"
        ),
        "price_basis_passed_through_unmodified": (
            "PASS" if binding.declared_price_basis == C1_DECLARED_PRICE_BASIS else "FAIL"
        ),
        "instrument_type_never_asserted_in_evidence": (
            "PASS"
            if all("instrument_type" not in row for row in (*legs, *transactions))
            else "FAIL"
        ),
        "no_cost_quantity_produced": "PASS" if not cost_report.get("legs") else "FAIL",
        "named_hold_for_every_leg": (
            "PASS"
            if wiring.adapter_report["hold_count"] == wiring.adapter_report["leg_count"]
            and wiring.adapter_report["leg_count"] == len(wiring.derived_legs)
            else "FAIL"
        ),
        "no_performance_field_in_inputs": "PASS" if no_performance_leak else "FAIL",
        "trade_table_instrument_independent": (
            "PASS" if instrument_sweep["all_identical"] else "FAIL"
        ),
    }
    readback_status = (
        "PASS" if all(value == "PASS" for value in gates.values()) else "HOLD"
    )

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "module_version": MODULE_VERSION,
        "run_id": run_id,
        "note": "No wall-clock timestamp is recorded so the run stays reproducible.",
        "status": "PASS_WITH_ALL_HOLD" if readback_status == "PASS" else "HOLD",
        "readback_status": readback_status,
        "workdata_root": str(workdata_root),
        "research_only": True,
        "point_in_time_verified": False,
        "performance_read_or_computed": False,
        "rules_changed": False,
        "selection_program_modified": False,
        "sample_definition": (
            "the 01 eight-program daily selections for the official sessions "
            "2022-04-19 .. 2022-04-28; 2023 / 2024 are never read"
        ),
        "runner_commit": commit,
        "inputs": {
            "official_calendar": calendar.as_binding(),
            "price_snapshot_content_sha256": snapshot.content_sha256,
            "price_snapshot_manifest_file_sha256": snapshot.manifest_file_sha256,
            "price_basis_binding": binding.as_dict(),
            "selection_dates": [report.as_coverage() for report in reports],
        },
        "coverage": {
            "selection_date_count": len(reports),
            "official_sessions_in_window": len(window_sessions),
            "candidate_codes": plan.as_dict(),
            "gaps": coverage_gaps,
        },
        "engine": {
            "episode_window_count": len(plan.episodes),
            "matrix_slippage_runs": engine_stats["matrix_slippage_runs"],
            "leg_rows": engine_stats["leg_rows"],
            "transaction_rows": engine_stats["transaction_rows"],
            "engine_event_kind_counts": kind_counts,
        },
        "instrument_type": {
            "claimed_in_evidence": False,
            "engine_parameter_used": ENGINE_INSTRUMENT_PARAMETER.value,
            "engine_parameter_is_not_an_instrument_claim": (
                ENGINE_INSTRUMENT_IS_NOT_AN_INSTRUMENT_CLAIM
            ),
            "engine_money_columns_published_as_results": False,
            "trade_table_sweep": {
                "checked_episode_count": instrument_sweep["checked_episode_count"],
                "trade_row_count_compared": instrument_sweep[
                    "trade_row_count_compared"
                ],
                "all_identical": instrument_sweep["all_identical"],
            },
            "note": (
                "the frozen engine requires an InstrumentType argument, so it is "
                "handed its own existing default; no evidence row carries "
                "instrument_type and no published trade row carries an "
                "instrument-dependent money column"
            ),
        },
        "adapter": {
            "adapter_version": wiring.adapter_report["adapter_version"],
            "leg_count": wiring.adapter_report["leg_count"],
            "mapped_count": wiring.adapter_report["mapped_count"],
            "hold_count": wiring.adapter_report["hold_count"],
            "hold_code_counts": dict(sorted(hold_code_counts.items())),
            "next_unresolved_blocker": wiring.next_unresolved_blocker,
            "market_evidence": wiring.market_evidence_counts,
        },
        "sidecar": {
            "row_count": len(wiring.sidecar_rows),
            "hold_count": len(wiring.sidecar_holds),
            "counts": dict(wiring.sidecar_counts),
        },
        "run_level_blockers": {
            "resolved_by_this_batch": list(RUN_LEVEL_BLOCKERS_RESOLVED_BY_THIS_BATCH),
            "still_open_requiring_00_adjudication": list(
                RUN_LEVEL_BLOCKERS_STILL_OPEN
            ),
            "worked_around": False,
        },
        "cost": {
            "computable_cell_count": cost_grid["computable_cell_count"],
            "held_cell_count": cost_grid["held_cell_count"],
            "total_leg_count": cost_grid["total_leg_count"],
            "total_hold_leg_count": cost_grid["total_hold_leg_count"],
            "hold_coverage": cost_grid["hold_coverage"],
        },
        "gates": gates,
        "artifact_sha256": artifact_hashes,
    }
    (run_dir / "manifest.json").write_bytes(_json_bytes(manifest))

    verified = {name: sha256_file(run_dir / name) for name in sorted(artifact_hashes)}
    readback = {
        "schema_version": READBACK_SCHEMA_VERSION,
        "run_id": run_id,
        "status": readback_status,
        "gates": gates,
        "artifact_sha256": artifact_hashes,
        "artifact_sha256_recomputed": verified,
        "artifacts_verified": artifact_hashes == verified,
        "coverage": manifest["coverage"],
        "engine": manifest["engine"],
        "adapter": manifest["adapter"],
        "sidecar": manifest["sidecar"],
        "run_level_blockers": manifest["run_level_blockers"],
        "cost": manifest["cost"],
        "price_basis_binding": binding.as_dict(),
        "spot_checks": spot_checks,
        "instrument_sweep": instrument_sweep,
        "checkpoint": {
            "kind": "ATOMIC_SINGLE_BATCH",
            "state": "COMPLETE_AWAITING_00_QA",
            "resumable": False,
            "next_action": (
                "00 adjudicates the price-basis label and the dated instrument "
                "identity source; until then the six-matrix cost grid stays an "
                "all-HOLD ledger"
            ),
        },
        "performance_read_or_computed": False,
        "rules_changed": False,
    }
    (run_dir / "readback.json").write_bytes(_json_bytes(readback))
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdata-root", required=True, type=Path)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--run-id", default=RUN_ID_DEFAULT)
    parser.add_argument("--window-limit", type=int, default=None)
    args = parser.parse_args(argv)

    def progress(done: int, total: int, stats: Mapping[str, Any]) -> None:
        print(
            json.dumps(
                {
                    "episode_windows_done": done,
                    "episode_windows_total": total,
                    "leg_rows": stats["leg_rows"],
                }
            ),
            flush=True,
        )

    manifest = run_real_pilot(
        workdata_root=args.workdata_root,
        output_root=args.output_root,
        run_id=args.run_id,
        window_limit=args.window_limit,
        progress=progress,
    )
    print(
        json.dumps(
            {
                "run_id": manifest["run_id"],
                "readback_status": manifest["readback_status"],
                "episode_window_count": manifest["engine"]["episode_window_count"],
                "leg_rows": manifest["engine"]["leg_rows"],
                "adapter_hold_count": manifest["adapter"]["hold_count"],
                "held_cell_count": manifest["cost"]["held_cell_count"],
            }
        )
    )
    return 0 if manifest["readback_status"] == "PASS" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
