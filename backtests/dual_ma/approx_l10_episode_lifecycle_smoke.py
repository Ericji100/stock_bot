"""Episode-lifecycle engineering smoke for the four-day L10 selections.

The frozen intent for the 2022 H1 milestone is:

* a selection day D puts the symbol under monitoring from the next official
  session (D+1);
* once an episode has started it keeps being monitored, even when the symbol is
  absent from later daily lists, until that strategy's fast/slow regime fails;
* repeated selections never restart an active episode, refresh its leg budget or
  create an extra mother entry;
* after the episode ends, a later selection may start a new episode;
* medium (5/21) and long (21/144) keep independent state for the same symbol.

This module replaces the earlier practice of cutting monitoring into contiguous
"eligible segments", which truncated 654 of 768 legs at the last eligible day.
It reuses the verified projection helpers (lineage, stop source from the entry
decision and reclaim signal, performance isolation, prefix replay) and writes to
a new run id; existing artefacts stay immutable.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from .approx_daily_engineering_replay import (
    CALENDAR_RELATIVE_PATH,
    canonical_bytes,
    canonical_sha256,
    sha256_file,
)
from .approx_l10_witness_smoke import (
    DECISION_FIELDS,
    LEG_FIELDS,
    MONITOR_END,
    MONITOR_START,
    TRANSACTION_FIELDS,
    _leg_stop_lineage,
    _lineage_fields,
    _manifest_for,
    _run_one,
    _stop_lineage_index,
    audit_events,
    build_monitor_lineage,
    economic_event_key,
    frozen_rule_gates,
    load_verified_l10,
    prefix_replay,
    project_fields,
    resolve_price_binding,
    resolve_runner_commit,
)
from .causal_price_adapter import load_fugle_causal_raw_bundle
from .causal_availability_v2 import load_frozen_availability_v2
from .engine import MATRIX_GROUPS
from .models import AddVariant, Bar, ExitVariant, InstrumentType, StrategyKind
from stock_ai_bot.data_sources.causal_price_contract import AvailabilityStatus


MODULE_VERSION = "APPROX_L10_EPISODE_LIFECYCLE_SMOKE_V0_1"
SMOKE_SCHEMA = "dual-ma-approx-l10-episode-lifecycle-smoke-v0.1"
REQUIRED_WARMUP_BARS = 144
# Only classifications the frozen availability reader accepts may explain an
# absent bar; anything else is an unexplained gap and must not be crossed.
PROVEN_NON_TRADING_STATUSES = frozenset(
    {AvailabilityStatus.SUSPENDED, AvailabilityStatus.NO_TRANSACTION}
)
# Frozen regime definition: fast just above slow (same rule the pilot window used).
REGIME_WINDOWS: Mapping[StrategyKind, tuple[int, int]] = {
    StrategyKind.MEDIUM: (5, 21),
    StrategyKind.LONG: (21, 144),
}


class EpisodeLifecycleError(RuntimeError):
    code = "APPROX_L10_EPISODE_LIFECYCLE_ERROR"


@dataclass(frozen=True)
class SymbolCoverage:
    """Whether every official session between a symbol's first and last bar is explained."""

    code: str
    symbol: str
    status: str
    reason: str
    proven_non_trading_sessions: int
    unexplained_gaps: tuple[date, ...]
    usable_last_session: date | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "symbol": self.symbol,
            "status": self.status,
            "reason": self.reason,
            "proven_non_trading_sessions": self.proven_non_trading_sessions,
            "unexplained_gap_count": len(self.unexplained_gaps),
            "unexplained_gap_examples": [day.isoformat() for day in self.unexplained_gaps[:5]],
            "usable_last_session": self.usable_last_session,
        }


