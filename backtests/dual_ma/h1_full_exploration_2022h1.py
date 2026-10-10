"""H1 full-sample exploration over the 117 official 2022 H1 selection days.

This module is the minimal extension of the accepted eight-session real pilot
(h1_real_pilot_2022_0419_0428) and its cost-inclusive leg summary
(h1_real_pilot_cost_summary).  It answers one question: when the frozen
D -> E monitoring life cycle, the frozen v0.4.1 engine and the frozen
CATHAY_WEB_028_TW_V1 cost model are fed every official 2022 H1 selection day
produced by the nine accepted 01 runs (117 unique days, 6 SUCCESS / 1
UNAVAILABLE / 1 FAILED programs per day), what do the six-matrix x 0/10/20 bps
closed-leg results look like, and which named evidence is still missing.

Nothing is re-implemented:

* the selection -> episode life cycle, the engine wiring and the strict adapter
  wiring are the accepted pilot module's own functions, called with an
  optional date -> run map so more days can be read without changing any
  default;
* the closed-leg cost rule is the accepted summary module's own
  Decimal(sell_net) - Decimal(buy_total) over the engine's own already costed
  fields, aggregated by its own Cell / aggregate_cells;
* the strict adapter is left exactly as it is.  Its allow-list is never
  widened, so every wired leg still holds HOLD_MISSING_PRICE_BASIS_EVIDENCE
  under the frozen UNADJUSTED_VENDOR_CLOSE_PROXY label.  The exploration cost
  table is produced from the closed LEG rows directly, which is why the
  adapter hold does not blank the whole grid.

The run is research-only: 2023 / 2024 data is never read, the sample is the
6/8 TDCC partial exploration sample, the price label is the unadjusted vendor
close proxy and the instrument proxy is the current company registration.  No
output here is formal historical tradable performance.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, OrderedDict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import h1_real_pilot_2022_0419_0428 as pilot
from . import h1_real_pilot_cost_summary as legcost
from .approx_h1_event_cost_adapter import PRICE_BASIS_ALLOWED


MODULE_VERSION = "dual-ma-h1-full-exploration-2022h1-v1"
RUN_ID_DEFAULT = "h1-exploration-2022h1-v1"
MANIFEST_SCHEMA_VERSION = "dual-ma-h1-full-exploration-manifest-v1"
READBACK_SCHEMA_VERSION = "dual-ma-h1-full-exploration-readback-v1"
COVERAGE_SCHEMA_VERSION = "dual-ma-h1-full-exploration-coverage-v1"
NAMED_HOLDS_SCHEMA_VERSION = "dual-ma-h1-full-exploration-named-holds-v1"
PRICE_AVAILABILITY_SCHEMA_VERSION = (
    "dual-ma-h1-full-exploration-price-availability-v1"
)
COST_SUMMARY_SCHEMA_VERSION = "dual-ma-h1-full-exploration-cost-summary-v1"
SPOT_CHECK_SCHEMA_VERSION = "dual-ma-h1-full-exploration-spot-checks-v1"

H1_FIRST_DAY = date(2022, 1, 1)
H1_LAST_DAY = date(2022, 6, 30)
LATEST_READABLE_YEAR = 2022
GAP_LIST_LIMIT = 5000

# The nine accepted 01 selection runs that together cover every official 2022
# H1 session exactly once.
PARENT_RUNS: tuple[str, ...] = (
    "run-2026-10-03-d2022-01-v1-isolated",
    "run-2026-10-03-d2022-02-v1-isolated",
    "run-2026-10-03-d2022-03-v1-isolated",
    "run-2026-10-03-d2022-04-19-v2-isolated",
    "run-2026-10-03-d2022-04-20-21-v1-isolated",
    "run-2026-10-03-d2022-04-22-28-v1-isolated",
    "run-2026-10-03-d2022-04-missing11-v1-isolated",
    "run-2026-10-03-d2022-05-v1-isolated",
    "run-2026-10-03-d2022-06-v1-isolated",
)

# The 00-brief SHA-256 of each accepted pilot_report.json.  The run 02 string in
# the brief carried one extra character; 00 confirmed the 64-character value
# used here.
PARENT_PILOT_REPORT_SHA256: Mapping[str, str] = {
    "run-2026-10-03-d2022-01-v1-isolated": (
        "1926284f56f6bd50101be5ee5cf140c33972201118e7a22048386d5bc4ea94df"
    ),
    "run-2026-10-03-d2022-02-v1-isolated": (
        "ef2842d1110c5b633f7baf6a244088fb8eaf3f1d36949cce4bdca177096ce61f"
    ),
    "run-2026-10-03-d2022-03-v1-isolated": (
        "887a8daf5684f19e34ffd58938dd290f8ecf812b87873fdd2a412f285cbd37ef"
    ),
    "run-2026-10-03-d2022-04-19-v2-isolated": (
        "683c80f61400e6caf9f42f11cb4e3056a1e8dfe781a6d66747e57314a633f866"
    ),
    "run-2026-10-03-d2022-04-20-21-v1-isolated": (
        "b0ff548981565a529e98f24b77a264af804338ead2e764ffdf1f3ddd469a7c95"
    ),
    "run-2026-10-03-d2022-04-22-28-v1-isolated": (
        "ac319c93aa406565e840cf9203e4e29902ee0d63136c8389db5e4d9cb45e1718"
    ),
    "run-2026-10-03-d2022-04-missing11-v1-isolated": (
        "5a263c8931c1116680872bb8d0fa9be455327091f28f8724a3d4be9b47e6e5b8"
    ),
    "run-2026-10-03-d2022-05-v1-isolated": (
        "f18056a114c57f866ef00202913d80acf7401e04d3db780e3fe3987e3f0b5dd3"
    ),
    "run-2026-10-03-d2022-06-v1-isolated": (
        "78aa4733f3735fa1a2560120faa0cc0641a24e609a1d9039065963beb0a70d99"
    ),
}

UNIVERSE_SOURCES_RELATIVE_PATH = (
    "data/dual_ma/universe_sources/2026/2026-09-23/"
    "dual-ma-universe-sources#ea8eddeba3577309.json"
)
UNIVERSE_SOURCES_RAW_SHA256 = (
    "41ce724fee9b00d8ceafba990a287a07c337d52b7695aee2c85abf0b74ce03ac"
)
UNIVERSE_SOURCES_SOURCE_ID = "dual-ma-universe-sources#ea8eddeba3577309"

# The frozen program-status shape of a single 2022 H1 selection day.
EXPECTED_PROGRAM_STATUS_COUNTS: Mapping[str, int] = {
    "SUCCESS": 6,
    "UNAVAILABLE": 1,
    "FAILED": 1,
}

# The strict adapter's allow-list is captured so a run can prove it was never
# widened.  The frozen C1 label is deliberately absent from it.
FROZEN_STRICT_ADAPTER_PRICE_BASIS_ALLOWED: tuple[str, ...] = tuple(
    PRICE_BASIS_ALLOWED
)


class H1ExplorationError(RuntimeError):
    """A structural problem with a declared frozen input."""


def verify_parent_runs(
    pilot_root: str | Path,
    *,
    expectations: pilot.FrozenExpectations = pilot.FROZEN_EXPECTATIONS,
) -> dict[str, Any]:
    """Read the nine accepted 01 reports and check their SHA-256 hashes."""

    pilot_root = Path(pilot_root)
    rows: list[dict[str, Any]] = []
    all_matched = True
    for run in PARENT_RUNS:
        path = pilot_root / run / pilot.PILOT_REPORT_NAME
        if not path.is_file():
            raise H1ExplorationError(f"parent pilot report is missing: {path}")
        digest = pilot.sha256_file(path)
        expected = PARENT_PILOT_REPORT_SHA256[run]
        matched = expected is None or digest == expected
        all_matched = all_matched and matched
        blocks = pilot._date_reports(pilot_root / run)
        rows.append(
            {
                "run_dir": run,
                "report_path": str(path),
                "report_sha256": digest,
                "expected_sha256": expected,
                "matched": matched,
                "date_block_count": len(blocks),
                "report_dates": [str(item.get("report_date")) for item in blocks],
            }
        )
    return {
        "schema_version": "dual-ma-h1-full-exploration-parent-runs-v1",
        "run_count": len(rows),
        "all_matched": all_matched,
        "runs": rows,
    }


def build_run_by_date(pilot_root: str | Path) -> OrderedDict[str, str]:
    """Map every reported selection date to the accepted run that produced it."""

    pilot_root = Path(pilot_root)
    mapping: OrderedDict[str, str] = OrderedDict()
    for run in PARENT_RUNS:
        run_dir = pilot_root / run
        for item in pilot._date_reports(run_dir):
            report_date = str(item.get("report_date"))
            if not report_date or report_date == "None":
                raise H1ExplorationError(f"{run}: date block without report_date")
            previous = mapping.get(report_date)
            if previous is not None and previous != run:
                raise H1ExplorationError(
                    f"{report_date} is claimed by both {previous} and {run}"
                )
            mapping[report_date] = run
    return mapping


def build_coverage(
    *,
    reports: Sequence[pilot.SelectionDateReport],
    parent: Mapping[str, Any],
    calendar: pilot.OfficialCalendar,
) -> dict[str, Any]:
    """Per-day six-SUCCESS / one-UNAVAILABLE / one-FAILED program coverage."""

    expected_sessions = [
        item.isoformat()
        for item in calendar.sessions_between(H1_FIRST_DAY, H1_LAST_DAY)
    ]
    present = sorted(report.report_date for report in reports)
    status_counts: Counter[str] = Counter()
    per_day: list[dict[str, Any]] = []
    every_day_expected_shape = True
    for report in reports:
        row = dict(report.as_coverage())
        day_counts = Counter(report.program_status.values())
        every_day_expected_shape = every_day_expected_shape and (
            dict(day_counts) == dict(EXPECTED_PROGRAM_STATUS_COUNTS)
        )
        for status, count in day_counts.items():
            status_counts[status] += count
        row["day_status_counts"] = dict(sorted(day_counts.items()))
        row["unavailable_groups"] = sorted(
            key for key, value in report.program_status.items() if value == "UNAVAILABLE"
        )
        row["failed_groups"] = sorted(
            key for key, value in report.program_status.items() if value == "FAILED"
        )
        row["success_groups"] = sorted(
            key for key, value in report.program_status.items() if value == "SUCCESS"
        )
        row["deduplicated_candidate_count"] = report.candidate_count
        per_day.append(row)
    return {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "run_count": len(PARENT_RUNS),
        "reported_date_count": len(present),
        "unique_date_count": len(set(present)),
        "first_date": present[0] if present else None,
        "last_date": present[-1] if present else None,
        "official_h1_session_count": len(expected_sessions),
        "official_h1_sessions": expected_sessions,
        "dates_are_exactly_official_h1_sessions": present == expected_sessions,
        "missing_official_sessions": [
            item for item in expected_sessions if item not in set(present)
        ],
        "unexpected_dates": [
            item for item in present if item not in set(expected_sessions)
        ],
        "program_status_counts": dict(sorted(status_counts.items())),
        "expected_program_status_per_day": dict(EXPECTED_PROGRAM_STATUS_COUNTS),
        "every_day_has_the_expected_program_status_shape": every_day_expected_shape,
        "parent_runs": dict(parent),
        "dates": per_day,
    }


def build_price_availability(
    *,
    reports: Sequence[pilot.SelectionDateReport],
    resolution: pilot.SymbolResolution,
    snapshot: pilot.FrozenPriceSnapshot,
    calendar: pilot.OfficialCalendar,
    plan: pilot.LifecyclePlan,
    gap_limit: int = GAP_LIST_LIMIT,
) -> dict[str, Any]:
    """Selected codes with / without a price series, plus per-window gaps."""

    selected_codes = sorted(
        {code for report in reports for code in report.code_programs}
    )
    with_price: list[str] = []
    without_price: list[str] = []
    for code in selected_codes:
        symbol = resolution.symbol_by_code.get(code)
        bars = snapshot.bars_by_symbol.get(symbol) if symbol else None
        if bars:
            with_price.append(code)
        else:
            without_price.append(code)

    bar_dates = {
        symbol: set(snapshot.bar_dates(symbol)) for symbol in snapshot.bars_by_symbol
    }
    gaps: list[dict[str, Any]] = []
    gap_by_code: Counter[str] = Counter()
    for episode in plan.episodes:
        available = bar_dates.get(episode.symbol, set())
        for session in calendar.sessions_between(
            episode.window.regime_window_start, episode.window.regime_window_end
        ):
            if session in available:
                continue
            gap_by_code[episode.code] += 1
            if len(gaps) < gap_limit:
                gaps.append(
                    {
                        "code": episode.code,
                        "symbol": episode.symbol,
                        "strategy": episode.strategy,
                        "episode_ordinal": episode.window.ordinal,
                        "session": session.isoformat(),
                    }
                )
    truncated = sum(gap_by_code.values()) > len(gaps)
    return {
        "schema_version": PRICE_AVAILABILITY_SCHEMA_VERSION,
        "selected_code_count": len(selected_codes),
        "codes_with_a_price_series": len(with_price),
        "codes_without_a_price_series": len(without_price),
        "codes_without_a_price_series_list": without_price,
        "conflicting_symbol_codes": list(resolution.conflicting_codes),
        "unknown_symbol_codes": list(resolution.unknown_codes),
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_content_sha256": snapshot.content_sha256,
        "snapshot_first_session": snapshot.first_session.isoformat(),
        "snapshot_last_session": snapshot.last_session.isoformat(),
        "gap_definition": (
            "an official session inside one activated episode's regime window "
            "for which the selected symbol has no bar in the frozen Fugle v2 "
            "snapshot"
        ),
        "gap_row_count": sum(gap_by_code.values()),
        "gap_code_count": len(gap_by_code),
        "gap_counts_by_code": dict(sorted(gap_by_code.items())),
        "gap_rows": gaps,
        "gap_rows_truncated": truncated,
        "gap_row_limit": gap_limit,
    }


def build_cost_tables(
    *, engine_events_path: str | Path, universe_sources_path: str | Path
) -> tuple[dict[str, Any], dict[str, Any], Any, Any]:
    """The 18-cell closed-leg table over the frozen engine's own costed fields."""

    scan = legcost.scan_engine_events(engine_events_path)
    registry = legcost.load_universe_code_registry(universe_sources_path)
    basis = legcost.check_instrument_basis(scan.code_suffixes, registry)
    held = frozenset(
        set(basis.unresolved_codes)
        | set(basis.suffix_conflicts)
        | set(basis.exchange_mismatches)
    )
    cells = legcost.aggregate_cells(scan.legs, held_codes=held)
    rows = [cells[key].as_dict() for key in legcost.cell_keys()]
    closed = sum(cell.closed_leg_count for cell in cells.values())
    costed = sum(cell.costed_closed_leg_count for cell in cells.values())
    open_count = sum(cell.open_leg_count for cell in cells.values())
    exception = sum(cell.exception_leg_count for cell in cells.values())
    identity = sum(cell.identity_hold_leg_count for cell in cells.values())
    unmapped = len(scan.unmapped_rows)
    totals = {
        "cell_count": len(rows),
        "leg_row_count": scan.leg_row_count,
        "closed_leg_count": closed,
        "costed_closed_leg_count": costed,
        "open_censored_leg_count": open_count,
        "exception_leg_count": exception,
        "identity_hold_leg_count": identity,
        "unmapped_leg_count": unmapped,
        "identity_hold_codes": sorted(held),
        "closed_open_exception_identity_partition_leg_rows": (
            closed + open_count + exception + identity + unmapped
            == scan.leg_row_count
        ),
        "entry_date_min": min(scan.entry_dates) if scan.entry_dates else None,
        "entry_date_max": max(scan.entry_dates) if scan.entry_dates else None,
        "exit_date_min": min(scan.exit_dates) if scan.exit_dates else None,
        "exit_date_max": max(scan.exit_dates) if scan.exit_dates else None,
        "cross_cell_sums_sum_over_overlapping_counterfactual_variants": {
            "warning": (
                "the six matrices share the same episodes and the three slippage "
                "scenarios are the same legs re-priced, so any sum across cells "
                "double counts.  These sums are reporting aids only and are NOT a "
                "portfolio result"
            ),
            "costed_net_pnl_by_slippage": {
                str(bps): legcost._decimal_str(
                    sum(
                        (cells[(matrix, bps)].net_pnl for matrix in legcost.MATRIX_IDS),
                        Decimal("0"),
                    )
                )
                for bps in legcost.SLIPPAGE_SCENARIOS
            },
            "costed_net_pnl_by_matrix": {
                matrix: legcost._decimal_str(
                    sum(
                        (
                            cells[(matrix, bps)].net_pnl
                            for bps in legcost.SLIPPAGE_SCENARIOS
                        ),
                        Decimal("0"),
                    )
                )
                for matrix in legcost.MATRIX_IDS
            },
        },
    }
    summary = {
        "schema_version": COST_SUMMARY_SCHEMA_VERSION,
        "module_version": MODULE_VERSION,
        "research_only": True,
        "matrix_ids": list(legcost.MATRIX_IDS),
        "slippage_scenarios": list(legcost.SLIPPAGE_SCENARIOS),
        "cells": rows,
        "totals": totals,
        "instrument_basis": basis.as_dict(),
        "scanned_engine_events": {
            "path": scan.path,
            "raw_sha256": scan.raw_sha256,
            "row_count": scan.row_count,
            "kind_counts": dict(sorted(scan.kind_counts.items())),
            "leg_row_count": scan.leg_row_count,
            "open_censored_leg_count": scan.open_censored_count,
        },
        "note": (
            "costed_net_pnl is Decimal(sell_net) - Decimal(buy_total) from the "
            "frozen engine's own already costed LEG fields over closed legs only; "
            "open_censored legs are listed separately and are never a zero return"
        ),
    }
    return summary, totals, basis, scan


