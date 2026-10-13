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
import re
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


VERSION = "dual-ma-0050-regime-entry-exit-comparison-v1.2-qa2-fix"
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
OLD_V5_RUN = Path(r"D:\code\_codex_worktrees\0050-regime-entry-exit-comparison\formal-run-0050-regime-entry-exit-v5\run")
OLD_V5_REPORT = Path(r"D:\code\_codex_worktrees\0050-regime-entry-exit-comparison\formal-run-0050-regime-entry-exit-v5\report")
OLD_V5_RUN_TREE_SHA256 = "693b41c6a8ba4ddd3e0c85174cb3c73fb567a48888ce22175b97e183ef2cd2b1"
OLD_V5_REPORT_TREE_SHA256 = "bbb7aa0d2de2e709d5b3df6a34559eb282484525b357679fdcec1e2c1d9c05a6"


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


def _tree_digest(root: Path) -> tuple[int, str]:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        rows.append((path.relative_to(root).as_posix(), path.stat().st_size, _sha(path)))
    blob = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return len(rows), hashlib.sha256(blob).hexdigest()


def validate_old_v5() -> dict[str, Any]:
    run_count, run_hash = _tree_digest(OLD_V5_RUN)
    report_count, report_hash = _tree_digest(OLD_V5_REPORT)
    result = {
        "status": "PASS_EXACT" if (
            run_hash == OLD_V5_RUN_TREE_SHA256
            and report_hash == OLD_V5_REPORT_TREE_SHA256
        ) else "FAIL",
        "run": {"path": str(OLD_V5_RUN), "files": run_count,
                "expected": OLD_V5_RUN_TREE_SHA256, "actual": run_hash},
        "report": {"path": str(OLD_V5_REPORT), "files": report_count,
                   "expected": OLD_V5_REPORT_TREE_SHA256, "actual": report_hash},
    }
    if result["status"] != "PASS_EXACT":
        raise ETF0050Error(f"old v5 byte parity failed: {result}")
    return result


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
                        legacy_low_stop_inactive: bool = False,
                        stop_allowed: bool | None = None) -> str | None:
    """Pure boundary function used by the engine and unit tests."""
    rule = str(position["exit_rule"])
    if stop_allowed is None:
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
    stop_policy: str = "ALL_LOCKED",
) -> dict[str, Any]:
    """Replay one independent continuously compounded, cash-safe account."""
    if regime not in (*REGIMES, "BOTH") or entry_rule not in ENTRIES or exit_rule not in EXITS:
        raise ValueError("unknown matrix cell")
    if timing not in TIMINGS:
        raise ValueError("unknown timing")
    if stop_policy not in {"ALL_LOCKED", "ENTRY_HIGH_ONLY", "DAILY_HIGH_ONLY"}:
        raise ValueError("unknown stop policy")
    if legacy_low_stop_inactive:
        stop_policy = "ENTRY_HIGH_ONLY"
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
        entitlement = _d(position["dividend_entitlement_cash"])
        paid = _d(position["dividend_paid_cash"])
        pnl = order.cash_amount + entitlement - position["buy_total"]
        r_multiple = pnl / position["initial_r"]
        trade = {
            **position, "exit_signal_date": signal_day, "exit_date": day,
            "exit_reason": reason, "exit_reference_price": reference,
            "exit_fill_price": order.fill_price, "exit_gross": order.gross,
            "exit_commission": order.commission, "exit_tax": order.tax,
            "exit_cash": order.cash_amount, "pnl": pnl, "r_multiple": r_multiple,
            "dividend_cash": entitlement,
            "dividend_paid_by_exit": paid,
            "dividend_receivable_at_exit": entitlement - paid,
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
            "two_r_armed_date": None,
            "dividend_entitlement_cash": Decimal("0"),
            "dividend_paid_cash": Decimal("0"),
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
            previous_initial_stop = position["initial_stop"]
            previous_effective_stop = position["effective_stop"]
            position["initial_stop"] *= factor
            position["effective_stop"] *= factor
            if day == package.split.effective_date:
                position["shares"] = int(Decimal(before) * package.split.ratio)
            events.append({"date": day,
                           "event": "SPLIT_REBASE" if day == package.split.effective_date
                           else "CASH_ACTION_TECHNICAL_REBASE",
                           "factor": factor, "shares_before": before,
                           "shares_after": position["shares"],
                           "previous_initial_stop": previous_initial_stop,
                           "initial_stop": position["initial_stop"],
                           "previous_effective_stop": previous_effective_stop,
                           "effective_stop": position["effective_stop"],
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
                position["dividend_entitlement_cash"] += item["cash"]
                events.append({**item, "date": day, "event": "DIVIDEND_ENTITLEMENT"})
        for payload in payments.get(day, ()):
            dividend = payload["event"]
            for item in rights.pop(dividend.payment_date, []):
                cash += item["cash"]
                if position is not None and position["episode_id"] == item["episode_id"]:
                    position["dividend_paid_cash"] += item["cash"]
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
        day_regime = _regime(row)

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
            open_pnl = (
                mark.cash_amount + position["dividend_entitlement_cash"]
                - position["buy_total"]
            )
            open_r = open_pnl / position["initial_r"]
            position["mfe_r"] = max(position["mfe_r"], open_r)
            position["mae_r"] = min(position["mae_r"], open_r)
            if active_exit_rule == "E2" and not position["two_r_armed"] and open_r >= 2:
                position["two_r_armed"] = True
                position["two_r_armed_date"] = day
                events.append({"date": day, "event": "E2_TWO_R_ARMED",
                               "episode_id": position["episode_id"], "net_open_r": open_r})

            stop_allowed = (
                True if stop_policy == "ALL_LOCKED"
                else position["regime"] == "HIGH" if stop_policy == "ENTRY_HIGH_ONLY"
                else day_regime == "HIGH"
            )
            reason = resolve_exit_reason(
                close=_d(row["technical_close"]), position=position,
                macd_death=macd_death, kd_death=kd_death,
                stop_allowed=stop_allowed,
            )
            if reason:
                if timing == "SIGNAL_CLOSE":
                    execute_exit(day, _d(row["raw_close"]), reason, day)
                    exited_today = True
                else:
                    pending = {"side": "EXIT", "signal_date": day, "reason": reason}
                    events.append({"date": day, "event": "EXIT_ORDER_QUEUED",
                                   "episode_id": position["episode_id"], "reason": reason})

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
        receivable = sum(
            (_d(item["cash"]) for pending_rights in rights.values()
             for item in pending_rights), Decimal("0")
        )
        economic_equity = equity + receivable
        peak_equity = max(peak_equity, equity)
        daily.append({"date": day, "cash": cash,
                      "shares": 0 if position is None else position["shares"],
                      "equity": equity, "drawdown": equity / peak_equity - 1,
                      "dividend_receivable": receivable,
                      "economic_equity": economic_equity,
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
    economic_peak = CAPITAL
    economic_mdd = Decimal("0")
    for row in daily:
        economic_peak = max(economic_peak, _d(row["economic_equity"]))
        economic_mdd = min(economic_mdd, _d(row["economic_equity"]) / economic_peak - 1)
    return {
        "cell_id": cell_id, **metadata, "starting_capital": CAPITAL,
        "ending_equity": ending, "total_return": ending / CAPITAL - 1,
        "ending_economic_equity": _d(daily[-1]["economic_equity"]),
        "cagr": cagr, "max_drawdown": max_dd,
        "economic_max_drawdown": economic_mdd,
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
        "peak_concurrent_investment": max(
            (_d(row["cash_amount"]) for row in result["transactions"] if row["side"] == "BUY"),
            default=Decimal("0")
        ),
        "max_losing_streak": losing_streak, "top5_profit_concentration": concentration,
        "exit_reason_counts": dict(sorted(exit_counts.items())),
    }


def _quantile(values: Sequence[Decimal], fraction: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    cursor = int((Decimal(len(ordered) - 1) * fraction).to_integral_value())
    return ordered[cursor]


def trade_quality_rows(
    summaries: Sequence[Mapping[str, Any]],
    primary_results: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Independent trade-distribution analysis; deliberately not a summary copy."""
    output = []
    for summary in summaries:
        cell_id = str(summary["cell_id"])
        trades = list(primary_results[cell_id]["trades"])
        rs = [_d(row["r_multiple"]) for row in trades]
        wins = [value for value in rs if value > 0]
        losses = [value for value in rs if value < 0]
        mfes = [_d(row["mfe_r"]) for row in trades]
        maes = [_d(row["mae_r"]) for row in trades]
        holds = [Decimal(row["holding_sessions"]) for row in trades]
        exit_counts: dict[str, int] = defaultdict(int)
        for row in trades:
            exit_counts[str(row["exit_reason"])] += 1
        average_win = sum(wins, Decimal("0")) / len(wins) if wins else None
        average_loss = sum(losses, Decimal("0")) / len(losses) if losses else None
        mean_r = sum(rs, Decimal("0")) / len(rs) if rs else None
        r_variance = (
            sum(((value - mean_r) ** 2 for value in rs), Decimal("0")) / len(rs)
            if rs else None
        )
        output.append({
            "cell_id": cell_id, "regime": summary["regime"],
            "entry_rule": summary["entry_rule"], "exit_rule": summary["exit_rule"],
            "timing": summary["timing"], "closed_trades": len(trades),
            "winning_trades": len(wins), "losing_trades": len(losses),
            "expectancy_r": mean_r, "r_stddev": None if r_variance is None
            else Decimal(str(float(r_variance) ** 0.5)),
            "r_p10": _quantile(rs, Decimal("0.10")),
            "r_q25": _quantile(rs, Decimal("0.25")),
            "r_median": _quantile(rs, Decimal("0.50")),
            "r_q75": _quantile(rs, Decimal("0.75")),
            "r_p90": _quantile(rs, Decimal("0.90")),
            "best_r": max(rs) if rs else None, "worst_r": min(rs) if rs else None,
            "average_win_r": average_win, "average_loss_r": average_loss,
            "payoff_ratio": None if not average_win or not average_loss
            else average_win / -average_loss,
            "mfe_median_r": _quantile(mfes, Decimal("0.50")),
            "mfe_p90_r": _quantile(mfes, Decimal("0.90")),
            "mae_median_r": _quantile(maes, Decimal("0.50")),
            "mae_p10_r": _quantile(maes, Decimal("0.10")),
            "holding_q25": _quantile(holds, Decimal("0.25")),
            "holding_median": _quantile(holds, Decimal("0.50")),
            "holding_q75": _quantile(holds, Decimal("0.75")),
            "holding_max": max(holds) if holds else None,
            "trades_with_dividend_entitlement": sum(
                1 for row in trades if _d(row["dividend_cash"]) > 0
            ),
            "trades_exited_with_receivable": sum(
                1 for row in trades if _d(row["dividend_receivable_at_exit"]) > 0
            ),
            "receivable_at_exit_total": sum(
                (_d(row["dividend_receivable_at_exit"]) for row in trades), Decimal("0")
            ),
            "fixed_stop_exits": exit_counts.get("FIXED_STOP", 0),
            "dow_defense_exits": exit_counts.get("E3_EFFECTIVE_DEFENSE", 0),
            "macd_exits": exit_counts.get("E1_MACD_DEATH", 0)
            + exit_counts.get("E3_MACD_FALLBACK", 0),
            "kd_exits": exit_counts.get("E2_POST_2R_KD_DEATH", 0),
            "top5_profit_concentration": summary["top5_profit_concentration"],
        })
    return output


def dividend_reconciliation_rows(
    trades: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    entitlements: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    payments: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    entitlement_count: dict[tuple[str, str], int] = defaultdict(int)
    for event in events:
        key = (str(event.get("cell_id", "")), str(event.get("episode_id", "")))
        if event.get("event") == "DIVIDEND_ENTITLEMENT":
            entitlements[key] += _d(event["cash"])
            entitlement_count[key] += 1
        elif event.get("event") == "DIVIDEND_PAID":
            payments[key] += _d(event["cash"])
    output = []
    for trade in trades:
        key = (str(trade["cell_id"]), str(trade["episode_id"]))
        expected = entitlements[key]
        actual = _d(trade["dividend_cash"])
        output.append({
            "cell_id": key[0], "episode_id": key[1],
            "entry_date": trade["entry_date"], "exit_date": trade["exit_date"],
            "entitlement_events": entitlement_count[key],
            "entitlement_total": expected, "trade_dividend_cash": actual,
            "paid_total_by_study_end": payments[key],
            "receivable_at_exit": trade["dividend_receivable_at_exit"],
            "difference": actual - expected,
            "status": "PASS" if actual == expected else "FAIL",
        })
    return output


def account_reconciliation_row(
    *, cell_id: str, result: Mapping[str, Any], metadata: Mapping[str, Any],
) -> dict[str, Any]:
    reconstructed_cash = CAPITAL
    for transaction in result["transactions"]:
        amount = _d(transaction["cash_amount"])
        reconstructed_cash += amount if transaction["side"] == "SELL" else -amount
    reconstructed_cash += sum(
        (_d(event["cash"]) for event in result["events"]
         if event["event"] == "DIVIDEND_PAID"), Decimal("0")
    )
    ending_cash = _d(result["daily"][-1]["cash"])
    ending_shares = int(result["daily"][-1]["shares"])
    position_shares = 0 if result["open_position"] is None else int(result["open_position"]["shares"])
    tagged_trades = [{"cell_id": cell_id, **row} for row in result["trades"]]
    tagged_events = [{"cell_id": cell_id, **row} for row in result["events"]]
    dividend_rows = dividend_reconciliation_rows(tagged_trades, tagged_events)
    dividend_failures = sum(1 for row in dividend_rows if row["status"] != "PASS")
    status = "PASS" if (
        reconstructed_cash == ending_cash
        and ending_shares == position_shares
        and dividend_failures == 0
        and all(_d(row["cash"]) >= 0 for row in result["daily"])
    ) else "FAIL"
    return {
        "cell_id": cell_id, **metadata, "reconstructed_ending_cash": reconstructed_cash,
        "recorded_ending_cash": ending_cash,
        "cash_difference": ending_cash - reconstructed_cash,
        "recorded_ending_shares": ending_shares,
        "position_ending_shares": position_shares,
        "minimum_cash": min(_d(row["cash"]) for row in result["daily"]),
        "dividend_closed_trade_failures": dividend_failures,
        "transactions": len(result["transactions"]), "closed_trades": len(result["trades"]),
        "status": status,
    }


def period_rows(cell_id: str, daily: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    definitions: list[tuple[str, date, date, bool | None]] = [
        ("2006-2012", START, date(2012, 12, 31), None),
        ("2013-2019", date(2013, 1, 1), date(2019, 12, 31), None),
        ("2020-2026/09", date(2020, 1, 1), END, None),
    ]
    for window in (3, 5, 10):
        for year in range(START.year, END.year - window + 2):
            nominal_end = date(year + window - 1, 12, 31)
            definitions.append((f"ROLLING_{window}Y_{year}", date(year, 1, 1),
                                min(END, nominal_end), nominal_end <= END))
    output = []
    for label, start, end, complete_window in definitions:
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
                       "cagr": cagr, "max_drawdown": mdd,
                       "complete_window": complete_window})
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


def _svg_lines(series: Sequence[tuple[str, Sequence[Mapping[str, Any]]]], title: str,
               *, source: str) -> str:
    """Render a reader-facing equity comparison with dates and TWD tick labels."""
    width, height, left, top, plot_w, plot_h = 1200, 650, 120, 85, 1020, 430
    points_all = [_d(row["equity"]) for _, rows in series for row in rows]
    lo, hi = min(min(points_all), CAPITAL), max(points_all)
    lo = Decimal("0")
    span = hi - lo or Decimal("1")
    colors = ("#1565c0", "#c62828", "#2e7d32", "#6a1b9a", "#ef6c00")
    pieces = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" data-chart-kind="equity-comparison">',
        f'<metadata>source={source}; version={VERSION}</metadata>',
        '<rect width="100%" height="100%" fill="#fff"/>',
        f'<text x="{left}" y="35" font-size="22" font-family="sans-serif">{title}</text>',
        f'<line x1="{left}" y1="{top+plot_h}" x2="{left+plot_w}" y2="{top+plot_h}" stroke="#555"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top+plot_h}" stroke="#555"/>',
    ]
    for tick in range(6):
        value = hi * Decimal(tick) / Decimal(5)
        y = top + plot_h * (1 - tick / 5)
        pieces.extend([
            f'<line x1="{left}" y1="{y:.1f}" x2="{left+plot_w}" y2="{y:.1f}" stroke="#ddd"/>',
            f'<text x="{left-12}" y="{y+4:.1f}" text-anchor="end" font-size="12" font-family="sans-serif">{value:,.0f} 元</text>',
        ])
    first_rows = series[0][1]
    for tick in range(6):
        position = round((len(first_rows) - 1) * tick / 5)
        x = left + plot_w * tick / 5
        label = str(first_rows[position]["date"])[:7]
        pieces.extend([
            f'<line x1="{x:.1f}" y1="{top+plot_h}" x2="{x:.1f}" y2="{top+plot_h+6}" stroke="#555"/>',
            f'<text x="{x:.1f}" y="{top+plot_h+24}" text-anchor="middle" font-size="12" font-family="sans-serif">{label}</text>',
        ])
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
        legend_x = left + 205 * (index % 5)
        pieces.append(f'<line x1="{legend_x}" y1="{height-45}" x2="{legend_x+28}" y2="{height-45}" stroke="{color}" stroke-width="3"/>')
        pieces.append(f'<text x="{legend_x+34}" y="{height-40}" fill="{color}" font-size="13" font-family="sans-serif">{name}</text>')
    pieces.append('</svg>')
    return "\n".join(pieces)


def _money(value: Any) -> str:
    return "—" if value in (None, "") else f"{_d(value):,.2f}"


def _pct(value: Any) -> str:
    return "—" if value in (None, "") else f"{_d(value) * 100:.2f}%"


def _num(value: Any, digits: int = 2) -> str:
    return "—" if value in (None, "") else f"{_d(value):.{digits}f}"


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    return [
        "|" + "|".join(headers) + "|",
        "|" + "|".join("---" for _ in headers) + "|",
        *("|" + "|".join(str(value) for value in row) + "|" for row in rows),
    ]


def _markdown_slug(heading: str) -> str:
    """Approximate the Codex/GFM native heading anchor without raw HTML."""
    plain = re.sub(r"<[^>]+>", "", heading).lower()
    plain = re.sub(r"[`*_~]", "", plain)
    plain = re.sub(r"[^\w\s-]", "", plain, flags=re.UNICODE)
    return re.sub(r"\s+", "-", plain.strip())


def _markdown_heading_ids(text: str) -> set[str]:
    counts: dict[str, int] = defaultdict(int)
    output = set()
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*$", text, flags=re.MULTILINE):
        base = _markdown_slug(heading)
        suffix = counts[base]
        output.add(base if suffix == 0 else f"{base}-{suffix}")
        counts[base] += 1
    return output


def _svg_bars(*, rows: Sequence[tuple[str, Decimal]], title: str,
              source: str, percent: bool = False) -> str:
    width, height, left, top = 1100, 520, 95, 65
    plot_w, plot_h = 930, 350
    maximum = max((abs(value) for _, value in rows), default=Decimal("1")) or Decimal("1")
    bar_w = plot_w / max(len(rows), 1)
    pieces = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" data-chart-kind="bar">',
        f'<metadata>source={source}; version={VERSION}</metadata>',
        '<rect width="100%" height="100%" fill="#fff"/>',
        f'<text x="{left}" y="34" font-size="22" font-family="sans-serif">{title}</text>',
        f'<line x1="{left}" y1="{top+plot_h}" x2="{left+plot_w}" y2="{top+plot_h}" stroke="#555"/>',
    ]
    for index, (label, value) in enumerate(rows):
        height_value = plot_h * float(abs(value) / maximum)
        x = left + index * bar_w + bar_w * 0.15
        y = top + plot_h - height_value
        color = "#1565c0" if value >= 0 else "#c62828"
        pieces.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w*0.7:.1f}" height="{height_value:.1f}" fill="{color}"/>')
        shown = f"{value*100:.1f}%" if percent else f"{value:,.0f}"
        pieces.append(f'<text x="{x:.1f}" y="{max(y-5, 55):.1f}" font-size="11" font-family="sans-serif">{shown}</text>')
        pieces.append(f'<text transform="translate({x+bar_w*0.35:.1f},{top+plot_h+12}) rotate(55)" font-size="10" font-family="sans-serif">{label}</text>')
    pieces.append('</svg>')
    return "\n".join(pieces)


def trade_details_text(
    summaries: Sequence[Mapping[str, Any]],
    primary_results: Mapping[str, Mapping[str, Any]],
    attribution: Sequence[Mapping[str, Any]],
    attribution_trades: Sequence[Mapping[str, Any]],
) -> str:
    summary_by_id = {str(row["cell_id"]): row for row in summaries}
    both_summaries = {
        str(row["timing"]): row for row in attribution
        if row.get("case") == "BOTH_RULES" and row.get("timing") in TIMINGS
    }
    both_trades = {
        timing: [row for row in attribution_trades
                 if row.get("case") == "BOTH_RULES" and row.get("timing") == timing]
        for timing in TIMINGS
    }
    lines = [
        "# 0050 HIGH／LOW 進出場矩陣｜完整繁體中文逐筆交易明細", "",
        "[返回主報告](README.md#06)", "",
        "本附件列出 36 個 20bps 主矩陣帳戶，以及主結論組合候選之次日開盤與訊號收盤帳戶的全部已平倉交易。股息欄採除息日已成立的經濟權利；付款前仍列應收，不提前列為可用現金。", "",
        "## 快速索引", "",
    ]
    display_headings: dict[str, str] = {}
    timing_zh = {"NEXT_SESSION_OPEN": "次日開盤", "SIGNAL_CLOSE": "訊號收盤"}
    for timing in ("NEXT_SESSION_OPEN", "SIGNAL_CLOSE"):
        heading = f"組合候選｜{timing_zh[timing]}"
        display_headings[f"BOTH::{timing}"] = heading
        lines.append(f"- [{heading}](#{_markdown_slug(heading)})")
    for cell_id in sorted(primary_results):
        summary = summary_by_id[cell_id]
        heading = (f"{summary['regime']} {summary['entry_rule']}／{summary['exit_rule']}｜"
                   f"{timing_zh[str(summary['timing'])]}")
        display_headings[cell_id] = heading
        lines.append(f"- [{heading}](#{_markdown_slug(heading)})")

    accounts: list[tuple[str, Mapping[str, Any], Sequence[Mapping[str, Any]]]] = []
    for timing in ("NEXT_SESSION_OPEN", "SIGNAL_CLOSE"):
        accounts.append((display_headings[f"BOTH::{timing}"], both_summaries[timing],
                         both_trades[timing]))
    accounts.extend((display_headings[cell_id], summary_by_id[cell_id],
                     primary_results[cell_id]["trades"])
                    for cell_id in sorted(primary_results))
    for heading, summary, trades in accounts:
        lines.extend([
            "", f"## {heading}", "",
            f"已平倉 {len(trades)} 筆，期末資產 {_money(summary['ending_equity'])} 元。", "",
        ])
        rows = []
        for trade in trades:
            rows.append([
                trade["episode_id"], trade["entry_date"], trade["exit_date"],
                trade["shares"], _money(trade["entry_fill_price"]),
                _money(trade["initial_stop_at_entry"]), _money(trade["exit_fill_price"]),
                _money(trade["dividend_cash"]), _money(trade["dividend_receivable_at_exit"]),
                _money(trade["pnl"]), _num(trade["r_multiple"]),
                _num(trade["mfe_r"]), _num(trade["mae_r"]),
                trade["holding_sessions"], trade["exit_reason"],
            ])
        lines.extend(_md_table(
            ["交易編號", "進場日", "出場日", "股數", "進場價", "初始防守",
             "出場價", "股息權利", "出場時應收", "淨損益", "R", "MFE R",
             "MAE R", "持有日", "出場原因"], rows,
        ))
        lines.extend(["", "[回到快速索引](#快速索引)｜[返回主報告](README.md#06)"])
    return "\n".join(lines) + "\n"


def validate_report_links(report_root: Path) -> dict[str, Any]:
    failures = []
    checked = []
    for markdown in sorted(report_root.glob("*.md")):
        text = markdown.read_text(encoding="utf-8")
        own_ids = _markdown_heading_ids(text)
        for target in re.findall(r'!?(?:\[[^\]]*\])\(([^)]+)\)', text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path_part, _, anchor = target.partition("#")
            destination = markdown if not path_part else (markdown.parent / path_part).resolve()
            try:
                destination.relative_to(report_root.resolve())
            except ValueError:
                failures.append(f"outside-root:{markdown.name}:{target}")
                continue
            if not destination.is_file():
                failures.append(f"missing:{markdown.name}:{target}")
                continue
            if anchor:
                destination_text = destination.read_text(encoding="utf-8")
                ids = own_ids if destination == markdown else _markdown_heading_ids(destination_text)
                if anchor not in ids:
                    failures.append(f"anchor:{markdown.name}:{target}")
                    continue
            checked.append({"source": markdown.name, "target": target})
    return {"status": "PASS" if not failures else "FAIL",
            "checked_count": len(checked), "checked": checked, "failures": failures}


def _report_text(*, summaries: Sequence[Mapping[str, Any]],
                 pareto: Sequence[Mapping[str, Any]], parity: Mapping[str, Any],
                 sealed_summary: Sequence[Mapping[str, Any]],
                 trade_quality: Sequence[Mapping[str, Any]],
                 costs: Sequence[Mapping[str, Any]], periods: Sequence[Mapping[str, Any]],
                 annual: Sequence[Mapping[str, Any]],
                 attribution: Sequence[Mapping[str, Any]],
                 attribution_annual: Sequence[Mapping[str, Any]],
                 attribution_periods: Sequence[Mapping[str, Any]],
                 attribution_design: Sequence[Mapping[str, Any]],
                 best_high: Mapping[str, Any], best_low: Mapping[str, Any],
                 named_dividend_case: Mapping[str, Any]) -> str:
    summary_by = {str(row["cell_id"]): row for row in summaries}
    quality_by = {str(row["cell_id"]): row for row in trade_quality}
    high_id, low_id = str(best_high["cell_id"]), str(best_low["cell_id"])
    both_next = next(row for row in attribution
                     if row.get("case") == "BOTH_RULES" and row.get("timing") == "NEXT_SESSION_OPEN")
    both_close = next(row for row in attribution
                      if row.get("case") == "BOTH_RULES" and row.get("timing") == "SIGNAL_CLOSE")
    sealed_by = {str(row["comparison_id"]): row for row in sealed_summary}
    v17_next = sealed_by["FINAL_NEXT_SESSION_OPEN_20BPS"]
    v17_close = sealed_by["FINAL_SIGNAL_CLOSE_20BPS"]
    benchmark_ids = ("BUY_HOLD_CASH", "BUY_HOLD_REINVEST", "TAIEX_TOTAL_RETURN_EQUIVALENT_500K")

    lines = [
        "# 0050 2006～2026/09 HIGH／LOW 市場狀態進出場策略分組比較｜QA 修正版", "",
        "狀態：**研究候選，未採用、未取代 v1.7**。本報告只陳述樣本內正報酬，不把樣本內平均 R 或回測獲利寫成已證明的未來正期望。", "",
        "## 閱讀順序", "",
    ]
    titles = [
        "一頁白話結論", "完整績效總表", "相對 v1.7／0050 持有／大盤",
        "規則、狀態與股息帳務", "完整逐年績效", "交易紀錄與資金投入",
        "成交時間與成本壓力", "不同時期與 rolling 3／5／10 年",
        "交易品質與出場歸因", "HIGH 九格實務矩陣", "LOW 九格實務矩陣",
        "分組 Pareto 與候選篩選", "候選與控制重合檢查", "技術驗收與完整附件",
    ]
    lines.extend(f"- [{index:02d}｜{title}](#{index:02d})" for index, title in enumerate(titles, 1))

    def section(number: int, title: str, objective: str, method: str,
                observations: Sequence[str], conclusion: str, impact: str) -> None:
        lines.extend(["", f"## {number:02d}", "",
                      f"### {title}", "", "### 測試目的", "", objective, "",
                      "### 測試方法", "", method, "", "### 觀察結果", ""])
        lines.extend(observations)
        lines.extend(["", "### 白話結論", "", conclusion, "",
                      "### 對最終決策的影響", "", impact, ""])

    first_table = _md_table(
        ["策略／基準", "成交", "期末資產", "總報酬", "CAGR", "MDD", "交易筆數", "最大投入"],
        [
            ["組合候選", "次日開盤", _money(both_next["ending_equity"]), _pct(both_next["total_return"]), _pct(both_next["cagr"]), _pct(both_next["max_drawdown"]), both_next["closed_trades"], _money(both_next["peak_concurrent_investment"])],
            ["組合候選", "訊號收盤", _money(both_close["ending_equity"]), _pct(both_close["total_return"]), _pct(both_close["cagr"]), _pct(both_close["max_drawdown"]), both_close["closed_trades"], _money(both_close["peak_concurrent_investment"])],
            ["封存 v1.7", "次日開盤", _money(v17_next["ending_equity"]), _pct(v17_next["total_return"]), _pct(v17_next["cagr"]), _pct(v17_next["max_drawdown"]), v17_next["closed_rounds"], _money(v17_next["peak_concurrent_investment"])],
            *[[sealed_by[key]["label"], sealed_by[key]["execution_timing"], _money(sealed_by[key]["ending_equity"]), _pct(sealed_by[key]["total_return"]), _pct(sealed_by[key]["cagr"]), _pct(sealed_by[key]["max_drawdown"]), sealed_by[key].get("closed_rounds", "—"), _money(sealed_by[key].get("peak_concurrent_investment"))] for key in benchmark_ids],
        ],
    )
    section(1, titles[0],
            "先回答這套新矩陣在相同資料、成本與資金限制下是否有樣本內正報酬，以及是否值得取代 v1.7。",
            "候選只從 NEXT_SESSION_OPEN、20bps、HIGH 與 LOW 各自 Pareto 母體選出；SIGNAL_CLOSE 只作同規則敏感度對照。",
            [*first_table, "", "![同本金主策略與基準權益比較](main-comparison-equity.svg)", "",
             "分組候選曲線另見：[HIGH／LOW 分組候選](regime-candidates-equity.svg)。"],
            f"新候選次日開盤期末 {_money(both_next['ending_equity'])} 元、CAGR {_pct(both_next['cagr'])}、MDD {_pct(both_next['max_drawdown'])}；這是樣本內正報酬，不是已處理選擇偏差後的期望值證明。",
            "維持研究候選；是否採用必須先看它相對封存 v1.7、持有線與大盤是否提供足夠增量。")

    section(2, titles[1],
            "把候選、封存策略與三條基準放在同一本金與現金制正式估值下比較。",
            "正式 equity 與 v1.7 一致，等於可用現金加未還原價費後清算值；付款前應收股息不提前加進正式 CAGR/MDD。",
            [*first_table, "", "註：另有 `economic_equity` 加回應收股息，僅供 2R 與交易經濟權益診斷，不拿來和 v1.7 的正式 CAGR/MDD 混比。"],
            "股息再投入持有線仍是期末資產較高的交易基準；候選的主要價值若存在，應來自回落與執行特性，而非宣稱全面勝出。",
            "不以單一期末資產決定採用；保留現金制比較口徑。")

    relative_rows = []
    for label, row in (("新候選次日開盤", both_next), ("新候選訊號收盤", both_close)):
        relative_rows.append([
            label,
            _money(_d(row["ending_equity"]) - _d(v17_next["ending_equity"])),
            f"{(_d(row['cagr'])-_d(v17_next['cagr']))*100:+.2f} pp",
            f"{(_d(row['max_drawdown'])-_d(v17_next['max_drawdown']))*100:+.2f} pp",
            _money(_d(row["ending_equity"]) - _d(sealed_by["BUY_HOLD_REINVEST"]["ending_equity"])),
            _money(_d(row["ending_equity"]) - _d(sealed_by["TAIEX_TOTAL_RETURN_EQUIVALENT_500K"]["ending_equity"])),
        ])
    section(3, titles[2],
            "直接量化新候選相對既有採用版與外部基準的增減。",
            "差額使用未四捨五入值；MDD 改善為候選 MDD 減 v1.7 MDD，正值才代表較淺。",
            _md_table(["候選", "相較v1.7期末", "CAGR差", "MDD改善", "相較0050再投入", "相較TAIEX"], relative_rows),
            "若候選沒有同時改善報酬與風險，就沒有足夠證據替換 v1.7。",
            "本研究不更新總目錄，也不改採用狀態。")

    section(4, titles[3],
            "消除 HIGH／LOW、P1～P3、E1～E3 與股息時間點的口徑歧義。",
            "HIGH 為 MA21>=MA144（相等歸 HIGH），進場狀態鎖定；所有新矩陣格的初始防守都實際執行。P1=MA21突破，P2=MACD<=0轉>0，P3=KD(9,9,55)兩次回檔且K2>K1後突破。E1=防守優先加 armed MACD，E2=費後2R永久 armed 後新 KD 死叉，E3=只上移的因果 n=2 道氏防線加 MACD fallback。",
            [
                "股息分四個概念：除息日成立的**經濟權利**立即歸屬原 episode；付款日前不是**可用現金**；交易 PnL/R、MFE/MAE 與 E2 2R 使用已成立權利；正式 daily equity 仍採 v1.7 現金制。",
                "",
                * _md_table(["具名案例", "除息權利", "出場日", "付款日", "交易股息", "出場時應收", "重建"], [[
                    "HIGH-P1-E1-0007", "2007-10-24", "2007-11-08", "2007-12-03",
                    _money(named_dividend_case["trade_dividend_cash"]),
                    _money(named_dividend_case["receivable_at_exit"]), named_dividend_case["status"],
                ]]),
            ],
            "2007 案例在付款前賣出，23,792.50 元不能提前下單，但必須計入該筆交易的經濟損益。",
            "股息 ledger reconstruction 必須全 36 格零差異，否則正式 run fail closed。")

    selected_annual = [row for row in attribution_annual
                       if row["case"] == "BOTH_RULES" and row["timing"] == "NEXT_SESSION_OPEN"]
    annual_table = _md_table(
        ["年度", "年初資產", "年末／截止資產", "報酬", "年度MDD"],
        [[("2026（截至09月）" if int(row["year"]) == 2026 else row["year"]),
          _money(row["starting_equity"]), _money(row["ending_equity"]),
          _pct(row["return"]), _pct(row["max_drawdown"])] for row in selected_annual],
    )
    positive_years = sum(1 for row in selected_annual if _d(row["return"]) > 0)
    section(5, titles[4],
            "檢查樣本內正報酬是否只靠少數年度。",
            "使用組合候選次日開盤共享帳戶；每年年初等於前一年年末，年度 MDD 以該年獨立峰值計算；2026 僅統計至 09 月 30 日。",
            [*annual_table, "", "![候選逐年報酬](yearly-returns.svg)"],
            f"21 個年度中 {positive_years} 年為正、{len(selected_annual)-positive_years} 年為負；逐年分散度顯示不能只看全期 CAGR。",
            "監控應保留年度與 rolling 視角，不能因全期正報酬忽略長時間弱勢。")

    section(6, titles[5],
            "確認投入不是固定金額，並提供人可閱讀的全部逐筆交易。",
            "每次以當時可用現金為預算，費後初始 R 不超過可用現金 20%，只買整股、不借款、不加碼。",
            [
                *_md_table(["帳戶", "交易筆數", "最大單次投入", "曝險", "周轉總額"], [
                    ["HIGH候選", best_high["closed_trades"], _money(best_high["peak_concurrent_investment"]), _pct(best_high["exposure"]), _money(best_high["turnover"])],
                    ["LOW候選", best_low["closed_trades"], _money(best_low["peak_concurrent_investment"]), _pct(best_low["exposure"]), _money(best_low["turnover"])],
                    ["組合候選", both_next["closed_trades"], _money(both_next["peak_concurrent_investment"]), _pct(both_next["exposure"]), _money(both_next["turnover"])],
                ]),
                "", "- [完整繁體中文逐筆交易明細](trade-details.md)",
                "- [組合候選原始逐筆交易 CSV](replacement-attribution-trades.csv)",
                "- [組合候選逐日帳 CSV](replacement-attribution-daily.csv)",
                "- [原始逐筆交易 CSV](trade-ledger.csv)",
                "- [逐日正式／經濟權益 CSV](daily-equity.csv)",
                "- [完整事件帳 CSV](event-ledger.csv)",
            ],
            "每筆投入隨可用現金與停損距離變動，不是固定 50 萬或固定 1 萬元。",
            "實務評估應看最大投入、連敗與現金需求，不只看報酬率。")

    timing_rows = []
    for practical in (best_high, best_low):
        paired = next(row for row in summaries if row["regime"] == practical["regime"]
                      and row["entry_rule"] == practical["entry_rule"]
                      and row["exit_rule"] == practical["exit_rule"]
                      and row["timing"] == "SIGNAL_CLOSE")
        timing_rows.append([practical["regime"], f"{practical['entry_rule']}/{practical['exit_rule']}",
                            _money(practical["ending_equity"]), _pct(practical["cagr"]),
                            _money(paired["ending_equity"]), _pct(paired["cagr"])])
    selected_costs = [row for row in costs if row["timing"] == "NEXT_SESSION_OPEN"
                      and ((row["regime"] == "HIGH" and row["entry_rule"] == best_high["entry_rule"] and row["exit_rule"] == best_high["exit_rule"])
                           or (row["regime"] == "LOW" and row["entry_rule"] == best_low["entry_rule"] and row["exit_rule"] == best_low["exit_rule"]))]
    section(7, titles[6],
            "檢查可交易時點與成本上升後結果。",
            "只用 NEXT_SESSION_OPEN_20BPS 選候選；訊號收盤是同一 P/E 規則的配對敏感度。40/60bps 重新做部位 sizing、2R 與完整路徑。",
            [
                *_md_table(["狀態", "規則", "次日開盤期末", "次日CAGR", "收盤期末", "收盤CAGR"], timing_rows), "",
                *_md_table(["狀態", "規則", "滑價", "期末", "CAGR", "MDD", "交易"], [[
                    row["regime"], f"{row['entry_rule']}/{row['exit_rule']}", f"{row['slippage_bps']}bps",
                    _money(row["ending_equity"]), _pct(row["cagr"]), _pct(row["max_drawdown"]), row["closed_trades"],
                ] for row in selected_costs]), "", "![成本壓力](cost-sensitivity.svg)",
            ],
            "成本較高可能因整股 sizing 與 E2 armed 路徑改變而非單調，但所有壓力格都按完整規則重播，不能只線性扣成本。",
            "候選選擇固定以次日開盤 20bps；不得混入收盤版排名。")

    selected_periods = [row for row in attribution_periods
                        if row["case"] == "BOTH_RULES" and row["timing"] == "NEXT_SESSION_OPEN"]
    fixed_periods = [row for row in selected_periods if not str(row["period"]).startswith("ROLLING_")]
    rolling = [row for row in selected_periods if str(row["period"]).startswith("ROLLING_")]
    complete_rolling = [row for row in rolling if str(row.get("complete_window", "")).lower() == "true"]
    partial_rolling = [row for row in rolling if str(row.get("complete_window", "")).lower() != "true"]
    section(8, titles[7],
            "檢查結果在不同年代與完整 rolling 視窗是否一致，並把樣本尾端未滿期視窗分開揭露。",
            "固定三期加上完整 rolling 3／5／10 年視窗；起始資產取區間前一交易日，MDD 用區間內獨立峰值。樣本只到 2026-09-30，因此尾端三筆另列為未滿期觀察，不混入完整視窗統計。",
            [
                *_md_table(["期間", "起日", "迄日", "報酬", "CAGR", "MDD"], [[row["period"], row["start"], row["end"], _pct(row["return"]), _pct(row["cagr"]), _pct(row["max_drawdown"])] for row in fixed_periods]), "",
                "#### 完整 rolling 視窗", "",
                *_md_table(["視窗", "起日", "迄日", "報酬", "CAGR", "MDD"], [[row["period"], row["start"], row["end"], _pct(row["return"]), _pct(row["cagr"]), _pct(row["max_drawdown"])] for row in complete_rolling]), "",
                "#### 未滿完整年限（截至 2026-09-30；不納入完整視窗統計）", "",
                *_md_table(["視窗", "起日", "迄日", "狀態", "報酬", "年化報酬", "MDD"], [[row["period"], row["start"], row["end"], "未滿整個視窗", _pct(row["return"]), _pct(row["cagr"]), _pct(row["max_drawdown"])] for row in partial_rolling]),
            ],
            f"完整 rolling 視窗共 {len(complete_rolling)} 個，另有 {len(partial_rolling)} 個未滿期觀察；完整視窗最差 CAGR 為 {_pct(min(_d(row['cagr']) for row in complete_rolling))}，顯示全期結果不能代表每個持有起點。",
            "若最差 rolling 視窗不可接受，就不能因全期正報酬直接採用。")

    selected_quality = [quality_by[high_id], quality_by[low_id]]
    section(9, titles[8],
            "檢查交易分布、股息歸屬與退出來源，而非重複績效摘要。",
            "獨立由 trade ledger 重建 R 分位數、勝負平均、MFE/MAE、持有期、出場類型、應收股息與集中度。",
            [
                *_md_table(["狀態", "規則", "筆數", "期望R(樣本平均)", "R中位", "R P10/P90", "勝R/敗R", "MFE中位", "MAE中位", "前5集中", "出場時有應收"], [[
                    row["regime"], f"{row['entry_rule']}/{row['exit_rule']}", row["closed_trades"],
                    _num(row["expectancy_r"]), _num(row["r_median"]), f"{_num(row['r_p10'])}/{_num(row['r_p90'])}",
                    f"{_num(row['average_win_r'])}/{_num(row['average_loss_r'])}", _num(row["mfe_median_r"]),
                    _num(row["mae_median_r"]), _pct(row["top5_profit_concentration"]), row["trades_exited_with_receivable"],
                ] for row in selected_quality]), "", "![出場歸因](exit-reasons.svg)",
            ],
            "表中的 expectancy_r 只是已選樣本內交易平均 R，未校正多重選擇偏差，因此報告不把它稱為未來正期望。",
            "交易稀疏或前五大獲利過度集中者即使位於 Pareto，也不自動推薦。")

    def matrix_observations(regime: str) -> list[str]:
        cohort = sorted((row for row in summaries if row["regime"] == regime
                         and row["timing"] == "NEXT_SESSION_OPEN"),
                        key=lambda row: (row["entry_rule"], row["exit_rule"]))
        paired = {(row["regime"], row["entry_rule"], row["exit_rule"]): row
                  for row in summaries if row["timing"] == "SIGNAL_CLOSE"}
        return _md_table(["進場", "出場", "期末", "CAGR", "MDD", "Calmar", "交易", "平均R", "PF", "收盤版CAGR"], [[
            row["entry_rule"], row["exit_rule"], _money(row["ending_equity"]), _pct(row["cagr"]),
            _pct(row["max_drawdown"]), _num(row["calmar"]), row["closed_trades"],
            _num(row["average_r"]), _num(row["profit_factor"]),
            _pct(paired[(regime, row["entry_rule"], row["exit_rule"])]["cagr"]),
        ] for row in cohort])

    section(10, titles[9],
            "完整展示 HIGH 狀態的 3×3 實務矩陣。",
            "九格都採 NEXT_SESSION_OPEN 20bps；最後一欄只展示相同規則的 SIGNAL_CLOSE 敏感度。",
            [*matrix_observations("HIGH"), "", "![次日開盤九格CAGR](next-open-cagr.svg)"],
            f"HIGH 實務候選為 {best_high['entry_rule']}/{best_high['exit_rule']}，Calmar {_num(best_high['calmar'])}、{best_high['closed_trades']} 筆。",
            "只在 HIGH 自己的實務母體內選擇，不讓 LOW 或收盤版支配它。")

    low_sparse = [row for row in pareto if row["regime"] == "LOW" and not row["eligible_for_automatic_selection"]]
    section(11, titles[10],
            "完整展示 LOW 狀態九格，並揭露稀疏／集中候選。",
            "選擇母體與 HIGH 相同，但 LOW Pareto 中交易少於20筆或前五獲利集中度>80%的格只列示、不推薦。",
            [*matrix_observations("LOW"), "", *(["受限候選：" + "；".join(f"{row['cell_id']}（{row['closed_trades']}筆、前5集中{_pct(row['top5_profit_concentration'])}）" for row in low_sparse)] if low_sparse else ["本次沒有受限 LOW Pareto 候選。"])],
            f"LOW 實務候選為 {best_low['entry_rule']}/{best_low['exit_rule']}，Calmar {_num(best_low['calmar'])}、{best_low['closed_trades']} 筆；稀疏格不因高平均R自動勝出。",
            "LOW 的結論必須附交易數與集中度，不能只看 CAGR/MDD。")

    section(12, titles[11],
            "在正確分組後縮小候選，避免把不同狀態與成交口徑混為同一 Pareto。",
            "先固定 NEXT_SESSION_OPEN_20BPS，再分 HIGH、LOW 各自以 CAGR 高、MDD 較淺判定非支配；最後才套交易數與集中度門檻。",
            _md_table(["狀態", "候選", "CAGR", "MDD", "Calmar", "交易", "前5集中", "可自動選", "說明"], [[
                row["regime"], row["cell_id"], _pct(row["cagr"]), _pct(row["max_drawdown"]),
                _num(row["calmar"]), row["closed_trades"], _pct(row["top5_profit_concentration"]),
                row["eligible_for_automatic_selection"], row["selection_note"],
            ] for row in pareto]),
            "Pareto 是分組篩選工具，不是採用結論；交易稀疏與獲利集中會否決自動推薦。",
            "選定 P/E 規則後，收盤版只能作配對敏感度，不能反過來改選候選。")

    practical_attr = [row for row in attribution if row.get("timing") == "NEXT_SESSION_OPEN"]
    section(13, titles[12],
            "檢查候選路由是否真的和控制組不同，避免把完全相同的規則錯稱為單因素替換。",
            "封存 v1.7 只作外部基準。新引擎內比較三種防守政策；規則替換部分先核對候選與控制路由是否不同。本次 HIGH 與 LOW 候選都恰好等於事先控制路由，因此規則替換效果不可估。",
            [
                *_md_table(["案例", "HIGH", "LOW", "防守政策", "可辨識規則效果", "說明"], [[row["display_label"], f"{row['high_entry']}/{row['high_exit']}", f"{row['low_entry']}/{row['low_exit']}", row["stop_policy_label"], row["identifiable_rule_change"], row["factor_changed"]] for row in attribution_design]), "",
                *_md_table(["案例", "期末", "CAGR", "MDD", "Calmar", "交易"], [[next(item["display_label"] for item in attribution_design if item["case"] == row["case"]), _money(row["ending_equity"]), _pct(row["cagr"]), _pct(row["max_drawdown"]), _num(row["calmar"]), row["closed_trades"]] for row in practical_attr]),
            ],
            "本樣本的候選與控制規則完全重合，六列績效相同；這是 no-op 診斷，不是 HIGH／LOW 規則替換的因果估計。三種防守政策在本樣本也未造成已觀察績效差異。",
            "本節只能判定『規則替換效果不可估』；不得據此推論候選規則優於 v1.7 或任何控制。")

    section(14, titles[13],
            "讓 00 可從單一入口重現數值、逐筆交易、股息重建、圖表與 SHA。",
            f"v1.7 封存 {parity['status']}；舊 v5 run/report 另做全樹 byte parity；新 run/report 各自 manifest/readback，並做兩次隔離重播。",
            [
                "- [主矩陣](matrix-summary.csv)",
                "- [獨立交易品質表](matrix-trade-metrics.csv)",
                "- [完整繁體中文逐筆交易](trade-details.md)",
                "- [組合候選逐筆交易](replacement-attribution-trades.csv)／[逐日帳](replacement-attribution-daily.csv)",
                "- [原始逐筆交易](trade-ledger.csv)",
                "- [逐日正式／經濟權益](daily-equity.csv)",
                "- [事件帳](event-ledger.csv)",
                "- [股息 ledger 重建](dividend-ledger-reconciliation.csv)",
                "- [36／108 帳戶守恆](account-reconciliation-108.csv)",
                "- [2007 具名案例](named-dividend-case-2007.json)",
                "- [逐年](annual.csv)／[期間與 rolling](periods.csv)",
                "- [成本壓力](cost-sensitivity.csv)",
                "- [分組 Pareto](pareto-candidates.csv)",
                "- [控制重合診斷](replacement-attribution.csv)／[設計](replacement-attribution-design.json)",
                "- [研究契約](research-contract.json)／[限制](limitations.json)",
                "- [artifact manifest](artifact-manifest.json)／[readback](readback.json)",
                "- 圖表：[主比較權益](main-comparison-equity.svg)／[分組候選權益](regime-candidates-equity.svg)／[矩陣](next-open-cagr.svg)／[成本](cost-sensitivity.svg)／[年度](yearly-returns.svg)／[出場](exit-reasons.svg)",
            ],
            "所有報告相對連結與 anchor 都由驗證器實際走訪；任一附件缺失即 FAIL。",
            "維持 STOP_WAIT_00_0050_REGIME_ENTRY_EXIT_QA，等待第二輪獨立 QA；不得 merge、push 或更新總目錄。")
    return "\n".join(lines) + "\n"


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
            item = dict(row)
            sparse = int(item["closed_trades"]) < 20
            concentrated = (
                item.get("top5_profit_concentration") is None
                or _d(item["top5_profit_concentration"]) > Decimal("0.80")
            )
            item.update({
                "sparse_trade_warning": sparse,
                "profit_concentration_warning": concentrated,
                "eligible_for_automatic_selection": not sparse and not concentrated,
                "selection_note": (
                    "交易少於20筆；不得自動推薦" if sparse else
                    "前五大獲利集中度高於80%；不得自動推薦" if concentrated else
                    "可進入實務候選排序"
                ),
            })
            chosen.append(item)
    return sorted(chosen, key=lambda row: _d(row["cagr"]), reverse=True)


def practical_pareto(summaries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for regime in REGIMES:
        cohort = [row for row in summaries
                  if row["regime"] == regime and row["timing"] == "NEXT_SESSION_OPEN"
                  and int(row["slippage_bps"]) == 20]
        output.extend(_pareto(cohort))
    return output


def _write_run(*, run_root: Path, report_root: Path) -> dict[str, Any]:
    if run_root.exists() or report_root.exists():
        raise FileExistsError("refusing to overwrite versioned research output")
    free = shutil.disk_usage("D:\\").free
    if free < 25 * 1024 ** 3:
        raise ETF0050Error("D drive free space below 25 GiB stop threshold")
    parity = validate_sealed_v17()
    old_v5_parity = validate_old_v5()
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
    account_audits: list[dict[str, Any]] = []
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
                        account_audits.append(account_reconciliation_row(
                            cell_id=cell_id, result=result, metadata=meta,
                        ))
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
    if len(account_audits) != 108 or any(row["status"] != "PASS" for row in account_audits):
        raise ETF0050Error("36/108 account reconciliation failed")
    pareto = practical_pareto(summaries)
    sealed_summary = _read_csv(SEALED_RUN / "main-comparison-summary.csv")
    selectable = [row for row in pareto if row["eligible_for_automatic_selection"]]
    best_high = max((row for row in selectable if row["regime"] == "HIGH"),
                    key=lambda x: _d(x["calmar"] or "-999"))
    best_low = max((row for row in selectable if row["regime"] == "LOW"),
                   key=lambda x: _d(x["calmar"] or "-999"))
    attribution: list[dict[str, Any]] = [
        {"case": "ORIGINAL_V1_7", "status": "SEALED_EXACT_BASELINE",
         "high_rule": "V1.7_DYNAMIC", "low_rule": "V1.7_DYNAMIC",
         "ending_equity_signal_close": sealed_summary[0]["ending_equity"],
         "ending_equity_next_open": sealed_summary[1]["ending_equity"]},
    ]
    attribution_daily: list[dict[str, Any]] = []
    attribution_trades: list[dict[str, Any]] = []
    attribution_annual: list[dict[str, Any]] = []
    attribution_periods: list[dict[str, Any]] = []
    candidate_high = (str(best_high["entry_rule"]), str(best_high["exit_rule"]))
    candidate_low = (str(best_low["entry_rule"]), str(best_low["exit_rule"]))
    base_routing = {"HIGH": ("P1", "E1"), "LOW": ("P2", "E1")}
    high_rule_identifiable = candidate_high != base_routing["HIGH"]
    low_rule_identifiable = candidate_low != base_routing["LOW"]
    case_definitions = [
        {"case": "NEW_ENGINE_DAILY_STOP_POLICY_CONTROL", "routing": base_routing,
         "display_label": "每日狀態防守控制", "stop_policy": "DAILY_HIGH_ONLY",
         "stop_policy_label": "每日狀態決定防守", "identifiable_rule_change": False,
         "factor_changed": "防守政策控制；未替換進出規則"},
        {"case": "LOCKED_ENTRY_STOP_POLICY_CONTROL", "routing": base_routing,
         "display_label": "進場狀態防守控制", "stop_policy": "ENTRY_HIGH_ONLY",
         "stop_policy_label": "進場狀態鎖定防守", "identifiable_rule_change": False,
         "factor_changed": "防守政策控制；未替換進出規則"},
        {"case": "ALL_STOPS_CONTROL", "routing": base_routing,
         "display_label": "全狀態防守控制", "stop_policy": "ALL_LOCKED",
         "stop_policy_label": "所有進場狀態均執行防守", "identifiable_rule_change": False,
         "factor_changed": "規則重合基準；未替換進出規則"},
        {"case": "HIGH_RULE_ONLY", "routing": {"HIGH": candidate_high, "LOW": base_routing["LOW"]},
         "display_label": "HIGH 候選重合檢查", "stop_policy": "ALL_LOCKED",
         "stop_policy_label": "所有進場狀態均執行防守",
         "identifiable_rule_change": high_rule_identifiable,
         "factor_changed": ("HIGH 規則與控制不同，可辨識" if high_rule_identifiable
                            else "HIGH 候選等於控制規則；替換效果不可估")},
        {"case": "LOW_RULE_ONLY", "routing": {"HIGH": base_routing["HIGH"], "LOW": candidate_low},
         "display_label": "LOW 候選重合檢查", "stop_policy": "ALL_LOCKED",
         "stop_policy_label": "所有進場狀態均執行防守",
         "identifiable_rule_change": low_rule_identifiable,
         "factor_changed": ("LOW 規則與控制不同，可辨識" if low_rule_identifiable
                            else "LOW 候選等於控制規則；替換效果不可估")},
        {"case": "BOTH_RULES", "routing": {"HIGH": candidate_high, "LOW": candidate_low},
         "display_label": "組合候選", "stop_policy": "ALL_LOCKED",
         "stop_policy_label": "所有進場狀態均執行防守",
         "identifiable_rule_change": high_rule_identifiable or low_rule_identifiable,
         "factor_changed": ("至少一側規則與控制不同，可辨識" if high_rule_identifiable or low_rule_identifiable
                            else "HIGH／LOW 候選皆等於控制規則；替換效果不可估")},
    ]
    attribution_design = []
    for definition in case_definitions:
        case = str(definition["case"])
        routing = definition["routing"]
        stop_policy = str(definition["stop_policy"])
        attribution_design.append({
            "case": case, "high_entry": routing["HIGH"][0],
            "high_exit": routing["HIGH"][1], "low_entry": routing["LOW"][0],
            "low_exit": routing["LOW"][1], "stop_policy": stop_policy,
            "display_label": definition["display_label"],
            "stop_policy_label": definition["stop_policy_label"],
            "identifiable_rule_change": definition["identifiable_rule_change"],
            "factor_changed": definition["factor_changed"],
        })
        for timing in TIMINGS:
            result = simulate_cell(
                rows=rows, package=package, regime="BOTH", entry_rule="P1", exit_rule="E1",
                timing=timing, slippage_bps=20, candidates=candidates,
                rules_by_regime=routing, stop_policy=stop_policy,
            )
            summary = performance_summary(
                result, cell_id=f"{case}_{timing}_20BPS",
                metadata={"case": case, "timing": timing, "slippage_bps": 20,
                          "high_rule": "/".join(routing["HIGH"]),
                          "low_rule": "/".join(routing["LOW"]),
                          "stop_policy": stop_policy,
                          "factor_changed": definition["factor_changed"]},
            )
            attribution.append(summary)
            attribution_daily.extend({"case": case, "timing": timing, **item}
                                     for item in result["daily"])
            attribution_trades.extend({"case": case, "timing": timing, **item}
                                      for item in result["trades"])
            attribution_annual.extend(
                {"case": case, "timing": timing, **item}
                for item in annual_rows(f"{case}_{timing}_20BPS", result["daily"])
            )
            attribution_periods.extend(
                {"case": case, "timing": timing, **item}
                for item in period_rows(f"{case}_{timing}_20BPS", result["daily"])
            )
    regime_days = [{"date": row["date"], "regime": _regime(row), "sma21": row.get("sma21"),
                    "sma144": row.get("sma144"),
                    "equality_is_high": row.get("sma21") is not None and row.get("sma144") is not None
                    and _d(row["sma21"]) == _d(row["sma144"])}
                   for row in rows if START <= row["date"] <= END]
    trade_quality = trade_quality_rows(summaries, primary_results)
    dividend_reconciliation = dividend_reconciliation_rows(trades, events)
    reconciliation_failures = [row for row in dividend_reconciliation
                               if row["status"] != "PASS"]
    if reconciliation_failures:
        raise ETF0050Error(
            f"dividend entitlement/trade reconstruction failed: {reconciliation_failures[:3]}"
        )
    named_dividend_case = next(
        row for row in dividend_reconciliation
        if row["cell_id"] == "HIGH_P1_E1_SIGNAL_CLOSE_20BPS"
        and row["episode_id"] == "HIGH-P1-E1-0007"
    )
    if not (
        named_dividend_case["entry_date"] <= date(2007, 10, 24)
        and named_dividend_case["exit_date"] == date(2007, 11, 8)
        and _d(named_dividend_case["entitlement_total"]) == Decimal("23792.5")
        and _d(named_dividend_case["receivable_at_exit"]) == Decimal("23792.5")
    ):
        raise ETF0050Error(f"named 2007 dividend bridge mismatch: {named_dividend_case}")
    _write_csv(run_root / "matrix-summary.csv", summaries)
    _write_csv(run_root / "matrix-trade-metrics.csv", trade_quality)
    _write_csv(run_root / "cost-sensitivity.csv", costs)
    _write_csv(run_root / "account-reconciliation-108.csv", account_audits)
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
    _write_csv(run_root / "replacement-attribution-annual.csv", attribution_annual)
    _write_csv(run_root / "replacement-attribution-periods.csv", attribution_periods)
    _write_json(run_root / "replacement-attribution-design.json", attribution_design)
    _write_csv(run_root / "dividend-ledger-reconciliation.csv", dividend_reconciliation)
    _write_json(run_root / "named-dividend-case-2007.json", named_dividend_case)
    _write_csv(run_root / "regime-daily.csv", regime_days)
    _write_csv(run_root / "kd-cycle-audit.csv", kd_audit)
    _write_csv(run_root / "sealed-v1-7-main-comparison-summary.csv", sealed_summary)
    shutil.copy2(SEALED_RUN / "benchmark.csv", run_root / "sealed-v1-7-benchmark.csv")
    shutil.copy2(SEALED_RUN / "main-comparison-daily.csv",
                 run_root / "sealed-v1-7-main-comparison-daily.csv")
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
        "sealed_v1_7": parity, "old_v5_byte_parity": old_v5_parity,
    }
    _write_json(run_root / "research-contract.json", contract)
    representative = [best_high["cell_id"], best_low["cell_id"]]
    regime_svg = _svg_lines(
        [("高檔狀態候選", primary_results[representative[0]]["daily"]),
         ("低檔狀態候選", primary_results[representative[1]]["daily"])],
        "0050 高低狀態分組候選權益曲線（20 基點）",
        source="matrix-summary.csv",
    )
    (run_root / "regime-candidates-equity.svg").write_text(regime_svg, encoding="utf-8")
    sealed_daily = _read_csv(SEALED_RUN / "main-comparison-daily.csv")
    sealed_daily_by_id = {
        comparison_id: [row for row in sealed_daily if row["comparison_id"] == comparison_id]
        for comparison_id in (
            "FINAL_NEXT_SESSION_OPEN_20BPS", "BUY_HOLD_CASH", "BUY_HOLD_REINVEST",
            "TAIEX_TOTAL_RETURN_EQUIVALENT_500K",
        )
    }
    both_daily = [row for row in attribution_daily
                  if row["case"] == "BOTH_RULES" and row["timing"] == "NEXT_SESSION_OPEN"]
    main_svg = _svg_lines(
        [("組合候選", both_daily),
         ("封存 v1.7", sealed_daily_by_id["FINAL_NEXT_SESSION_OPEN_20BPS"]),
         ("0050 持有－股利留現金", sealed_daily_by_id["BUY_HOLD_CASH"]),
         ("0050 持有－股利再投入", sealed_daily_by_id["BUY_HOLD_REINVEST"]),
         ("加權報酬指數等值", sealed_daily_by_id["TAIEX_TOTAL_RETURN_EQUIVALENT_500K"])],
        "同本金主策略與基準權益比較（2006～2026/09）",
        source="replacement-attribution-daily.csv + sealed-v1-7-main-comparison-daily.csv",
    )
    (run_root / "main-comparison-equity.svg").write_text(main_svg, encoding="utf-8")
    (run_root / "matrix-equity.svg").write_text(main_svg, encoding="utf-8")
    practical_rows = sorted(
        (row for row in summaries if row["timing"] == "NEXT_SESSION_OPEN"),
        key=lambda row: (row["regime"], row["entry_rule"], row["exit_rule"]),
    )
    (run_root / "next-open-cagr.svg").write_text(_svg_bars(
        rows=[(f"{'高檔' if row['regime']=='HIGH' else '低檔'}-{row['entry_rule']}/{row['exit_rule']}", _d(row["cagr"]))
              for row in practical_rows],
        title="次日開盤、20 基點｜高低狀態各九格年化報酬",
        source="matrix-summary.csv", percent=True,
    ), encoding="utf-8")
    selected_cost_rows = [row for row in costs if row["timing"] == "NEXT_SESSION_OPEN"
                          and ((row["regime"] == "HIGH" and row["entry_rule"] == best_high["entry_rule"] and row["exit_rule"] == best_high["exit_rule"])
                               or (row["regime"] == "LOW" and row["entry_rule"] == best_low["entry_rule"] and row["exit_rule"] == best_low["exit_rule"]))]
    (run_root / "cost-sensitivity.svg").write_text(_svg_bars(
        rows=[(f"{'高檔' if row['regime']=='HIGH' else '低檔'}-{row['slippage_bps']}基點", _d(row["ending_equity"]))
              for row in selected_cost_rows],
        title="候選規則成本壓力｜期末資產", source="cost-sensitivity.csv",
    ), encoding="utf-8")
    selected_attr_annual = [row for row in attribution_annual
                            if row["case"] == "BOTH_RULES" and row["timing"] == "NEXT_SESSION_OPEN"]
    (run_root / "yearly-returns.svg").write_text(_svg_bars(
        rows=[(str(row["year"]), _d(row["return"])) for row in selected_attr_annual],
        title="組合候選次日開盤｜逐年報酬（2026 截至 09 月）", source="replacement-attribution-annual.csv",
        percent=True,
    ), encoding="utf-8")
    quality_by_id = {str(row["cell_id"]): row for row in trade_quality}
    exit_chart_rows = []
    for candidate in (best_high, best_low):
        quality = quality_by_id[str(candidate["cell_id"])]
        prefix = "H" if candidate["regime"] == "HIGH" else "L"
        for label, field in (("停損", "fixed_stop_exits"), ("道氏防線", "dow_defense_exits"),
                             ("MACD", "macd_exits"), ("KD", "kd_exits")):
            exit_chart_rows.append((f"{'高檔' if prefix=='H' else '低檔'}-{label}", _d(quality[field])))
    (run_root / "exit-reasons.svg").write_text(_svg_bars(
        rows=exit_chart_rows, title="HIGH／LOW 候選出場原因筆數",
        source="matrix-trade-metrics.csv",
    ), encoding="utf-8")
    report = _report_text(
        summaries=summaries, pareto=pareto, parity=parity,
        sealed_summary=sealed_summary, trade_quality=trade_quality,
        costs=costs, periods=periods, annual=annual, attribution=attribution,
        attribution_annual=attribution_annual, attribution_periods=attribution_periods,
        attribution_design=attribution_design, best_high=best_high, best_low=best_low,
        named_dividend_case=named_dividend_case,
    )
    trade_details = trade_details_text(summaries, primary_results, attribution,
                                       attribution_trades)
    (run_root / "README.md").write_text(report, encoding="utf-8")
    (run_root / "report.md").write_text(report, encoding="utf-8")
    (run_root / "trade-details.md").write_text(trade_details, encoding="utf-8")
    limitations = {
        "candidate_only": True, "adopted": False, "replaces_v1_7": False,
        "limitations": [
            "本研究只證實樣本內正報酬；未定義或證明經選擇偏差校正後的未來正期望。",
            "未做多重檢定校正。",
            "封存 v1.7 只作外部基準；本次候選與控制路由重合，規則替換效果不可估。",
            "正式 equity 採 v1.7 現金制；economic_equity 僅作應收股息與交易歸因診斷。",
        ],
    }
    _write_json(run_root / "limitations.json", limitations)
    _write_json(run_root / "baseline-parity.json", parity)
    excluded = {"artifact-manifest.json", "readback.json", STOP_CODE + ".json"}
    artifact_rows = _artifact_rows(run_root, excluded)
    manifest = {"version": VERSION, "created_at": datetime.now(ZoneInfo("Asia/Taipei")).isoformat(),
                "artifacts": artifact_rows, "sealed_v1_7": parity["pins"],
                "old_v5_byte_parity": old_v5_parity}
    _write_json(run_root / "artifact-manifest.json", manifest)
    readback = {"status": "PASS", "artifact_count": len(artifact_rows),
                "mismatches": _verify_artifacts(run_root, artifact_rows),
                "manifest_sha256": _sha(run_root / "artifact-manifest.json"),
                "matrix_lines": len(summaries), "cost_lines": len(costs)}
    _write_json(run_root / "readback.json", readback)
    if readback["mismatches"]:
        raise ETF0050Error(f"run readback mismatch: {readback['mismatches']}")

    report_files = ["README.md", "report.md", "trade-details.md",
                    "matrix-summary.csv", "matrix-trade-metrics.csv",
                    "pareto-candidates.csv", "replacement-attribution.csv",
                    "replacement-attribution-daily.csv", "replacement-attribution-trades.csv",
                    "replacement-attribution-annual.csv", "replacement-attribution-periods.csv",
                    "replacement-attribution-design.json", "trade-ledger.csv",
                    "daily-equity.csv", "event-ledger.csv", "transaction-ledger.csv",
                    "signal-ledger.csv", "dividend-ledger-reconciliation.csv",
                    "named-dividend-case-2007.json", "periods.csv", "annual.csv",
                    "cost-sensitivity.csv", "account-reconciliation-108.csv",
                    "sealed-v1-7-main-comparison-summary.csv",
                    "sealed-v1-7-main-comparison-daily.csv", "matrix-equity.svg",
                    "main-comparison-equity.svg", "regime-candidates-equity.svg",
                    "next-open-cagr.svg", "cost-sensitivity.svg", "yearly-returns.svg",
                    "exit-reasons.svg",
                    "limitations.json", "baseline-parity.json", "research-contract.json"]
    for name in report_files:
        shutil.copy2(run_root / name, report_root / name)
    _write_json(report_root / "artifact-manifest.json", {})
    _write_json(report_root / "readback.json", {})
    link_validation = validate_report_links(report_root)
    _write_json(report_root / "link-validation.json", link_validation)
    if link_validation["status"] != "PASS":
        raise ETF0050Error(f"report link validation failed: {link_validation}")
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
        "branch": "codex/0050-regime-entry-exit-qa2",
        "run_root": str(run_root), "report_root": str(report_root),
        "run_manifest_sha256": readback["manifest_sha256"],
        "report_manifest_sha256": _sha(report_root / "artifact-manifest.json"),
        "sealed_v1_7_unchanged": validate_sealed_v17()["status"] == "PASS_EXACT",
        "old_v5_byte_parity": validate_old_v5(),
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
    trade_metrics = _read_csv(run_root / "matrix-trade-metrics.csv")
    account_audits = _read_csv(run_root / "account-reconciliation-108.csv")
    dividend_audits = _read_csv(run_root / "dividend-ledger-reconciliation.csv")
    pareto = _read_csv(run_root / "pareto-candidates.csv")
    matrix_metrics_independent = (
        _sha(run_root / "matrix-summary.csv") != _sha(run_root / "matrix-trade-metrics.csv")
        and "r_p10" in trade_metrics[0] and "trades_exited_with_receivable" in trade_metrics[0]
    )
    pass_state = (
        not mismatches and len(summary) == 36 and len(costs) == 108
        and len(trade_metrics) == 36 and matrix_metrics_independent
        and len(account_audits) == 108
        and all(row["status"] == "PASS" for row in account_audits)
        and all(row["status"] == "PASS" for row in dividend_audits)
        and {row["regime"] for row in pareto} == {"HIGH", "LOW"}
        and all(row["timing"] == "NEXT_SESSION_OPEN" for row in pareto)
    )
    result = {"status": "PASS" if pass_state else "FAIL",
              "mismatches": mismatches, "matrix_lines": len(summary), "cost_lines": len(costs),
              "trade_metric_lines": len(trade_metrics),
              "matrix_metrics_independent": matrix_metrics_independent,
              "account_reconciliation_lines": len(account_audits),
              "dividend_reconciliation_lines": len(dividend_audits),
              "manifest_sha256": _sha(run_root / "artifact-manifest.json"),
              "sealed_v1_7": validate_sealed_v17()["status"],
              "old_v5_byte_parity": validate_old_v5()["status"]}
    if report_root is not None:
        report_manifest = json.loads((report_root / "artifact-manifest.json").read_text(encoding="utf-8-sig"))
        result["report_mismatches"] = _verify_artifacts(report_root, report_manifest["artifacts"])
        result["report_manifest_sha256"] = _sha(report_root / "artifact-manifest.json")
        result["report_links"] = validate_report_links(report_root)
        if result["report_mismatches"] or result["report_links"]["status"] != "PASS":
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