def evaluate_symbol_coverage(
    *,
    code: str,
    symbol: str,
    bar_dates: Sequence[date],
    sessions: Sequence[date],
    availability: Mapping[tuple[str, date], Any],
) -> SymbolCoverage:
    """Explain every official session between the first and last delivered bar.

    A missing bar is acceptable only when the frozen availability snapshot
    carries an official suspension or no-transaction receipt for that symbol and
    session.  Any other absence is an unexplained gap: the symbol is held and its
    usable series stops before the gap so later bars are never chained onto a
    broken price history.
    """

    ordered = sorted(bar_dates)
    if not ordered:
        return SymbolCoverage(code, symbol, "HOLD", "MISSING_OHLC", 0, (), None)
    first, last = ordered[0], ordered[-1]
    present = set(ordered)
    gaps: list[date] = []
    proven = 0
    for day in sessions:
        if day < first or day > last or day in present:
            continue
        record = availability.get((symbol, day))
        if record is not None and record.status in PROVEN_NON_TRADING_STATUSES:
            proven += 1
        else:
            gaps.append(day)
    if not gaps:
        return SymbolCoverage(code, symbol, "COVERED", "OK", proven, (), last)
    usable = [day for day in ordered if day < gaps[0]]
    return SymbolCoverage(
        code=code,
        symbol=symbol,
        status="HOLD",
        reason="UNEXPLAINED_PRICE_GAP",
        proven_non_trading_sessions=proven,
        unexplained_gaps=tuple(gaps),
        usable_last_session=usable[-1] if usable else None,
    )


def truncate_to_usable(coverage: SymbolCoverage, bars: Sequence[Bar]) -> list[Bar]:
    """Drop bars after the first unexplained gap; nothing may cross it."""

    if coverage.status != "HOLD" or coverage.usable_last_session is None:
        return list(bars)
    return [bar for bar in bars if bar.date <= coverage.usable_last_session]


@dataclass(frozen=True)
class EpisodeWindow:
    code: str
    strategy: str
    ordinal: int
    monitor_effective_from: date
    regime_window_start: date
    regime_window_end: date
    source_selection_date: date
    ended_by_regime: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "strategy": self.strategy,
            "episode_ordinal": self.ordinal,
            "monitor_effective_from": self.monitor_effective_from,
            "regime_window_start": self.regime_window_start,
            "regime_window_end": self.regime_window_end,
            "source_selection_date": self.source_selection_date,
            "ended_by_regime": self.ended_by_regime,
        }


def regime_series(bars: Sequence[Bar], strategy: StrategyKind) -> dict[date, bool]:
    """Daily fast>slow regime for one strategy, from the frozen SMA definition."""

    fast_window, slow_window = REGIME_WINDOWS[strategy]
    closes = [bar.close for bar in bars]
    series: dict[date, bool] = {}
    for index, bar in enumerate(bars):
        if index + 1 < slow_window:
            continue
        fast = sum(closes[index + 1 - fast_window : index + 1]) / fast_window
        slow = sum(closes[index + 1 - slow_window : index + 1]) / slow_window
        series[bar.date] = fast > slow
    return series


def regime_values(
    bars: Sequence[Bar], strategy: StrategyKind
) -> dict[date, tuple[Decimal, Decimal, bool]]:
    """Fast average, slow average and the strict fast>slow verdict per session."""

    fast_window, slow_window = REGIME_WINDOWS[strategy]
    closes = [bar.close for bar in bars]
    values: dict[date, tuple[Decimal, Decimal, bool]] = {}
    for index, bar in enumerate(bars):
        if index + 1 < slow_window:
            continue
        fast = sum(closes[index + 1 - fast_window : index + 1]) / fast_window
        slow = sum(closes[index + 1 - slow_window : index + 1]) / slow_window
        values[bar.date] = (fast, slow, fast > slow)
    return values