def build_named_holds(
    *,
    plan: pilot.LifecyclePlan,
    wiring: Any,
    basis: Any,
    totals: Mapping[str, Any],
) -> dict[str, Any]:
    lifecycle_reason = Counter(
        str(record["reason"])
        for record in plan.selection_records
        if str(record["status"]) != "ACTIVATED"
    )
    lifecycle_status = Counter(
        str(record["status"])
        for record in plan.selection_records
        if str(record["status"]) != "ACTIVATED"
    )
    adapter_hold_code = Counter(
        str(row.get("hold_code")) for row in wiring.adapter_report["holds"]
    )
    sidecar_hold_code = Counter(str(row.get("code")) for row in wiring.sidecar_holds)
    return {
        "schema_version": NAMED_HOLDS_SCHEMA_VERSION,
        "lifecycle": {
            "non_activated_selection_event_count": sum(lifecycle_status.values()),
            "status_counts": dict(sorted(lifecycle_status.items())),
            "reason_counts": dict(sorted(lifecycle_reason.items())),
            "named_hold_counts": {
                key: value
                for key, value in sorted(lifecycle_reason.items())
                if key.startswith("HOLD")
            },
            "skipped_counts": {
                key: value
                for key, value in sorted(lifecycle_reason.items())
                if key.startswith("SKIPPED")
            },
        },
        "strict_adapter": {
            "leg_count": wiring.adapter_report["leg_count"],
            "mapped_count": wiring.adapter_report["mapped_count"],
            "hold_count": wiring.adapter_report["hold_count"],
            "hold_code_counts": dict(sorted(adapter_hold_code.items())),
            "sidecar_hold_code_counts": dict(sorted(sidecar_hold_code.items())),
            "next_unresolved_blocker": wiring.next_unresolved_blocker,
            "strict_allow_list_unchanged": (
                tuple(PRICE_BASIS_ALLOWED) == FROZEN_STRICT_ADAPTER_PRICE_BASIS_ALLOWED
            ),
        },
        "cost_identity": {
            "identity_hold_leg_count": totals["identity_hold_leg_count"],
            "identity_hold_codes": list(totals["identity_hold_codes"]),
            "unresolved_codes": list(basis.unresolved_codes),
            "suffix_conflicts": list(basis.suffix_conflicts),
            "exchange_mismatches": list(basis.exchange_mismatches),
        },
        "note": (
            "named HOLDs are recorded, never silently dropped or zero-filled; a "
            "selection event that is selected but cannot run stays a named hold "
            "and every wired leg keeps its own adapter hold code"
        ),
    }


