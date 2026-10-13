"""Isolated corrected KD(9,9,55) cycle + MA21 breakout study for 00635U.

The module is deliberately offline and contract-pinned.  It reuses the
accepted Strategy-B execution, cost, sizing, corporate-action, 2R and exit
machinery while keeping the new entry detector separate and auditable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .etf_00635u_strategy_v1 import (
    ETFResearchConfig,
    EngineState,
    EntryPolicy,
    EntryType,
    ExecutionTiming,
    IndicatorRow,
    PendingOrder,
    RawETFRow,
    RunResult,
    StrategyVersion,
    _apply_corporate_action,
    _apply_dividend_ledger,
    _candidate_stop,
    _can_execute_open,
    _emit_event,
    _execute_pending_open,
    _exit_signal,
    _record_entry,
    _record_exit,
    _signal_observable,
    build_causal_indicator_rows,
    decimal,
)


CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "docs" / "strategies" / "dual_ma"
    / "00635u-kd-cycle-ma21-breakout-entry-v4.md"
)
CONTRACT_SHA256 = "2cffeef16decb2b2747ccdff63d2c16143a5d25c03441b8d8858e2d71dc062fc"
SNAPSHOT_ID = "00635U_FINMIND_PRIMARY_20150415_20261008_V3_D6735B7A5FF5"
INPUT_MANIFEST_SHA256 = "4b0431d777f239241bc1dddd64dba124b61f990dcf907539815dbd685d761d9d"
STOP_CODE = "STOP_WAIT_00_00635U_KD_PIVOT_STOP_REPORT_QA"

ENTRY_RSV_PERIOD = 9
ENTRY_K_PERIOD = 9
ENTRY_D_PERIOD = 55


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class KDCyclePolicy(str, Enum):
    C = "C_LOW_KD_CYCLE_HIGH_ORIGINAL"
    D = "D_ALL_MARKET_KD_CYCLE"

    @property
    def engine_policy(self) -> EntryPolicy:
        return (
            EntryPolicy.KD_CYCLE_C
            if self == KDCyclePolicy.C else EntryPolicy.KD_CYCLE_D
        )


@dataclass(frozen=True)
class KDEntryRow:
    base: IndicatorRow
    technical_high: Decimal
    entry_k: Decimal | None
    entry_d: Decimal | None

    @property
    def day(self) -> date:
        return self.base.day


@dataclass(frozen=True)
class KDCycleRunResult:
    result: RunResult
    cycle_audit: tuple[dict[str, Any], ...]

    @property
    def state(self) -> EngineState:
        return self.result.state

    @property
    def final_equity(self) -> Decimal:
        return self.result.final_equity


def _ema(previous: Decimal | None, value: Decimal, span: int) -> Decimal:
    if previous is None:
        return value
    alpha = Decimal("2") / Decimal(span + 1)
    return value * alpha + previous * (Decimal("1") - alpha)


def build_kd_entry_rows(
    raw_rows: Sequence[RawETFRow | Mapping[str, Any]],
    *,
    ma_fast: int = 21,
    ma_slow: int = 144,
    entry_k_period: int = ENTRY_K_PERIOD,
    entry_d_period: int = ENTRY_D_PERIOD,
) -> list[KDEntryRow]:
    """Build canonical base indicators plus independent entry KD(9,9,55)."""

    normalized = [
        row if isinstance(row, RawETFRow) else RawETFRow.from_mapping(row)
        for row in raw_rows
    ]
    if min(ma_fast, ma_slow, entry_k_period, entry_d_period) < 1:
        raise ValueError("indicator periods must be positive")
    base_rows = build_causal_indicator_rows(
        normalized, ma_fast=ma_fast, ma_slow=ma_slow
    )
    highs: list[Decimal] = []
    lows: list[Decimal] = []
    k_value: Decimal | None = None
    d_value: Decimal | None = None
    output: list[KDEntryRow] = []
    for raw, base in zip(normalized, base_rows, strict=True):
        if raw.price_rebase_factor != 1:
            highs = [value * raw.price_rebase_factor for value in highs]
            lows = [value * raw.price_rebase_factor for value in lows]
        highs.append(raw.high)
        lows.append(raw.low)
        highs = highs[-ENTRY_RSV_PERIOD:]
        lows = lows[-ENTRY_RSV_PERIOD:]
        if len(highs) == ENTRY_RSV_PERIOD:
            highest, lowest = max(highs), min(lows)
            if highest != lowest:
                rsv = (raw.close - lowest) / (highest - lowest) * Decimal("100")
                rsv = max(Decimal("0"), min(Decimal("100"), rsv))
                k_value = (
                    rsv if k_value is None else
                    rsv / Decimal(entry_k_period)
                    + k_value * (Decimal("1") - Decimal("1") / Decimal(entry_k_period))
                )
                d_value = (
                    k_value if d_value is None else
                    k_value / Decimal(entry_d_period)
                    + d_value * (Decimal("1") - Decimal("1") / Decimal(entry_d_period))
                )
        output.append(KDEntryRow(base, raw.high, k_value, d_value))
    return output


def _relation(row: KDEntryRow) -> str:
    if row.entry_k is None or row.entry_d is None:
        return "NOT_READY"
    if row.entry_k < row.entry_d:
        return "LT"
    if row.entry_k > row.entry_d:
        return "GT"
    return "EQ"


def _ma21_breakout(previous: KDEntryRow | None, row: KDEntryRow) -> bool:
    return bool(
        previous is not None
        and previous.base.sma21 is not None
        and row.base.sma21 is not None
        and previous.base.technical_close <= previous.base.sma21
        and row.base.technical_close > row.base.sma21
    )


def _cycle_policy_branch(row: KDEntryRow, policy: KDCyclePolicy) -> tuple[bool, str]:
    if policy == KDCyclePolicy.D:
        return True, "D_ALL_MARKET_KD_CYCLE"
    if row.base.sma21 is None or row.base.sma144 is None:
        return False, "C_WARMUP"
    if row.base.sma21 < row.base.sma144:
        return True, "C_LOW_KD_CYCLE"
    if row.base.sma21 > row.base.sma144:
        return False, "C_HIGH_ORIGINAL_ONLY"
    return False, "C_EQUAL_NO_ENTRY"


class _CycleScanner:
    def __init__(self, policy: KDCyclePolicy):
        self.policy = policy
        self.phase = "SEEK_FIRST_LOW"
        self.sequence = 0
        self.cycle_id = ""
        self.first_start: date | None = None
        self.first_end: date | None = None
        self.l1: Decimal | None = None
        self.l1_date: date | None = None
        self.k1: Decimal | None = None
        self.k1_date: date | None = None
        self.golden_cross_date: date | None = None
        self.middle_start: date | None = None
        self.middle_end: date | None = None
        self.h1: Decimal | None = None
        self.h1_date: date | None = None
        self.death_cross_date: date | None = None
        self.second_start: date | None = None
        self.second_end: date | None = None
        self.k2: Decimal | None = None
        self.k2_date: date | None = None
        self.second_l1: Decimal | None = None
        self.second_l1_date: date | None = None

    def _apply_rebase(self, factor: Decimal) -> None:
        if factor == 1:
            return
        for name in ("l1", "h1", "second_l1"):
            value = getattr(self, name)
            if value is not None:
                setattr(self, name, value * factor)

    def _new_first(self, row: KDEntryRow) -> None:
        self.sequence += 1
        self.cycle_id = f"CYCLE{self.sequence:06d}"
        self.phase = "FIRST_LOW"
        self.first_start = self.first_end = row.day
        self.l1 = row.base.technical_low
        self.l1_date = row.day
        self.k1 = row.entry_k
        self.k1_date = row.day
        self.golden_cross_date = None
        self.middle_start = self.middle_end = None
        self.h1 = None
        self.h1_date = None
        self.death_cross_date = None
        self.second_start = self.second_end = None
        self.k2 = None
        self.k2_date = None
        self.second_l1 = None
        self.second_l1_date = None

    def _update_first(self, row: KDEntryRow) -> None:
        self.first_end = row.day
        if row.base.technical_low < self.l1:
            self.l1, self.l1_date = row.base.technical_low, row.day
        if row.entry_k < self.k1:
            self.k1, self.k1_date = row.entry_k, row.day

    def _start_middle(self, row: KDEntryRow) -> None:
        self.phase = "MIDDLE_HIGH"
        self.golden_cross_date = row.day
        self.middle_start = self.middle_end = row.day
        self.h1, self.h1_date = row.technical_high, row.day

    def _update_middle(self, row: KDEntryRow) -> None:
        self.middle_end = row.day
        if row.technical_high > self.h1:
            self.h1, self.h1_date = row.technical_high, row.day

    def _start_second(self, row: KDEntryRow) -> None:
        self.phase = "SECOND_LOW"
        self.death_cross_date = row.day
        self.second_start = self.second_end = row.day
        self.k2, self.k2_date = row.entry_k, row.day
        self.second_l1, self.second_l1_date = row.base.technical_low, row.day

    def _update_second(self, row: KDEntryRow) -> None:
        self.second_end = row.day
        if row.entry_k < self.k2:
            self.k2, self.k2_date = row.entry_k, row.day
        if row.base.technical_low < self.second_l1:
            self.second_l1, self.second_l1_date = row.base.technical_low, row.day

    def _snapshot(
        self,
        row: KDEntryRow,
        *,
        status: str,
        previous: KDEntryRow | None,
        in_signal_window: bool = False,
        same_day_golden: bool = False,
        signal_generated: bool = False,
        branch: str = "",
        pivot_end: date | None = None,
        pivot_low: Decimal | None = None,
        pivot_low_date: date | None = None,
    ) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "date": row.day,
            "cycle_phase": self.phase,
            "cycle_status": status,
            "entry_k_relation": {
                "LT": "K_LT_D_SEGMENT", "GT": "K_GT_D_SEGMENT",
                "EQ": "EQUAL_BOUNDARY", "NOT_READY": "NOT_READY",
            }[_relation(row)],
            "first_k_lt_d_start": self.first_start,
            "first_k_lt_d_end": self.first_end,
            "l1": self.l1,
            "l1_date": self.l1_date,
            "k1": self.k1,
            "k1_date": self.k1_date,
            "first_golden_cross_date": self.golden_cross_date,
            "middle_k_gt_d_start": self.middle_start,
            "middle_k_gt_d_end": self.middle_end,
            "h1": self.h1,
            "h1_date": self.h1_date,
            "second_death_cross_date": self.death_cross_date,
            "second_k_lt_d_start": self.second_start,
            "second_k_lt_d_end": self.second_end,
            "pivot_start": self.second_start if pivot_end is not None else None,
            "pivot_end": pivot_end,
            "pivot_low": pivot_low,
            "pivot_low_date": pivot_low_date,
            "pivot_uses_signal_day": bool(
                pivot_end is not None and pivot_end >= row.day
            ),
            "k2_so_far": self.k2,
            "k2_low_date": self.k2_date,
            "k2_strictly_above_k1": (
                False if self.k2 is None or self.k1 is None else self.k2 > self.k1
            ),
            "in_signal_window": in_signal_window,
            "ma21_breakout": _ma21_breakout(previous, row),
            "entry_k": row.entry_k,
            "entry_d": row.entry_d,
            "breakout_kd_state": (
                "SECOND_END_GOLDEN_CROSS" if same_day_golden else
                "SECOND_K_LT_D" if in_signal_window else "OUTSIDE_WINDOW"
            ),
            "same_day_golden_cross": same_day_golden,
            "signal_generated": signal_generated,
            "strategy_branch": branch,
            "sma21": row.base.sma21,
            "sma144": row.base.sma144,
            "technical_close": row.base.technical_close,
            "technical_low": row.base.technical_low,
            "initial_stop": pivot_low if signal_generated else None,
            "execution_status": "SIGNAL_GENERATED" if signal_generated else "",
            "execution_detail": "",
        }

    def _candidate(
        self,
        previous: KDEntryRow | None,
        row: KDEntryRow,
        *,
        same_day_golden: bool,
        pivot_end: date | None,
        pivot_low: Decimal | None,
        pivot_low_date: date | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        allowed, branch = _cycle_policy_branch(row, self.policy)
        valid = bool(self.k2 is not None and self.k1 is not None and self.k2 > self.k1)
        pivot_ready = bool(
            pivot_end is not None
            and pivot_low is not None
            and pivot_low_date is not None
            and self.second_start is not None
            and self.second_start <= pivot_low_date <= pivot_end < row.day
        )
        breakout_ready = valid and _ma21_breakout(previous, row) and allowed
        signal = breakout_ready and pivot_ready
        audit = self._snapshot(
            row,
            status=(
                "LEGAL_SIGNAL" if signal else
                "PIVOT_HISTORY_REQUIRED" if breakout_ready else
                "CANDIDATE_CHECKED"
            ),
            previous=previous,
            in_signal_window=True,
            same_day_golden=same_day_golden,
            signal_generated=signal,
            branch=branch,
            pivot_end=pivot_end,
            pivot_low=pivot_low,
            pivot_low_date=pivot_low_date,
        )
        return audit, dict(audit) if signal else None

    def _roll_second_to_first(self, row: KDEntryRow) -> None:
        second_start, second_end = self.second_start, self.second_end
        second_l1, second_l1_date = self.second_l1, self.second_l1_date
        k2, k2_date = self.k2, self.k2_date
        self.sequence += 1
        self.cycle_id = f"CYCLE{self.sequence:06d}"
        self.first_start, self.first_end = second_start, second_end
        self.l1, self.l1_date = second_l1, second_l1_date
        self.k1, self.k1_date = k2, k2_date
        self.golden_cross_date = row.day
        self.middle_start = self.middle_end = row.day
        self.h1, self.h1_date = row.technical_high, row.day
        self.death_cross_date = None
        self.second_start = self.second_end = None
        self.k2 = self.k2_date = None
        self.second_l1 = self.second_l1_date = None
        self.phase = "MIDDLE_HIGH"

    def process(
        self, previous: KDEntryRow | None, row: KDEntryRow
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        self._apply_rebase(row.base.price_rebase_factor)
        relation = _relation(row)
        prior_relation = "NOT_READY" if previous is None else _relation(previous)
        golden = prior_relation in {"LT", "EQ"} and relation == "GT"
        death = prior_relation in {"GT", "EQ"} and relation == "LT"

        if relation == "NOT_READY":
            return self._snapshot(row, status="INDICATOR_WARMUP", previous=previous), None
        if self.phase == "SEEK_FIRST_LOW":
            if relation == "LT":
                self._new_first(row)
                return self._snapshot(row, status="FIRST_LOW_STARTED", previous=previous), None
            return self._snapshot(row, status="SEEKING_FIRST_LOW", previous=previous), None
        if self.phase == "CONSUMED_WAIT_HIGH":
            if relation == "GT":
                self.phase = "SEEK_FIRST_LOW"
            return self._snapshot(row, status="CONSUMED_PATTERN_NOT_REUSED", previous=previous), None
        if self.phase in {"FIRST_LOW", "FIRST_WAIT_HIGH"}:
            if relation == "LT":
                if self.phase == "FIRST_WAIT_HIGH":
                    self._new_first(row)
                    return self._snapshot(row, status="FIRST_LOW_RESTARTED_AFTER_EQUAL", previous=previous), None
                self._update_first(row)
                return self._snapshot(row, status="FIRST_LOW_UPDATED", previous=previous), None
            if golden:
                self._start_middle(row)
                return self._snapshot(row, status="MIDDLE_HIGH_STARTED", previous=previous), None
            self.phase = "FIRST_WAIT_HIGH"
            return self._snapshot(row, status="EQUAL_BOUNDARY_AFTER_FIRST_LOW", previous=previous), None
        if self.phase in {"MIDDLE_HIGH", "MIDDLE_WAIT_LOW"}:
            if relation == "GT":
                if self.phase == "MIDDLE_WAIT_LOW":
                    self.middle_start = row.day
                self.phase = "MIDDLE_HIGH"
                self._update_middle(row)
                return self._snapshot(row, status="MIDDLE_HIGH_UPDATED", previous=previous), None
            if death:
                self._start_second(row)
                audit, signal = self._candidate(
                    previous, row, same_day_golden=False,
                    pivot_end=None, pivot_low=None, pivot_low_date=None,
                )
                if signal is not None:
                    # A legal signal consumes the cycle immediately.  Execution
                    # outcome (fill, exclusion, pending order, or cancellation)
                    # must never make the same pattern eligible again.
                    self.phase = "CONSUMED_WAIT_HIGH"
                return audit, signal
            self.phase = "MIDDLE_WAIT_LOW"
            return self._snapshot(row, status="EQUAL_BOUNDARY_AFTER_MIDDLE", previous=previous), None
        if self.phase in {"SECOND_LOW", "SECOND_WAIT_HIGH"}:
            if relation == "LT":
                if self.phase == "SECOND_WAIT_HIGH":
                    # Equality ended the prior second segment.  A new low without
                    # a complete high cannot be joined to it, so roll causally.
                    self._new_first(row)
                    return self._snapshot(row, status="SECOND_BROKEN_NEW_FIRST_LOW", previous=previous), None
                pivot_end = self.second_end
                pivot_low = self.second_l1
                pivot_low_date = self.second_l1_date
                self._update_second(row)
                audit, signal = self._candidate(
                    previous, row, same_day_golden=False,
                    pivot_end=pivot_end, pivot_low=pivot_low,
                    pivot_low_date=pivot_low_date,
                )
                if signal is not None:
                    self.phase = "CONSUMED_WAIT_HIGH"
                return audit, signal
            if golden:
                audit, signal = self._candidate(
                    previous, row, same_day_golden=True,
                    pivot_end=self.second_end, pivot_low=self.second_l1,
                    pivot_low_date=self.second_l1_date,
                )
                if signal is not None:
                    self.phase = "SEEK_FIRST_LOW"
                    return audit, signal
                audit["cycle_status"] = "EXPIRED_ROLLED_FORWARD"
                self._roll_second_to_first(row)
                return audit, None
            self.phase = "SECOND_WAIT_HIGH"
            return self._snapshot(row, status="EQUAL_BOUNDARY_AFTER_SECOND", previous=previous, in_signal_window=True), None
        raise AssertionError(f"unknown cycle phase: {self.phase}")


def scan_kd_cycles(
    rows: Sequence[KDEntryRow], policy: KDCyclePolicy
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scanner = _CycleScanner(policy)
    audit: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    consumed_cycle_ids: set[str] = set()
    previous: KDEntryRow | None = None
    for row in rows:
        if not _signal_observable(row.base):
            audit.append(scanner._snapshot(
                row, status="NON_OBSERVABLE_SESSION", previous=previous
            ))
            continue
        item, signal = scanner.process(previous, row)
        audit.append(item)
        if signal is not None:
            cycle_id = signal["cycle_id"]
            if cycle_id in consumed_cycle_ids:
                raise AssertionError(
                    f"cycle generated more than one legal signal: {cycle_id}"
                )
            consumed_cycle_ids.add(cycle_id)
            signals.append(signal)
        if _signal_observable(row.base):
            previous = row
    return audit, signals


def assert_cycle_consumption(
    cycle_audit: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Fail fast if a cycle is reused after its first legal signal."""

    def key(row: Mapping[str, Any]) -> tuple[str, str]:
        return str(row.get("line_id", "")), str(row.get("cycle_id", ""))

    grouped: dict[tuple[str, str], dict[str, int]] = {}
    for row in cycle_audit:
        group_key = key(row)
        if not group_key[1]:
            continue
        counts = grouped.setdefault(
            group_key,
            {"signal_generated": 0, "entry_signal_generated": 0, "filled": 0},
        )
        counts["signal_generated"] += int(bool(row.get("signal_generated")))
        counts["entry_signal_generated"] += int(
            bool(row.get("entry_signal_generated"))
        )
        counts["filled"] += int(
            bool(row.get("signal_generated"))
            and row.get("execution_status") == "FILLED"
        )

    trade_counts: dict[tuple[str, str], int] = {}
    for row in trades:
        if (
            row.get("role") == "ENTRY"
            and row.get("entry_type") == EntryType.KD_CYCLE_9_9_55.value
            and row.get("cycle_id")
        ):
            group_key = key(row)
            trade_counts[group_key] = trade_counts.get(group_key, 0) + 1

    violations = []
    for group_key, counts in grouped.items():
        for field, count in counts.items():
            if count > 1:
                violations.append({
                    "line_id": group_key[0], "cycle_id": group_key[1],
                    "field": field, "count": count,
                })
    for group_key, count in trade_counts.items():
        if count > 1:
            violations.append({
                "line_id": group_key[0], "cycle_id": group_key[1],
                "field": "cycle_entry_trades", "count": count,
            })
    if violations:
        raise AssertionError(f"cycle consumption invariant violated: {violations}")
    return {
        "status": "PASS",
        "groups_checked": len(grouped),
        "signal_groups": sum(
            counts["signal_generated"] == 1 for counts in grouped.values()
        ),
        "filled_groups": sum(counts["filled"] == 1 for counts in grouped.values()),
        "cycle_entry_trade_groups": len(trade_counts),
        "maximum_signal_generated_per_line_cycle": max(
            (counts["signal_generated"] for counts in grouped.values()), default=0
        ),
        "maximum_entry_signal_generated_per_line_cycle": max(
            (counts["entry_signal_generated"] for counts in grouped.values()),
            default=0,
        ),
        "maximum_filled_per_line_cycle": max(
            (counts["filled"] for counts in grouped.values()), default=0
        ),
        "maximum_cycle_entry_trades_per_line_cycle": max(
            trade_counts.values(), default=0
        ),
    }