def episode_windows(
    *,
    code: str,
    strategy: StrategyKind,
    selections: Sequence[tuple[date, date]],
    regime: Mapping[date, bool],
    sessions: Sequence[date] | None = None,
    bar_dates: Sequence[date] | None = None,
    regime_detail: Mapping[date, tuple[Decimal, Decimal, bool]] | None = None,
) -> tuple[list[EpisodeWindow], list[dict[str, Any]]]:
    """Fail-closed selection to episode activation (life-cycle contract v1).

    A selection event D may only start monitoring at its next official session E
    when that strategy's strict fast>slow regime holds on E itself.  E with a
    verified bar but a non-qualifying average expires the event
    (SKIPPED_NO_REGIME_AT_EFFECTIVE); a missing E bar, or an average that cannot
    be computed yet, is an independent HOLD and is never deferred.  An episode
    that did start keeps running until the regime first fails.
    """

    session_list = list(sessions) if sessions is not None else sorted(regime)
    bars = set(bar_dates) if bar_dates is not None else set(regime)
    regime_days = [day for day in session_list if day in regime]
    windows: list[EpisodeWindow] = []
    records: list[dict[str, Any]] = []
    ordinal = 0
    active_end: date | None = None

    def evidence(day: date) -> dict[str, Any]:
        detail = (regime_detail or {}).get(day)
        if detail is None:
            return {}
        fast, slow, verdict = detail
        return {"fast": str(fast), "slow": str(slow), "regime": verdict}

    for selection_date, effective in sorted(selections, key=lambda item: item[1]):
        record: dict[str, Any] = {
            "code": code,
            "strategy": strategy.value,
            "selection_date": selection_date,
            "effective_date": effective,
            "status": None,
            "reason": None,
            "episode_ordinal": None,
            "regime_evidence": evidence(effective),
        }
        if effective not in bars:
            record["status"] = "HOLD"
            record["reason"] = "HOLD_MISSING_E_BAR_OR_UNVERIFIED_NON_TRADING"
            records.append(record)
            continue
        if effective not in regime:
            record["status"] = "HOLD"
            record["reason"] = "HOLD_SMA_WARMUP_INSUFFICIENT"
            records.append(record)
            continue
        if not regime[effective]:
            record["status"] = "SKIPPED"
            record["reason"] = "SKIPPED_NO_REGIME_AT_EFFECTIVE"
            records.append(record)
            continue
        # Only a selection whose effective session is still inside a running
        # episode is a repeat.  A failing effective session was already expired
        # above, which is what the contract requires even when the previous
        # episode terminated on that same day.
        if active_end is not None and effective <= active_end:
            record["status"] = "SKIPPED"
            record["reason"] = "ALREADY_ACTIVE_EPISODE"
            records.append(record)
            continue
        ordinal += 1
        end = regime_days[-1] if regime_days else effective
        ended = False
        for day in regime_days:
            if day <= effective:
                continue
            if not regime[day]:
                end = day
                ended = True
                break
        windows.append(
            EpisodeWindow(
                code=code,
                strategy=strategy.value,
                ordinal=ordinal,
                monitor_effective_from=effective,
                regime_window_start=effective,
                regime_window_end=end,
                source_selection_date=selection_date,
                ended_by_regime=ended,
            )
        )
        active_end = end
        record["status"] = "ACTIVATED"
        record["reason"] = "REGIME_QUALIFIED_AT_EFFECTIVE"
        record["episode_ordinal"] = ordinal
        record["episode_end"] = end
        records.append(record)
    return windows, records


def _load_bars(
    *, binding: Any, symbols: Sequence[str], cutoff: date
) -> tuple[dict[str, list[Bar]], dict[str, str], int]:
    """Load frozen bars for the requested symbols up to the cutoff session."""

    bundle = load_fugle_causal_raw_bundle(
        snapshot_manifest_path=binding.snapshot_manifest_path,
        identity_csv=binding.identity_csv,
        official_universe_source=binding.universe_source,
        route_lineage_sidecar=binding.route_lineage_sidecar,
        route_lineage_sha256=binding.route_lineage_sha256,
        route_lineage_as_of=binding.route_lineage_as_of,
    )
    bars: dict[str, list[Bar]] = {}
    digests: dict[str, str] = {}
    for symbol in symbols:
        rows = [row for row in bundle.bars_by_symbol.get(symbol, ()) if row.trade_date <= cutoff]
        if not rows:
            continue
        bars[symbol] = [
            Bar.from_values(row.trade_date, row.raw_open, row.raw_high, row.raw_low, row.raw_close)
            for row in rows
        ]
        digests[symbol] = canonical_sha256([row.row_sha256 for row in rows])
    return bars, digests, len(bundle.bars_by_symbol)


def audit_episode_bounds(events: Sequence[Mapping[str, Any]]) -> int:
    """Count events that fall after their own episode window end.

    Episodes now run until the strategy regime fails, so the old four-session
    boundary is not a valid upper bound any more: every event carries the end of
    the episode it belongs to and is checked against that.
    """

    violations = 0
    for event in events:
        day = event.get("trade_date")
        end = event.get("regime_window_end")
        if day is None or end is None:
            continue
        day_value = day if isinstance(day, date) else date.fromisoformat(str(day))
        end_value = end if isinstance(end, date) else date.fromisoformat(str(end))
        if day_value > end_value:
            violations += 1
    return violations