def build_spot_checks(
    *, legs: Sequence[Mapping[str, Any]], plan: pilot.LifecyclePlan, limit: int = 3
) -> dict[str, Any]:
    closed = [
        row
        for row in legs
        if not row.get("open_censored") and row.get("exit_date") and row.get("sell_net")
    ]
    ordered = sorted(
        closed,
        key=lambda row: (
            str(row.get("entry_date")),
            str(row.get("code")),
            str(row.get("matrix_id")),
            int(row.get("slippage_bps") or 0),
            str(row.get("leg_id")),
        ),
    )
    checks: list[dict[str, Any]] = []
    for row in ordered[:limit]:
        sell = legcost._parse_decimal(row.get("sell_net"))
        buy = legcost._parse_decimal(row.get("buy_total"))
        net = None if sell is None or buy is None else sell - buy
        checks.append(
            {
                "kind": "D_TO_E_TO_T_CLOSED_LEG",
                "code": row.get("code"),
                "symbol": row.get("symbol"),
                "strategy": row.get("strategy"),
                "matrix_id": row.get("matrix_id"),
                "slippage_bps": row.get("slippage_bps"),
                "leg_id": row.get("leg_id"),
                "selection_date_D": row.get("source_selection_date"),
                "monitor_effective_date_E": row.get("monitor_effective_from"),
                "regime_window_start": row.get("regime_window_start"),
                "regime_window_end": row.get("regime_window_end"),
                "entry_signal_date_T": row.get("entry_date"),
                "entry_reference_close": row.get("entry_reference_close"),
                "entry_fill_price": row.get("entry_fill_price"),
                "exit_date": row.get("exit_date"),
                "exit_reason": row.get("exit_reason"),
                "exit_reference_close": row.get("exit_reference_close"),
                "exit_fill_price": row.get("exit_fill_price"),
                "shares": row.get("shares"),
                "sell_net": row.get("sell_net"),
                "buy_total": row.get("buy_total"),
                "hand_calc_sell_net_minus_buy_total": (
                    legcost._decimal_str(net) if net is not None else None
                ),
                "note": (
                    "signal and fill share the T session close (EOD_CLOSE_PROXY_V1); "
                    "sell_net already nets the sell commission and the STOCK sell "
                    "tax, buy_total already nets the buy commission"
                ),
            }
        )
    for record in plan.selection_records:
        if len(checks) >= limit:
            break
        if str(record["status"]) != "ACTIVATED":
            checks.append(
                {
                    "kind": "SELECTED_BUT_NOT_TRADED",
                    "code": record.get("code"),
                    "symbol": record.get("symbol"),
                    "strategy": record.get("strategy"),
                    "selection_date_D": _iso(record.get("selection_date")),
                    "monitor_effective_date_E": _iso(record.get("effective_date")),
                    "status": record.get("status"),
                    "reason": record.get("reason"),
                    "note": (
                        "the selection event was held or skipped before the engine "
                        "could open a monitored episode"
                    ),
                }
            )
    return {
        "schema_version": SPOT_CHECK_SCHEMA_VERSION,
        "check_count": len(checks),
        "checks": checks,
    }


