"""Isolated 0050 HIGH/LOW entry-exit matrix research.

The accepted final v1.7 package is a byte-read-only baseline.  This module
builds a separate research candidate using the same locked 2005 warm-up,
2006-01-02 through 2026-09-30 formal window, corporate actions, costs, and
technical/raw coordinate contract.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import etf_0050_2006_fixed_methods_v1 as locked
from . import etf_0050_execution_stability_v1 as analysis
from . import etf_0050_ma21_ma144_regime_v1 as accepted_regime
from .costs import CATHAY_WEB_028_TW_V1, size_entry
from .etf_0050_base_v1 import ETF0050Error
from .etf_0050_formal_v2 import (
    CONTINUOUS_CAPITAL,
    _artifact_rows,
    _verify_artifacts,
    _write_csv,
    _write_json,
)
from .etf_0050_formal_v1 import _jsonable
from .models import InstrumentType
from .paths import dev_run, report_path


VERSION = "dual-ma-0050-regime-entry-exit-comparison-v1"
START = date(2006, 1, 2)
END = date(2026, 9, 30)
CAPITAL = Decimal("500000")
TIMINGS = ("SIGNAL_CLOSE", "NEXT_SESSION_OPEN")
REGIMES = ("HIGH", "LOW")
ENTRIES = ("P1", "P2", "P3")
EXITS = ("E1", "E2", "E3")
SLIPPAGE_BPS = (20, 40, 60)
STOP_CODE = "STOP_WAIT_00_0050_REGIME_ENTRY_EXIT_QA"

RUN_ROOT_DEFAULT = dev_run("02-etf-0050-regime-entry-exit-comparison-v1")
REPORT_ROOT_DEFAULT = report_path(
    "etf", "0050", "experiments", "2006-2026-regime-entry-exit-comparison-v1"
)
SEALED_RUN = dev_run("02-etf-0050-final-strategy-v1-7")
SEALED_REPORT = report_path("etf", "0050", "final", "2006-2026-final-strategy-v1-7")
SEALED_RUN_MANIFEST_SHA256 = "7a560d7f23f7f6e222573abca6b3d66da7caac515727acc15200f04ab479b174"
SEALED_REPORT_MANIFEST_SHA256 = "fc7181a77496827996650708df97ee3ba9bb8d58132b035f6455c5c67d1e8b7f"
SEALED_READBACK_SHA256 = "366fdcc7bb4c554f467c0942e9b85c652f7c37b82a070e1c3878634586751c94"


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def validate_sealed_v17() -> dict[str, Any]:
    pins = {
        "run_manifest": (SEALED_RUN / "manifest.json", SEALED_RUN_MANIFEST_SHA256),
        "report_manifest": (
            SEALED_REPORT / "artifact-manifest.json", SEALED_REPORT_MANIFEST_SHA256,
        ),
        "run_readback": (SEALED_RUN / "readback.json", SEALED_READBACK_SHA256),
        "report_readback": (SEALED_REPORT / "readback.json", SEALED_READBACK_SHA256),
    }
    checks = {
        name: {"path": str(path), "expected": expected, "actual": _sha(path)}
        for name, (path, expected) in pins.items()
    }
    if any(row["actual"] != row["expected"] for row in checks.values()):
        raise ETF0050Error(f"sealed v1.7 pin mismatch: {checks}")
    run_manifest = json.loads((SEALED_RUN / "manifest.json").read_text(encoding="utf-8-sig"))
    report_manifest = json.loads(
        (SEALED_REPORT / "artifact-manifest.json").read_text(encoding="utf-8-sig")
    )
    run_bad = _verify_artifacts(SEALED_RUN, run_manifest["artifacts"])
    report_bad = _verify_artifacts(SEALED_REPORT, report_manifest["artifacts"])
    if run_bad or report_bad:
        raise ETF0050Error(f"sealed v1.7 artifact mismatch: {run_bad}; {report_bad}")
    return {
        "status": "PASS_EXACT", "pins": checks,
        "run_artifacts": len(run_manifest["artifacts"]),
        "report_artifacts": len(report_manifest["artifacts"]),
        "run_mismatches": run_bad, "report_mismatches": report_bad,
    }


def _relation(row: Mapping[str, Any]) -> str:
    if row.get("entry_k") is None or row.get("entry_d") is None:
        return "NOT_READY"
    if _d(row["entry_k"]) < _d(row["entry_d"]):
        return "LT"
    if _d(row["entry_k"]) > _d(row["entry_d"]):
        return "GT"
    return "EQ"


def build_indicator_rows(
    raw_rows: Sequence[Mapping[str, Any]], events: Sequence[Any]
) -> list[dict[str, Any]]:
    """Build the accepted indicators plus independent entry KD(9,9,55)."""
    rows = [dict(row) for row in accepted_regime.build_indicator_rows(raw_rows, events)]
    highs: list[Decimal] = []
    lows: list[Decimal] = []
    k_value: Decimal | None = None
    d_value: Decimal | None = None
    for row, raw in zip(rows, raw_rows, strict=True):
        factor = _d(row["price_rebase_factor"])
        if factor != 1:
            highs = [value * factor for value in highs]
            lows = [value * factor for value in lows]
        high, low = _d(raw["high"]), _d(raw["low"])
        highs.append(high)
        lows.append(low)
        highs, lows = highs[-9:], lows[-9:]
        if len(highs) == 9 and max(highs) != min(lows):
            rsv = (_d(raw["close"]) - min(lows)) / (max(highs) - min(lows)) * 100
            rsv = max(Decimal("0"), min(Decimal("100"), rsv))
            k_value = rsv if k_value is None else rsv / 9 + k_value * Decimal(8) / 9
            d_value = k_value if d_value is None else k_value / 55 + d_value * Decimal(54) / 55
        row.update({
            "technical_high": high, "technical_low": low,
            "raw_open": _d(raw["open"]), "raw_high": high, "raw_low": low,
            "entry_k": k_value, "entry_d": d_value,
        })
    return rows


@dataclass
class _CycleState:
    phase: str = "SEEK_FIRST_LOW"
    sequence: int = 0
    cycle_id: str = ""
    first_start: date | None = None
    first_end: date | None = None
    l1: Decimal | None = None
    l1_date: date | None = None
    k1: Decimal | None = None
    k1_date: date | None = None
    golden_date: date | None = None
    middle_start: date | None = None
    middle_end: date | None = None
    h1: Decimal | None = None
    h1_date: date | None = None
    death_date: date | None = None
    second_start: date | None = None
    second_end: date | None = None
    k2: Decimal | None = None
    k2_date: date | None = None
    pivot_low: Decimal | None = None
    pivot_low_date: date | None = None

    def rebase(self, factor: Decimal) -> None:
        if factor != 1:
            for name in ("l1", "h1", "pivot_low"):
                value = getattr(self, name)
                if value is not None:
                    setattr(self, name, value * factor)

    def new_first(self, row: Mapping[str, Any]) -> None:
        self.sequence += 1
        self.cycle_id = f"KD{self.sequence:06d}"
        self.phase = "FIRST_LOW"
        self.first_start = self.first_end = row["date"]
        self.l1, self.l1_date = _d(row["technical_low"]), row["date"]
        self.k1, self.k1_date = _d(row["entry_k"]), row["date"]
        self.golden_date = self.middle_start = self.middle_end = None
        self.h1 = self.h1_date = self.death_date = None
        self.second_start = self.second_end = None
        self.k2 = self.k2_date = self.pivot_low = self.pivot_low_date = None


def _breakout(previous: Mapping[str, Any] | None, row: Mapping[str, Any]) -> bool:
    return bool(
        previous is not None and previous.get("sma21") is not None
        and row.get("sma21") is not None
        and _d(previous["technical_close"]) <= _d(previous["sma21"])
        and _d(row["technical_close"]) > _d(row["sma21"])
    )


def kd_cycle_candidates(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[date, dict[str, Any]], list[dict[str, Any]]]:
    """Causal two-pullback KD state machine copied from the accepted pivot contract."""
    state = _CycleState()
    previous: Mapping[str, Any] | None = None
    signals: dict[date, dict[str, Any]] = {}
    audit: list[dict[str, Any]] = []

    def snapshot(row: Mapping[str, Any], status: str, signal: bool = False,
                 pivot_end: date | None = None, pivot_low: Decimal | None = None,
                 pivot_low_date: date | None = None) -> dict[str, Any]:
        return {
            "date": row["date"], "cycle_id": state.cycle_id,
            "cycle_phase": state.phase, "cycle_status": status,
            "entry_k_relation": _relation(row),
            "first_k_lt_d_start": state.first_start,
            "first_k_lt_d_end": state.first_end,
            "l1": state.l1, "l1_date": state.l1_date,
            "k1": state.k1, "k1_date": state.k1_date,
            "first_golden_cross_date": state.golden_date,
            "middle_k_gt_d_start": state.middle_start,
            "middle_k_gt_d_end": state.middle_end,
            "h1": state.h1, "h1_date": state.h1_date,
            "second_death_cross_date": state.death_date,
            "second_k_lt_d_start": state.second_start,
            "second_k_lt_d_end": state.second_end,
            "k2_so_far": state.k2, "k2_low_date": state.k2_date,
            "k2_strictly_above_k1": bool(
                state.k1 is not None and state.k2 is not None and state.k2 > state.k1
            ),
            "ma21_breakout": _breakout(previous, row),
            "signal_generated": signal,
            "pivot_start": state.second_start if pivot_end is not None else None,
            "pivot_end": pivot_end, "pivot_low": pivot_low,
            "pivot_low_date": pivot_low_date,
            "entry_k": row.get("entry_k"), "entry_d": row.get("entry_d"),
        }

    for row in rows:
        state.rebase(_d(row["price_rebase_factor"]))
        relation = _relation(row)
        prior = "NOT_READY" if previous is None else _relation(previous)
        golden = prior in {"LT", "EQ"} and relation == "GT"
        death = prior in {"GT", "EQ"} and relation == "LT"
        item: dict[str, Any]
        if relation == "NOT_READY":
            item = snapshot(row, "INDICATOR_WARMUP")
        elif state.phase == "SEEK_FIRST_LOW":
            if relation == "LT":
                state.new_first(row)
                item = snapshot(row, "FIRST_LOW_STARTED")
            else:
                item = snapshot(row, "SEEKING_FIRST_LOW")
        elif state.phase == "CONSUMED_WAIT_HIGH":
            if relation == "GT":
                state.phase = "SEEK_FIRST_LOW"
            item = snapshot(row, "CONSUMED_PATTERN_NOT_REUSED")
        elif state.phase in {"FIRST_LOW", "FIRST_WAIT_HIGH"}:
            if relation == "LT":
                if state.phase == "FIRST_WAIT_HIGH":
                    state.new_first(row)
                    item = snapshot(row, "FIRST_LOW_RESTARTED_AFTER_EQUAL")
                else:
                    state.first_end = row["date"]
                    if _d(row["technical_low"]) < state.l1:
                        state.l1, state.l1_date = _d(row["technical_low"]), row["date"]
                    if _d(row["entry_k"]) < state.k1:
                        state.k1, state.k1_date = _d(row["entry_k"]), row["date"]
                    item = snapshot(row, "FIRST_LOW_UPDATED")
            elif golden:
                state.phase = "MIDDLE_HIGH"
                state.golden_date = state.middle_start = state.middle_end = row["date"]
                state.h1, state.h1_date = _d(row["technical_high"]), row["date"]
                item = snapshot(row, "MIDDLE_HIGH_STARTED")
            else:
                state.phase = "FIRST_WAIT_HIGH"
                item = snapshot(row, "EQUAL_BOUNDARY_AFTER_FIRST_LOW")
        elif state.phase in {"MIDDLE_HIGH", "MIDDLE_WAIT_LOW"}:
            if relation == "GT":
                state.phase = "MIDDLE_HIGH"
                state.middle_end = row["date"]
                if _d(row["technical_high"]) > state.h1:
                    state.h1, state.h1_date = _d(row["technical_high"]), row["date"]
                item = snapshot(row, "MIDDLE_HIGH_UPDATED")
            elif death:
                state.phase = "SECOND_LOW"
                state.death_date = state.second_start = state.second_end = row["date"]
                state.k2, state.k2_date = _d(row["entry_k"]), row["date"]
                state.pivot_low, state.pivot_low_date = _d(row["technical_low"]), row["date"]
                item = snapshot(
                    row,
                    "PIVOT_HISTORY_REQUIRED" if _breakout(previous, row) else "CANDIDATE_CHECKED",
                )
            else:
                state.phase = "MIDDLE_WAIT_LOW"
                item = snapshot(row, "EQUAL_BOUNDARY_AFTER_MIDDLE")
        else:
            if relation == "LT" and state.phase == "SECOND_WAIT_HIGH":
                state.new_first(row)
                item = snapshot(row, "SECOND_BROKEN_NEW_FIRST_LOW")
            elif relation == "LT":
                pivot_end, pivot_low, pivot_low_date = (
                    state.second_end, state.pivot_low, state.pivot_low_date,
                )
                state.second_end = row["date"]
                if _d(row["entry_k"]) < state.k2:
                    state.k2, state.k2_date = _d(row["entry_k"]), row["date"]
                if _d(row["technical_low"]) < state.pivot_low:
                    state.pivot_low, state.pivot_low_date = _d(row["technical_low"]), row["date"]
                valid = bool(state.k2 > state.k1 and _breakout(previous, row))
                item = snapshot(
                    row, "LEGAL_SIGNAL" if valid else "CANDIDATE_CHECKED", valid,
                    pivot_end, pivot_low, pivot_low_date,
                )
                if valid:
                    signals[row["date"]] = {
                        "entry_type": "P3_KD_9_9_55_TWO_PULLBACK_MA21_BREAKOUT",
                        **item,
                        "initial_stop": pivot_low,
                    }
                    state.phase = "CONSUMED_WAIT_HIGH"
            elif golden:
                valid = bool(state.k2 > state.k1 and _breakout(previous, row))
                item = snapshot(
                    row, "LEGAL_SIGNAL" if valid else "EXPIRED_ROLLED_FORWARD", valid,
                    state.second_end, state.pivot_low, state.pivot_low_date,
                )
                if valid:
                    signals[row["date"]] = {
                        "entry_type": "P3_KD_9_9_55_TWO_PULLBACK_MA21_BREAKOUT",
                        **item,
                        "initial_stop": state.pivot_low,
                    }
                    state.phase = "SEEK_FIRST_LOW"
                else:
                    old_start, old_end = state.second_start, state.second_end
                    old_low, old_low_date = state.pivot_low, state.pivot_low_date
                    old_k, old_k_date = state.k2, state.k2_date
                    state.sequence += 1
                    state.cycle_id = f"KD{state.sequence:06d}"
                    state.first_start, state.first_end = old_start, old_end
                    state.l1, state.l1_date = old_low, old_low_date
                    state.k1, state.k1_date = old_k, old_k_date
                    state.golden_date = state.middle_start = state.middle_end = row["date"]
                    state.h1, state.h1_date = _d(row["technical_high"]), row["date"]
                    state.death_date = state.second_start = state.second_end = None
                    state.k2 = state.k2_date = state.pivot_low = state.pivot_low_date = None
                    state.phase = "MIDDLE_HIGH"
            else:
                state.phase = "SECOND_WAIT_HIGH"
                item = snapshot(row, "EQUAL_BOUNDARY_AFTER_SECOND")
        audit.append(item)
        previous = row
    if len({row["cycle_id"] for row in signals.values()}) != len(signals):
        raise ETF0050Error("P3 cycle emitted more than one signal")
    return signals, audit


def entry_candidates(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, dict[date, dict[str, Any]]], list[dict[str, Any]]]:
    candidates: dict[str, dict[date, dict[str, Any]]] = {key: {} for key in ENTRIES}
    previous: Mapping[str, Any] | None = None
    phase_low: Decimal | None = None
    phase_low_date: date | None = None
    phase_start: date | None = None
    negative_start: date | None = None
    negative_low: Decimal | None = None
    negative_low_date: date | None = None
    for row in rows:
        factor = _d(row["price_rebase_factor"])
        if factor != 1:
            if phase_low is not None:
                phase_low *= factor
            if negative_low is not None:
                negative_low *= factor
        histogram = _d(row["histogram"])
        prior_hist = None if previous is None else _d(previous["histogram"])
        if _breakout(previous, row):
            choices = [(phase_low, phase_low_date)]
            choices.extend(((
                _d(previous["technical_low"]), previous["date"]),
                (_d(row["technical_low"]), row["date"])))
            stop, stop_date = min(
                (item for item in choices if item[0] is not None), key=lambda item: item[0]
            )
            candidates["P1"][row["date"]] = {
                "entry_type": "P1_MA21_BREAKOUT",
                "structure_start": phase_start,
                "structure_end": row["date"], "pivot_low": stop,
                "pivot_low_date": stop_date,
                "initial_stop": stop,
            }
            phase_low = None
            phase_low_date = phase_start = None
        elif row.get("sma21") is not None and _d(row["technical_close"]) <= _d(row["sma21"]):
            if phase_start is None:
                phase_start = row["date"]
            if phase_low is None or _d(row["technical_low"]) < phase_low:
                phase_low, phase_low_date = _d(row["technical_low"]), row["date"]
        elif row.get("sma21") is not None:
            phase_low = phase_low_date = phase_start = None

        if histogram < 0:
            if negative_start is None:
                negative_start = row["date"]
                negative_low = _d(row["technical_low"])
                negative_low_date = row["date"]
            else:
                if _d(row["technical_low"]) < negative_low:
                    negative_low, negative_low_date = _d(row["technical_low"]), row["date"]
        if prior_hist is not None and prior_hist <= 0 and histogram > 0:
            stop, stop_date = min(
                ((negative_low, negative_low_date),
                 (_d(previous["technical_low"]), previous["date"]),
                 (_d(row["technical_low"]), row["date"])),
                key=lambda item: item[0],
            )
            candidates["P2"][row["date"]] = {
                "entry_type": "P2_MACD_21_55_55_GREEN_TO_RED",
                "structure_start": negative_start,
                "structure_end": previous["date"],
                "pivot_low": stop,
                "pivot_low_date": stop_date,
                "initial_stop": stop,
            }
            negative_start = None
            negative_low = None
            negative_low_date = None
        elif histogram >= 0:
            negative_start = None
            negative_low = None
            negative_low_date = None
        previous = row
    candidates["P3"], kd_audit = kd_cycle_candidates(rows)
    return candidates, kd_audit


def _regime(row: Mapping[str, Any]) -> str:
    if row.get("sma21") is None or row.get("sma144") is None:
        return "WARMUP"
    return "HIGH" if _d(row["sma21"]) >= _d(row["sma144"]) else "LOW"


def _order_dict(order: Any) -> dict[str, Any]:
    return {
        "reference_price": order.reference_price, "fill_price": order.fill_price,
        "shares": order.shares, "gross": order.gross,
        "commission": order.commission, "tax": order.tax,
        "cash_amount": order.cash_amount,
    }


def raised_defense(current: Decimal, candidate: Decimal) -> Decimal:
    """E3 defense is monotone: an older/higher defense can never be lowered."""
    return max(current, candidate)


def resolve_exit_reason(*, close: Decimal, position: Mapping[str, Any],
                        macd_death: bool, kd_death: bool,
                        legacy_low_stop_inactive: bool = False) -> str | None:
    """Pure boundary function used by the engine and unit tests."""
    rule = str(position["exit_rule"])
    stop_allowed = not (
        legacy_low_stop_inactive and position.get("regime") == "LOW"
    )
    if stop_allowed and close < _d(position["effective_stop"]):
        return "FIXED_STOP" if rule != "E3" else "E3_EFFECTIVE_DEFENSE"
    if rule in {"E1", "E3"} and position.get("macd_armed") and macd_death:
        return "E1_MACD_DEATH" if rule == "E1" else "E3_MACD_FALLBACK"
    if rule == "E2" and position.get("two_r_armed") and kd_death:
        return "E2_POST_2R_KD_DEATH"
    return None


def simulate_cell(
    *, rows: Sequence[Mapping[str, Any]], package: Any, regime: str,
    entry_rule: str, exit_rule: str, timing: str, slippage_bps: int,
    candidates: Mapping[str, Mapping[date, Mapping[str, Any]]],
    initial_capital: Decimal = CAPITAL,
    rules_by_regime: Mapping[str, tuple[str, str]] | None = None,
    legacy_low_stop_inactive: bool = False,
) -> dict[str, Any]:
    """Replay one independent continuously compounded, cash-safe account."""
    if regime not in (*REGIMES, "BOTH") or entry_rule not in ENTRIES or exit_rule not in EXITS:
        raise ValueError("unknown matrix cell")
    if timing not in TIMINGS:
        raise ValueError("unknown timing")
    if rules_by_regime is None:
        rules_by_regime = {regime: (entry_rule, exit_rule)}
    if any(r not in ENTRIES or x not in EXITS for r, x in rules_by_regime.values()):
        raise ValueError("unknown routed matrix rule")
    model = CATHAY_WEB_028_TW_V1.with_research_slippage(slippage_bps)
    formal = [row for row in rows if START <= row["date"] <= END]
    sessions = [row["date"] for row in formal]
    cash = initial_capital
    position: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None
    transactions: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    rights: dict[date, list[dict[str, Any]]] = defaultdict(list)
    payments: dict[date, list[dict[str, Any]]] = defaultdict(list)
    row_index = {day: index for index, day in enumerate(sessions)}
    for dividend in package.dividends:
        if dividend.ex_date not in row_index or not (START <= dividend.ex_date <= END):
            continue
        payment_day = next((day for day in sessions if day >= dividend.payment_date), None)
        if payment_day is not None:
            payments[payment_day].append({"event": dividend})

    previous: Mapping[str, Any] | None = None
    episode = 0
    peak_equity = initial_capital

    def execute_exit(day: date, reference: Decimal, reason: str, signal_day: date) -> None:
        nonlocal cash, position
        assert position is not None
        order = model.sell(reference, int(position["shares"]), InstrumentType.ETF)
        cash += order.cash_amount
        pnl = order.cash_amount + position["dividend_cash"] - position["buy_total"]
        r_multiple = pnl / position["initial_r"]
        trade = {
            **position, "exit_signal_date": signal_day, "exit_date": day,
            "exit_reason": reason, "exit_reference_price": reference,
            "exit_fill_price": order.fill_price, "exit_gross": order.gross,
            "exit_commission": order.commission, "exit_tax": order.tax,
            "exit_cash": order.cash_amount, "pnl": pnl, "r_multiple": r_multiple,
            "holding_sessions": row_index[day] - row_index[position["entry_date"]] + 1,
        }
        trades.append(trade)
        transactions.append({
            "date": day, "signal_date": signal_day, "side": "SELL",
            "reason": reason, "episode_id": position["episode_id"], **_order_dict(order),
        })
        events.append({
            "date": day, "event": "EXIT_FILLED", "signal_date": signal_day,
            "episode_id": position["episode_id"], "reason": reason,
        })
        position = None

    def execute_entry(day: date, reference: Decimal, signal_day: date,
                      signal: Mapping[str, Any], locked_regime: str,
                      routed_entry: str, routed_exit: str) -> bool:
        nonlocal cash, position, episode
        stop = _d(signal["initial_stop"])
        if stop >= reference:
            events.append({"date": day, "event": "ENTRY_FAIL_CLOSED_STOP_NOT_BELOW",
                           "signal_date": signal_day, "stop": stop, "reference": reference})
            return False
        sized = size_entry(
            reference, stop, InstrumentType.ETF, model,
            max_total_cost=cash, max_initial_r=cash * Decimal("0.20"),
            max_shares=10_000_000,
        )
        if sized is None:
            events.append({"date": day, "event": "ENTRY_REJECTED_SIZE",
                           "signal_date": signal_day, "cash": cash})
            return False
        cash -= sized.buy.cash_amount
        episode += 1
        hist = _d(next(row["histogram"] for row in rows if row["date"] == signal_day))
        position = {
            "episode_id": f"{regime}-{entry_rule}-{exit_rule}-{episode:04d}",
            "regime": locked_regime, "entry_rule": routed_entry, "exit_rule": routed_exit,
            "timing": timing, "entry_signal_date": signal_day, "entry_date": day,
            "entry_reference_price": reference, "entry_fill_price": sized.buy.fill_price,
            "shares": sized.shares, "buy_total": sized.buy.cash_amount,
            "buy_commission": sized.buy.commission, "initial_stop": stop,
            "initial_stop_at_entry": stop,
            "effective_stop": stop, "initial_r": sized.initial_r,
            "macd_armed": hist >= 0, "two_r_armed": False,
            "two_r_armed_date": None, "dividend_cash": Decimal("0"),
            "mfe_r": Decimal("-999999"), "mae_r": Decimal("999999"),
            "pivot_low": signal.get("pivot_low"),
            "pivot_low_date": signal.get("pivot_low_date"),
            "structure_start": signal.get("structure_start", signal.get("second_k_lt_d_start")),
            "structure_end": signal.get("structure_end", signal.get("second_k_lt_d_end")),
            "cycle_id": signal.get("cycle_id", ""),
        }
        transactions.append({"date": day, "signal_date": signal_day, "side": "BUY",
                             "episode_id": position["episode_id"], **_order_dict(sized.buy)})
        events.append({"date": day, "event": "ENTRY_FILLED", "signal_date": signal_day,
                       "episode_id": position["episode_id"], "regime_locked": locked_regime,
                       "initial_stop": stop, "initial_r": sized.initial_r})
        return True

    for row in formal:
        day = row["date"]
        factor = _d(row["price_rebase_factor"])
        if factor != 1 and position is not None:
            before = position["shares"]
            position["initial_stop"] *= factor
            position["effective_stop"] *= factor
            if day == package.split.effective_date:
                position["shares"] = int(Decimal(before) * package.split.ratio)
            events.append({"date": day,
                           "event": "SPLIT_REBASE" if day == package.split.effective_date
                           else "CASH_ACTION_TECHNICAL_REBASE",
                           "factor": factor, "shares_before": before,
                           "shares_after": position["shares"],
                           "episode_id": position["episode_id"]})
        if factor != 1 and pending is not None and pending.get("side") == "ENTRY":
            pending["signal"]["initial_stop"] = _d(pending["signal"]["initial_stop"]) * factor
            if pending["signal"].get("pivot_low") is not None:
                pending["signal"]["pivot_low"] = _d(pending["signal"]["pivot_low"]) * factor
            events.append({"date": day, "event": "PENDING_ENTRY_STOP_REBASED",
                           "factor": factor, "signal_date": pending["signal_date"]})

        for dividend in package.dividends:
            if dividend.ex_date == day and position is not None:
                item = {"ex_date": day, "payment_date": dividend.payment_date,
                        "episode_id": position["episode_id"], "shares": position["shares"],
                        "amount_per_share": dividend.amount_per_share,
                        "cash": Decimal(position["shares"]) * dividend.amount_per_share}
                rights[dividend.payment_date].append(item)
                events.append({**item, "date": day, "event": "DIVIDEND_ENTITLEMENT"})
        for payload in payments.get(day, ()):
            dividend = payload["event"]
            for item in rights.pop(dividend.payment_date, []):
                cash += item["cash"]
                if position is not None and position["episode_id"] == item["episode_id"]:
                    position["dividend_cash"] += item["cash"]
                events.append({**item, "date": day, "event": "DIVIDEND_PAID"})

        exited_today = False
        if pending is not None:
            job, pending = pending, None
            reference = _d(row["raw_open"])
            if job["side"] == "EXIT" and position is not None:
                execute_exit(day, reference, job["reason"], job["signal_date"])
                exited_today = True
            elif job["side"] == "ENTRY" and position is None:
                execute_entry(day, reference, job["signal_date"], job["signal"], job["regime"],
                              job["entry_rule"], job["exit_rule"])

        histogram = _d(row["histogram"])
        prior_hist = None if previous is None else _d(previous["histogram"])
        macd_death = prior_hist is not None and prior_hist >= 0 and histogram < 0
        relation = _relation(row)
        prior_relation = "NOT_READY" if previous is None else _relation(previous)
        kd_death = prior_relation in {"GT", "EQ"} and relation == "LT"

        if position is not None:
            active_exit_rule = position["exit_rule"]
            if not position["macd_armed"] and histogram >= 0:
                position["macd_armed"] = True
                events.append({"date": day, "event": "E1_MACD_ARMED",
                               "episode_id": position["episode_id"]})
            if active_exit_rule == "E3":
                for dow in row.get("dow_events", ()):
                    defense = _d(dow.defense)
                    if day >= position["entry_date"] and defense > position["effective_stop"]:
                        old = position["effective_stop"]
                        position["effective_stop"] = raised_defense(old, defense)
                        events.append({"date": day, "event": "E3_DOW_DEFENSE_RAISED",
                                       "episode_id": position["episode_id"],
                                       "previous_stop": old, "stop": defense,
                                       "pivot_confirmed_date": dow.pivot_low.confirmed_date,
                                       "breakout_date": dow.breakout_date})
            mark = model.sell(_d(row["technical_close"]), int(position["shares"]), InstrumentType.ETF)
            open_pnl = mark.cash_amount + position["dividend_cash"] - position["buy_total"]
            open_r = open_pnl / position["initial_r"]
            position["mfe_r"] = max(position["mfe_r"], open_r)
            position["mae_r"] = min(position["mae_r"], open_r)
            if active_exit_rule == "E2" and not position["two_r_armed"] and open_r >= 2:
                position["two_r_armed"] = True
                position["two_r_armed_date"] = day
                events.append({"date": day, "event": "E2_TWO_R_ARMED",
                               "episode_id": position["episode_id"], "net_open_r": open_r})

            reason = resolve_exit_reason(
                close=_d(row["technical_close"]), position=position,
                macd_death=macd_death, kd_death=kd_death,
                legacy_low_stop_inactive=legacy_low_stop_inactive,
            )
            if reason:
                if timing == "SIGNAL_CLOSE":
                    execute_exit(day, _d(row["raw_close"]), reason, day)
                    exited_today = True
                else:
                    pending = {"side": "EXIT", "signal_date": day, "reason": reason}
                    events.append({"date": day, "event": "EXIT_ORDER_QUEUED",
                                   "episode_id": position["episode_id"], "reason": reason})

        day_regime = _regime(row)
        routed = rules_by_regime.get(day_regime)
        day_entry, day_exit = routed if routed is not None else (entry_rule, exit_rule)
        signal = candidates[day_entry].get(day)
        if signal is not None and (day_regime == regime or regime == "BOTH") and routed is not None:
            legal = _d(signal["initial_stop"]) < _d(row["technical_close"])
            signals.append({"date": day, "regime": day_regime, "entry_rule": day_entry,
                            "exit_rule": day_exit,
                            "legal_stop": legal, "initial_stop": signal["initial_stop"],
                            "filled_or_queued": False})
            if position is None and pending is None and not exited_today and legal:
                if timing == "SIGNAL_CLOSE":
                    signals[-1]["filled_or_queued"] = execute_entry(
                        day, _d(row["raw_close"]), day, signal, day_regime, day_entry, day_exit
                    )
                else:
                    pending = {"side": "ENTRY", "signal_date": day, "signal": dict(signal),
                               "regime": day_regime, "entry_rule": day_entry,
                               "exit_rule": day_exit}
                    signals[-1]["filled_or_queued"] = True
                    events.append({"date": day, "event": "ENTRY_ORDER_QUEUED",
                                   "regime_locked": day_regime})

        liquidation = (
            model.sell(_d(row["raw_close"]), int(position["shares"]), InstrumentType.ETF).cash_amount
            if position is not None else Decimal("0")
        )
        equity = cash + liquidation
        peak_equity = max(peak_equity, equity)
        daily.append({"date": day, "cash": cash,
                      "shares": 0 if position is None else position["shares"],
                      "equity": equity, "drawdown": equity / peak_equity - 1,
                      "position_regime": "" if position is None else position["regime"],
                      "effective_stop": "" if position is None else position["effective_stop"]})
        if cash < 0:
            raise ETF0050Error("cash became negative")
        previous = row
    return {"daily": daily, "trades": trades, "transactions": transactions,
            "events": events, "signals": signals, "open_position": position,
            "pending_order": pending}


def performance_summary(result: Mapping[str, Any], *, cell_id: str,
                        metadata: Mapping[str, Any]) -> dict[str, Any]:
    daily, trades = result["daily"], result["trades"]
    ending = _d(daily[-1]["equity"])
    years = Decimal((END - START).days + 1) / Decimal("365.2425")
    cagr = Decimal(str(float(ending / CAPITAL) ** (1 / float(years)) - 1))
    max_dd = min((_d(row["drawdown"]) for row in daily), default=Decimal("0"))
    wins = [row for row in trades if _d(row["pnl"]) > 0]
    losses = [row for row in trades if _d(row["pnl"]) < 0]
    gross_profit = sum((_d(row["pnl"]) for row in wins), Decimal("0"))
    gross_loss = -sum((_d(row["pnl"]) for row in losses), Decimal("0"))
    r_values = [_d(row["r_multiple"]) for row in trades]
    losing_streak = current = 0
    for trade in trades:
        current = current + 1 if _d(trade["pnl"]) < 0 else 0
        losing_streak = max(losing_streak, current)
    positives = sorted((_d(row["pnl"]) for row in wins), reverse=True)
    concentration = (
        sum(positives[:5], Decimal("0")) / gross_profit if gross_profit else None
    )
    exit_counts: dict[str, int] = defaultdict(int)
    for trade in trades:
        exit_counts[str(trade["exit_reason"])] += 1
    exposure = sum(1 for row in daily if int(row["shares"]) > 0) / len(daily)
    turnover = sum((_d(row["gross"]) for row in result["transactions"]), Decimal("0"))
    return {
        "cell_id": cell_id, **metadata, "starting_capital": CAPITAL,
        "ending_equity": ending, "total_return": ending / CAPITAL - 1,
        "cagr": cagr, "max_drawdown": max_dd,
        "calmar": None if max_dd == 0 else cagr / -max_dd,
        "signals": len(result["signals"]),
        "fills": sum(1 for row in result["transactions"] if row["side"] == "BUY"),
        "closed_trades": len(trades), "open_positions": int(result["open_position"] is not None),
        "win_rate": None if not trades else Decimal(len(wins)) / Decimal(len(trades)),
        "average_r": None if not r_values else sum(r_values, Decimal("0")) / len(r_values),
        "median_r": None if not r_values else Decimal(str(median(r_values))),
        "profit_factor": None if gross_loss == 0 else gross_profit / gross_loss,
        "average_mfe_r": None if not trades else sum((_d(x["mfe_r"]) for x in trades), Decimal("0")) / len(trades),
        "average_mae_r": None if not trades else sum((_d(x["mae_r"]) for x in trades), Decimal("0")) / len(trades),
        "median_holding_sessions": None if not trades else median(x["holding_sessions"] for x in trades),
        "exposure": exposure, "turnover": turnover,
        "max_losing_streak": losing_streak, "top5_profit_concentration": concentration,
        "exit_reason_counts": dict(sorted(exit_counts.items())),
    }


def period_rows(cell_id: str, daily: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    definitions: list[tuple[str, date, date]] = [
        ("2006-2012", START, date(2012, 12, 31)),
        ("2013-2019", date(2013, 1, 1), date(2019, 12, 31)),
        ("2020-2026/09", date(2020, 1, 1), END),
    ]
    for window in (3, 5, 10):
        for year in range(START.year, END.year - window + 2):
            definitions.append((f"ROLLING_{window}Y_{year}", date(year, 1, 1),
                                min(END, date(year + window - 1, 12, 31))))
    output = []
    for label, start, end in definitions:
        chosen = [row for row in daily if start <= row["date"] <= end]
        if not chosen:
            continue
        first_index = next(index for index, row in enumerate(daily)
                           if row["date"] == chosen[0]["date"])
        start_value = CAPITAL if first_index == 0 else _d(daily[first_index - 1]["equity"])
        finish = _d(chosen[-1]["equity"])
        peak = start_value
        mdd = Decimal("0")
        for row in chosen:
            peak = max(peak, _d(row["equity"]))
            mdd = min(mdd, _d(row["equity"]) / peak - 1)
        days = max((chosen[-1]["date"] - chosen[0]["date"]).days + 1, 1)
        cagr = Decimal(str(float(finish / start_value) ** (365.2425 / days) - 1))
        output.append({"cell_id": cell_id, "period": label, "start": chosen[0]["date"],
                       "end": chosen[-1]["date"], "starting_equity": start_value,
                       "ending_equity": finish, "return": finish / start_value - 1,
                       "cagr": cagr, "max_drawdown": mdd})
    return output


def annual_rows(cell_id: str, daily: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output = []
    prior = CAPITAL
    for year in range(2006, 2027):
        chosen = [row for row in daily if row["date"].year == year]
        if not chosen:
            continue
        ending = _d(chosen[-1]["equity"])
        peak, mdd = prior, Decimal("0")
        for row in chosen:
            peak = max(peak, _d(row["equity"]))
            mdd = min(mdd, _d(row["equity"]) / peak - 1)
        output.append({"cell_id": cell_id, "year": year, "starting_equity": prior,
                       "ending_equity": ending, "return": ending / prior - 1,
                       "max_drawdown": mdd})
        prior = ending
    return output


def _svg_lines(series: Sequence[tuple[str, Sequence[Mapping[str, Any]]]], title: str) -> str:
    width, height, left, top, plot_w, plot_h = 1000, 460, 80, 70, 850, 320
    points_all = [_d(row["equity"]) for _, rows in series for row in rows]
    lo, hi = min(points_all), max(points_all)
    span = hi - lo or Decimal("1")
    colors = ("#1565c0", "#c62828", "#2e7d32", "#6a1b9a", "#ef6c00")
    pieces = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" data-chart-kind="equity-comparison">',
        f'<metadata>source=main-matrix-summary.csv; version={VERSION}</metadata>',
        '<rect width="100%" height="100%" fill="#fff"/>',
        f'<text x="{left}" y="35" font-size="22" font-family="sans-serif">{title}</text>',
        f'<line x1="{left}" y1="{top+plot_h}" x2="{left+plot_w}" y2="{top+plot_h}" stroke="#555"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top+plot_h}" stroke="#555"/>',
    ]
    for index, (name, rows) in enumerate(series):
        step = max(1, len(rows) // 500)
        sampled = rows[::step] + ([rows[-1]] if rows[-1] is not rows[::step][-1] else [])
        coords = []
        for cursor, row in enumerate(sampled):
            x = left + plot_w * cursor / max(len(sampled) - 1, 1)
            y = top + plot_h * float((hi - _d(row["equity"])) / span)
            coords.append(f"{x:.1f},{y:.1f}")
        color = colors[index % len(colors)]
        pieces.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{" ".join(coords)}"/>')
        pieces.append(f'<text x="{left + 180*(index%5)}" y="{height-20}" fill="{color}" font-size="13" font-family="sans-serif">{name}</text>')
    pieces.append('</svg>')
    return "\n".join(pieces)


def _report_text(summaries: Sequence[Mapping[str, Any]], pareto: Sequence[Mapping[str, Any]],
                 parity: Mapping[str, Any], run_root: Path) -> str:
    best = max(summaries, key=lambda row: _d(row["cagr"]))
    sections = [
        ("一、研究問題與結論", "確認把進場當日 HIGH／LOW 狀態鎖到整筆交易後，哪一組進出場規則具有較佳風險調整後結果。", "36 條主線全部使用相同本金與資料。", f"單看 CAGR 最高的是 {best['cell_id']}；這只是研究候選，不代表已取代 v1.7。", "保留候選身分，等待 00 QA。"),
        ("二、v1.7 精確基線閘門", "避免新研究污染既有採用版。", "先驗證封存 manifest、report manifest 與 readback SHA，再驗每一件 artifact。", f"結果 {parity['status']}，run/report 分別 {parity['run_artifacts']}/{parity['report_artifacts']} 件。", "基線可作唯一比較錨點。"),
        ("三、資料範圍與公司行動", "鎖定 2005 暖機與 2006-01-02～2026-09-30 正式期。", "沿用 v1.7 原始 OHLC、股利與 2025-06-18 分割事件。", "技術座標在事件日因果重設，成交使用未還原價。", "禁止補值與回看。"),
        ("四、HIGH／LOW 狀態機", "測試進場時市場狀態是否具有分流價值。", "MA21>=MA144（含相等）為 HIGH，否則 LOW；進場後鎖定。", "交易中穿越不觸發出場，空手時才讀取新狀態。", "可把差異歸因到進場狀態。"),
        ("五、三種進場", "比較 MA21、MACD 與 KD 雙回檔型態。", "P1、P2、P3 都要求初始防守低於進場價，否則 fail closed。", "訊號、結構區間與 pivot 日期均保存在 ledger。", "非法結構不成交。"),
        ("六、三種出場", "比較固定防守後的三個獲利退出邏輯。", "E1 有 MACD armed；E2 以費後 2R 永久 armed 後等新 KD 死叉；E3 只採因果確認且只上移的 n=2 道氏防線。", "所有策略固定防守優先，收盤嚴格跌破才出場。", "退出原因可逐筆稽核。"),
        ("七、成交時點", "量化理想研究成交與可執行成交差異。", "同一訊號分別在當日收盤與下一交易日開盤成交。", "下一開盤版不使用訊號日未來價格。", "實務判讀以 NEXT_SESSION_OPEN 為主。"),
        ("八、資金與成本", "確保績效不是槓桿或忽略摩擦造成。", "每帳戶 500,000 元、整股、費後初始風險上限 20%、不借款；主口徑每邊 20bps。", "每日現金均非負，40/60bps 另作壓力。", "投入額由當日現金與停損距離共同決定，不是每次固定金額。"),
        ("九、36 條主矩陣", "在兩個狀態各比較 3×3×2。", "每格獨立連續帳戶，輸出 CAGR、MDD、Calmar 與交易品質。", f"最高 CAGR 格為 {best['cell_id']}，期末 {best['ending_equity']}。", "不把獨立帳戶相加解讀成單一可交易組合。"),
        ("十、交易品質", "檢查平均數是否由少數交易主導。", "同時計算勝率、平均/中位 R、PF、MFE/MAE、持有期、曝險、周轉、連敗與前五大獲利集中度。", "完整欄位在 matrix-trade-metrics.csv 與 ledgers。", "先看分布，再看總報酬。"),
        ("十一、年代與滾動窗", "檢查跨市場週期穩定性。", "輸出 2006-12、2013-19、2020-2026/09、逐年與 rolling 3/5/10 年。", "不同窗格結果可在 periods.csv、annual.csv 查核。", "單一期末排名不足以證明正期望。"),
        ("十二、Pareto 與替換歸因", "縮小值得後續審查的組合，避免 81 格暴力搜尋。", "只以 CAGR 高且 MDD 不劣的非支配格形成小型 Pareto 清單；v1.7、只換 HIGH、只換 LOW、兩者皆換用同一 500,000 元共享帳戶重播。", f"目前列出 {len(pareto)} 個非支配候選；歸因逐日與逐筆檔均已輸出。", "本版不宣告替換 v1.7。"),
        ("十三、基準比較", "避免只和自己比較。", "直接引用封存 v1.7 的 0050 股利留現金/再投入與 TAIEX total return 基準，不改寫舊檔。", "基準快照保存在 sealed-v1-7-main-comparison-summary.csv。", "策略需同時面對交易基準與不可交易市場指數。"),
        ("十四、限制、決策與重跑", "明確界定本研究能回答與不能回答的問題。", "所有規則為樣本內固定比較；沒有做多重檢定校正。封存 v1.7 採逐日動態狀態，新候選採進場狀態鎖定，因此兩者定義必須並列而不可偷換。", "因此只可稱研究候選；不能說已證明未來正期望。", "由 `python -m backtests.dual_ma.etf_0050_regime_entry_exit_comparison_v1 validate --run-root <RUN_ROOT> --report-root <REPORT_ROOT>` 重驗，待 00 嚴格 QA。"),
    ]
    lines = ["# 0050 2006～2026/09 HIGH／LOW 市場狀態進出場策略分組比較", "", "> 結論狀態：研究候選，未採用、未取代 v1.7。", ""]
    for title, objective, method, observation, conclusion in sections:
        lines.extend([f"## {title}", "", f"**測試目的：** {objective}", "", f"**方法：** {method}", "", f"**觀察：** {observation}", "", f"**白話結論：** {conclusion}", "", f"**決策影響：** {conclusion}", ""])
    lines.extend(["## 產物連結", "", "- [36 條主矩陣](matrix-summary.csv)", "- [完整逐筆交易](trade-ledger.csv)", "- [逐日權益](daily-equity.csv)", "- [狀態機事件](event-ledger.csv)", "- [期間分析](periods.csv)", "- [成本壓力](cost-sensitivity.csv)", "- [權益曲線](matrix-equity.svg)", "- [manifest](artifact-manifest.json)", ""])
    return "\n".join(lines)


def _pareto(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    chosen = []
    for row in rows:
        dominated = any(
            other["cell_id"] != row["cell_id"]
            and _d(other["cagr"]) >= _d(row["cagr"])
            and _d(other["max_drawdown"]) >= _d(row["max_drawdown"])
            and (_d(other["cagr"]) > _d(row["cagr"])
                 or _d(other["max_drawdown"]) > _d(row["max_drawdown"]))
            for other in rows
        )
        if not dominated:
            chosen.append(dict(row))
    return sorted(chosen, key=lambda row: (row["regime"] == "HIGH", _d(row["cagr"])), reverse=True)


def _write_run(*, run_root: Path, report_root: Path) -> dict[str, Any]:
    if run_root.exists() or report_root.exists():
        raise FileExistsError("refusing to overwrite versioned research output")
    free = shutil.disk_usage("D:\\").free
    if free < 25 * 1024 ** 3:
        raise ETF0050Error("D drive free space below 25 GiB stop threshold")
    parity = validate_sealed_v17()
    package, raw_rows, corporate_events = locked.load_package()
    rows = build_indicator_rows(raw_rows, corporate_events)
    candidates, kd_audit = entry_candidates(rows)
    run_root.mkdir(parents=True)
    report_root.mkdir(parents=True)
    summaries: list[dict[str, Any]] = []
    costs: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    transactions: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    periods: list[dict[str, Any]] = []
    annual: list[dict[str, Any]] = []
    primary_results: dict[str, Mapping[str, Any]] = {}
    for slip in SLIPPAGE_BPS:
        for regime in REGIMES:
            for entry_rule in ENTRIES:
                for exit_rule in EXITS:
                    for timing in TIMINGS:
                        cell_id = f"{regime}_{entry_rule}_{exit_rule}_{timing}_{slip}BPS"
                        result = simulate_cell(
                            rows=rows, package=package, regime=regime,
                            entry_rule=entry_rule, exit_rule=exit_rule, timing=timing,
                            slippage_bps=slip, candidates=candidates,
                        )
                        meta = {"regime": regime, "entry_rule": entry_rule,
                                "exit_rule": exit_rule, "timing": timing,
                                "slippage_bps": slip}
                        summary = performance_summary(result, cell_id=cell_id, metadata=meta)
                        costs.append(summary)
                        if slip != 20:
                            continue
                        summaries.append(summary)
                        primary_results[cell_id] = result
                        daily.extend({"cell_id": cell_id, **item} for item in result["daily"])
                        trades.extend({"cell_id": cell_id, **item} for item in result["trades"])
                        transactions.extend({"cell_id": cell_id, **item} for item in result["transactions"])
                        events.extend({"cell_id": cell_id, **item} for item in result["events"])
                        signals.extend({"cell_id": cell_id, **item} for item in result["signals"])
                        periods.extend(period_rows(cell_id, result["daily"]))
                        annual.extend(annual_rows(cell_id, result["daily"]))
    if len(summaries) != 36:
        raise ETF0050Error(f"main matrix cardinality mismatch: {len(summaries)}")
    pareto = _pareto(summaries)
    sealed_summary = _read_csv(SEALED_RUN / "main-comparison-summary.csv")
    best_high = max((row for row in summaries if row["regime"] == "HIGH"), key=lambda x: _d(x["calmar"] or "-999"))
    best_low = max((row for row in summaries if row["regime"] == "LOW"), key=lambda x: _d(x["calmar"] or "-999"))
    attribution: list[dict[str, Any]] = [
        {"case": "ORIGINAL_V1_7", "status": "SEALED_EXACT_BASELINE",
         "high_rule": "V1.7_DYNAMIC", "low_rule": "V1.7_DYNAMIC",
         "ending_equity_signal_close": sealed_summary[0]["ending_equity"],
         "ending_equity_next_open": sealed_summary[1]["ending_equity"]},
    ]
    attribution_daily: list[dict[str, Any]] = []
    attribution_trades: list[dict[str, Any]] = []
    candidate_high = (str(best_high["entry_rule"]), str(best_high["exit_rule"]))
    candidate_low = (str(best_low["entry_rule"]), str(best_low["exit_rule"]))
    cases = {
        "ONLY_HIGH_CHANGED": ({"HIGH": candidate_high, "LOW": ("P2", "E1")}, True),
        "ONLY_LOW_CHANGED": ({"HIGH": ("P1", "E3"), "LOW": candidate_low}, False),
        "BOTH_CHANGED": ({"HIGH": candidate_high, "LOW": candidate_low}, False),
    }
    for case, (routing, legacy_low) in cases.items():
        for timing in TIMINGS:
            result = simulate_cell(
                rows=rows, package=package, regime="BOTH", entry_rule="P1", exit_rule="E1",
                timing=timing, slippage_bps=20, candidates=candidates,
                rules_by_regime=routing, legacy_low_stop_inactive=legacy_low,
            )
            summary = performance_summary(
                result, cell_id=f"{case}_{timing}_20BPS",
                metadata={"case": case, "timing": timing, "slippage_bps": 20,
                          "high_rule": "/".join(routing["HIGH"]),
                          "low_rule": "/".join(routing["LOW"]),
                          "legacy_low_stop_inactive": legacy_low},
            )
            attribution.append(summary)
            attribution_daily.extend({"case": case, "timing": timing, **item}
                                     for item in result["daily"])
            attribution_trades.extend({"case": case, "timing": timing, **item}
                                      for item in result["trades"])
    regime_days = [{"date": row["date"], "regime": _regime(row), "sma21": row.get("sma21"),
                    "sma144": row.get("sma144"),
                    "equality_is_high": row.get("sma21") is not None and row.get("sma144") is not None
                    and _d(row["sma21"]) == _d(row["sma144"])}
                   for row in rows if START <= row["date"] <= END]
    _write_csv(run_root / "matrix-summary.csv", summaries)
    _write_csv(run_root / "matrix-trade-metrics.csv", summaries)
    _write_csv(run_root / "cost-sensitivity.csv", costs)
    _write_csv(run_root / "trade-ledger.csv", trades)
    _write_csv(run_root / "transaction-ledger.csv", transactions)
    _write_csv(run_root / "event-ledger.csv", events)
    _write_csv(run_root / "signal-ledger.csv", signals)
    _write_csv(run_root / "daily-equity.csv", daily)
    _write_csv(run_root / "periods.csv", periods)
    _write_csv(run_root / "annual.csv", annual)
    _write_csv(run_root / "pareto-candidates.csv", pareto)
    _write_csv(run_root / "replacement-attribution.csv", attribution)
    _write_csv(run_root / "replacement-attribution-daily.csv", attribution_daily)
    _write_csv(run_root / "replacement-attribution-trades.csv", attribution_trades)
    _write_csv(run_root / "regime-daily.csv", regime_days)
    _write_csv(run_root / "kd-cycle-audit.csv", kd_audit)
    _write_csv(run_root / "sealed-v1-7-main-comparison-summary.csv", sealed_summary)
    shutil.copy2(SEALED_RUN / "benchmark.csv", run_root / "sealed-v1-7-benchmark.csv")
    contract = {
        "version": VERSION, "formal_start": START, "formal_end": END,
        "warmup_start": rows[0]["date"], "capital": CAPITAL,
        "risk_cap": "20% of entry-time available cash per initial fee-adjusted R",
        "regime": "HIGH iff MA21 >= MA144; locked at entry; no crossing exit",
        "entries": {"P1": "MA21 breakout + structural low",
                    "P2": "MACD(21,55,55) <=0 to >0 + preceding negative segment low",
                    "P3": "KD(9,9,55) two-pullback pivot + MA21 breakout"},
        "exits": {"E1": "fixed stop first, armed MACD death fallback",
                  "E2": "fixed stop first, fee-adjusted 2R permanent arm then new KD death",
                  "E3": "initial stop + causal n=2 upward-only Dow defense, then armed MACD fallback"},
        "timings": TIMINGS, "slippage_bps": SLIPPAGE_BPS,
        "sealed_v1_7": parity,
    }
    _write_json(run_root / "research-contract.json", contract)
    representative = [best_high["cell_id"], best_low["cell_id"]]
    svg = _svg_lines([("HIGH 最佳候選", primary_results[representative[0]]["daily"]),
                      ("LOW 最佳候選", primary_results[representative[1]]["daily"])],
                     "0050 HIGH／LOW 代表候選權益曲線（20bps）")
    (run_root / "matrix-equity.svg").write_text(svg, encoding="utf-8")
    report = _report_text(summaries, pareto, parity, run_root)
    (run_root / "report.md").write_text(report, encoding="utf-8")
    limitations = {
        "candidate_only": True, "adopted": False, "replaces_v1_7": False,
        "limitations": [
            "樣本內比較不能證明未來正期望。",
            "未做多重檢定校正。",
            "封存 v1.7 為逐日動態狀態；新歸因線是進場狀態鎖定，因此只能回答規則替換效果，不能重寫基線定義。",
        ],
    }
    _write_json(run_root / "limitations.json", limitations)
    _write_json(run_root / "baseline-parity.json", parity)
    excluded = {"artifact-manifest.json", "readback.json", STOP_CODE + ".json"}
    artifact_rows = _artifact_rows(run_root, excluded)
    manifest = {"version": VERSION, "created_at": datetime.now(ZoneInfo("Asia/Taipei")).isoformat(),
                "artifacts": artifact_rows, "sealed_v1_7": parity["pins"]}
    _write_json(run_root / "artifact-manifest.json", manifest)
    readback = {"status": "PASS", "artifact_count": len(artifact_rows),
                "mismatches": _verify_artifacts(run_root, artifact_rows),
                "manifest_sha256": _sha(run_root / "artifact-manifest.json"),
                "matrix_lines": len(summaries), "cost_lines": len(costs)}
    _write_json(run_root / "readback.json", readback)
    if readback["mismatches"]:
        raise ETF0050Error(f"run readback mismatch: {readback['mismatches']}")

    report_files = ["report.md", "matrix-summary.csv", "matrix-trade-metrics.csv",
                    "pareto-candidates.csv", "replacement-attribution.csv",
                    "replacement-attribution-daily.csv", "replacement-attribution-trades.csv",
                    "periods.csv", "annual.csv", "cost-sensitivity.csv",
                    "sealed-v1-7-main-comparison-summary.csv", "matrix-equity.svg",
                    "limitations.json", "baseline-parity.json", "research-contract.json"]
    for name in report_files:
        shutil.copy2(run_root / name, report_root / name)
    report_artifacts = _artifact_rows(report_root, {"artifact-manifest.json", "readback.json"})
    _write_json(report_root / "artifact-manifest.json",
                {"version": VERSION, "source_run_manifest": readback["manifest_sha256"],
                 "artifacts": report_artifacts})
    _write_json(report_root / "readback.json",
                {"status": "PASS", "artifact_count": len(report_artifacts),
                 "mismatches": _verify_artifacts(report_root, report_artifacts),
                 "manifest_sha256": _sha(report_root / "artifact-manifest.json")})
    gate = {
        "status": STOP_CODE, "candidate_only": True, "adopted": False,
        "branch": "codex/0050-regime-entry-exit-comparison",
        "run_root": str(run_root), "report_root": str(report_root),
        "run_manifest_sha256": readback["manifest_sha256"],
        "report_manifest_sha256": _sha(report_root / "artifact-manifest.json"),
        "sealed_v1_7_unchanged": validate_sealed_v17()["status"] == "PASS_EXACT",
        "dirty_ownership": "only new research module/tests/contract plus isolated outputs",
        "data_pin": package.package_hash, "matrix_lines": len(summaries),
        "limitations": limitations["limitations"],
    }
    _write_json(run_root / (STOP_CODE + ".json"), gate)
    return gate


def validate(run_root: Path, report_root: Path | None = None) -> dict[str, Any]:
    manifest = json.loads((run_root / "artifact-manifest.json").read_text(encoding="utf-8-sig"))
    mismatches = _verify_artifacts(run_root, manifest["artifacts"])
    summary = _read_csv(run_root / "matrix-summary.csv")
    costs = _read_csv(run_root / "cost-sensitivity.csv")
    result = {"status": "PASS" if not mismatches and len(summary) == 36 and len(costs) == 108 else "FAIL",
              "mismatches": mismatches, "matrix_lines": len(summary), "cost_lines": len(costs),
              "manifest_sha256": _sha(run_root / "artifact-manifest.json"),
              "sealed_v1_7": validate_sealed_v17()["status"]}
    if report_root is not None:
        report_manifest = json.loads((report_root / "artifact-manifest.json").read_text(encoding="utf-8-sig"))
        result["report_mismatches"] = _verify_artifacts(report_root, report_manifest["artifacts"])
        result["report_manifest_sha256"] = _sha(report_root / "artifact-manifest.json")
        if result["report_mismatches"]:
            result["status"] = "FAIL"
    return result


def certify_rerun(*, run_root: Path, report_root: Path, rerun_root: Path,
                  rerun_report_root: Path, repo_root: Path) -> dict[str, Any]:
    volatile_run = {"artifact-manifest.json", "readback.json", STOP_CODE + ".json"}
    volatile_report = {"artifact-manifest.json", "readback.json"}

    def compare(left: Path, right: Path, excluded: set[str]) -> list[str]:
        left_files = {path.relative_to(left).as_posix(): path for path in left.rglob("*")
                      if path.is_file() and path.relative_to(left).as_posix() not in excluded}
        right_files = {path.relative_to(right).as_posix(): path for path in right.rglob("*")
                       if path.is_file() and path.relative_to(right).as_posix() not in excluded}
        differences = sorted(set(left_files) ^ set(right_files))
        differences.extend(sorted(name for name in set(left_files) & set(right_files)
                                  if _sha(left_files[name]) != _sha(right_files[name])))
        return differences

    run_differences = compare(run_root, rerun_root, volatile_run)
    report_differences = compare(report_root, rerun_report_root, volatile_report)
    gate_path = run_root / (STOP_CODE + ".json")
    gate = json.loads(gate_path.read_text(encoding="utf-8-sig"))
    gate.update({
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root,
                                          text=True).strip(),
        "git_status": subprocess.check_output(["git", "status", "--short"], cwd=repo_root,
                                              text=True).splitlines(),
        "readback": validate(run_root, report_root),
        "rerun": {"status": "PASS_EXACT" if not run_differences and not report_differences else "FAIL",
                  "run_differences": run_differences,
                  "report_differences": report_differences,
                  "excluded_volatile_run": sorted(volatile_run),
                  "excluded_volatile_report": sorted(volatile_report)},
        "d_drive_free_gib": Decimal(shutil.disk_usage("D:\\").free) / Decimal(1024 ** 3),
    })
    _write_json(gate_path, gate)
    if gate["rerun"]["status"] != "PASS_EXACT" or gate["readback"]["status"] != "PASS":
        raise ETF0050Error(f"rerun certification failed: {gate}")
    return gate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--run-root", type=Path, default=RUN_ROOT_DEFAULT)
    run_parser.add_argument("--report-root", type=Path, default=REPORT_ROOT_DEFAULT)
    check = sub.add_parser("validate")
    check.add_argument("--run-root", type=Path, default=RUN_ROOT_DEFAULT)
    check.add_argument("--report-root", type=Path)
    certify = sub.add_parser("certify-rerun")
    certify.add_argument("--run-root", type=Path, required=True)
    certify.add_argument("--report-root", type=Path, required=True)
    certify.add_argument("--rerun-root", type=Path, required=True)
    certify.add_argument("--rerun-report-root", type=Path, required=True)
    certify.add_argument("--repo-root", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "run":
        print(json.dumps(_jsonable(_write_run(run_root=args.run_root, report_root=args.report_root)),
                         ensure_ascii=False, indent=2))
    elif args.command == "validate":
        result = validate(args.run_root, args.report_root)
        print(json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
        if result["status"] != "PASS":
            return 1
    else:
        print(json.dumps(_jsonable(certify_rerun(
            run_root=args.run_root, report_root=args.report_root,
            rerun_root=args.rerun_root, rerun_report_root=args.rerun_report_root,
            repo_root=args.repo_root,
        )), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