def run_episode(
    *,
    symbol: str,
    code: str,
    strategy: StrategyKind,
    window: EpisodeWindow,
    bars: Sequence[Bar],
    binding: Any,
    data_hash: str,
    lineage: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Run the frozen engine once per matrix for one episode window."""

    events: list[dict[str, Any]] = []
    run_bars = [bar for bar in bars if bar.date <= window.regime_window_end]

    def episode_fields(monitor_date: Any) -> dict[str, Any]:
        """Causal provenance for an event inside this episode.

        The episode belongs to the selection that started it, so every event
        carries that selection date rather than a per-day list lookup that only
        exists for the four days covered by the delivered manifests.
        """

        iso = (
            monitor_date.isoformat()
            if isinstance(monitor_date, date)
            else str(monitor_date)
        )
        fields: dict[str, Any] = {
            "event_monitor_date": iso,
            "source_selection_date": window.source_selection_date.isoformat(),
            "membership_effective_date": window.monitor_effective_from.isoformat(),
            "episode_source_selection_date": window.source_selection_date.isoformat(),
        }
        daily = lineage.get(iso)
        if daily is not None:
            fields["daily_list_source_selection_date"] = daily["source_selection_date"]
        return fields

    for add_variant in AddVariant:
        for exit_variant in ExitVariant:
            matrix_id = MATRIX_GROUPS[(add_variant, exit_variant)]
            payload = _run_one(
                symbol=symbol,
                bars=run_bars,
                manifest=_manifest_for(
                    code=code,
                    matrix_id=matrix_id,
                    bars=run_bars,
                    binding=binding,
                    data_hash=data_hash,
                    add_variant=add_variant,
                    exit_variant=exit_variant,
                    start=window.regime_window_start,
                    end=window.regime_window_end,
                    run_suffix="-episode",
                ),
                add_variant=add_variant,
                exit_variant=exit_variant,
                slippage_bps=0,
                start=window.regime_window_start,
                end=window.regime_window_end,
            )
            entry_index, reclaim_index = _stop_lineage_index(payload["decisions"])
            leg_strategies = {
                str(leg.get("leg_id")): leg.get("strategy") for leg in payload["legs"]
            }
            base = {
                "code": code,
                "symbol": symbol,
                "matrix_id": matrix_id,
                "slippage_bps": 0,
                "episode_ordinal": window.ordinal,
                "monitor_effective_from": window.monitor_effective_from,
                "regime_window_start": window.regime_window_start,
                "regime_window_end": window.regime_window_end,
                "episode_ended_by_regime": window.ended_by_regime,
                "episode_source_selection_date": window.source_selection_date,
                "warmup_bars_before_segment": sum(
                    1 for bar in run_bars if bar.date < window.regime_window_start
                ),
            }
            for row in payload["decisions"]:
                if row.get("strategy") != strategy.value:
                    continue
                events.append(
                    {
                        **base,
                        **episode_fields(row.get("date")),
                        "kind": "DECISION",
                        "strategy": row.get("strategy"),
                        "trade_date": row.get("date"),
                        **project_fields(row, DECISION_FIELDS, kind="decision"),
                    }
                )
            for row in payload["transactions"]:
                resolved = row.get("strategy") or leg_strategies.get(str(row.get("leg_id")))
                if resolved != strategy.value:
                    continue
                fields = episode_fields(row.get("date"))
                events.append(
                    {
                        **base,
                        **fields,
                        "kind": "TRANSACTION",
                        "strategy": resolved,
                        "trade_date": row.get("date"),
                        "same_day_as_source_selection": fields["source_selection_date"]
                        == fields["event_monitor_date"],
                        **project_fields(row, TRANSACTION_FIELDS, kind="transaction"),
                    }
                )
            for row in payload["legs"]:
                if row.get("strategy") != strategy.value:
                    continue
                entry = row.get("entry_date")
                exit_day = row.get("exit_date")
                fields = episode_fields(entry)
                leg = {
                    **base,
                    **fields,
                    "kind": "LEG",
                    "strategy": row.get("strategy"),
                    "trade_date": entry,
                    "open_censored": exit_day is None,
                    "entry_source_selection_date": fields["source_selection_date"],
                    **project_fields(row, LEG_FIELDS, kind="leg"),
                }
                if exit_day is not None:
                    leg["exit_source_selection_date"] = episode_fields(exit_day)[
                        "source_selection_date"
                    ]
                leg.update(
                    _leg_stop_lineage(
                        leg=row,
                        strategy=row.get("strategy"),
                        entry_index=entry_index,
                        reclaim_index=reclaim_index,
                    )
                )
                leg["episode_key"] = (
                    f"{symbol}|{strategy.value}|EP{window.ordinal}|"
                    f"{window.source_selection_date.isoformat()}"
                )
                events.append(leg)
    return events


def run_episode_lifecycle_smoke(*, workdata_root: Path, output_dir: Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite episode smoke output: {output_dir}")
    root = Path(workdata_root)
    runner = resolve_runner_commit(Path(__file__).resolve().parents[2])
    calendar, documents = load_verified_l10(root, root / CALENDAR_RELATIVE_PATH)
    binding = resolve_price_binding(root, documents)
    identity_rows = list(csv.DictReader(binding.identity_csv.open(encoding="utf-8-sig")))
    code_to_symbol = {str(row["code"]): str(row["symbol"]) for row in identity_rows}
    lineage = build_monitor_lineage(documents)
    sessions = tuple(sorted(calendar.open_dates))
    availability_binding = next(
        item
        for item in documents[0]["source_bindings"]
        if item["role"] == "availability_snapshot"
    )
    availability_path = root / str(availability_binding["path"])
    if sha256_file(availability_path) != str(availability_binding["sha256"]):
        raise EpisodeLifecycleError(
            "availability snapshot file hash differs from the manifest binding"
        )
    availability = load_frozen_availability_v2(availability_path, workdata_root=root)
    per_day = {
        str(document["selection_date"]): set(str(code) for code in document["candidate_codes"])
        for document in documents
    }
    selections_by_code: dict[str, list[tuple[date, date]]] = {}
    for document in documents:
        selection_date = date.fromisoformat(str(document["selection_date"]))
        effective = date.fromisoformat(str(document["next_official_trading_day"]))
        for code in document["candidate_codes"]:
            selections_by_code.setdefault(str(code), []).append((selection_date, effective))
    codes = sorted(selections_by_code)
    covered_symbols = [code_to_symbol[code] for code in codes if code in code_to_symbol]
    bars, data_hash, bundle_symbol_count = _load_bars(
        binding=binding, symbols=sorted(set(covered_symbols)), cutoff=sessions[-1]
    )

    events: list[dict[str, Any]] = []
    episode_rows: list[EpisodeWindow] = []
    selection_records: list[dict[str, Any]] = []
    per_code: dict[str, Any] = {}
    hold_rows: list[dict[str, Any]] = []
    coverage_rows: list[SymbolCoverage] = []
    for code in codes:
        symbol = code_to_symbol.get(code)
        if symbol is None:
            hold_rows.append({"code": code, "status": "HOLD", "reason": "ROUTE_UNKNOWN"})
            continue
        symbol_bars = bars.get(symbol)
        if not symbol_bars:
            hold_rows.append({"code": code, "symbol": symbol, "status": "HOLD", "reason": "MISSING_OHLC"})
            continue
        warmup = sum(1 for bar in symbol_bars if bar.date < MONITOR_START)
        if warmup < REQUIRED_WARMUP_BARS:
            hold_rows.append(
                {
                    "code": code,
                    "symbol": symbol,
                    "status": "HOLD",
                    "reason": "INSUFFICIENT_WARMUP",
                    "warmup_bars_before_first_eligible_session": warmup,
                }
            )
            continue
        coverage = evaluate_symbol_coverage(
            code=code,
            symbol=symbol,
            bar_dates=[bar.date for bar in symbol_bars],
            sessions=sessions,
            availability=availability.by_symbol_date,
        )
        coverage_rows.append(coverage)
        if coverage.status != "COVERED":
            symbol_bars = truncate_to_usable(coverage, symbol_bars)
            hold_rows.append({**coverage.to_dict(), "hold_kind": "PRICE_COVERAGE"})
            if not symbol_bars:
                continue
            if sum(1 for bar in symbol_bars if bar.date < MONITOR_START) < REQUIRED_WARMUP_BARS:
                continue
        code_events: list[dict[str, Any]] = []
        for strategy in (StrategyKind.MEDIUM, StrategyKind.LONG):
            regime = regime_series(symbol_bars, strategy)
            detail = regime_values(symbol_bars, strategy)
            windows, skipped = episode_windows(
                code=code,
                strategy=strategy,
                selections=selections_by_code[code],
                regime=regime,
                sessions=sessions,
                bar_dates=[bar.date for bar in symbol_bars],
                regime_detail=detail,
            )
            selection_records.extend(skipped)
            for window in windows:
                episode_rows.append(window)
                code_events.extend(
                    run_episode(
                        symbol=symbol,
                        code=code,
                        strategy=strategy,
                        window=window,
                        bars=symbol_bars,
                        binding=binding,
                        data_hash=data_hash[symbol],
                        lineage=lineage,
                    )
                )
        events.extend(code_events)
        per_code[code] = {
            "symbol": symbol,
            "bar_count": len(symbol_bars),
            "last_bar": symbol_bars[-1].date,
            "data_hash": data_hash[symbol],
            "event_count": len(code_events),
        }

    segments_by_code = {
        code: [(window.regime_window_start, window.regime_window_end) for window in episode_rows if window.code == code]
        for code in {window.code for window in episode_rows}
    }
    audit = audit_events(events, segments_by_code)
    audit["events_after_monitor_window"] = audit_episode_bounds(events)
    legs = [row for row in events if row["kind"] == "LEG"]
    stop_source_counts = Counter(str(leg.get("stop_source")) for leg in legs)
    stop_reason_counts = Counter(
        str(leg.get("stop_source_reason"))
        for leg in legs
        if leg.get("stop_source") != "PULLBACK_EVENT_LOW"
    )
    event_codes = sorted({row["code"] for row in legs})
    matrices = tuple(
        (add_variant, exit_variant)
        for add_variant in AddVariant
        for exit_variant in ExitVariant
    )
    gates = {
        **frozen_rule_gates(),
        "events_outside_eligible_segment": (
            "PASS" if audit["events_outside_eligible_segment"] == 0 else "HOLD"
        ),
        "leg_entries_outside_eligible_segment": (
            "PASS" if audit["leg_entries_outside_eligible_segment"] == 0 else "HOLD"
        ),
        "leg_exits_outside_eligible_segment": (
            "PASS" if audit["leg_exits_outside_eligible_segment"] == 0 else "HOLD"
        ),
        "events_after_monitor_window": (
            "PASS" if audit["events_after_monitor_window"] == 0 else "HOLD"
        ),
        "same_day_entry_as_selection_date": (
            "PASS" if audit["same_day_entry_as_selection_date"] == 0 else "HOLD"
        ),
        "forced_end_exit": "PASS" if audit["forced_end_exit"] == 0 else "HOLD",
        "open_censored_flag_mismatches": (
            "PASS" if audit["open_censored_flag_mismatches"] == 0 else "HOLD"
        ),
        "event_selection_lineage_present": (
            "PASS" if audit["events_without_selection_lineage"] == 0 else "HOLD"
        ),
        "stop_lineage_complete": (
            "PASS"
            if legs and stop_source_counts.get("PULLBACK_EVENT_LOW", 0) == len(legs)
            else "PARTIAL"
        ),
        "price_coverage_explained": (
            "PASS"
            if not any(row.status != "COVERED" for row in coverage_rows)
            else "HOLD"
        ),
        "no_delayed_activation": (
            "PASS"
            if all(
                window.regime_window_start == window.monitor_effective_from
                for window in episode_rows
            )
            else "HOLD"
        ),
        "selection_activation_fail_closed": (
            "PASS"
            if all(row["status"] in {"ACTIVATED", "SKIPPED", "HOLD"} for row in selection_records)
            else "HOLD"
        ),
        "unresolved_selection_holds": (
            "PASS"
            if not any(row["status"] == "HOLD" for row in selection_records)
            else "HOLD"
        ),
    }
    prefix_status = "NOT_APPLICABLE"
    prefix: dict[str, Any] = {"status": "NOT_APPLICABLE", "checks": 0, "mismatches": []}
    if event_codes:
        sample = [code for code in codes if code not in set(event_codes)][:5]
        prefix_segments = {
            code: tuple(
                (window.regime_window_start, window.regime_window_end)
                for window in episode_rows
                if window.code == code
            )
            for code in event_codes + sample
        }
        prefix = prefix_replay(
            coverage={
                **{code: matrices for code in event_codes},
                **{code: (matrices[0],) for code in sample},
            },
            bars_by_symbol=bars,
            symbol_by_code={code: code_to_symbol[code] for code in event_codes + sample},
            segments_by_code=prefix_segments,
            binding=binding,
            data_hash_by_symbol=data_hash,
            lineage=lineage,
        )
        prefix_status = prefix["status"]
    gates["prefix_replay_stability"] = prefix_status

    events.sort(
        key=lambda row: (
            row["code"],
            str(row.get("regime_window_start")),
            row["kind"],
            str(row.get("trade_date")),
            str(row.get("leg_id") or ""),
        )
    )
    economic_keys = sorted({economic_event_key(leg) for leg in legs})
    payload = {
        "input_readback.json": canonical_bytes(
            {
                "schema_version": SMOKE_SCHEMA,
                "module_version": MODULE_VERSION,
                "selection_days": sorted(per_day),
                "per_day_candidate_counts": {day: len(values) for day, values in per_day.items()},
                "union_count": len(codes),
                "manifests": [
                    {
                        "selection_date": str(document["selection_date"]),
                        "next_official_trading_day": str(document["next_official_trading_day"]),
                        "file_sha256": document["_record"].file_sha256,
                        "content_sha256": document["_record"].content_sha256,
                    }
                    for document in documents
                ],
            }
        )
        + b"\n",
        "episode_windows.json": canonical_bytes(
            {
                "episode_count": len(episode_rows),
                "windows": [window.to_dict() for window in episode_rows],
                "skipped_selections": [
                    row for row in selection_records if row["status"] != "ACTIVATED"
                ],
                "rule": (
                    "D selects; monitoring starts at the next official session E only when "
                    "the strict fast>slow regime holds on E itself; E with a verified bar "
                    "but a failing average expires the event; a missing or unverifiable E is "
                    "held, never deferred; a started episode runs until the regime first fails."
                ),
            }
        )
        + b"\n",
        "selection_diagnostics.json": canonical_bytes(
            {
                "selection_count": len(selection_records),
                "by_status": dict(
                    sorted(Counter(row["status"] for row in selection_records).items())
                ),
                "by_reason": dict(
                    sorted(Counter(str(row["reason"]) for row in selection_records).items())
                ),
                "delayed_activation_count": sum(
                    1
                    for window in episode_rows
                    if window.regime_window_start != window.monitor_effective_from
                ),
                "records": selection_records,
            }
        )
        + b"\n",
        "coverage.json": canonical_bytes(
            {
                "bundle_symbol_count": bundle_symbol_count,
                "union_count": len(codes),
                "engine_run_symbol_count": len(per_code),
                "hold_rows": hold_rows,
                "availability": {
                    "path": availability.relative_path,
                    "file_sha256": availability.file_sha256,
                    "content_sha256": availability.content_sha256,
                    "schema_version": availability.schema_version,
                    "proven_non_trading_records": len(availability.by_symbol_date),
                },
                "symbol_coverage": {
                    "covered": sum(1 for row in coverage_rows if row.status == "COVERED"),
                    "hold": sum(1 for row in coverage_rows if row.status != "COVERED"),
                    "proven_non_trading_sessions": sum(
                        row.proven_non_trading_sessions for row in coverage_rows
                    ),
                    "unexplained_gap_symbols": [
                        row.to_dict() for row in coverage_rows if row.status != "COVERED"
                    ][:20],
                },
            }
        )
        + b"\n",
        "membership_lineage.json": canonical_bytes(
            {"monitor_to_selection_lineage": lineage}
        )
        + b"\n",
        "engine_events.jsonl": b"".join(canonical_bytes(row) + b"\n" for row in events),
        "audit.json": canonical_bytes(
            {
                **audit,
                "gates": gates,
                "monitor_window": [MONITOR_START, MONITOR_END],
                "performance_note": (
                    "The frozen engine still computes per-leg PnL internally; this smoke "
                    "excludes those fields by name and neither reports nor aggregates them."
                ),
            }
        )
        + b"\n",
        "prefix_replay.json": canonical_bytes(prefix) + b"\n",
        "summary_engineering_only.json": canonical_bytes(
            {
                "schema_version": SMOKE_SCHEMA,
                "candidate_union_count": len(codes),
                "engine_run_symbol_count": len(per_code),
                "hold_or_missing_count": len(hold_rows),
                "hold_reasons": dict(sorted(Counter(row["reason"] for row in hold_rows).items())),
                "event_count": len(events),
                "episode_count": len(episode_rows),
                "episodes_ended_by_regime": sum(1 for row in episode_rows if row.ended_by_regime),
                "episodes_still_open_at_data_end": sum(
                    1 for row in episode_rows if not row.ended_by_regime
                ),
                "repeated_selections_ignored": sum(
                    1 for row in selection_records if row["reason"] == "ALREADY_ACTIVE_EPISODE"
                ),
                "selection_activated": sum(
                    1 for row in selection_records if row["status"] == "ACTIVATED"
                ),
                "selection_skipped_no_regime_at_effective": sum(
                    1
                    for row in selection_records
                    if row["reason"] == "SKIPPED_NO_REGIME_AT_EFFECTIVE"
                ),
                "selection_holds": sum(
                    1 for row in selection_records if row["status"] == "HOLD"
                ),
                "leg_count": len(legs),
                "matrix_leg_count": len(legs),
                "unique_economic_entry_count": len(economic_keys),
                "economic_event_keys": economic_keys,
                "open_leg_count": sum(1 for leg in legs if leg.get("open_censored")),
                "stop_source_counts": dict(sorted(stop_source_counts.items())),
                "stop_unavailable_reason_counts": dict(sorted(stop_reason_counts.items())),
                "performance_read_or_computed": False,
                "rules_changed": False,
                "note": (
                    "Episode-lifecycle engineering evidence only; four selection days are not "
                    "enough for any expectancy conclusion."
                ),
            }
        )
        + b"\n",
        "runner_commit.json": canonical_bytes(runner) + b"\n",
        "checkpoint.json": canonical_bytes(
            {
                "status": "EPISODE_LIFECYCLE_SMOKE_COMPLETE",
                "runner_commit": runner["runner_commit"],
                "resume_command": (
                    "python -m backtests.dual_ma.approx_l10_episode_lifecycle_smoke "
                    "--workdata-root <workdata> --output-dir <new-run-id>"
                ),
            }
        )
        + b"\n",
    }
    output_dir.mkdir(parents=True)
    for name, blob in sorted(payload.items()):
        (output_dir / name).write_bytes(blob)
    artifact_rows = [
        {"path": path.name, "sha256": sha256_file(path), "bytes": path.stat().st_size}
        for path in sorted(output_dir.iterdir())
        if path.is_file()
    ]
    (output_dir / "artifact_manifest.json").write_text(
        json.dumps(
            sorted(artifact_rows, key=lambda row: row["path"]),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": SMOKE_SCHEMA,
        "runner_commit": runner["runner_commit"],
        "union_count": len(codes),
        "engine_run_symbol_count": len(per_code),
        "hold_or_missing_count": len(hold_rows),
        "episode_count": len(episode_rows),
        "event_count": len(events),
        "leg_count": len(legs),
        "unique_economic_entry_count": len(economic_keys),
        "open_leg_count": sum(1 for leg in legs if leg.get("open_censored")),
        "stop_source_counts": dict(sorted(stop_source_counts.items())),
        "gates": gates,
        "prefix": {
            "status": prefix_status,
            "checks": prefix.get("checks", 0),
            "mismatches": len(prefix.get("mismatches") or ()),
        },
        "output_dir": str(output_dir.resolve()),
        "artifact_manifest_sha256": sha256_file(output_dir / "artifact_manifest.json"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdata-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    result = run_episode_lifecycle_smoke(
        workdata_root=args.workdata_root, output_dir=args.output_dir
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    sys.exit(main())