def assert_pivot_stops(
    cycle_audit: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]] = (),
    events: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Recompute every cycle signal's T-1 pivot and verify executions."""

    def line_cycle(row: Mapping[str, Any]) -> tuple[str, str]:
        return str(row.get("line_id", "")), str(row.get("cycle_id", ""))

    signals = [row for row in cycle_audit if row.get("signal_generated")]
    signal_by_key: dict[tuple[str, str, Any], Mapping[str, Any]] = {}
    for signal in signals:
        pivot_start = signal.get("pivot_start")
        pivot_end = signal.get("pivot_end")
        pivot_low = signal.get("pivot_low")
        pivot_low_date = signal.get("pivot_low_date")
        if not (
            pivot_start is not None
            and pivot_end is not None
            and pivot_low is not None
            and pivot_low_date is not None
            and pivot_start <= pivot_low_date <= pivot_end < signal["date"]
            and not signal.get("pivot_uses_signal_day")
        ):
            raise AssertionError(f"invalid or non-causal pivot: {signal}")
        expected_rows = [
            row for row in cycle_audit
            if line_cycle(row) == line_cycle(signal)
            and pivot_start <= row["date"] <= pivot_end
            and row.get("entry_k_relation") == "K_LT_D_SEGMENT"
        ]
        if not expected_rows:
            raise AssertionError(f"pivot has no completed second-low row: {signal}")
        expected_low = min(row["technical_low"] for row in expected_rows)
        expected_low_date = next(
            row["date"] for row in expected_rows
            if row["technical_low"] == expected_low
        )
        if pivot_low != expected_low or pivot_low_date != expected_low_date:
            raise AssertionError(
                f"pivot mismatch for {line_cycle(signal)} on {signal['date']}: "
                f"reported {(pivot_low, pivot_low_date)}, "
                f"expected {(expected_low, expected_low_date)}"
            )
        if signal.get("initial_stop") != pivot_low:
            raise AssertionError(f"signal stop does not equal pivot: {signal}")
        signal_by_key[(*line_cycle(signal), signal["date"])] = signal

    cycle_entries = [
        row for row in trades
        if row.get("role") == "ENTRY"
        and row.get("entry_type") == EntryType.KD_CYCLE_9_9_55.value
    ]
    for trade in cycle_entries:
        key = (
            str(trade.get("line_id", "")), str(trade.get("cycle_id", "")),
            trade["signal_date"],
        )
        signal = signal_by_key.get(key)
        if signal is None or trade.get("initial_stop") != signal.get("pivot_low"):
            raise AssertionError(f"cycle entry stop is not its causal pivot: {trade}")
        for field in ("pivot_start", "pivot_end", "pivot_low", "pivot_low_date"):
            if trade.get(field) != signal.get(field):
                raise AssertionError(f"cycle entry missing {field}: {trade}")

    cancellations = [
        row for row in events
        if row.get("event") == "ENTRY_REJECTED_GAP_AT_OR_BELOW_STOP"
        and (
            not row.get("line_id")
            or str(row.get("line_id", "")).startswith("KD_CYCLE_")
        )
    ]
    for event in cancellations:
        if event.get("fill_reference_price") > event.get("initial_stop"):
            raise AssertionError(f"invalid next-open pivot cancellation: {event}")

    return {
        "status": "PASS",
        "signals_checked": len(signals),
        "cycle_entries_checked": len(cycle_entries),
        "next_open_cancellations_checked": len(cancellations),
        "all_pivot_ends_strictly_before_signal": True,
        "all_cycle_entry_stops_equal_recomputed_pivot": True,
        "signal_day_and_future_lows_excluded": True,
        "next_open_cancellation_operator": "OPEN_LE_INITIAL_STOP",
        "protective_exit_operator": "TECHNICAL_CLOSE_LT_INITIAL_STOP",
    }


