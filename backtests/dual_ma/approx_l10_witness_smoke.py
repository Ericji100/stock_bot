"""L10 three-witness engineering smoke over the frozen dual-MA engine.

Scope: prove that the rolling approximate selection reaches the frozen v0.4.1
engine with correct causality for three fixed witness symbols. It is not a
performance study and it is not a full candidate backtest.

The frozen engine accepts one inclusive window (plus warmup) per run and has no
native per-day eligibility, so this module expresses daily membership by running
one engine call per contiguous eligible segment. No engine rule is changed:
warmup bars only feed indicators, decisions can only occur inside an eligible
segment, and a position still open at the end of the monitor window stays
open/censored rather than being force-closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .approx_daily_engineering_replay import (
    ALLOWLIST,
    APPROX_DAILY_ROOT,
    CALENDAR_RELATIVE_PATH,
    ApproxReplayError,
    canonical_bytes,
    canonical_sha256,
    load_official_calendar,
    manifest_name,
    sha256_file,
    verify_daily_manifest,
)
from .contracts import (
    COST_MODEL,
    EXECUTION_MODEL,
    SPEC_SHA256,
    STRATEGY_VERSION,
    RunManifest,
)
from .models import AddVariant, Bar, EntryVariant, ExitVariant, InstrumentType, StrategyKind
from .causal_price_adapter import load_fugle_causal_raw_bundle
from .engine import MATRIX_GROUPS, DualMABacktestEngine


MODULE_VERSION = "APPROX_L10_WITNESS_SMOKE_V0_1"
SMOKE_SCHEMA = "dual-ma-approx-l10-witness-smoke-v0.1"
MONITOR_START = date(2022, 1, 5)
MONITOR_END = date(2022, 1, 10)
SLIPPAGE_BPS = (0,)
ENTRY_VARIANT = EntryVariant.RECLAIM_ONLY
# One engine run per matrix already reports every strategy layer; the row's own
# strategy field is authoritative and is never re-labelled by the caller.
STRATEGY_LAYERS: tuple[StrategyKind, ...] = (StrategyKind.MEDIUM, StrategyKind.LONG)

# Projected engine fields.  Anything not present is reported as unavailable
# instead of being invented, and no performance field is ever carried out.
DECISION_FIELDS = ("date", "strategy", "regime", "sma5", "sma21", "sma144", "events")
LEG_FIELDS = (
    "leg_id",
    "episode_id",
    "leg_number",
    "entry_date",
    "entry_reference_close",
    "entry_fill_price",
    "shares",
    "buy_gross",
    "buy_commission",
    "buy_total",
    "initial_stop",
    "initial_r",
    "effective_stop",
    "breakeven_stop",
    "dow_stop",
    "two_r_date",
    "exit_date",
    "exit_reason",
    "exit_reference_close",
    "exit_fill_price",
    "sell_gross",
    "sell_commission",
    "sell_tax",
    "sell_net",
)
TRANSACTION_FIELDS = (
    "date",
    "role",
    "leg_id",
    "price",
    "shares",
    "gross_amount",
    "commission",
    "tax",
    "cash_amount",
)
FORBIDDEN_PERFORMANCE_TOKENS = ("pnl", "profit", "return", "expectancy", "win_rate", "mfe", "mae")


def project_fields(row: Mapping[str, Any], fields: Sequence[str], *, kind: str) -> dict[str, Any]:
    """Keep only allow-listed engine fields and never a performance quantity.

    The frozen engine emits performance fields such as net_pnl on its leg rows.
    They are dropped by name here and recorded only as excluded key names, so no
    performance value is ever carried into this artefact.
    """

    projected: dict[str, Any] = {}
    unavailable: list[str] = []
    for field in fields:
        if field in row:
            projected[field] = row[field]
        else:
            unavailable.append(field)
    for key in projected:
        lowered = str(key).lower()
        if any(token in lowered for token in FORBIDDEN_PERFORMANCE_TOKENS):
            raise PerformanceLeakError(f"{kind} projection would carry performance field {key!r}")
    excluded = sorted(
        str(key)
        for key in row
        if any(token in str(key).lower() for token in FORBIDDEN_PERFORMANCE_TOKENS)
    )
    if excluded:
        projected["performance_fields_excluded"] = excluded
    if unavailable:
        projected["unavailable_engine_fields"] = unavailable
    return projected
# The frozen adapter requires the route-identity as-of that the bound snapshot
# supports (identical to 01's approximate-selection runners for this snapshot).
# It is an identity-lineage as-of, not an event-date cutoff: event truncation is
# enforced separately by the <= cutoff bar filter and by the engine window.
ROUTE_LINEAGE_AS_OF = date(2022, 12, 30)
# Filled in at run time from the real worktree HEAD; never a hardcoded literal.
RUNNER_COMMIT: dict[str, str] = {"value": "UNRESOLVED"}

# Fixed witnesses; the reason for each choice is the membership pattern only.
WITNESS_CODES: tuple[str, ...] = ("1101", "1447", "1227")
EXPECTED_MEMBERSHIP: Mapping[str, tuple[bool, bool, bool, bool]] = {
    "1101": (True, True, True, True),
    "1447": (False, True, False, False),
    "1227": (True, False, True, False),
}

L10_ALLOWLIST = tuple(entry for entry in ALLOWLIST if entry[1] == "L10")
SELECTION_DAYS = tuple(entry[0] for entry in L10_ALLOWLIST)


class WitnessSmokeError(ApproxReplayError):
    code = "L10_WITNESS_SMOKE_ERROR"


class WitnessMembershipError(WitnessSmokeError):
    code = "WITNESS_MEMBERSHIP_PATTERN_MISMATCH"


class WarmupLeakError(WitnessSmokeError):
    code = "EVENT_OUTSIDE_ELIGIBLE_SEGMENT"


class PerformanceLeakError(WitnessSmokeError):
    code = "PERFORMANCE_FIELD_LEAK"


@dataclass(frozen=True)
class WitnessDay:
    selection_date: date
    effective_date: date
    symbol: str
    code: str
    in_candidate_union: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection_date": self.selection_date,
            "effective_date": self.effective_date,
            "code": self.code,
            "symbol": self.symbol,
            "eligible_from_next_official_session": self.in_candidate_union,
            "same_day_monitoring_allowed": False,
        }


def resolve_symbol(code: str, identity_rows: Sequence[Mapping[str, str]]) -> str:
    for row in identity_rows:
        if str(row.get("code")) == code:
            return str(row.get("symbol"))
    raise WitnessSmokeError(f"witness code {code} is not present in market identity")


def load_verified_l10(workdata_root: Path, calendar_path: Path) -> tuple[Any, list[dict]]:
    """Verify the four frozen L10 manifests and return calendar + documents."""

    root = Path(workdata_root)
    calendar = load_official_calendar(calendar_path)
    documents: list[dict] = []
    for selection_date, scenario, tag, file_sha, content_sha, commit in L10_ALLOWLIST:
        path = root / APPROX_DAILY_ROOT / selection_date / manifest_name(scenario, tag)
        record = verify_daily_manifest(
            manifest_path=path,
            selection_date=selection_date,
            scenario=scenario,
            tag=tag,
            file_sha256=file_sha,
            content_sha256=content_sha,
            git_commit=commit,
            calendar=calendar,
            workdata_root=root,
            frozen_allowlist=ALLOWLIST,
        )
        documents.append(json.loads(path.read_text(encoding="utf-8-sig")) | {"_record": record})
    return calendar, documents


def derive_membership(
    documents: Sequence[Mapping[str, Any]], identity_rows: Sequence[Mapping[str, str]]
) -> tuple[list[WitnessDay], dict[str, tuple[bool, ...]]]:
    """Derive witness membership from the frozen manifests, never by hand."""

    rows: list[WitnessDay] = []
    pattern: dict[str, tuple[bool, ...]] = {code: () for code in WITNESS_CODES}
    for document in documents:
        selection_day = date.fromisoformat(str(document["selection_date"]))
        effective = date.fromisoformat(str(document["next_official_trading_day"]))
        union = {str(code) for code in document["candidate_codes"]}
        for code in WITNESS_CODES:
            symbol = resolve_symbol(code, identity_rows)
            present = code in union
            rows.append(
                WitnessDay(
                    selection_date=selection_day,
                    effective_date=effective,
                    symbol=symbol,
                    code=code,
                    in_candidate_union=present,
                )
            )
            pattern[code] = (*pattern[code], present)
    for code, expected in EXPECTED_MEMBERSHIP.items():
        if pattern[code] != expected:
            raise WitnessMembershipError(
                f"{code} membership {pattern[code]} does not match the declared witness pattern {expected}"
            )
    return rows, pattern


def eligible_segments(
    pattern: Mapping[str, tuple[bool, ...]], documents: Sequence[Mapping[str, Any]]
) -> dict[str, tuple[tuple[date, date], ...]]:
    """Contiguous effective-date segments per witness."""

    effective_dates = [date.fromisoformat(str(doc["next_official_trading_day"])) for doc in documents]
    segments: dict[str, tuple[tuple[date, date], ...]] = {}
    for code, flags in pattern.items():
        built: list[tuple[date, date]] = []
        start: date | None = None
        previous: date | None = None
        for flag, effective in zip(flags, effective_dates):
            if flag:
                if start is None:
                    start = effective
                previous = effective
            elif start is not None and previous is not None:
                built.append((start, previous))
                start = None
                previous = None
        if start is not None and previous is not None:
            built.append((start, previous))
        segments[code] = tuple(built)
    return segments


@dataclass(frozen=True)
class PriceBinding:
    snapshot_manifest_path: Path
    snapshot_file_sha256: str
    snapshot_content_sha256: str
    identity_csv: Path
    identity_sha256: str
    universe_source: Path
    route_lineage_sidecar: Path
    route_lineage_sha256: str
    route_lineage_as_of: date

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_manifest_path": str(self.snapshot_manifest_path),
            "snapshot_file_sha256": self.snapshot_file_sha256,
            "snapshot_content_sha256": self.snapshot_content_sha256,
            "identity_csv": str(self.identity_csv),
            "identity_sha256": self.identity_sha256,
            "universe_source": str(self.universe_source),
            "route_lineage_sidecar": str(self.route_lineage_sidecar),
            "route_lineage_sha256": self.route_lineage_sha256,
            "route_lineage_as_of": self.route_lineage_as_of,
        }


def resolve_price_binding(workdata_root: Path, documents: Sequence[Mapping[str, Any]]) -> PriceBinding:
    """Read the frozen source bindings declared by the verified L10 manifests."""

    root = Path(workdata_root)
    document = documents[0]
    bindings = {str(item["role"]): item for item in document["source_bindings"]}
    evidence = document["programs"]["chip_1"]["evidence"]
    identity = evidence["market_identity"]
    sidecar = evidence["route_lineage_sidecar"]

    def verify(path: Path, expected: str, label: str) -> str:
        if not path.is_file():
            raise WitnessSmokeError(f"{label} is missing: {path}")
        actual = sha256_file(path)
        if actual != expected:
            raise WitnessSmokeError(f"{label} hash mismatch: {path}")
        return actual

    snapshot_path = root / str(bindings["raw_price_snapshot"]["path"])
    snapshot_sha = verify(
        snapshot_path, str(bindings["raw_price_snapshot"]["sha256"]), "raw price snapshot"
    )
    snapshot_doc = json.loads(snapshot_path.read_text(encoding="utf-8-sig"))
    identity_path = root / str(identity["path"])
    identity_sha = verify(identity_path, str(identity["sha256"]), "market identity")
    universe_path = root / str(bindings["universe_snapshot"]["path"])
    verify(universe_path, str(bindings["universe_snapshot"]["sha256"]), "universe snapshot")
    sidecar_path = root / str(sidecar["path"])
    sidecar_sha = verify(sidecar_path, str(sidecar["file_sha256"]), "route lineage sidecar")
    # The frozen adapter requires the exact target source the sidecar was built
    # against; it is recorded inside the sidecar, not in the daily manifest.
    sidecar_doc = json.loads(sidecar_path.read_text(encoding="utf-8-sig"))
    target = sidecar_doc["target_source"]
    target_path = Path(str(target["path"]))
    if sha256_file(target_path) != str(target["file_sha256"]):
        raise WitnessSmokeError("route lineage target source hash mismatch")
    return PriceBinding(
        snapshot_manifest_path=snapshot_path,
        snapshot_file_sha256=snapshot_sha,
        snapshot_content_sha256=str(snapshot_doc.get("content_sha256")),
        identity_csv=identity_path,
        identity_sha256=identity_sha,
        universe_source=target_path,
        route_lineage_sidecar=sidecar_path,
        route_lineage_sha256=str(sidecar["content_sha256"]),
        route_lineage_as_of=ROUTE_LINEAGE_AS_OF,
    )


def load_witness_bars(
    binding: PriceBinding, *, symbols: Iterable[str], cutoff: date
) -> tuple[dict[str, list[Bar]], dict[str, str]]:
    bundle = load_fugle_causal_raw_bundle(
        snapshot_manifest_path=binding.snapshot_manifest_path,
        identity_csv=binding.identity_csv,
        official_universe_source=binding.universe_source,
        route_lineage_sidecar=binding.route_lineage_sidecar,
        route_lineage_sha256=binding.route_lineage_sha256,
        route_lineage_as_of=binding.route_lineage_as_of,
    )
    bars: dict[str, list[Bar]] = {}
    lineage: dict[str, str] = {}
    for symbol in symbols:
        rows = [row for row in bundle.bars_by_symbol.get(symbol, ()) if row.trade_date <= cutoff]
        if not rows:
            raise WitnessSmokeError(f"price snapshot has no bars for {symbol} at or before {cutoff}")
        bars[symbol] = [
            Bar.from_values(row.trade_date, row.raw_open, row.raw_high, row.raw_low, row.raw_close)
            for row in rows
        ]
        lineage[symbol] = canonical_sha256([row.row_sha256 for row in rows])
    return bars, lineage


MONITOR_TO_SELECTION_FALLBACK = "MISSING_SELECTION_LINEAGE"
EXPECTED_EXIT_ALLOWLIST = ("EXIT_2R_21MA_2CLOSE", "EXIT_2R_DOW_TRAIL", "EXIT_DOW_TRAIL")
EXPECTED_ADD_ALLOWLIST = ("ADD_R1_AGGREGATE", "ADD_SIGNAL_ONLY")
EXPECTED_ENTRY_ALLOWLIST = ("ENTRY_RECLAIM_ONLY", "ENTRY_RECLAIM_V_PIVOT_CONFIRMED")
EXPECTED_SPEC_SHA256 = "17f3e15b360eba03d9cbc47a5c1013f6ab4e86352cac8beaa87ad1d94fdc5c1a"
EXPECTED_STRATEGY_VERSION = "v0.4.1-frozen"
FORCED_EXIT_TOKENS = ("FORCED", "END_OF_WINDOW", "WINDOW_END", "BATCH_END")
PREFIX_LEG_ENTRY_FIELDS = (
    "entry_date",
    "entry_reference_close",
    "entry_fill_price",
    "shares",
    "buy_gross",
    "buy_commission",
    "buy_total",
    "initial_stop",
    "initial_r",
)
# effective_stop, breakeven_stop, dow_stop and two_r_date are post-entry state that
# the frozen engine keeps updating, so they are deliberately excluded from the
# prefix comparison: only entry-time fields are then-knowable.
PREFIX_LEG_STATE_FIELDS_NOT_COMPARED = (
    "effective_stop",
    "breakeven_stop",
    "dow_stop",
    "two_r_date",
)


def build_monitor_lineage(documents: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map each official monitor date to the selection day that governs it.

    Jan5 from Jan4, Jan6 from Jan5, Jan7 from Jan6 and Jan10 from Jan7 for this
    batch, derived from the manifests rather than typed in.
    """

    lineage: dict[str, dict[str, Any]] = {}
    for document in documents:
        selection = str(document["selection_date"])
        effective = str(document["next_official_trading_day"])
        if effective in lineage:
            raise WitnessSmokeError(f"duplicate monitor date in lineage: {effective}")
        lineage[effective] = {
            "membership_effective_date": effective,
            "source_selection_date": selection,
            "candidate_codes": sorted(str(code) for code in document["candidate_codes"]),
        }
    return lineage


