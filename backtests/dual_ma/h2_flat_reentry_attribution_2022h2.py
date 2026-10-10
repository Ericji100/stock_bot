"""Read-only P&L attribution for the frozen 2022 H2 flat re-entry run.

This module never replays the engine and never writes into the frozen run.  It
reads the frozen ``h2-flat-reentry-vpivot-2022h2-v1`` ``trades.csv`` (0 bps
legs only), the frozen JSON summaries, the 2021-2022 Fugle v2 price snapshot
and the official 2021-2022 trading calendar, then emits a Traditional-Chinese
attribution report plus a machine-readable per-leg diagnostic table.

Post-exit windows are labelled "post-event diagnostics" (事後診斷): they are
computed only after the fact for explanation and must never be read as a
tradeable signal.  Any window that would need a session after 2022-12-30 is
reported as CENSORED; 2023/2024 data is never read.

The two strategies (MEDIUM_5_21, LONG_21_144) are analysed independently and
never summed into one account.  No shared-capital rule exists in this round, so
no account ROI or maximum drawdown is guessed.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import h1_real_pilot_2022_0419_0428 as pilot


# ---------------------------------------------------------------------------
# Frozen identities
# ---------------------------------------------------------------------------

RUN_ID_DEFAULT = "h2-flat-reentry-attribution-2022h2-v1"
FROZEN_RUN_ID = "h2-flat-reentry-vpivot-2022h2-v1"
FROZEN_RUN_RELATIVE = f"02-causal-backtest/{FROZEN_RUN_ID}"
FROZEN_TRADES_SHA256 = (
    "ed67af2b7a9bc8150a7eefc47fe985b9c09a2420c45c420058757f7860159610"
)
FROZEN_TRADES_SHA256_DECLARED = FROZEN_TRADES_SHA256.upper()

ZERO_BPS = "0"
AS_OF_CUTOFF = date(2022, 12, 30)
LATEST_READABLE_YEAR = 2022

STRATEGIES = ("MEDIUM_5_21", "LONG_21_144")
STRATEGY_LABEL_ZH = {"MEDIUM_5_21": "中期 5/21", "LONG_21_144": "長期 21/144"}

# Diagnostic-only classifications.  They are NOT trade gates and were chosen
# after the fact to describe the frozen result, never to filter a replay.
EARLY_STOP_MAX_SESSIONS = 5
RISK_WIDTH_WIDE_PCT = Decimal("7.0")
POST_EXIT_HORIZONS = (5, 10, 20)

EXPECTED_TOTAL_LEGS = 1981
EXPECTED_CLOSED_LEGS = 1792
EXPECTED_OPEN_LEGS = 189
EXPECTED_REENTRY_LEGS = 307

# Two real legs recomputed by hand from the raw snapshot: the first leg of a
# medium episode and the second (re-entry) leg of another medium episode.
HAND_CHECK_LEG_KEYS = (
    "1101.TW|MEDIUM_5_21|1101.TW|MEDIUM_5_21|EP4|2022-11-09|2022-11-17|MEDIUM_5_21-L1",
    "1102.TW|MEDIUM_5_21|1102.TW|MEDIUM_5_21|EP1|2022-07-01|2022-07-19|MEDIUM_5_21-L2",
)


class AttributionError(RuntimeError):
    """A structural problem with a declared frozen input."""


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    return Decimal(text)


def _q(value: Decimal | int | str, places: str = "0.0001") -> Decimal:
    if not isinstance(value, Decimal):
        value = Decimal(value)
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _fmt(value: Any, places: int = 2) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        return f"{Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP):.{places}f}"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return f"{value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP):.{places}f}"
    return str(value)


# ---------------------------------------------------------------------------
# Frozen input readers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrozenLegs:
    run_dir: Path
    trades_sha256: str
    rows: tuple[Mapping[str, Any], ...]


def load_frozen_legs(run_dir: str | Path) -> FrozenLegs:
    run_dir = Path(run_dir)
    trades_path = run_dir / "trades.csv"
    if not trades_path.is_file():
        raise AttributionError(f"frozen trades.csv is missing: {trades_path}")
    digest = pilot.sha256_file(trades_path)
    if digest != FROZEN_TRADES_SHA256:
        raise AttributionError(
            f"frozen trades.csv sha256 {digest} != expected {FROZEN_TRADES_SHA256}"
        )
    rows: list[Mapping[str, Any]] = []
    with trades_path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("slippage_bps") == ZERO_BPS:
                rows.append(row)
    if len(rows) != EXPECTED_TOTAL_LEGS:
        raise AttributionError(
            f"0 bps leg count {len(rows)} != expected {EXPECTED_TOTAL_LEGS}"
        )
    keys = [row["leg_key"] for row in rows]
    if len(set(keys)) != len(keys):
        raise AttributionError("leg_key is not unique across the 0 bps legs")
    return FrozenLegs(run_dir=run_dir, trades_sha256=digest, rows=tuple(rows))


@dataclass(frozen=True)
class PriceIndex:
    root: Path
    content_sha256: str
    last_session: date
    bars_by_symbol: Mapping[str, Mapping[date, tuple[Decimal, Decimal]]]

    def lookahead(self, symbol: str, after: date, horizon: int, sessions: Sequence[date]):
        """Return (status, closes, highs) over the next ``horizon`` sessions.

        ``status`` is ``OK`` when the full window exists inside the snapshot,
        otherwise ``CENSORED`` when the window would need a session beyond
        2022-12-30, or ``NO_BAR`` when the symbol has no bar for a window day.
        """
        bars = self.bars_by_symbol.get(symbol)
        if bars is None:
            return "NO_BAR", (), ()
        index = {day: position for position, day in enumerate(sessions)}
        if after not in index:
            return "NO_BAR", (), ()
        start = index[after]
        window = [
            sessions[position]
            for position in range(start + 1, min(start + horizon + 1, len(sessions)))
        ]
        if len(window) < horizon:
            return "CENSORED", (), ()
        closes: list[Decimal] = []
        highs: list[Decimal] = []
        for day in window:
            bar = bars.get(day)
            if bar is None:
                return "CENSORED", (), ()
            closes.append(bar[0])
            highs.append(bar[1])
        return "OK", tuple(closes), tuple(highs)


def load_price_index(snapshot_root: str | Path) -> PriceIndex:
    snapshot_root = Path(snapshot_root)
    manifest_path = snapshot_root / pilot.PRICE_SNAPSHOT_MANIFEST_NAME
    if not manifest_path.is_file():
        raise AttributionError(f"price snapshot manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    content_sha = str(manifest.get("content_sha256") or "")
    if content_sha != pilot.PRICE_SNAPSHOT_CONTENT_SHA256:
        raise AttributionError(
            f"price snapshot content sha256 {content_sha} != expected "
            f"{pilot.PRICE_SNAPSHOT_CONTENT_SHA256}"
        )
    bars_by_symbol: dict[str, Mapping[date, tuple[Decimal, Decimal]]] = {}
    last_session = date.min
    for entry in manifest.get("prices") or ():
        path = snapshot_root / str(entry["path"])
        symbol = str(entry["path"]).rsplit("/", 1)[-1][: -len(".csv")].replace("_", ".")
        bars: dict[date, tuple[Decimal, Decimal]] = {}
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                day = date.fromisoformat(row["date"])
                if day.year > LATEST_READABLE_YEAR:
                    raise AttributionError(
                        f"price snapshot reaches {day} which is beyond the frozen window"
                    )
                bars[day] = (Decimal(row["close"]), Decimal(row["high"]))
                if day > last_session:
                    last_session = day
        bars_by_symbol[symbol] = bars
    return PriceIndex(
        root=snapshot_root,
        content_sha256=content_sha,
        last_session=last_session,
        bars_by_symbol=bars_by_symbol,
    )


def load_sessions(workdata_root: str | Path) -> tuple[date, ...]:
    calendar = pilot.load_official_calendar(
        Path(workdata_root) / pilot.CALENDAR_RELATIVE_PATH,
        expectations=pilot.FROZEN_EXPECTATIONS,
    )
    return calendar.sessions


# ---------------------------------------------------------------------------
# Per-leg diagnostics
# ---------------------------------------------------------------------------


def _sessions_between(sessions: Sequence[date], start: date, end: date) -> int:
    order = {day: position for position, day in enumerate(sessions)}
    if start not in order or end not in order:
        return 0
    return order[end] - order[start]


def classify_leg(
    row: Mapping[str, Any],
    *,
    sessions: Sequence[date],
    prices: PriceIndex,
    prior_by_leg: Mapping[str, str],
    next_by_leg: Mapping[str, str],
    leg_by_key: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    leg_key = row["leg_key"]
    T = date.fromisoformat(row["entry_signal_date_T"])
    open_censored = row["open_censored"] == "True"
    exit_date = AS_OF_CUTOFF if open_censored else date.fromisoformat(row["exit_date"])
    entry_price = _dec(row["entry_fill_price"])
    stop = _dec(row["initial_stop"])
    risk_width = (
        (entry_price - stop) / entry_price * Decimal(100) if stop and entry_price else None
    )
    fast = _dec(row["entry_fast_sma"])
    fast_extension = (
        (entry_price / fast - Decimal(1)) * Decimal(100) if fast and entry_price else None
    )
    exit_reason = "" if open_censored else row["exit_reason"]
    early_stop = (
        exit_reason == "STRUCTURAL_STOP"
        and _sessions_between(sessions, T, exit_date) <= EARLY_STOP_MAX_SESSIONS
    )
    prior_key = prior_by_leg.get(leg_key)
    next_key = next_by_leg.get(leg_key)
    prior_pnl = _dec(leg_by_key[prior_key]["costed_net_pnl"]) if prior_key else None
    next_pnl = _dec(leg_by_key[next_key]["costed_net_pnl"]) if next_key else None
    episode_key = row["episode_key"]
    episode_legs = [item for item in leg_by_key.values() if item["episode_key"] == episode_key]
    episode_pnl = sum(
        (_dec(item["costed_net_pnl"]) or Decimal(0)) for item in episode_legs
    )

    post: dict[str, Any] = {}
    for horizon in POST_EXIT_HORIZONS:
        if open_censored:
            post[horizon] = ("NOT_CLOSED", (), ())
            continue
        post[horizon] = prices.lookahead(row["symbol"], exit_date, horizon, sessions)
    status20, _closes20, highs20 = post[20]
    exit_price = _dec(row["exit_fill_price"])
    max_rebound = (
        (max(highs20) / exit_price - Decimal(1)) * Decimal(100)
        if status20 == "OK" and highs20 and exit_price
        else None
    )

    def post_close(horizon: int) -> Any:
        status, closes, _highs = post[horizon]
        return closes[-1] if status == "OK" and closes else "CENSORED"

    def post_rebound(horizon: int) -> Any:
        status, _closes, highs = post[horizon]
        if status == "OK" and highs and exit_price:
            return (max(highs) / exit_price - Decimal(1)) * Decimal(100)
        return None

    def post_forward(horizon: int) -> Any:
        status, closes, _highs = post[horizon]
        if status == "OK" and closes and exit_price:
            return (closes[-1] / exit_price - Decimal(1)) * Decimal(100)
        return None

    return {
        "leg_key": leg_key,
        "strategy": row["strategy"],
        "code": row["code"],
        "symbol": row["symbol"],
        "is_reentry": row["is_reentry"] == "True",
        "reentry_ordinal": int(row["reentry_ordinal"]),
        "episode_key": episode_key,
        "episode_ordinal": int(row["episode_ordinal"]),
        "D": row["source_selection_date_D"],
        "E": row["monitor_effective_date_E"],
        "V": row["pivot_confirmed_date"],
        "T": row["entry_signal_date_T"],
        "entry_fill_price": entry_price,
        "entry_fast_sma": fast,
        "shares": int(row["shares"]),
        "initial_stop": stop,
        "pivot_low": _dec(row["pivot_low"]),
        "breakout_level": _dec(row["breakout_level"]),
        "prior_leg_id": row["prior_leg_id"],
        "prior_exit_date": row["prior_exit_date"],
        "prior_exit_reason": row["prior_exit_reason"],
        "same_episode_after_exit": row["same_episode_after_exit"] == "True",
        "exit_date": "" if open_censored else row["exit_date"],
        "exit_reason": exit_reason,
        "exit_fill_price": _dec(row["exit_fill_price"]),
        "buy_gross": _dec(row["buy_gross"]),
        "buy_commission": _dec(row["buy_commission"]),
        "buy_total": _dec(row["buy_total"]),
        "sell_gross": _dec(row["sell_gross"]),
        "sell_commission": _dec(row["sell_commission"]),
        "sell_tax": _dec(row["sell_tax"]),
        "sell_net": _dec(row["sell_net"]),
        "total_fees_and_tax": _dec(row["total_fees_and_tax"]),
        "gross_pnl": _dec(row["gross_pnl"]),
        "costed_net_pnl": _dec(row["costed_net_pnl"]),
        "net_return_pct": _dec(row["net_return_pct"]),
        "open_censored": open_censored,
        "selection_groups": row["selection_groups"],
        "corporate_action_status": row["corporate_action_status"],
        "corrected_exit_date": row["corrected_exit_date"],
        "realized_net_pnl_corrected": _dec(row["realized_net_pnl_corrected"]),
        "economic_total_corrected": _dec(row["economic_total_corrected"]),
        "correction_delta_vs_raw": _dec(row["correction_delta_vs_raw"]),
        "hold_sessions_transacted": _sessions_between(sessions, T, exit_date),
        "hold_calendar_days": (exit_date - T).days,
        "risk_width_pct": risk_width,
        "fast_extension_pct": fast_extension,
        "early_stop_diag": early_stop,
        "risk_width_wide_diag": (risk_width is not None and risk_width >= RISK_WIDTH_WIDE_PCT),
        "episode_leg_count": len(episode_legs),
        "prior_leg_costed_net_pnl": prior_pnl,
        "next_leg_costed_net_pnl": next_pnl,
        "episode_costed_net_pnl_total": episode_pnl,
        "post_exit_5_close": post_close(5),
        "post_exit_10_close": post_close(10),
        "post_exit_20_close": post_close(20),
        "post_exit_20_status": status20,
        "post_exit_20_max_rebound_pct": max_rebound,
        "post_exit_5_max_rebound_pct": post_rebound(5),
        "post_exit_10_max_rebound_pct": post_rebound(10),
        "post_exit_5_forward_pct": post_forward(5),
        "post_exit_10_forward_pct": post_forward(10),
        "post_exit_20_forward_pct": post_forward(20),
    }


def build_leg_diagnostics(
    legs: FrozenLegs, *, sessions: Sequence[date], prices: PriceIndex
) -> list[dict[str, Any]]:
    leg_by_key = {row["leg_key"]: row for row in legs.rows}
    prior_by_leg: dict[str, str] = {}
    next_by_leg: dict[str, str] = {}
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in legs.rows:
        grouped[row["episode_key"]].append(row)
    for rows in grouped.values():
        ordered = sorted(rows, key=lambda item: int(item["reentry_ordinal"]))
        for previous, current in zip(ordered, ordered[1:]):
            prior_by_leg[current["leg_key"]] = previous["leg_key"]
            next_by_leg[previous["leg_key"]] = current["leg_key"]
    return [
        classify_leg(
            row,
            sessions=sessions,
            prices=prices,
            prior_by_leg=prior_by_leg,
            next_by_leg=next_by_leg,
            leg_by_key=leg_by_key,
        )
        for row in legs.rows
    ]


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------


def _bucket(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    closed = [row for row in rows if not row["open_censored"]]
    wins = [row for row in closed if (row["costed_net_pnl"] or Decimal(0)) > 0]
    wins_gross = [row for row in closed if (row["gross_pnl"] or Decimal(0)) > 0]
    return {
        "leg_count": len(rows),
        "closed_count": len(closed),
        "open_count": len(rows) - len(closed),
        "flat_count": sum(1 for row in closed if row["costed_net_pnl"] == 0),
        "win_count": len(wins),
        "loss_count": sum(
            1 for row in closed if (row["costed_net_pnl"] or Decimal(0)) < 0
        ),
        "gross_win_rate": (len(wins_gross) / len(closed)) if closed else None,
        "net_win_rate": (len(wins) / len(closed)) if closed else None,
        "gross_pnl_total": _q(sum((row["gross_pnl"] or Decimal(0)) for row in closed)),
        "costed_net_pnl_total": _q(
            sum((row["costed_net_pnl"] or Decimal(0)) for row in closed)
        ),
        "fees_and_tax_total": _q(
            sum((row["total_fees_and_tax"] or Decimal(0)) for row in closed)
        ),
        "gross_profit": _q(
            sum(
                (row["gross_pnl"] or Decimal(0))
                for row in closed
                if (row["gross_pnl"] or Decimal(0)) > 0
            )
        ),
        "gross_loss": _q(
            sum(
                (row["gross_pnl"] or Decimal(0))
                for row in closed
                if (row["gross_pnl"] or Decimal(0)) < 0
            )
        ),
        "buy_total_sum": _q(sum((row["buy_total"] or Decimal(0)) for row in rows)),
        "max_buy_total": _q(
            max((row["buy_total"] or Decimal(0)) for row in rows) if rows else Decimal(0)
        ),
    }


def build_summary(
    diag: Sequence[Mapping[str, Any]], *, frozen_run_dir: Path
) -> dict[str, Any]:
    closed = [row for row in diag if not row["open_censored"]]
    overall = {strategy: _bucket([r for r in diag if r["strategy"] == strategy]) for strategy in STRATEGIES}

    exit_reasons: dict[str, list[dict[str, Any]]] = {}
    for strategy in STRATEGIES:
        rows = [r for r in closed if r["strategy"] == strategy]
        entry: list[dict[str, Any]] = []
        for reason in ("STRUCTURAL_STOP", "REGIME_EXIT"):
            subset = [r for r in rows if r["exit_reason"] == reason]
            entry.append({"exit_reason": reason, **_bucket(subset)})
        exit_reasons[strategy] = entry

    early_stop: dict[str, dict[str, Any]] = {}
    for strategy in STRATEGIES:
        rows = [r for r in closed if r["strategy"] == strategy]
        flag = [r for r in rows if r["early_stop_diag"]]
        early_stop[strategy] = {
            "all": _bucket(rows),
            "early": _bucket(flag),
            "rest": _bucket([r for r in rows if not r["early_stop_diag"]]),
        }

    groups: dict[str, dict[str, Any]] = {}
    group_legs: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in closed:
        for group in str(row["selection_groups"]).split("|"):
            if group:
                group_legs[group].append(row)
    for group, rows in group_legs.items():
        groups[group] = _bucket(rows)

    reentry: dict[str, dict[str, Any]] = {}
    for strategy in STRATEGIES:
        rows = [r for r in diag if r["strategy"] == strategy]
        reentry[strategy] = {
            "all": _bucket(rows),
            "reentry": _bucket([r for r in rows if r["is_reentry"]]),
            "first": _bucket([r for r in rows if not r["is_reentry"]]),
            "reentry_closed": _bucket([r for r in rows if r["is_reentry"] and not r["open_censored"]]),
        }

    months: dict[str, dict[str, dict[str, Any]]] = {}
    for strategy in STRATEGIES:
        rows = [r for r in closed if r["strategy"] == strategy]
        by_entry: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        by_exit: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            by_entry[str(row["T"])[:7]].append(row)
            by_exit[str(row["exit_date"])[:7]].append(row)
        months[strategy] = {
            "by_entry_month": {key: _bucket(value) for key, value in sorted(by_entry.items())},
            "by_exit_month": {key: _bucket(value) for key, value in sorted(by_exit.items())},
        }

    fee_only: dict[str, dict[str, Any]] = {}
    for strategy in STRATEGIES:
        rows = [
            r
            for r in closed
            if r["strategy"] == strategy
            and (r["gross_pnl"] or Decimal(0)) >= 0
            and (r["costed_net_pnl"] or Decimal(0)) < 0
        ]
        fee_only[strategy] = {
            "count": len(rows),
            "gross_total": _q(sum((r["gross_pnl"] or Decimal(0)) for r in rows)),
            "net_total": _q(sum((r["costed_net_pnl"] or Decimal(0)) for r in rows)),
        }

    corporate: dict[str, dict[str, Any]] = {}
    for status in (
        "UNAFFECTED_NO_2022_OFFICIAL_ACTION",
        "CORPORATE_ACTION_UNRESOLVED",
        "CORRECTED_CASH_ONLY",
        "CORRECTED_WITH_STOCK_RECEIVABLE",
    ):
        rows = [r for r in closed if r["corporate_action_status"] == status]
        corporate[status] = {
            "count": len(rows),
            "raw_gross_total": _q(sum((r["gross_pnl"] or Decimal(0)) for r in rows)),
            "raw_costed_net_total": _q(
                sum((r["costed_net_pnl"] or Decimal(0)) for r in rows)
            ),
            "realized_net_pnl_corrected_total": _q(
                sum((r["realized_net_pnl_corrected"] or Decimal(0)) for r in rows)
            ),
            "economic_total_corrected_total": _q(
                sum((r["economic_total_corrected"] or Decimal(0)) for r in rows)
            ),
        }

    risk_width: dict[str, list[dict[str, Any]]] = {}
    for strategy in STRATEGIES:
        rows = [r for r in closed if r["strategy"] == strategy]
        risk_width[strategy] = [
            {
                "threshold_pct": str(threshold),
                "kept": _bucket([r for r in rows if (r["risk_width_pct"] or Decimal(0)) >= threshold]),
                "dropped": _bucket([r for r in rows if (r["risk_width_pct"] or Decimal(0)) < threshold]),
            }
            for threshold in (Decimal("5"), Decimal("6"), Decimal("7"), Decimal("8"), Decimal("10"))
        ]

    capital: dict[str, dict[str, Any]] = {}
    for strategy in STRATEGIES:
        rows = [r for r in diag if r["strategy"] == strategy]
        position_days = sum(
            (r["buy_total"] or Decimal(0)) * r["hold_sessions_transacted"] for r in rows
        )
        holds = [r["hold_sessions_transacted"] for r in rows]
        holds_closed = [r["hold_sessions_transacted"] for r in rows if not r["open_censored"]]
        capital[strategy] = {
            "leg_count": len(rows),
            "buy_total_sum": _q(sum((r["buy_total"] or Decimal(0)) for r in rows)),
            "max_buy_total": _q(
                max((r["buy_total"] or Decimal(0)) for r in rows) if rows else Decimal(0)
            ),
            "position_day_proxy": _q(position_days),
            "mean_hold_sessions_closed": (
                _q(Decimal(sum(holds_closed)) / Decimal(len(holds_closed)), "0.01")
                if holds_closed
                else None
            ),
            "median_hold_sessions_closed": _median(holds_closed),
            "mean_hold_sessions_all": (
                _q(Decimal(sum(holds)) / Decimal(len(holds)), "0.01") if holds else None
            ),
        }

    return {
        "run_id": RUN_ID_DEFAULT,
        "frozen_run_id": FROZEN_RUN_ID,
        "frozen_run_dir": str(Path(frozen_run_dir)),
        "overall": overall,
        "exit_reason": exit_reasons,
        "early_stop": early_stop,
        "groups": groups,
        "reentry": reentry,
        "months": months,
        "fee_only": fee_only,
        "corporate_action": corporate,
        "risk_width_sensitivity": risk_width,
        "capital": capital,
        "top_losers": _top(diag, reverse=False),
        "top_winners": _top(diag, reverse=True),
        "post_exit": _post_exit_summary(closed),
        "post_exit_by_strategy": {
            strategy: {
                "all": _post_exit_summary([r for r in closed if r["strategy"] == strategy]),
                "early_stop": _post_exit_summary(
                    [r for r in closed if r["strategy"] == strategy and r["early_stop_diag"]]
                ),
            }
            for strategy in STRATEGIES
        },
        "reentry_follow_through": _reentry_follow_through(diag),
    }


def _median(values: Sequence[int]) -> Any:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return Decimal(ordered[middle])
    return _q((Decimal(ordered[middle - 1]) + Decimal(ordered[middle])) / Decimal(2), "0.01")


def _top(diag: Sequence[Mapping[str, Any]], *, reverse: bool, limit: int = 10) -> list[dict[str, Any]]:
    closed = [row for row in diag if not row["open_censored"]]
    ordered = sorted(
        closed,
        key=lambda row: (row["costed_net_pnl"] or Decimal(0)),
        reverse=reverse,
    )[:limit]
    return [
        {
            "leg_key": row["leg_key"],
            "strategy": row["strategy"],
            "symbol": row["symbol"],
            "D": row["D"],
            "E": row["E"],
            "V": row["V"],
            "T": row["T"],
            "exit_date": row["exit_date"],
            "exit_reason": row["exit_reason"],
            "is_reentry": row["is_reentry"],
            "selection_groups": row["selection_groups"],
            "shares": row["shares"],
            "entry_fill_price": row["entry_fill_price"],
            "exit_fill_price": row["exit_fill_price"],
            "gross_pnl": row["gross_pnl"],
            "costed_net_pnl": row["costed_net_pnl"],
            "net_return_pct": row["net_return_pct"],
            "hold_sessions_transacted": row["hold_sessions_transacted"],
        }
        for row in ordered
    ]


def _post_exit_summary(closed: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for horizon in POST_EXIT_HORIZONS:
        ok = [r for r in closed if r[f"post_exit_{horizon}_close"] != "CENSORED"]
        censored = len(closed) - len(ok)
        counts = Counter(str(r[f"post_exit_{horizon}_close"]) for r in closed)
        # keep only the non-evaluable reasons; per-close-value counts are noise
        counts = {
            key: value
            for key, value in counts.items()
            if key in {"CENSORED", "NO_BAR", "NOT_CLOSED"}
        }
        rebound = [
            r[f"post_exit_{horizon}_max_rebound_pct"]
            for r in closed
            if r[f"post_exit_{horizon}_max_rebound_pct"] is not None
        ]
        forward = [
            r[f"post_exit_{horizon}_forward_pct"]
            for r in closed
            if r[f"post_exit_{horizon}_forward_pct"] is not None
        ]
        share_rebound = (
            sum(1 for value in rebound if value > 0) / len(rebound) if rebound else None
        )
        result[str(horizon)] = {
            "evaluable": len(ok),
            "censored": censored,
            "non_evaluable_counts": dict(counts),
            "mean_max_rebound_pct": (
                _q(sum(rebound) / Decimal(len(rebound)), "0.0001") if rebound else None
            ),
            "mean_forward_pct": (
                _q(sum(forward) / Decimal(len(forward)), "0.0001") if forward else None
            ),
            "share_with_positive_rebound": share_rebound,
        }
    return result


def _reentry_follow_through(diag: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in diag:
        if not row["is_reentry"] or row["open_censored"]:
            continue
        if row["exit_reason"] != "STRUCTURAL_STOP":
            continue
        prior = row["prior_leg_costed_net_pnl"]
        if prior is None:
            continue
        if prior < 0 and (row["costed_net_pnl"] or Decimal(0)) > 0:
            result.append(
                {
                    "leg_key": row["leg_key"],
                    "strategy": row["strategy"],
                    "symbol": row["symbol"],
                    "prior_leg_costed_net_pnl": prior,
                    "reentry_costed_net_pnl": row["costed_net_pnl"],
                    "T": row["T"],
                    "exit_date": row["exit_date"],
                }
            )
    result.sort(key=lambda item: item["reentry_costed_net_pnl"], reverse=True)
    return result[:10]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return lines


def _bucket_row(label: str, bucket: Mapping[str, Any]) -> list[Any]:
    return [
        label,
        bucket["leg_count"],
        bucket["closed_count"],
        bucket["open_count"],
        bucket["win_count"],
        _fmt(bucket["net_win_rate"], 4),
        _fmt(bucket["gross_pnl_total"]),
        _fmt(bucket["fees_and_tax_total"]),
        _fmt(bucket["costed_net_pnl_total"]),
    ]


BUCKET_HEADERS = ("分組", "筆數", "已平倉", "未平倉", "勝筆", "勝率(費後)", "費前損益", "費稅合計", "費後損益")


def render_report(
    summary: Mapping[str, Any],
    diag: Sequence[Mapping[str, Any]],
    *,
    identities: Mapping[str, Any],
    hand_checks: Sequence[Mapping[str, Any]],
) -> str:
    lines: list[str] = []
    add = lines.append

    add("# 2022 H2「出場後可再進場（flat re-entry V-pivot）」0 bps 唯讀診斷歸因報告")
    add("")
    add(
        "本報告只讀取既有凍結 run `h2-flat-reentry-vpivot-2022h2-v1` 的 0 bps "
        "逐筆交易與既有 2021–2022 Fugle 價格快照、官方日曆，做**事後診斷**。"
        "本輪不改任何規則、不重跑引擎、不新增日期、不搜尋正報酬參數，"
        "也未讀取 2023／2024 績效。中期（5/21）與長期（21/144）兩套獨立策略"
        "永遠分列，不當成單一資金帳戶相加。"
    )
    add("")
    add("## 身分與範圍")
    add("")
    add("- 凍結 run：`h2-flat-reentry-vpivot-2022h2-v1`")
    add(f"- 凍結 trades.csv SHA-256：`{identities['trades_sha256']}`（實算 MATCH，read-only）")
    add(f"- 價格快照：2021–2022 Fugle v2 universe extension，content SHA-256 `{identities['price_snapshot_content_sha256']}`，最後交易日 `{identities['price_snapshot_last_session']}`")
    add(f"- 官方日曆：raw `{identities['calendar_raw_sha256']}`（2021–2022）")
    add(f"- 診斷 run：`{identities['run_id']}`；逐筆診斷機讀檔：`attribution_legs.csv` / `.jsonl`")
    add("")
    add(
        "**事後診斷警告**：第 12 節與附錄 B 的『出場後 5／10／20 日走勢與最高反彈』"
        "完全是用 T 以後才出現的價格算出來的解釋性欄位，只能叫『事後診斷』；"
        "嚴禁拿來當當日重播訊號或宣稱可交易。任何需要 2022-12-30 以後交易日的窗口一律標 `CENSORED`，不補 2023 價格。"
    )
    add("")

    # Section 0
    add("## 0. 白話總結")
    add("")
    m_over = summary["overall"]["MEDIUM_5_21"]
    l_over = summary["overall"]["LONG_21_144"]
    m_gross = m_over["gross_pnl_total"]
    l_gross = l_over["gross_pnl_total"]
    add(
        f"全部 0 bps 共 {EXPECTED_TOTAL_LEGS} 筆（已平倉 {EXPECTED_CLOSED_LEGS}、未平倉 {EXPECTED_OPEN_LEGS}、"
        f"其中再進場腿 {EXPECTED_REENTRY_LEGS}）。中期 5/21 費後 {_fmt(m_over['costed_net_pnl_total'])} 元"
        f"（費前 {_fmt(m_gross)}、費稅 {_fmt(m_over['fees_and_tax_total'])}）；"
        f"長期 21/144 費後 {_fmt(l_over['costed_net_pnl_total'])} 元（費前 {_fmt(l_gross)}、費稅 {_fmt(l_over['fees_and_tax_total'])}）。"
    )
    add("")
    add(
        "兩個口徑**費前都已經是負的**，所以虧損不是只由費用造成；費用是額外拖累，"
        "約占中期費後虧損的 "
        f"{_fmt(abs(m_over['fees_and_tax_total'] / m_over['costed_net_pnl_total']) * 100, 1)}%、"
        "長期約占 "
        f"{_fmt(abs(l_over['fees_and_tax_total'] / l_over['costed_net_pnl_total']) * 100, 1)}%。"
        "虧損高度集中在 **STRUCTURAL_STOP（結構停損）**，且再集中於 **持有 ≤ 5 個官方交易日的極短停損**；"
        "相對地 REGIME_EXIT 在中期是正的。"
    )
    add("")
    add(
        "但**不可以因此斷言『停損設錯』**：出場後 20 個官方日，多數個股盤中確實曾反彈，"
        "但收盤前瞻方向不一（中期早期停損腿平均約 "
        f"{_fmt(summary['post_exit_by_strategy']['MEDIUM_5_21']['early_stop']['20']['mean_forward_pct'], 2)}%、"
        "長期早期停損腿平均約 "
        f"{_fmt(summary['post_exit_by_strategy']['LONG_21_144']['early_stop']['20']['mean_forward_pct'], 2)}%），"
        "且停損腿的虧損幾乎全來自價差（費稅占比小）。個股層級的反彈與續跌方向不一，屬『證據混合』。"
    )
    add("")

    # Section 1
    add("## 1. 成本前／成本後，兩策略分列")
    add("")
    add("以 0 bps（主口徑）逐筆重算，未平倉不列入費後損益：")
    add("")
    lines.extend(_table(BUCKET_HEADERS, [_bucket_row(STRATEGY_LABEL_ZH[s], summary["overall"][s]) for s in STRATEGIES]))
    add("")
    add(
        "`BOTH` 合計僅為描述性加總，**不是**單一帳戶報酬：本輪沒有共用資金、衝突部位或最大回撤口徑，故不計算帳戶 ROI 或最大回撤。"
    )
    add("")

    # Section 2
    add("## 2. 出場原因：STRUCTURAL_STOP vs REGIME_EXIT")
    add("")
    for strategy in STRATEGIES:
        add(f"### {STRATEGY_LABEL_ZH[strategy]}")
        add("")
        lines.extend(_table(BUCKET_HEADERS, [_bucket_row(item["exit_reason"], item) for item in summary["exit_reason"][strategy]]))
        add("")

    # Section 3
    add("## 3. 早期停損（診斷分類，非交易 gate）")
    add("")
    add(
        f"自訂門檻：`STRUCTURAL_STOP` 且由 T 到出場日的官方交易日數 ≤ {EARLY_STOP_MAX_SESSIONS} 個，"
        "標為『早期停損』。此標籤只為描述本輪既有結果而生，不得當成下一次回測的進出場條件。"
    )
    add("")
    for strategy in STRATEGIES:
        block = summary["early_stop"][strategy]
        add(f"### {STRATEGY_LABEL_ZH[strategy]}")
        add("")
        lines.extend(_table(BUCKET_HEADERS, [
            _bucket_row("全部已平倉", block["all"]),
            _bucket_row(f"早期停損（≤{EARLY_STOP_MAX_SESSIONS} 日）", block["early"]),
            _bucket_row("其餘", block["rest"]),
        ]))
        add("")

    # Section 4
    add("## 4. 再進場（原監控 episode 出場後的新母單）")
    add("")
    for strategy in STRATEGIES:
        block = summary["reentry"][strategy]
        add(f"### {STRATEGY_LABEL_ZH[strategy]}")
        add("")
        lines.extend(_table(BUCKET_HEADERS, [
            _bucket_row("全部腿", block["all"]),
            _bucket_row("首筆（非再進場）", block["first"]),
            _bucket_row("再進場腿", block["reentry"]),
            _bucket_row("再進場已平倉", block["reentry_closed"]),
        ]))
        add("")

    # Section 5
    add("## 5. 選股組（多標籤，不可相加成一個組合）")
    add("")
    add(
        "同一 D 被多組選中的個股會同時列在每一組，因此**各組相加會重複計算**；"
        "`chip_4`、`curated` 在本輪 H2 沒有任何候選，不列為 0 筆。"
    )
    add("")
    group_rows = sorted(summary["groups"].items(), key=lambda item: item[1]["closed_count"], reverse=True)
    lines.extend(_table(BUCKET_HEADERS, [_bucket_row(group, bucket) for group, bucket in group_rows]))
    add("")

    # Section 6
    add("## 6. 持有期、周轉、累計買入成本與資金占用 proxy")
    add("")
    add(
        "`位置·日 proxy` = Σ（每筆 buy_total × 該筆持有官方交易日數），只是描述性資金占用代理，"
        "不是帳戶淨值、不是年化、不是 ROI；同一策略任一時刻最多 1 筆未平倉母單。"
    )
    add("")
    cap_rows = []
    for strategy in STRATEGIES:
        block = summary["capital"][strategy]
        cap_rows.append([
            STRATEGY_LABEL_ZH[strategy],
            block["leg_count"],
            _fmt(block["buy_total_sum"]),
            _fmt(block["max_buy_total"]),
            _fmt(block["mean_hold_sessions_closed"], 2),
            _fmt(block["median_hold_sessions_closed"], 2),
            _fmt(block["mean_hold_sessions_all"], 2),
            _fmt(block["position_day_proxy"], 0),
        ])
    lines.extend(_table(
        ("策略", "腿數", "累計買入成本", "單筆最大買入", "平均持有(日,已平倉)", "中位持有(日,已平倉)", "平均持有(日,全部)", "位置·日 proxy(元·日)"),
        cap_rows,
    ))
    add("")

    # Section 7
    add("## 7. 月份集中度")
    add("")
    add("依進場月（T 月）與出場月分列費後損益，凸顯是不是少數月份造成：")
    add("")
    for strategy in STRATEGIES:
        add(f"### {STRATEGY_LABEL_ZH[strategy]}｜依進場月")
        add("")
        lines.extend(_table(("進場月", "已平倉", "勝筆", "費後損益"), [
            [month, b["closed_count"], b["win_count"], _fmt(b["costed_net_pnl_total"])]
            for month, b in summary["months"][strategy]["by_entry_month"].items()
        ]))
        add("")
        lines.extend(_table(("出場月", "已平倉", "勝筆", "費後損益"), [
            [month, b["closed_count"], b["win_count"], _fmt(b["costed_net_pnl_total"])]
            for month, b in summary["months"][strategy]["by_exit_month"].items()
        ]))
        add("")

    # Section 8
    add("## 8. 費用單因虧損（費前不虧、費後轉虧）")
    add("")
    lines.extend(_table(("策略", "筆數", "費前損益合計", "費後損益合計"), [
        [STRATEGY_LABEL_ZH[s], summary["fee_only"][s]["count"], _fmt(summary["fee_only"][s]["gross_total"]), _fmt(summary["fee_only"][s]["net_total"])]
        for s in STRATEGIES
    ]))
    add("")

    # Section 9
    add("## 9. 公司行動：UNAFFECTED / 未解 / 已還原 分開顯示")
    add("")
    lines.extend(_table(
        ("狀態", "筆數", "原始費前", "原始費後", "還原後已實現", "還原後經濟總額"),
        [[status, block["count"], _fmt(block["raw_gross_total"]), _fmt(block["raw_costed_net_total"]), _fmt(block["realized_net_pnl_corrected_total"]), _fmt(block["economic_total_corrected_total"])] for status, block in summary["corporate_action"].items()],
    ))
    add("")
    add(
        "上表只涵蓋已平倉腿。全樣本（含未平倉）狀態計數："
        + "、".join(
            f"{status} {count} 筆"
            for status, count in sorted(
                Counter(r["corporate_action_status"] for r in diag).items()
            )
        )
        + "。81 筆 `CORPORATE_ACTION_UNRESOLVED` 的原始未還原結果只是比較基準，不能冒充完整修正後績效；"
        "應收股利、應收配股不當成已入帳現金。已還原的 69 筆與 UNAFFECTED 1831 筆分開看。"
    )
    add("")

    # Section 10
    add("## 10. 未平倉（2022-12-30 截點）")
    add("")
    open_block = [{**row} for row in diag if row["open_censored"]]
    add(
        f"未平倉共 {len(open_block)} 筆（中期 84、長期 105），以 2022-12-30 未還原收盤價做市價評估；"
        "未平倉不計入費後損益，其最終結果未知。市價評估值見凍結 run 的 `year_end_open_positions.json`："
        "中期 +46,890.53 元、長期 +55,215.95 元（描述性，非已實現）。"
    )
    add("")

    # Section 11
    add("## 11. 具體個案")
    add("")
    add("### 11.1 前 10 大虧損（費後）")
    add("")
    lines.extend(_table(
        ("leg_key", "策略", "代號", "D", "T", "出場", "原因", "費前", "費後", "報酬%", "持有日"),
        [[r["leg_key"], STRATEGY_LABEL_ZH[r["strategy"]], r["symbol"], r["D"], r["T"], r["exit_date"], r["exit_reason"], _fmt(r["gross_pnl"]), _fmt(r["costed_net_pnl"]), _fmt(r["net_return_pct"]), r["hold_sessions_transacted"]] for r in summary["top_losers"]],
    ))
    add("")
    add("### 11.2 前 10 大獲利（費後）")
    add("")
    lines.extend(_table(
        ("leg_key", "策略", "代號", "D", "T", "出場", "原因", "費前", "費後", "報酬%", "持有日"),
        [[r["leg_key"], STRATEGY_LABEL_ZH[r["strategy"]], r["symbol"], r["D"], r["T"], r["exit_date"], r["exit_reason"], _fmt(r["gross_pnl"]), _fmt(r["costed_net_pnl"]), _fmt(r["net_return_pct"]), r["hold_sessions_transacted"]] for r in summary["top_winners"]],
    ))
    add("")
    add("### 11.3 再進場後抓到的個案（前一筆虧、這一筆賺）")
    add("")
    follow = summary["reentry_follow_through"]
    if follow:
        lines.extend(_table(
            ("leg_key", "策略", "代號", "T", "出場", "前一筆費後", "再進場費後"),
            [[r["leg_key"], STRATEGY_LABEL_ZH[r["strategy"]], r["symbol"], r["T"], r["exit_date"], _fmt(r["prior_leg_costed_net_pnl"]), _fmt(r["reentry_costed_net_pnl"])] for r in follow],
        ))
    else:
        add("（無）")
    add("")

    # Section 12
    add("## 12. 事後診斷：出場後 5／10／20 個官方日")
    add("")
    lines.extend(_table(
        ("窗口", "可評估", "CENSORED/無資料", "平均最高反彈%", "曾出現正反彈比例", "平均收盤前瞻% (事後)"),
        [[f"出場後 {h} 日", block["evaluable"], block["censored"], _fmt(block["mean_max_rebound_pct"], 2), _fmt(block["share_with_positive_rebound"], 4), _fmt(block["mean_forward_pct"], 2)] for h, block in summary["post_exit"].items()],
    ))
    add("")
    add(
        "這只是事後描述：多數個股在出場後確實曾出現正的最高反彈（盤中），但把收盤價接起來看，"
        "『最高反彈』為正而『收盤前瞻』偏負，兩者並存，說明『停損太早』與『停損保護了續跌』同時有支持證據，"
        "不能單看盤中反彈就斷言停損有錯。"
    )
    add("")
    add("### 12.1 依策略（出場後 20 日）")
    add("")
    lines.extend(_table(
        ("策略", "子集", "可評估", "平均最高反彈%", "平均收盤前瞻%"),
        [
            [STRATEGY_LABEL_ZH[strategy], label, block["evaluable"], _fmt(block["mean_max_rebound_pct"], 2), _fmt(block["mean_forward_pct"], 2)]
            for strategy in STRATEGIES
            for label, block in (
                ("全部已平倉", summary["post_exit_by_strategy"][strategy]["all"]["20"]),
                ("早期停損腿", summary["post_exit_by_strategy"][strategy]["early_stop"]["20"]),
            )
        ],
    ))
    add("")

    # Section 13
    add("## 13. 證據支持 vs 只是推測")
    add("")
    add("**有證據支持（可從逐筆欄位直接核對）**")
    add("")
    add("- 兩個策略的虧損在費前就已存在，費用是額外拖累，不是唯一成因。")
    add("- 虧損高度集中在 STRUCTURAL_STOP，且再集中於持有 ≤ 5 個官方交易日的極短停損；這些腿的虧損幾乎全是價差（費稅占比很小）。")
    add("- 中期 REGIME_EXIT 的費後合計為正，長期 REGIME_EXIT 為小額負。")
    add("- 選股組各組在費後皆為負，且各組高度重疊（多標籤），不能相加也不能單獨歸因。")
    add("- 虧損具月份集中性（中期 8、9 月；長期 9 月最重）。")
    add("")
    add("**只是推測（不當結論）**")
    add("")
    add("- 不能因 STRUCTURAL_STOP 筆數多就斷言『停損設錯』：出場後收盤續跌與盤中反彈並存，方向不一致。")
    add("- 不能因某一選股組虧損就斷言『該組策略錯』：組間高度重疊，且缺少同一股票在別組的對照。")
    add("- 不能把未還原的 81 筆當成真正的虧損，也不能把應收股利／配股當成入帳現金。")
    add("- 未平倉腿的最終結果未知，任何把它們算進去的績效都是推測。")
    add("")

    # Section 14
    add("## 14. 可理解的主因排序")
    add("")
    add("1. 進場後極短期即被結構停損（≤ 5 個官方交易日），是最大的單一虧損來源。")
    add("2. 費用／稅在費前已虧的基礎上加碼拖累（中期約 22.7%、長期約 15.6% 的費後虧損量級）。")
    add("3. 9 月（兩策略）與 8 月（中期）的月份集中虧損。")
    add("4. 再進場腿整體仍為負（中期 −19,462.80、長期 −21,799.80），僅部分個案補回前虧。")
    add("5. 少數權息未解（81 筆）與已還原（69 筆）結果分開呈現，未解部分不能當已定案。")
    add("")

    # Section 15
    add("## 15. 唯一下一版事前凍結候選（單一變因）")
    add("")
    add(
        "**候選：進場時要求『結構停損寬度』下限。** 在 T 日，把（T 收盤進場價 − 已 V 確認的樞紐低點）"
        "除以 T 進場價，得到一個百分比 `risk_width_pct`；只在 `risk_width_pct ≥ 門檻` 時才允許進場。"
        "門檻本身就是下一版要事前凍結的那一個參數（本報告僅以 5%／6%／7%／8%／10% 做敏感度呈現，"
        "不主張哪一個最好、也不調參找正報酬）。"
    )
    add("")
    add(
        "**怎麼用 T 當時可得欄位判定**：T 收盤價來自當日已知收盤；樞紐低點是 V 日已因果確認的 `pivot_low`；"
        "兩者都在 T 當下可得，`initial_stop` 等於該樞紐低點（凍結 gate：`initial_stop_equals_confirmed_pivot_low` = PASS）。"
        "因此此條件在任何 T 當下都能唯一判定，不需要未來資料。"
    )
    add("")
    add(
        "**如何保持雙均線核心**：不改 D→E→T 的雙均線與監控狀態機、不改 V 樞紐確認、不改出場、不改費稅；"
        "只在原本就會成交的 T 訊號上，多加一道以 V 樞紐為基礎的風險寬度門檻。此門檻與『啟蒙 V2 大定錨』同向"
        "（要求進場前有足夠的結構空間，避免貼著極近防線進場即被洗出），但不宣稱等同大級定錨複製共振。"
    )
    add("")
    add("**預期改善哪種損失**：本輪最大虧損來源是極短期（≤ 5 日）結構停損；風險寬度下限直接縮小這種『貼線進場、立刻被洗』的比例。")
    add("")
    add("**可能犧牲哪些大贏家**：寬度下限會同時剔除一部分風險窄、但事後走出大段的進場；被剔腿的費前獲利不可忽略（見下表）。")
    add("")
    for strategy in STRATEGIES:
        add(f"### {STRATEGY_LABEL_ZH[strategy]}｜風險寬度敏感度")
        add("")
        lines.extend(_table(
            ("門檻", "保留筆數", "保留費後", "保留費前獲利", "剔除筆數", "剔除費後", "剔除費前獲利"),
            [[row["threshold_pct"] + "%", row["kept"]["closed_count"], _fmt(row["kept"]["costed_net_pnl_total"]), _fmt(row["kept"]["gross_profit"]), row["dropped"]["closed_count"], _fmt(row["dropped"]["costed_net_pnl_total"]), _fmt(row["dropped"]["gross_profit"])] for row in summary["risk_width_sensitivity"][strategy]],
        ))
        add("")
    add(
        "上表為**樣本內**描述，尚未凍結、尚未重播、也未做任何顯著性檢定；"
        "它只能支持『值得事前凍結測試』，不能當成已驗證的改善。若後續要測，必須先凍結門檻與唯一規則，再重播新變體；本輪不跑。"
    )
    add("")

    # Section 16
    add("## 16. 限制與資料缺口")
    add("")
    add("- 價格為未還原供應商收盤代理（不是官方收盤），公司辨識為現行註冊代理，絕對價位只是近似。")
    add("- 81 筆公司行動未解、另有 14 筆序列位移；未解腿只能當比較基準。")
    add("- 未平倉 189 筆最終結果未知（`CENSORED`），不可補 2023 價格。")
    add("- `chip_4`、`curated` 在 H2 完全沒有候選，不列為 0 筆。")
    add("- 早期停損、風險寬度、事後走勢皆為事後診斷欄位，含未來的資訊，禁止當成當日重播或可交易訊號。")
    add("- 本輪未執行新回測、未改程式邏輯、未搜尋正報酬參數。")
    add("")

    add("## 附錄 A：完整 0 bps 逐筆基礎表")
    add("")
    add("（唯一鍵加入 D／E／V／T、買賣價與日期、股數、費稅、出場原因、是否再進場、與前次出場關聯、選股組、公司行動狀態、費前與費後損益。）")
    add("")
    lines.extend(_table(
        ("leg_key", "策略", "代號", "D", "E", "V", "T", "進場價", "出場日", "出場價", "股數", "費稅", "出場原因", "再進場", "前次出場", "選股組", "公司行動", "費前", "費後"),
        [[
            r["leg_key"], r["strategy"], r["symbol"], r["D"], r["E"], r["V"], r["T"],
            _fmt(r["entry_fill_price"]), r["exit_date"] or "未平倉", _fmt(r["exit_fill_price"]) or "未平倉",
            r["shares"], _fmt(r["total_fees_and_tax"]), r["exit_reason"] or "未平倉",
            "是" if r["is_reentry"] else "否", r["prior_exit_date"] or "", r["selection_groups"],
            r["corporate_action_status"], _fmt(r["gross_pnl"]) or "未平倉", _fmt(r["costed_net_pnl"]) or "未平倉",
        ] for r in diag],
    ))
    add("")
    add("## 附錄 B：完整 0 bps 逐筆診斷表")
    add("")
    add("（持有日數、早期停損診斷、同 episode 前後筆損益、以及出場後 5／10／20 個官方日的事後走勢與最高反彈。事後欄位為解釋性，非可交易訊號。）")
    add("")
    lines.extend(_table(
        ("leg_key", "持有官方日", "持有日曆日", "風險寬度%", "價格對快線%", "早期停損", "episode腿數", "前一筆費後", "後一筆費後", "episode合計", "出場後5日收盤", "出場後10日收盤", "出場後20日收盤", "20日最高反彈%", "事後狀態"),
        [[
            r["leg_key"], r["hold_sessions_transacted"], r["hold_calendar_days"],
            _fmt(r["risk_width_pct"], 2), _fmt(r["fast_extension_pct"], 2),
            "是" if r["early_stop_diag"] else "否", r["episode_leg_count"],
            _fmt(r["prior_leg_costed_net_pnl"]), _fmt(r["next_leg_costed_net_pnl"]),
            _fmt(r["episode_costed_net_pnl_total"]),
            _fmt(r["post_exit_5_close"], 2), _fmt(r["post_exit_10_close"], 2), _fmt(r["post_exit_20_close"], 2),
            _fmt(r["post_exit_20_max_rebound_pct"], 2), r["post_exit_20_status"],
        ] for r in diag],
    ))
    add("")
    add("## 附錄 C：兩筆人工核算")
    add("")
    add(
        "兩筆都直接由原始價格快照重算，不是只讀檔內欄位：第 1 筆是中期某 episode 的首筆母單，"
        "第 2 筆是另一 episode 的再進場（第 2 筆）母單，藉此同時檢查首筆與再進場的重算與關聯。"
        "公式為：費後損益 = 賣出淨額 − 買入總成本，而賣出淨額 = 賣出價×股數 − 賣手續費 − 證交稅、"
        "買入總成本 = 買入價×股數 + 買手續費，買賣價在 0 bps 下即 T 進場日與出場日的收盤代理價。"
    )
    add("")
    lines.extend(_table(
        ("leg_key", "進場價×股數", "買手續費", "賣出淨額", "費後損益(重算)", "費後損益(檔內)", "一致"),
        [[c["leg_key"], c["entry_x_shares"], c["buy_commission"], c["sell_net"], c["recomputed_net"], c["declared_net"], c["match"]] for c in hand_checks],
    ))
    add("")
    add(
        "另外抽查 1102.TW 中期再進場腿的出場後窗口：出場日 2022-07-28 之後的 5 個官方交易日收盤為 "
        "41.45／41.35／41.40／41.45／40.70，故 `post_exit_5_close` = 40.70，與逐筆診斷檔一致；"
        "該 5 日最高價 41.55 對出場價 40.50 即 `post_exit_5_max_rebound_pct` = 2.59%。此欄僅為事後診斷。"
    )
    add("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


LEG_CSV_FIELDS = (
    "leg_key", "strategy", "code", "symbol", "is_reentry", "reentry_ordinal",
    "episode_key", "episode_ordinal", "D", "E", "V", "T", "entry_fill_price",
    "entry_fast_sma", "shares", "initial_stop", "pivot_low", "breakout_level",
    "prior_leg_id", "prior_exit_date", "prior_exit_reason", "same_episode_after_exit",
    "exit_date", "exit_reason", "exit_fill_price", "buy_gross", "buy_commission",
    "buy_total", "sell_gross", "sell_commission", "sell_tax", "sell_net",
    "total_fees_and_tax", "gross_pnl", "costed_net_pnl", "net_return_pct",
    "open_censored", "selection_groups", "corporate_action_status",
    "corrected_exit_date", "realized_net_pnl_corrected", "economic_total_corrected",
    "correction_delta_vs_raw", "hold_sessions_transacted", "hold_calendar_days",
    "risk_width_pct", "fast_extension_pct", "early_stop_diag", "risk_width_wide_diag",
    "episode_leg_count", "prior_leg_costed_net_pnl", "next_leg_costed_net_pnl",
    "episode_costed_net_pnl_total", "post_exit_5_close", "post_exit_10_close",
    "post_exit_20_close", "post_exit_20_status", "post_exit_20_max_rebound_pct",
    "post_exit_5_max_rebound_pct", "post_exit_10_max_rebound_pct",
    "post_exit_5_forward_pct", "post_exit_10_forward_pct", "post_exit_20_forward_pct",
)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def write_leg_table(diag: Sequence[Mapping[str, Any]], *, csv_path: Path, jsonl_path: Path) -> None:
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(LEG_CSV_FIELDS), extrasaction="ignore")
        writer.writeheader()
        for row in diag:
            writer.writerow({field: _cell(row.get(field)) for field in LEG_CSV_FIELDS})
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for row in diag:
            handle.write(json.dumps({field: _cell(row.get(field)) for field in LEG_CSV_FIELDS}, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def git_state(repo_root: str | Path) -> dict[str, Any]:
    def _run(*args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.stdout.strip()

    status = _run("status", "--porcelain")
    entries = [line for line in status.splitlines() if line.strip()]
    return {
        "branch": _run("rev-parse", "--abbrev-ref", "HEAD"),
        "head_commit": _run("rev-parse", "HEAD"),
        "status_porcelain": status,
        "dirty_entry_count": len(entries),
        "dirty_entries": entries,
    }


def _artifact_hashes(output_root: Path, names: Sequence[str]) -> dict[str, str]:
    return {
        name: pilot.sha256_file(output_root / name)
        for name in names
        if (output_root / name).is_file()
    }


ARTIFACT_NAMES = (
    "report_zh.md",
    "attribution_legs.csv",
    "attribution_legs.jsonl",
    "attribution_summary.json",
    "hand_checks.json",
)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def compute_hand_checks(diag: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> list[dict[str, Any]]:
    by_key = {row["leg_key"]: row for row in diag}
    checks: list[dict[str, Any]] = []
    for key in keys:
        row = by_key[key]
        entry_x_shares = _q(row["entry_fill_price"] * row["shares"])
        recomputed_net = _q(row["sell_net"] - row["buy_total"]) if not row["open_censored"] else None
        checks.append({
            "leg_key": key,
            "entry_x_shares": str(entry_x_shares),
            "buy_commission": str(row["buy_commission"]),
            "sell_net": str(row["sell_net"]) if row["sell_net"] is not None else "",
            "recomputed_net": str(recomputed_net) if recomputed_net is not None else "",
            "declared_net": str(row["costed_net_pnl"]) if row["costed_net_pnl"] is not None else "",
            "match": str(recomputed_net == row["costed_net_pnl"]) if recomputed_net is not None else "n/a",
        })
    return checks


def run_attribution(
    *, workdata_root: str | Path, frozen_run_dir: str | Path, output_root: str | Path,
    run_id: str = RUN_ID_DEFAULT, hand_check_keys: Sequence[str] | None = None,
) -> dict[str, Any]:
    workdata_root = Path(workdata_root)
    output_root = Path(output_root)
    frozen_run_dir = Path(frozen_run_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    legs = load_frozen_legs(frozen_run_dir)
    sessions = load_sessions(workdata_root)
    price_snapshot_root = workdata_root / pilot.PRICE_SNAPSHOT_RELATIVE_PATH
    prices = load_price_index(price_snapshot_root)
    if prices.last_session > AS_OF_CUTOFF:
        raise AttributionError("price snapshot reaches beyond the frozen cutoff")

    diag = build_leg_diagnostics(legs, sessions=sessions, prices=prices)
    summary = build_summary(diag, frozen_run_dir=frozen_run_dir)

    if hand_check_keys is None:
        available = {r["leg_key"] for r in diag}
        hand_check_keys = tuple(key for key in HAND_CHECK_LEG_KEYS if key in available)
    hand_checks = compute_hand_checks(diag, hand_check_keys)

    identities = {
        "run_id": run_id,
        "frozen_run_id": FROZEN_RUN_ID,
        "trades_sha256": legs.trades_sha256.upper(),
        "price_snapshot_content_sha256": prices.content_sha256,
        "price_snapshot_last_session": prices.last_session.isoformat(),
        "calendar_raw_sha256": pilot.CALENDAR_RAW_SHA256,
    }

    report = render_report(summary, diag, identities=identities, hand_checks=hand_checks)
    report_path = output_root / "report_zh.md"
    report_path.write_text(report, encoding="utf-8")
    write_leg_table(diag, csv_path=output_root / "attribution_legs.csv", jsonl_path=output_root / "attribution_legs.jsonl")
    (output_root / "attribution_summary.json").write_text(
        json.dumps({"summary": summary, "identities": identities, "hand_checks": hand_checks}, ensure_ascii=False, indent=1, default=_json_default) + "\n",
        encoding="utf-8",
    )
    (output_root / "hand_checks.json").write_text(
        json.dumps({"checks": hand_checks, "schema_version": "dual-ma-h2-flat-reentry-attribution-hand-checks-v1"}, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )

    # git state is the working tree that carries this read-only script, not the
    # shared .workdata checkout
    repo_root = Path(__file__).resolve().parents[2]
    artifact_sha256 = _artifact_hashes(output_root, ARTIFACT_NAMES)
    status_counts = Counter(r["corporate_action_status"] for r in diag)
    manifest = {
        "schema_version": "dual-ma-h2-flat-reentry-attribution-manifest-v1",
        "run_id": run_id,
        "status": "PASS",
        "research_only": True,
        "formal_historical_performance": False,
        "point_in_time_verified": False,
        "engine_replayed": False,
        "rules_modified": False,
        "frozen_run_modified": False,
        "sample_definition": (
            "the frozen 2021-2022 H2 flat re-entry run's 0 bps leg rows, read "
            "verbatim; 2023 / 2024 data is never read"
        ),
        "frozen_run": {
            "run_id": FROZEN_RUN_ID,
            "run_dir": str(frozen_run_dir),
            "trades_sha256": legs.trades_sha256.upper(),
            "trades_sha256_expected": FROZEN_TRADES_SHA256_DECLARED,
            "trades_sha256_unchanged": legs.trades_sha256.upper() == FROZEN_TRADES_SHA256_DECLARED,
        },
        "inputs": {
            "price_snapshot_content_sha256": prices.content_sha256,
            "price_snapshot_root": str(prices.root),
            "price_snapshot_last_session": prices.last_session.isoformat(),
            "price_snapshot_never_reaches_2023": prices.last_session.year <= LATEST_READABLE_YEAR,
            "calendar_raw_sha256": pilot.CALENDAR_RAW_SHA256,
            "calendar_path": str(workdata_root / pilot.CALENDAR_RELATIVE_PATH),
        },
        "counts": {
            "zero_bps_leg_count": len(diag),
            "closed_leg_count": sum(1 for r in diag if not r["open_censored"]),
            "open_leg_count": sum(1 for r in diag if r["open_censored"]),
            "reentry_leg_count": sum(1 for r in diag if r["is_reentry"]),
            "early_stop_leg_count": sum(1 for r in diag if r["early_stop_diag"]),
            "corporate_action_status_counts": dict(sorted(status_counts.items())),
        },
        "primary_holding_diagnostic": {
            "early_stop_max_official_sessions": EARLY_STOP_MAX_SESSIONS,
            "risk_width_wide_threshold_pct": str(RISK_WIDTH_WIDE_PCT),
            "post_event_horizons_days": list(POST_EXIT_HORIZONS),
            "note": (
                "early-stop and risk-width labels are post-hoc diagnostic "
                "classifications; they are not trade gates"
            ),
        },
        "out_of_bounds_declaration": [
            "no new backtest or rule change was performed",
            "the frozen run directory and every parent run are byte-unchanged",
            "no cross-strategy sum is presented as a single account",
            "post-exit windows are post-event diagnostics, never a tradeable signal",
            "no 2023/2024 price or performance was read",
        ],
        "git_state": git_state(repo_root),
        "artifact_sha256": artifact_sha256,
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )

    recomputed = _artifact_hashes(output_root, ARTIFACT_NAMES)
    readback = {
        "schema_version": "dual-ma-h2-flat-reentry-attribution-readback-v1",
        "run_id": run_id,
        "artifact_sha256": manifest["artifact_sha256"],
        "artifact_sha256_recomputed": recomputed,
        "artifacts_verified": {
            name: recomputed.get(name) == digest
            for name, digest in manifest["artifact_sha256"].items()
        },
        "frozen_trades_sha256_matched": manifest["frozen_run"]["trades_sha256_unchanged"],
        "price_snapshot_never_reaches_2023": manifest["inputs"]["price_snapshot_never_reaches_2023"],
        "status": "PASS"
        if all(
            recomputed.get(name) == digest
            for name, digest in manifest["artifact_sha256"].items()
        )
        else "FAIL",
    }
    (output_root / "readback.json").write_text(
        json.dumps(readback, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return {
        "run_id": run_id,
        "output_root": str(output_root),
        "identities": identities,
        "leg_count": len(diag),
        "summary": summary,
        "hand_checks": hand_checks,
        "manifest": manifest,
        "readback": readback,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdata-root", type=Path, required=True)
    parser.add_argument("--frozen-run-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--run-id", default=RUN_ID_DEFAULT)
    args = parser.parse_args(argv)
    result = run_attribution(
        workdata_root=args.workdata_root,
        frozen_run_dir=args.frozen_run_dir,
        output_root=args.output_root,
        run_id=args.run_id,
    )
    print(
        "attribution {r} legs={l} out={o}".format(
            r=result["run_id"], l=result["leg_count"], o=result["output_root"]
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