def _high_original_signal(previous: KDEntryRow | None, row: KDEntryRow) -> bool:
    return bool(
        previous is not None
        and row.base.sma21 is not None
        and row.base.sma144 is not None
        and row.base.sma21 > row.base.sma144
        and _ma21_breakout(previous, row)
    )


def run_kd_cycle_strategy(
    rows: Sequence[KDEntryRow],
    *,
    policy: KDCyclePolicy,
    timing: ExecutionTiming,
    slippage_bps: int = 20,
    initial_capital: Decimal = Decimal("500000"),
) -> KDCycleRunResult:
    """Run Strategy C or D with the accepted Strategy-B execution contract."""

    config = ETFResearchConfig(
        strategy=StrategyVersion.B,
        timing=timing,
        slippage_bps=slippage_bps,
        initial_capital=initial_capital,
        entry_policy=policy.engine_policy,
    )
    state = EngineState(cash=config.initial_capital)
    audit, signals = scan_kd_cycles(rows, policy)
    signal_by_day = {row["date"]: row for row in signals}
    audit_by_day = {row["date"]: row for row in audit}
    row_by_day = {row.day: row for row in rows}

    for wrapped in rows:
        row = wrapped.base
        _apply_corporate_action(state, row)
        _apply_dividend_ledger(state, row)
        open_execution = None
        if timing == ExecutionTiming.NEXT_SESSION_OPEN:
            open_execution = _execute_pending_open(state, config, row)

        if state.position is not None and row.cash_dividend_per_share and not row.dividend_stage:
            dividend = row.cash_dividend_per_share * state.position.shares
            state.cash += dividend
            state.position.cash_distributions += dividend
            _emit_event(state, {
                "date": row.day, "event": "CASH_DIVIDEND",
                "position_id": state.position.position_id, "cash": dividend,
            })

        previous_base = state.previous
        previous = None if previous_base is None else row_by_day[previous_base.day]
        exited_today = False
        reason = None if state.pending is not None else _exit_signal(
            state, config, previous_base, row
        )
        if reason is not None:
            if timing == ExecutionTiming.SIGNAL_CLOSE:
                _record_exit(
                    state, config, signal_date=row.day, fill_date=row.day,
                    reference_price=row.raw_close, reason=reason,
                )
                exited_today = True
            else:
                state.pending = PendingOrder("EXIT", row.day, exit_reason=reason)

        cycle_signal = signal_by_day.get(row.day)
        selected: EntryType | None = None
        signal_payload: Mapping[str, Any] | None = None
        if policy == KDCyclePolicy.C and _high_original_signal(previous, wrapped):
            selected = EntryType.HIGH_MA21
            signal_payload = {
                "cycle_id": "", "date": row.day,
                "strategy_branch": "C_HIGH_ORIGINAL",
            }
            audit_by_day[row.day]["cycle_status"] += "|C_HIGH_ORIGINAL_SIGNAL"
        elif cycle_signal is not None:
            selected = EntryType.KD_CYCLE_9_9_55
            signal_payload = cycle_signal

        candidate_stop = None
        if selected == EntryType.KD_CYCLE_9_9_55:
            candidate_stop = signal_payload.get("pivot_low")
        elif selected == EntryType.HIGH_MA21:
            candidate_stop = _candidate_stop(state.phase_low, previous_base, row)

        if selected is not None:
            blocked_reason = ""
            if state.position is not None:
                blocked_reason = "POSITION_ALREADY_OPEN"
            elif state.pending is not None:
                blocked_reason = "PENDING_ORDER_EXISTS"
            elif exited_today or open_execution == "EXIT":
                blocked_reason = "SAME_DAY_EXIT_NO_REENTRY"
            if blocked_reason:
                audit_by_day[row.day]["execution_status"] = "EXCLUDED"
                audit_by_day[row.day]["execution_detail"] = blocked_reason
            elif candidate_stop is None or candidate_stop >= row.technical_close:
                audit_by_day[row.day]["execution_status"] = "EXCLUDED"
                audit_by_day[row.day]["execution_detail"] = "INVALID_STOP"
                _emit_event(state, {"date": row.day, "event": "ENTRY_REJECTED_INVALID_STOP"})
            elif timing == ExecutionTiming.SIGNAL_CLOSE:
                before = len(state.trades)
                _record_entry(
                    state, config, signal_date=row.day, fill_date=row.day,
                    reference_price=row.raw_close, technical_entry=row.technical_close,
                    initial_stop=candidate_stop, entry_type=selected,
                    histogram=row.macd_histogram, entry_sma21=row.sma21,
                    entry_sma144=row.sma144,
                )
                audit_by_day[row.day]["execution_status"] = (
                    "FILLED" if len(state.trades) > before else "EXCLUDED"
                )
            else:
                state.pending = PendingOrder(
                    "ENTRY", row.day, entry_type=selected,
                    technical_entry=row.technical_close,
                    initial_stop=candidate_stop,
                    histogram_at_signal=row.macd_histogram,
                    entry_sma21=row.sma21,
                    entry_sma144=row.sma144,
                )
                audit_by_day[row.day]["execution_status"] = "PENDING_NEXT_OPEN"
            state.phase_low = None
        elif (
            _signal_observable(row) and row.sma21 is not None
            and row.technical_close <= row.sma21
        ):
            state.phase_low = min(
                value for value in (state.phase_low, row.technical_low)
                if value is not None
            )
        elif _signal_observable(row) and row.sma21 is not None:
            state.phase_low = None

        liquidation = Decimal("0")
        if state.position is not None:
            liquidation = config.cost_model.sell(
                row.raw_close, state.position.shares, config.instrument_type
            ).cash_amount
        equity = state.cash + liquidation
        receivable_cash = sum(
            (decimal(item["cash"]) for item in state.dividend_receivables.values()),
            Decimal("0"),
        )
        if state.cash < 0 or equity != state.cash + liquidation:
            raise AssertionError("daily accounting invariant violated")
        state.daily.append({
            "date": row.day,
            "cash": state.cash,
            "liquidation_value": liquidation,
            "equity": equity,
            "dividend_receivable_cash_excluded": receivable_cash,
            "equity_including_receivables_audit": equity + receivable_cash,
            "shares": 0 if state.position is None else state.position.shares,
            "pending": "" if state.pending is None else state.pending.side,
            "regime": (
                "ALL_MARKET_MA144_DIAGNOSTIC_ONLY"
                if policy == KDCyclePolicy.D else
                "WARMUP" if row.sma21 is None or row.sma144 is None else
                "HIGH" if row.sma21 > row.sma144 else
                "LOW" if row.sma21 < row.sma144 else "EQUAL_NO_ENTRY"
            ),
            "signal_ready": (
                wrapped.entry_k is not None and wrapped.entry_d is not None
                and row.sma21 is not None
                and (policy == KDCyclePolicy.D or row.sma144 is not None)
            ),
            "signal_observable": _signal_observable(row),
            "official_session": row.official_session,
            "tradable": row.tradable,
            "volume": row.volume,
            "tradability_reason": row.tradability_reason,
            "open_execution": "" if open_execution is None else open_execution,
        })
        if _signal_observable(row):
            state.previous = row
        state.last_processed_day = row.day

    entry_trades = [row for row in state.trades if row["role"] == "ENTRY"]
    for trade in entry_trades:
        source = audit_by_day.get(trade["signal_date"], {})
        is_cycle_entry = trade["entry_type"] == EntryType.KD_CYCLE_9_9_55.value
        trade.update({
            "cycle_id": source.get("cycle_id", "") if is_cycle_entry else "",
            "strategy_branch": (
                source.get("strategy_branch", "")
                if is_cycle_entry else "C_HIGH_ORIGINAL"
            ),
            "l1": source.get("l1") if is_cycle_entry else None,
            "l1_date": source.get("l1_date") if is_cycle_entry else None,
            "k1": source.get("k1") if is_cycle_entry else None,
            "k1_date": source.get("k1_date") if is_cycle_entry else None,
            "h1": source.get("h1") if is_cycle_entry else None,
            "h1_date": source.get("h1_date") if is_cycle_entry else None,
            "k2_so_far": source.get("k2_so_far") if is_cycle_entry else None,
            "entry_k": source.get("entry_k") if is_cycle_entry else None,
            "entry_d": source.get("entry_d") if is_cycle_entry else None,
            "same_day_golden_cross": (
                source.get("same_day_golden_cross", False)
                if is_cycle_entry else False
            ),
            "pivot_start": source.get("pivot_start") if is_cycle_entry else None,
            "pivot_end": source.get("pivot_end") if is_cycle_entry else None,
            "pivot_low": source.get("pivot_low") if is_cycle_entry else None,
            "pivot_low_date": (
                source.get("pivot_low_date") if is_cycle_entry else None
            ),
            "entry_sma21_diagnostic": row_by_day[trade["signal_date"]].base.sma21,
            "entry_sma144_diagnostic": row_by_day[trade["signal_date"]].base.sma144,
        })

    filled_by_signal = {row["signal_date"]: row for row in entry_trades}
    rejected_by_signal = {
        row.get("signal_date"): row
        for row in state.events
        if row.get("event") == "ENTRY_REJECTED_GAP_AT_OR_BELOW_STOP"
    }
    for item in audit:
        if item["date"] in filled_by_signal:
            trade = filled_by_signal[item["date"]]
            is_cycle_fill = (
                trade["entry_type"] == EntryType.KD_CYCLE_9_9_55.value
            )
            item["execution_status"] = "FILLED"
            item["execution_detail"] = trade["fill_date"]
            item["fill_price"] = trade["fill_price"]
            item["entry_type"] = trade["entry_type"]
            item["entry_signal_generated"] = is_cycle_fill
            item["strategy_branch"] = trade["strategy_branch"]
            item["initial_stop"] = trade["initial_stop"]
            item["initial_r"] = trade["initial_r"]
            item["fee_adjusted_two_r_threshold"] = trade["initial_r"] * Decimal("2")
            item["position_id"] = trade["position_id"]
            item["shares"] = trade["shares"]
        elif item["date"] in rejected_by_signal:
            event = rejected_by_signal[item["date"]]
            item["execution_status"] = "CANCELLED_OPEN_AT_OR_BELOW_STOP"
            item["execution_detail"] = event["date"]
            item["fill_price"] = event["fill_reference_price"]

    exit_by_position = {
        row["position_id"]: row for row in state.trades if row["role"] == "EXIT"
    }
    arm_by_position = {
        row["position_id"]: row
        for row in state.events
        if row.get("event") == "KD_2R_PERMANENTLY_ARMED"
    }
    priority_by_position = {
        row["position_id"]: row["event"]
        for row in state.events
        if row.get("event") in {"STOP_PRIORITY_OVER_KD", "STOP_PRIORITY_OVER_MACD"}
    }
    for item in audit:
        position_id = item.get("position_id")
        if not position_id:
            continue
        exit_trade = exit_by_position.get(position_id)
        arm = arm_by_position.get(position_id)
        item.update({
            "two_r_armed_date": None if arm is None else arm["date"],
            "two_r_net_pnl_at_arm": None if arm is None else arm["net_pnl"],
            "exit_signal_date": None if exit_trade is None else exit_trade["signal_date"],
            "exit_fill_date": None if exit_trade is None else exit_trade["fill_date"],
            "exit_reason": "OPEN" if exit_trade is None else exit_trade["exit_reason"],
            "stop_priority_evidence": priority_by_position.get(position_id, ""),
        })
        if exit_trade is not None:
            exit_row = row_by_day[exit_trade["signal_date"]].base
            previous_days = [day for day in row_by_day if day < exit_trade["signal_date"]]
            previous_exit = row_by_day[max(previous_days)].base if previous_days else None
            item.update({
                "exit_previous_k": None if previous_exit is None else previous_exit.k,
                "exit_previous_d": None if previous_exit is None else previous_exit.d,
                "exit_current_k": exit_row.k,
                "exit_current_d": exit_row.d,
                "exit_kd_new_death_cross": bool(
                    previous_exit is not None
                    and previous_exit.k is not None and previous_exit.d is not None
                    and exit_row.k is not None and exit_row.d is not None
                    and previous_exit.k >= previous_exit.d
                    and exit_row.k < exit_row.d
                ),
            })

    assert_cycle_consumption(audit, state.trades)
    assert_pivot_stops(audit, state.trades, state.events)

    result = RunResult(config=config, state=state)
    return KDCycleRunResult(result=result, cycle_audit=tuple(audit))


if sha256_file(CONTRACT) != CONTRACT_SHA256:
    raise RuntimeError("00635U KD-cycle contract SHA-256 mismatch")