def resolve_runner_commit(worktree: Path) -> dict[str, Any]:
    """Read and verify the actual runner commit instead of hardcoding one."""

    import subprocess

    worktree = Path(worktree)
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
    unexpected = [line for line in status if ".pytest_diag_subset" not in line]
    if unexpected:
        raise WitnessSmokeError(
            f"worktree must be clean except .pytest_diag_subset/: {unexpected}"
        )
    return {
        "runner_commit": head,
        "worktree_dirty_entries": len(status),
        "worktree_unexpected_entries": unexpected,
    }


def _lineage_fields(lineage: Mapping[str, Mapping[str, Any]], monitor_date: Any) -> dict[str, Any]:
    iso = monitor_date.isoformat() if isinstance(monitor_date, date) else str(monitor_date)
    entry = lineage.get(iso)
    if entry is None:
        raise WitnessSmokeError(f"monitor date {iso} has no selection lineage entry")
    return {
        "event_monitor_date": iso,
        "source_selection_date": entry["source_selection_date"],
        "membership_effective_date": entry["membership_effective_date"],
    }


def _manifest_for(
    *,
    code: str,
    matrix_id: str,
    bars: Sequence[Bar],
    binding: PriceBinding,
    data_hash: str,
    add_variant: AddVariant,
    exit_variant: ExitVariant,
    start: date,
    end: date,
    run_suffix: str = "",
) -> RunManifest:
    return RunManifest(
        run_id=(
            f"approx-l10-witness{run_suffix}:{code}:{matrix_id}:"
            f"{start.isoformat()}:{end.isoformat()}"
        ),
        strategy_version=STRATEGY_VERSION,
        spec_sha256=SPEC_SHA256,
        git_commit=RUNNER_COMMIT["value"],
        universe_id=str(binding.universe_source.name),
        data_snapshot_id="dual-ma-approx-l10-witness-smoke-v0.1",
        data_start=bars[0].date,
        data_end=bars[-1].date,
        data_hash=data_hash,
        execution_model=EXECUTION_MODEL,
        cost_model=COST_MODEL,
        matrix_id=matrix_id,
        slippage_bps=0,
        strategy_parameters={
            "entry_variant": ENTRY_VARIANT.value,
            "add_variant": add_variant.value,
            "exit_variant": exit_variant.value,
        },
        metadata={"source_code": code},
    )