def _iso(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def load_engine_events(path: str | Path) -> tuple[list[dict], list[dict]]:
    legs: list[dict] = []
    transactions: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("kind") == "LEG":
                legs.append(row)
            elif row.get("kind") == "TRANSACTION":
                transactions.append(row)
    return legs, transactions


def run_h1_exploration(
    *,
    workdata_root: str | Path,
    output_root: str | Path | None = None,
    run_id: str = RUN_ID_DEFAULT,
    expectations: pilot.FrozenExpectations = pilot.FROZEN_EXPECTATIONS,
    runner_commit: str | None = None,
    episode_limit: int | None = None,
    progress: Any = None,
    reuse_engine_events: bool = False,
) -> dict[str, Any]:
    workdata_root = Path(workdata_root)
    output_root = (
        Path(output_root)
        if output_root is not None
        else workdata_root / "02-causal-backtest"
    )
    run_dir = output_root / run_id
    pilot_root = workdata_root / pilot.PILOT_ROOT_RELATIVE

    parent = verify_parent_runs(pilot_root, expectations=expectations)
    run_by_date = build_run_by_date(pilot_root)
    selection_dates = tuple(sorted(run_by_date))
    if len(selection_dates) != len(set(selection_dates)):
        raise H1ExplorationError("duplicate selection dates across parent runs")

    calendar = pilot.load_official_calendar(
        workdata_root / pilot.CALENDAR_RELATIVE_PATH, expectations=expectations
    )
    binding = pilot.load_price_basis_binding(
        workdata_root / pilot.C1_BINDING_RELATIVE_PATH, expectations=expectations
    )
    reports = pilot.load_pilot_selection_dates(
        pilot_root, selection_dates=selection_dates, run_by_date=run_by_date
    )
    resolution = pilot.resolve_symbols(pilot_root, reports)
    snapshot = pilot.load_price_snapshot(
        workdata_root / pilot.PRICE_SNAPSHOT_RELATIVE_PATH,
        list(resolution.symbol_by_code.values()),
        expectations=expectations,
    )
    plan = pilot.build_lifecycle_plan(
        reports=reports,
        resolution=resolution,
        snapshot=snapshot,
        calendar=calendar,
        window_limit=episode_limit,
    )
    universe_sources_path = workdata_root / UNIVERSE_SOURCES_RELATIVE_PATH
    if not universe_sources_path.is_file():
        raise H1ExplorationError(
            f"frozen universe sources are missing: {universe_sources_path}"
        )
    universe_sha = pilot.sha256_file(universe_sources_path)
    if (
        expectations.calendar_raw_sha256 is not None
        and universe_sha != UNIVERSE_SOURCES_RAW_SHA256
    ):
        raise H1ExplorationError(
            f"universe sources sha256 {universe_sha} != expected "
            f"{UNIVERSE_SOURCES_RAW_SHA256}"
        )

    engine_events_exist = (run_dir / "engine_events.jsonl").is_file()
    if run_dir.exists() and not (reuse_engine_events and engine_events_exist):
        raise H1ExplorationError(
            f"refusing to overwrite an existing run directory: {run_dir}"
        )
    run_dir.mkdir(parents=True, exist_ok=True)

    commit = runner_commit or pilot.resolve_runner_commit(
        Path(__file__).resolve().parents[2]
    )["runner_commit"]

    reused = False
    if reuse_engine_events and engine_events_exist:
        legs, transactions = load_engine_events(run_dir / "engine_events.jsonl")
        engine_stats = {
            "episode_windows_run": len(plan.episodes),
            "matrix_slippage_runs": None,
            "leg_rows": len(legs),
            "transaction_rows": len(transactions),
        }
        reused = True
        if progress is not None:
            progress("engine_reused", len(plan.episodes), len(plan.episodes), engine_stats)
    else:
        engine_progress = (
            None
            if progress is None
            else (lambda done, total, stats: progress("engine", done, total, stats))
        )
        legs, transactions, engine_stats = pilot.run_all_episodes(
            plan=plan,
            snapshot=snapshot,
            run_id=run_id,
            runner_commit=commit,
            progress=engine_progress,
        )
        (run_dir / "episode_windows.json").write_bytes(
            pilot._json_bytes(
                {
                    "schema_version": pilot.EPISODE_REGISTRY_SCHEMA_VERSION,
                    "run_id": run_id,
                    "window_count": len(plan.episodes),
                    "windows": [item.registry_row() for item in plan.episodes],
                }
            )
        )
        (run_dir / "engine_events.jsonl").write_bytes(
            pilot._jsonl_bytes((*legs, *transactions))
        )

    registry_index, _ = pilot.load_lifecycle_registry(run_dir / "episode_windows.json")
    _, _, kind_counts = pilot.read_engine_events(run_dir / "engine_events.jsonl")

    market = pilot.build_market_evidence(
        snapshot=snapshot,
        calendar=calendar,
        price_basis=binding.declared_price_basis,
        binding=binding,
        decision_cutoff=snapshot.last_session,
    )
    if progress is not None:
        progress("adapter_start", 0, 1, {})
    wiring = pilot.wire_legs_and_adapt(
        leg_rows=legs,
        transaction_rows=transactions,
        registry_index=registry_index,
        market=market,
    )
    if progress is not None:
        progress(
            "adapter_done", 1, 1, {"hold_count": wiring.adapter_report["hold_count"]}
        )
    leg_inputs = list(wiring.adapter_report["leg_inputs"])
    cost_report = pilot.build_report(leg_inputs)

    cost_summary, cost_totals, basis, scan = build_cost_tables(
        engine_events_path=run_dir / "engine_events.jsonl",
        universe_sources_path=universe_sources_path,
    )
    coverage = build_coverage(reports=reports, parent=parent, calendar=calendar)
    price_availability = build_price_availability(
        reports=reports,
        resolution=resolution,
        snapshot=snapshot,
        calendar=calendar,
        plan=plan,
    )
    named_holds = build_named_holds(
        plan=plan, wiring=wiring, basis=basis, totals=cost_totals
    )
    trade_rows = pilot.build_trade_rows(legs)
    exception_rows = pilot.build_exception_rows(
        plan=plan,
        adapter_report=wiring.adapter_report,
        sidecar_holds=wiring.sidecar_holds,
    )
    spot_checks = build_spot_checks(legs=legs, plan=plan)

    cell_header = [
        "matrix_id",
        "slippage_bps",
        "leg_count",
        "closed_leg_count",
        "open_leg_count",
        "exception_leg_count",
        "identity_hold_leg_count",
        "costed_closed_leg_count",
        "costed_net_pnl_sum",
        "costed_mean_net_pnl_per_leg",
        "win_count",
        "win_rate",
    ]
    cell_rows = [[cell[name] for name in cell_header] for cell in cost_summary["cells"]]
    open_header = list(legcost.LEG_IDENTIFYING_FIELDS) + ["note"]
    open_rows = [[row.get(name) for name in open_header] for row in scan.open_rows]

    payloads: dict[str, bytes] = {
        "input_coverage.json": pilot._json_bytes(coverage),
        "selection_episode_ledger.json": pilot._json_bytes(
            {
                "schema_version": pilot.EPISODE_REGISTRY_SCHEMA_VERSION,
                "selection_record_count": len(plan.selection_records),
                "records": list(plan.selection_records),
                "windows": [item.registry_row() for item in plan.episodes],
            }
        ),
        "named_holds.json": pilot._json_bytes(named_holds),
        "price_availability.json": pilot._json_bytes(price_availability),
        "cost_summary.json": pilot._json_bytes(cost_summary),
        "cost_summary.csv": legcost._csv_bytes(cell_header, cell_rows),
        "open_censored_rows.csv": legcost._csv_bytes(open_header, open_rows),
        "exception_rows.jsonl": legcost._jsonl_bytes(
            scan.exception_rows + scan.unmapped_rows
        ),
        "adapter_hold_ledger.json": pilot._json_bytes(
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
        "trade_rows.jsonl": pilot._jsonl_bytes(trade_rows),
        "spot_checks.json": pilot._json_bytes(spot_checks),
        "price_basis_binding.json": pilot._json_bytes(binding.as_dict()),
        "parent_runs.json": pilot._json_bytes(parent),
    }
    for name, blob in payloads.items():
        (run_dir / name).write_bytes(blob)

    h1_sessions = calendar.sessions_between(H1_FIRST_DAY, H1_LAST_DAY)
    holds = named_holds["strict_adapter"]
    gates = {
        "parent_reports_match_declared_sha256": (
            "PASS" if parent["all_matched"] else "FAIL"
        ),
        "selection_dates_are_exactly_official_2022h1_sessions": (
            "PASS" if coverage["dates_are_exactly_official_h1_sessions"] else "FAIL"
        ),
        "every_day_has_the_expected_program_status_shape": (
            "PASS"
            if coverage["every_day_has_the_expected_program_status_shape"]
            else "FAIL"
        ),
        "calendar_bound": "PASS" if calendar.sessions else "FAIL",
        "price_snapshot_never_reaches_2023": (
            "PASS" if snapshot.last_session.year <= LATEST_READABLE_YEAR else "FAIL"
        ),
        "universe_sources_match_declared_sha256": (
            "PASS" if universe_sha == UNIVERSE_SOURCES_RAW_SHA256 else "FAIL"
        ),
        "engine_events_kinds_expected": (
            "PASS" if set(kind_counts) <= {"LEG", "TRANSACTION"} else "FAIL"
        ),
        "price_basis_passed_through_unmodified": (
            "PASS"
            if binding.declared_price_basis == pilot.C1_DECLARED_PRICE_BASIS
            else "FAIL"
        ),
        "strict_adapter_allow_list_unchanged": (
            "PASS"
            if tuple(PRICE_BASIS_ALLOWED) == FROZEN_STRICT_ADAPTER_PRICE_BASIS_ALLOWED
            else "FAIL"
        ),
        "no_cost_quantity_produced_by_the_strict_adapter": (
            "PASS" if not cost_report.get("legs") else "FAIL"
        ),
        "every_derived_leg_has_a_named_adapter_hold": (
            "PASS" if holds["hold_count"] == holds["leg_count"] else "FAIL"
        ),
        "cost_grid_has_eighteen_cells": (
            "PASS" if cost_totals["cell_count"] == 18 else "FAIL"
        ),
        "cost_rows_partition_the_leg_rows": (
            "PASS"
            if cost_totals["closed_open_exception_identity_partition_leg_rows"]
            else "FAIL"
        ),
        "no_unmapped_leg_row": (
            "PASS" if cost_totals["unmapped_leg_count"] == 0 else "FAIL"
        ),
    }
    readback_status = (
        "PASS" if all(value == "PASS" for value in gates.values()) else "HOLD"
    )

    artifact_hashes = {
        name: pilot.sha256_bytes(blob) for name, blob in sorted(payloads.items())
    }
    for extra in ("episode_windows.json", "engine_events.jsonl"):
        artifact_hashes[extra] = pilot.sha256_file(run_dir / extra)

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "module_version": MODULE_VERSION,
        "run_id": run_id,
        "note": "No wall-clock timestamp is recorded so the run stays reproducible.",
        "status": "PASS" if readback_status == "PASS" else "HOLD",
        "readback_status": readback_status,
        "workdata_root": str(workdata_root),
        "research_only": True,
        "point_in_time_verified": False,
        "formal_historical_performance": False,
        "performance_read_or_computed": True,
        "rules_changed": False,
        "selection_program_modified": False,
        "sample_definition": (
            "the nine accepted 01 selection runs for every official 2022 H1 "
            "session (2022-01-03 .. 2022-06-30); a 6/8 TDCC partial exploration "
            "sample; 2023 / 2024 data is never read and no formal performance "
            "label is produced"
        ),
        "data_labels": {
            "price_basis": pilot.C1_DECLARED_PRICE_BASIS,
            "price_basis_is_official_close": False,
            "engine_instrument_parameter": pilot.ENGINE_INSTRUMENT_PARAMETER.value,
            "instrument_is_a_current_registration_proxy": True,
            "dated_2022_instrument_identity_evidence": False,
            "cost_model": pilot.COST_MODEL,
            "execution_model": pilot.EXECUTION_MODEL,
            "strategy_version": pilot.STRATEGY_VERSION,
        },
        "runner_commit": commit,
        "engine_events_reused": reused,
        "inputs": {
            "official_calendar": calendar.as_binding(),
            "price_snapshot_content_sha256": snapshot.content_sha256,
            "price_snapshot_manifest_file_sha256": snapshot.manifest_file_sha256,
            "price_basis_binding": binding.as_dict(),
            "universe_sources": {
                "path": str(universe_sources_path),
                "raw_sha256": universe_sha,
                "source_id": UNIVERSE_SOURCES_SOURCE_ID,
            },
            "parent_runs": parent,
        },
        "coverage": {
            "reported_date_count": coverage["reported_date_count"],
            "unique_date_count": coverage["unique_date_count"],
            "official_h1_session_count": len(h1_sessions),
            "dates_are_exactly_official_h1_sessions": coverage[
                "dates_are_exactly_official_h1_sessions"
            ],
            "program_status_counts": coverage["program_status_counts"],
            "first_date": coverage["first_date"],
            "last_date": coverage["last_date"],
        },
        "lifecycle": plan.as_dict(),
        "engine": {
            "episode_window_count": len(plan.episodes),
            "matrix_slippage_runs": engine_stats["matrix_slippage_runs"],
            "leg_rows": engine_stats["leg_rows"],
            "transaction_rows": engine_stats["transaction_rows"],
            "engine_event_kind_counts": dict(sorted(kind_counts.items())),
        },
        "cost": {
            "cell_count": cost_totals["cell_count"],
            "leg_row_count": cost_totals["leg_row_count"],
            "closed_leg_count": cost_totals["closed_leg_count"],
            "costed_closed_leg_count": cost_totals["costed_closed_leg_count"],
            "open_censored_leg_count": cost_totals["open_censored_leg_count"],
            "exception_leg_count": cost_totals["exception_leg_count"],
            "identity_hold_leg_count": cost_totals["identity_hold_leg_count"],
            "costed_net_pnl_is_never_summed_into_one_portfolio_number": True,
        },
        "holds": {
            "lifecycle_named_hold_counts": named_holds["lifecycle"]["named_hold_counts"],
            "strict_adapter_hold_counts": holds["hold_code_counts"],
            "cost_identity_hold_leg_count": cost_totals["identity_hold_leg_count"],
        },
        "price_availability": {
            "selected_code_count": price_availability["selected_code_count"],
            "codes_with_a_price_series": price_availability[
                "codes_with_a_price_series"
            ],
            "codes_without_a_price_series": price_availability[
                "codes_without_a_price_series"
            ],
            "gap_row_count": price_availability["gap_row_count"],
            "gap_code_count": price_availability["gap_code_count"],
        },
        "gates": gates,
        "artifact_sha256": artifact_hashes,
    }
    (run_dir / "manifest.json").write_bytes(pilot._json_bytes(manifest))

    verified = {
        name: pilot.sha256_file(run_dir / name) for name in sorted(artifact_hashes)
    }
    readback = {
        "schema_version": READBACK_SCHEMA_VERSION,
        "module_version": MODULE_VERSION,
        "run_id": run_id,
        "status": readback_status,
        "research_only": True,
        "point_in_time_verified": False,
        "formal_historical_performance": False,
        "gates": gates,
        "coverage": manifest["coverage"],
        "lifecycle": manifest["lifecycle"],
        "engine": manifest["engine"],
        "cost": manifest["cost"],
        "holds": manifest["holds"],
        "price_availability": manifest["price_availability"],
        "cost_cells": cost_summary["cells"],
        "cost_totals": cost_totals,
        "instrument_basis": basis.as_dict(),
        "named_holds": named_holds,
        "spot_checks": spot_checks,
        "source_transparency": {
            "price_basis": pilot.C1_DECLARED_PRICE_BASIS,
            "unadjusted_vendor_close_proxy": True,
            "engine_instrument_parameter": pilot.ENGINE_INSTRUMENT_PARAMETER.value,
            "instrument_is_a_current_registration_proxy": True,
            "dated_2022_instrument_identity_evidence": False,
            "tdcc_partial_exploration_sample": True,
            "programs_success_per_day": 6,
            "programs_unavailable_per_day": 1,
            "programs_failed_per_day": 1,
            "not_formal_historical_tradable_performance": True,
        },
        "artifact_sha256": artifact_hashes,
        "artifact_sha256_recomputed": verified,
        "artifacts_verified": artifact_hashes == verified,
        "checkpoint": {
            "kind": "ATOMIC_FULL_H1_EXPLORATION",
            "state": "COMPLETE_AWAITING_00_QA",
            "resumable": True,
            "resume_hint": (
                "re-run with --reuse-engine-events to rebuild every derived "
                "artifact from the already written engine_events.jsonl without "
                "re-running the frozen engine"
            ),
            "next_action": (
                "00 reviews the 18-cell research_only H1 exploration; the strict "
                "adapter price-basis label and the dated instrument identity "
                "source still need 00 adjudication before any formal use"
            ),
        },
    }
    (run_dir / "readback.json").write_bytes(pilot._json_bytes(readback))
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdata-root", required=True, type=Path)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--run-id", default=RUN_ID_DEFAULT)
    parser.add_argument("--episode-limit", type=int, default=None)
    parser.add_argument("--reuse-engine-events", action="store_true")
    args = parser.parse_args(argv)

    def progress(stage: str, done: int, total: int, stats: Mapping[str, Any]) -> None:
        print(
            json.dumps(
                {
                    "stage": stage,
                    "done": done,
                    "total": total,
                    "hold_count": stats.get("hold_count"),
                }
            ),
            flush=True,
        )

    manifest = run_h1_exploration(
        workdata_root=args.workdata_root,
        output_root=args.output_root,
        run_id=args.run_id,
        episode_limit=args.episode_limit,
        progress=progress,
        reuse_engine_events=args.reuse_engine_events,
    )
    print(
        json.dumps(
            {
                "run_id": manifest["run_id"],
                "readback_status": manifest["readback_status"],
                "episode_window_count": manifest["engine"]["episode_window_count"],
                "leg_rows": manifest["engine"]["leg_rows"],
                "closed_leg_count": manifest["cost"]["closed_leg_count"],
                "open_censored_leg_count": manifest["cost"]["open_censored_leg_count"],
                "costed_closed_leg_count": manifest["cost"]["costed_closed_leg_count"],
            }
        )
    )
    return 0 if manifest["readback_status"] == "PASS" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