def _run_one(
    *,
    symbol: str,
    bars: Sequence[Bar],
    manifest: RunManifest,
    add_variant: AddVariant,
    exit_variant: ExitVariant,
    slippage_bps: int,
    start: date,
    end: date,
) -> dict[str, Any]:
    result = DualMABacktestEngine(
        add_variant,
        exit_variant,
        slippage_bps=slippage_bps,
        entry_variant=ENTRY_VARIANT,
    ).run(
        symbol,
        bars,
        run_manifest=manifest,
        instrument_type=InstrumentType.STOCK,
        backtest_start=start,
        backtest_end=end,
    )
    return result.to_dict()


def _stop_lineage_index(
    decisions: Sequence[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[tuple[str, str], list[dict[str, Any]]]]:
    """Index entry decisions by leg and reclaim signals by pullback event.

    PULLBACK_STARTED only marks where a pullback began; the frozen v0.4.1 A-group
    stop is the pullback interval low as of the reclaim that filled the entry.
    """

    entries: dict[tuple[str, str], list[dict[str, Any]]] = {}
    reclaims: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in decisions:
        strategy = str(row.get("strategy"))
        decision_date = row.get("date")
        for event in row.get("events") or ():
            kind = event.get("type")
            if kind == "ENTRY_DECISION" and event.get("status") == "FILLED":
                key = (strategy, str(event.get("leg_id")))
                entries.setdefault(key, []).append(
                    {
                        "decision_date": decision_date,
                        "leg_id": event.get("leg_id"),
                        "leg_number": event.get("leg_number"),
                        "role": event.get("role"),
                        "pullback_event_id": event.get("pullback_event_id"),
                        "initial_stop": event.get("initial_stop"),
                        "episode_id": event.get("episode_id"),
                    }
                )
            elif kind == "RECLAIM_SIGNAL":
                key = (strategy, str(event.get("pullback_event_id")))
                reclaims.setdefault(key, []).append(
                    {
                        "decision_date": decision_date,
                        "pullback_event_id": event.get("pullback_event_id"),
                        "pullback_low": event.get("pullback_low"),
                        "pullback_start_date": event.get("pullback_start_date"),
                    }
                )
    return entries, reclaims


def _leg_stop_lineage(
    *,
    leg: Mapping[str, Any],
    strategy: Any,
    entry_index: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
    reclaim_index: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Name the stop by its frozen v0.4.1 A-group source, never as a pivot/defense."""

    base = {
        "stop_price": leg.get("initial_stop"),
        "stop_source": "UNAVAILABLE",
        "stop_source_reason": None,
        "pullback_event_id": None,
        "pullback_start_date": None,
        "reclaim_date": None,
        "source_decision_date": None,
        "course_pivot_lineage": "NOT_EVALUATED",
        "defense_lineage": "UNAVAILABLE",
    }
    strategy_key = str(strategy)
    leg_id = str(leg.get("leg_id"))
    entry_rows = list(entry_index.get((strategy_key, leg_id)) or ())
    if not entry_rows:
        return {**base, "stop_source_reason": "ENTRY_DECISION_NOT_FOUND"}
    if len(entry_rows) > 1:
        return {**base, "stop_source_reason": "DUPLICATE_ENTRY_DECISION"}
    entry = entry_rows[0]
    entry_date = leg.get("entry_date")
    decision_date = entry.get("decision_date")
    if decision_date is None or str(decision_date) > str(entry_date):
        return {**base, "stop_source_reason": "ENTRY_DECISION_AFTER_ENTRY"}
    if str(entry.get("initial_stop")) != str(leg.get("initial_stop")):
        return {**base, "stop_source_reason": "ENTRY_DECISION_STOP_MISMATCH"}
    pullback_event_id = entry.get("pullback_event_id")
    if not pullback_event_id:
        return {**base, "stop_source_reason": "ENTRY_DECISION_WITHOUT_PULLBACK_ID"}
    reclaims = [
        item
        for item in reclaim_index.get((strategy_key, str(pullback_event_id))) or ()
        if item.get("decision_date") is not None
        and str(item["decision_date"]) <= str(entry_date)
    ]
    if not reclaims:
        return {**base, "stop_source_reason": "RECLAIM_SIGNAL_NOT_FOUND"}
    lows = {str(item.get("pullback_low")) for item in reclaims}
    if len(lows) > 1:
        return {**base, "stop_source_reason": "CONFLICTING_RECLAIM_LOWS"}
    reclaims.sort(key=lambda item: str(item["decision_date"]))
    chosen = reclaims[-1]
    if str(chosen.get("pullback_low")) != str(leg.get("initial_stop")):
        return {
            **base,
            "stop_source_reason": "INITIAL_STOP_DIFFERS_FROM_RECLAIM_LOW",
            "stop_source_detail": (
                f"initial_stop={leg.get('initial_stop')} reclaim_low={chosen.get('pullback_low')}"
            ),
        }
    return {
        **base,
        "stop_source": "PULLBACK_EVENT_LOW",
        "stop_source_reason": "RECLAIM_LOW_MATCHES_INITIAL_STOP",
        "stop_source_detail": (
            "v0.4.1 A-group initial stop equals the pullback interval low as of the "
            "reclaim that filled the entry"
        ),
        "pullback_event_id": pullback_event_id,
        "pullback_start_date": chosen.get("pullback_start_date"),
        "reclaim_date": chosen.get("decision_date"),
        "source_decision_date": decision_date,
    }


def run_segments(
    *,
    symbol: str,
    code: str,
    bars: Sequence[Bar],
    segments: Sequence[tuple[date, date]],
    binding: PriceBinding,
    data_hash: str,
    lineage: Mapping[str, Mapping[str, Any]],
    slippage_bps: int = 0,
) -> list[dict[str, Any]]:
    """Run the frozen engine once per matrix and contiguous eligible segment."""

    events: list[dict[str, Any]] = []
    for add_variant in AddVariant:
        for exit_variant in ExitVariant:
            matrix_id = MATRIX_GROUPS[(add_variant, exit_variant)]
            for start, end in segments:
                segment_bars = [bar for bar in bars if bar.date <= end]
                if not segment_bars or segment_bars[-1].date < start:
                    raise WitnessSmokeError(
                        f"no bars inside segment {start}..{end} for {symbol}"
                    )
                payload = _run_one(
                    symbol=symbol,
                    bars=segment_bars,
                    manifest=_manifest_for(
                        code=code,
                        matrix_id=matrix_id,
                        bars=segment_bars,
                        binding=binding,
                        data_hash=data_hash,
                        add_variant=add_variant,
                        exit_variant=exit_variant,
                        start=start,
                        end=end,
                    ),
                    add_variant=add_variant,
                    exit_variant=exit_variant,
                    slippage_bps=slippage_bps,
                    start=start,
                    end=end,
                )
                leg_strategies = {
                    str(leg.get("leg_id")): leg.get("strategy") for leg in payload["legs"]
                }
                base = {
                    "code": code,
                    "symbol": symbol,
                    "matrix_id": matrix_id,
                    "slippage_bps": slippage_bps,
                    "segment_start": start,
                    "segment_end": end,
                    "segment_input_cutoff": end,
                    "warmup_bars_before_segment": sum(
                        1 for bar in segment_bars if bar.date < start
                    ),
                }
                for row in payload["decisions"]:
                    events.append(
                        {
                            **base,
                            **_lineage_fields(lineage, row.get("date")),
                            "kind": "DECISION",
                            "strategy": row.get("strategy"),
                            "trade_date": row.get("date"),
                            **project_fields(row, DECISION_FIELDS, kind="decision"),
                        }
                    )
                for row in payload["transactions"]:
                    monitor = row.get("date")
                    fields = _lineage_fields(lineage, monitor)
                    events.append(
                        {
                            **base,
                            **fields,
                            "kind": "TRANSACTION",
                            "strategy": row.get("strategy")
                            or leg_strategies.get(str(row.get("leg_id"))),
                            "trade_date": monitor,
                            "same_day_as_source_selection": fields["source_selection_date"]
                            == fields["event_monitor_date"],
                            **project_fields(row, TRANSACTION_FIELDS, kind="transaction"),
                        }
                    )
                entry_index, reclaim_index = _stop_lineage_index(payload["decisions"])
                for row in payload["legs"]:
                    entry = row.get("entry_date")
                    exit_day = row.get("exit_date")
                    fields = _lineage_fields(lineage, entry)
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
                        leg["exit_source_selection_date"] = _lineage_fields(lineage, exit_day)[
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
                    events.append(leg)
    return events


def audit_events(
    events: Sequence[Mapping[str, Any]], segments_by_code: Mapping[str, Sequence[tuple[date, date]]]
) -> dict[str, int]:
    """Engineering causality audit; every counter can actually fail."""

    def as_date(value: Any) -> date | None:
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None
        return None

    outside = 0
    after_window = 0
    same_day = 0
    forced_end = 0
    flagged = 0
    leg_entry_outside = 0
    leg_exit_outside = 0
    unmapped = 0
    for event in events:
        segments = segments_by_code[str(event["code"])]
        if event.get("source_selection_date") in (None, MONITOR_TO_SELECTION_FALLBACK):
            unmapped += 1
        if event["kind"] in {"DECISION", "TRANSACTION", "LEG"}:
            day = as_date(event.get("trade_date"))
            if day is not None:
                if not any(start <= day <= end for start, end in segments):
                    outside += 1
                if day > MONITOR_END:
                    after_window += 1
        if event["kind"] == "TRANSACTION" and event.get("role") in {"MOTHER", "ADD"}:
            execution = as_date(event.get("trade_date"))
            source = as_date(event.get("source_selection_date"))
            if execution is not None and source is not None and execution <= source:
                same_day += 1
        if event["kind"] == "LEG":
            entry = as_date(event.get("entry_date"))
            if entry is not None and not any(start <= entry <= end for start, end in segments):
                leg_entry_outside += 1
            exit_day = as_date(event.get("exit_date"))
            if exit_day is not None and not any(
                start <= exit_day <= end for start, end in segments
            ):
                leg_exit_outside += 1
            reason = str(event.get("exit_reason") or "").upper()
            if any(token in reason for token in FORCED_EXIT_TOKENS):
                forced_end += 1
            if bool(event.get("open_censored")) != (exit_day is None):
                flagged += 1
    return {
        "events_outside_eligible_segment": outside,
        "events_after_monitor_window": after_window,
        "same_day_entry_as_selection_date": same_day,
        "leg_entries_outside_eligible_segment": leg_entry_outside,
        "leg_exits_outside_eligible_segment": leg_exit_outside,
        "forced_end_exit": forced_end,
        "open_censored_flag_mismatches": flagged,
        "events_without_selection_lineage": unmapped,
    }


def frozen_rule_gates() -> dict[str, str]:
    """PASS only while the frozen contract allow-lists still match."""

    exit_list = tuple(sorted(v.value for v in ExitVariant))
    add_list = tuple(sorted(v.value for v in AddVariant))
    entry_list = tuple(sorted(v.value for v in EntryVariant))
    exit_ok = exit_list == EXPECTED_EXIT_ALLOWLIST and not any(
        "008" in value for value in exit_list
    )
    rules_ok = (
        STRATEGY_VERSION == EXPECTED_STRATEGY_VERSION
        and SPEC_SHA256 == EXPECTED_SPEC_SHA256
        and exit_ok
        and add_list == EXPECTED_ADD_ALLOWLIST
        and entry_list == EXPECTED_ENTRY_ALLOWLIST
    )
    return {
        "exit_008_disabled": "PASS" if exit_ok else "HOLD",
        "rules_changed": "PASS" if rules_ok else "HOLD",
        "performance_not_reported_or_aggregated": "PASS",
    }


def economic_event_key(leg: Mapping[str, Any]) -> str:
    return "|".join(
        str(leg.get(field))
        for field in ("symbol", "strategy", "entry_date", "pullback_event_id", "initial_stop")
    )


def _comparable(row: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {field: row.get(field) for field in fields}


def prefix_replay(
    *,
    coverage: Mapping[str, Sequence[tuple[AddVariant, ExitVariant]]],
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    symbol_by_code: Mapping[str, str],
    segments_by_code: Mapping[str, Sequence[tuple[date, date]]],
    binding: PriceBinding,
    data_hash_by_symbol: Mapping[str, str],
    lineage: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Replay every covered segment truncated at each official monitor date."""

    checks = 0
    mismatches: list[dict[str, Any]] = []
    per_code: dict[str, dict[str, Any]] = {}
    monitor_days = sorted(date.fromisoformat(key) for key in lineage)
    for code, matrices in coverage.items():
        symbol = symbol_by_code[code]
        bars = bars_by_symbol[symbol]
        covered: list[str] = []
        for add_variant, exit_variant in matrices:
            matrix_id = MATRIX_GROUPS[(add_variant, exit_variant)]
            covered.append(matrix_id)
            for start, end in segments_by_code[code]:
                full_bars = [bar for bar in bars if bar.date <= end]
                full = _run_one(
                    symbol=symbol,
                    bars=full_bars,
                    manifest=_manifest_for(
                        code=code,
                        matrix_id=matrix_id,
                        bars=full_bars,
                        binding=binding,
                        data_hash=data_hash_by_symbol[symbol],
                        add_variant=add_variant,
                        exit_variant=exit_variant,
                        start=start,
                        end=end,
                        run_suffix="-full",
                    ),
                    add_variant=add_variant,
                    exit_variant=exit_variant,
                    slippage_bps=0,
                    start=start,
                    end=end,
                )
                for monitor in [day for day in monitor_days if start <= day <= end]:
                    truncated = [bar for bar in full_bars if bar.date <= monitor]
                    prefix = _run_one(
                        symbol=symbol,
                        bars=truncated,
                        manifest=_manifest_for(
                            code=code,
                            matrix_id=matrix_id,
                            bars=truncated,
                            binding=binding,
                            data_hash=data_hash_by_symbol[symbol],
                            add_variant=add_variant,
                            exit_variant=exit_variant,
                            start=start,
                            end=monitor,
                            run_suffix="-prefix",
                        ),
                        add_variant=add_variant,
                        exit_variant=exit_variant,
                        slippage_bps=0,
                        start=start,
                        end=monitor,
                    )
                    checks += 1
                    full_decisions = [
                        _comparable(row, DECISION_FIELDS)
                        for row in full["decisions"]
                        if date.fromisoformat(str(row["date"])) <= monitor
                    ]
                    prefix_decisions = [
                        _comparable(row, DECISION_FIELDS) for row in prefix["decisions"]
                    ]
                    if full_decisions != prefix_decisions:
                        mismatches.append(
                            {
                                "code": code,
                                "matrix_id": matrix_id,
                                "monitor_date": monitor,
                                "kind": "DECISION",
                            }
                        )
                    full_tx = [
                        _comparable(row, TRANSACTION_FIELDS)
                        for row in full["transactions"]
                        if date.fromisoformat(str(row["date"])) <= monitor
                    ]
                    prefix_tx = [
                        _comparable(row, TRANSACTION_FIELDS) for row in prefix["transactions"]
                    ]
                    if full_tx != prefix_tx:
                        mismatches.append(
                            {
                                "code": code,
                                "matrix_id": matrix_id,
                                "monitor_date": monitor,
                                "kind": "TRANSACTION",
                            }
                        )
                    prefix_legs = {str(row.get("leg_id")): row for row in prefix["legs"]}
                    for row in full["legs"]:
                        leg_id = str(row.get("leg_id"))
                        if date.fromisoformat(str(row["entry_date"])) > monitor:
                            continue
                        if leg_id not in prefix_legs:
                            mismatches.append(
                                {
                                    "code": code,
                                    "matrix_id": matrix_id,
                                    "monitor_date": monitor,
                                    "kind": "LEG_PRESENT",
                                    "leg_id": leg_id,
                                }
                            )
                        elif _comparable(row, PREFIX_LEG_ENTRY_FIELDS) != _comparable(
                            prefix_legs[leg_id], PREFIX_LEG_ENTRY_FIELDS
                        ):
                            mismatches.append(
                                {
                                    "code": code,
                                    "matrix_id": matrix_id,
                                    "monitor_date": monitor,
                                    "kind": "LEG_ENTRY_FIELDS",
                                    "leg_id": leg_id,
                                }
                            )
        per_code[code] = {"checks": checks, "matrices": sorted(set(covered))}
    all_matrices = {f"M{index}" for index in range(1, 7)}
    covered_matrices = {matrix for entry in per_code.values() for matrix in entry["matrices"]}
    return {
        "checks": checks,
        "mismatches": mismatches,
        "per_symbol": per_code,
        "covered_matrices": sorted(covered_matrices),
        "uncovered_matrices": sorted(all_matrices - covered_matrices),
        "status": "PASS" if not mismatches and covered_matrices == all_matrices else "PARTIAL",
        "note": (
            "A truncated replay reproduces the full run's decisions, transactions and the "
            "entry fields of legs already open at that monitor date; future exit fields and "
            "post-entry stop state (effective_stop, breakeven_stop, dow_stop, two_r_date) are "
            "intentionally not compared because the frozen engine keeps updating them."
        ),
    }


def _manual_trace(
    *,
    events: Sequence[Mapping[str, Any]],
    bars: Sequence[Bar],
    code: str,
    matrix_id: str,
    lineage: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Hand-checkable trace for one witness and one matrix."""

    wanted = {"2022-01-05", "2022-01-06", "2022-01-07"}
    price_rows = [
        {
            "date": bar.date,
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "monitor_lineage": lineage.get(bar.date.isoformat()),
        }
        for bar in bars
        if bar.date.isoformat() in wanted
    ]
    timeline = [
        dict(row)
        for row in events
        if row["code"] == code and row["matrix_id"] == matrix_id and row["kind"] != "LEG"
    ]
    legs = [
        dict(row)
        for row in events
        if row["code"] == code and row["matrix_id"] == matrix_id and row["kind"] == "LEG"
    ]
    return {
        "code": code,
        "matrix_id": matrix_id,
        "bars": price_rows,
        "decision_and_transaction_timeline": timeline,
        "legs": legs,
        "stop_lineage_note": (
            "The v0.4.1 A-group initial stop is the pullback event low of the first break "
            "below the fast average that is later reclaimed; it is not a confirmed pivot, "
            "not a course pivot and not a Dow defense."
        ),
    }


def _matrix_equivalence(events: Sequence[Mapping[str, Any]], *, code: str) -> dict[str, Any]:
    """Show which fields are identical across matrices for one witness."""

    legs = [row for row in events if row["code"] == code and row["kind"] == "LEG"]
    fields = sorted(
        {
            key
            for leg in legs
            for key in leg
            if key not in {"matrix_id", "segment_start", "segment_end", "segment_input_cutoff"}
        }
    )
    per_matrix = {
        str(leg["matrix_id"]): canonical_sha256({field: leg.get(field) for field in fields})
        for leg in legs
    }
    return {
        "code": code,
        "compared_fields": fields,
        "matrix_digests": per_matrix,
        "distinct_digests": len(set(per_matrix.values())),
        "identical_across_matrices": len(set(per_matrix.values())) <= 1,
    }


def run_witness_smoke(*, workdata_root: Path, output_dir: Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite witness smoke output: {output_dir}")
    root = Path(workdata_root)
    runner = resolve_runner_commit(Path(__file__).resolve().parents[2])
    RUNNER_COMMIT["value"] = str(runner["runner_commit"])
    calendar, documents = load_verified_l10(root, root / CALENDAR_RELATIVE_PATH)
    binding = resolve_price_binding(root, documents)
    import csv

    identity_rows = list(csv.DictReader(binding.identity_csv.open(encoding="utf-8-sig")))
    witness_rows, pattern = derive_membership(documents, identity_rows)
    segments = eligible_segments(pattern, documents)
    lineage = build_monitor_lineage(documents)
    symbols = [resolve_symbol(code, identity_rows) for code in WITNESS_CODES]
    bars, data_hash = load_witness_bars(binding, symbols=symbols, cutoff=MONITOR_END)
    symbol_by_code = {code: symbol for code, symbol in zip(WITNESS_CODES, symbols)}

    events: list[dict[str, Any]] = []
    per_symbol: dict[str, Any] = {}
    for code, symbol in symbol_by_code.items():
        symbol_events = run_segments(
            symbol=symbol,
            code=code,
            bars=bars[symbol],
            segments=segments[code],
            binding=binding,
            data_hash=data_hash[symbol],
            lineage=lineage,
        )
        events.extend(symbol_events)
        per_symbol[code] = {
            "symbol": symbol,
            "bar_count": len(bars[symbol]),
            "first_bar": bars[symbol][0].date,
            "last_bar": bars[symbol][-1].date,
            "data_hash": data_hash[symbol],
            "eligible_segments": [[start, end] for start, end in segments[code]],
            "event_count": len(symbol_events),
        }
    audit = audit_events(events, segments)
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
    }
    coverage = {
        "1101": tuple(
            (add_variant, exit_variant)
            for add_variant in AddVariant
            for exit_variant in ExitVariant
        ),
        "1447": ((AddVariant.R1_AGGREGATE, ExitVariant.DOW_TRAIL),),
        "1227": ((AddVariant.R1_AGGREGATE, ExitVariant.DOW_TRAIL),),
    }
    prefix = prefix_replay(
        coverage=coverage,
        bars_by_symbol=bars,
        symbol_by_code=symbol_by_code,
        segments_by_code=segments,
        binding=binding,
        data_hash_by_symbol=data_hash,
        lineage=lineage,
    )
    gates["prefix_replay_stability"] = prefix["status"]
    events.sort(
        key=lambda row: (
            row["code"],
            str(row["segment_start"]),
            row["kind"],
            str(row.get("trade_date")),
            str(row.get("leg_id") or ""),
        )
    )
    legs = [row for row in events if row["kind"] == "LEG"]
    economic_keys = sorted({economic_event_key(leg) for leg in legs})
    stop_sources = sorted({str(leg.get("stop_source")) for leg in legs})
    summary = {
        "event_count": len(events),
        "per_symbol": per_symbol,
        "matrix_leg_count": len(legs),
        "unique_economic_entry_count": len(economic_keys),
        "economic_event_keys": economic_keys,
        "stop_sources": stop_sources,
        "open_leg_count": sum(1 for leg in legs if leg.get("open_censored")),
    }
    m1_trace = _manual_trace(
        events=events, bars=bars["1101.TW"], code="1101", matrix_id="M1", lineage=lineage
    )
    equivalence = _matrix_equivalence(events, code="1101")

    payload = {
        "witness_membership.json": canonical_bytes(
            {
                "schema_version": SMOKE_SCHEMA,
                "module_version": MODULE_VERSION,
                "selection_days": list(SELECTION_DAYS),
                "monitor_window": [MONITOR_START, MONITOR_END],
                "monitor_to_selection_lineage": lineage,
                "witness_rows": [row.to_dict() for row in witness_rows],
                "membership_pattern": {code: list(flags) for code, flags in pattern.items()},
                "eligible_segments": {
                    code: [[start, end] for start, end in seg] for code, seg in segments.items()
                },
            }
        )
        + b"\n",
        "engine_events.jsonl": b"".join(canonical_bytes(row) + b"\n" for row in events),
        "summary.json": canonical_bytes(summary) + b"\n",
        "audit.json": canonical_bytes(
            {
                **audit,
                "monitor_window": [MONITOR_START, MONITOR_END],
                "gates": gates,
                "enforcement_note": (
                    "Daily eligibility is enforced by running the frozen engine once per "
                    "contiguous eligible segment; warmup bars before the segment only feed "
                    "indicators and the engine cannot decide outside its window."
                ),
                "performance_note": (
                    "The frozen engine still computes per-leg PnL internally; 02 excludes "
                    "those fields by name and neither reports nor aggregates any performance "
                    "quantity."
                ),
            }
        )
        + b"\n",
        "prefix_replay.json": canonical_bytes(prefix) + b"\n",
        "manual_trace_1101_M1.json": canonical_bytes(m1_trace) + b"\n",
        "matrix_equivalence_1101.json": canonical_bytes(equivalence) + b"\n",
        "price_binding.json": canonical_bytes(binding.to_dict()) + b"\n",
        "runner_commit.json": canonical_bytes(runner) + b"\n",
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
        "event_count": len(events),
        "audit": audit,
        "gates": gates,
        "summary": summary,
        "prefix_replay": {
            "status": prefix["status"],
            "checks": prefix["checks"],
            "mismatch_count": len(prefix["mismatches"]),
            "covered_matrices": prefix["covered_matrices"],
            "uncovered_matrices": prefix["uncovered_matrices"],
        },
        "matrix_equivalence_1101": equivalence,
        "output_dir": str(output_dir.resolve()),
        "artifact_manifest_sha256": sha256_file(output_dir / "artifact_manifest.json"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdata-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    result = run_witness_smoke(workdata_root=args.workdata_root, output_dir=args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    sys.exit(main())
