"""Data-agnostic research surfaces for the frozen 00635U preparation.

All functions require caller-supplied, already validated rows.  They never
download data and therefore cannot bypass the manifest gate in prep v1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import product
from typing import Any, Callable, Mapping, Sequence

from .etf_00635u_strategy_v1 import (
    CORE_ALLOCATIONS,
    GRID_SIZE,
    INITIAL_CAPITAL,
    STRESS_SLIPPAGE_BPS,
    ETFResearchConfig,
    ExecutionTiming,
    IndicatorRow,
    StrategyVersion,
    expected_comparison_lines,
    run_strategy,
)
from .models import InstrumentType, decimal


MA_FAST_VALUES = (18, 21, 24)
MA_SLOW_VALUES = (126, 144, 162)
MACD_FAST_VALUES = (18, 21, 24)
MACD_SLOW_VALUES = (50, 55, 60)
MACD_SIGNAL_VALUES = (45, 55, 65)
PARAMETER_GRID = tuple(
    product(
        MA_FAST_VALUES,
        MA_SLOW_VALUES,
        MACD_FAST_VALUES,
        MACD_SLOW_VALUES,
        MACD_SIGNAL_VALUES,
    )
)


@dataclass(frozen=True)
class ComparisonLine:
    line_id: str
    role: str
    curve: tuple[tuple[date, Decimal], ...]
    audit: Mapping[str, Any] = field(default_factory=dict)

    @property
    def final_equity(self) -> Decimal:
        return self.curve[-1][1]


@dataclass(frozen=True)
class IndexObservation:
    source_date: date
    available_date: date
    value: Decimal
    source_timezone: str

    def __post_init__(self) -> None:
        if self.value <= 0:
            raise ValueError("index observation must be positive")
        if self.available_date < self.source_date:
            raise ValueError("index cannot be available before its source date")
        if not self.source_timezone:
            raise ValueError("index source timezone is required")


def _affordable_shares(cash: Decimal, price: Decimal, model) -> int:
    shares = int(cash / model._fill_price(price, "BUY"))
    while shares > 0 and model.buy(price, shares).cash_amount > cash:
        shares -= 1
    return shares


def run_buy_hold(
    rows: Sequence[IndicatorRow],
    *,
    reinvest: bool,
    config: ETFResearchConfig = ETFResearchConfig(),
) -> ComparisonLine:
    if not rows:
        raise ValueError("buy-and-hold rows cannot be empty")
    if rows[0].day < config.listing_date:
        raise ValueError("pre-listing 00635U prices are forbidden")
    if any(left.day >= right.day for left, right in zip(rows, rows[1:])):
        raise ValueError("rows must be strictly increasing")
    model = config.cost_model
    cash = config.initial_capital
    shares = 0
    entered = False
    curve: list[tuple[date, Decimal]] = []
    dividend_rights: dict[str, Decimal] = {}
    paid_dividend_ids: set[str] = set()
    reinvest_budget = Decimal("0")
    deferred_sessions: list[str] = []
    account_daily: list[dict[str, object]] = []
    transactions: list[dict[str, object]] = []
    for row in rows:
        executable_open = (
            row.official_session
            and row.tradable
            and row.volume > 0
            and row.raw_open is not None
        )
        executable_close = row.official_session and row.tradable and row.volume > 0
        if shares and row.share_factor != 1:
            changed = Decimal(shares) * row.share_factor
            if changed != changed.to_integral_value():
                raise ValueError("share factor must preserve whole shares")
            shares = int(changed)
        dividend_cash = Decimal("0")
        if row.dividend_stage == "ENTITLEMENT":
            if row.dividend_event_id in dividend_rights or row.dividend_event_id in paid_dividend_ids:
                raise ValueError("duplicate dividend entitlement id")
            dividend_rights[row.dividend_event_id] = row.cash_dividend_per_share * shares
        elif row.dividend_stage == "PAYMENT":
            if row.dividend_event_id in paid_dividend_ids:
                raise ValueError("duplicate dividend payment id")
            if row.dividend_event_id not in dividend_rights:
                raise ValueError("dividend payment has no prior entitlement")
            dividend_cash = dividend_rights.pop(row.dividend_event_id)
            paid_dividend_ids.add(row.dividend_event_id)
        elif row.cash_dividend_per_share:
            dividend_cash = row.cash_dividend_per_share * shares
        cash += dividend_cash
        if reinvest:
            reinvest_budget += dividend_cash
        if not entered and executable_open:
            shares = _affordable_shares(cash, row.raw_open, model)
            if shares < 1:
                raise ValueError("initial capital cannot buy one share")
            entry_order = model.buy(row.raw_open, shares)
            cash -= entry_order.cash_amount
            transactions.append({
                "date": row.day, "role": "CORE_ENTRY", "shares": shares,
                "cash_amount": entry_order.cash_amount,
                "commission": entry_order.commission, "tax": entry_order.tax,
            })
            entered = True
        elif not entered:
            deferred_sessions.append(row.day.isoformat())
        if reinvest and reinvest_budget > 0 and executable_close and entered:
            added = _affordable_shares(min(cash, reinvest_budget), row.raw_close, model)
            if added:
                order = model.buy(row.raw_close, added)
                cash -= order.cash_amount
                reinvest_budget -= order.cash_amount
                shares += added
                transactions.append({
                    "date": row.day, "role": "DIVIDEND_REINVEST", "shares": added,
                    "cash_amount": order.cash_amount,
                    "commission": order.commission, "tax": order.tax,
                })
        liquidation = (
            model.sell(row.raw_close, shares, InstrumentType.ETF).cash_amount
            if shares
            else Decimal("0")
        )
        curve.append((row.day, cash + liquidation))
        account_daily.append({
            "date": row.day, "cash": cash, "shares": shares,
            "liquidation_value": liquidation, "equity": cash + liquidation,
        })
    if not entered:
        raise ValueError("no tradable official session with an opening price")
    return ComparisonLine(
        "00635U_BUY_HOLD_REINVEST" if reinvest else "00635U_BUY_HOLD_CASH",
        "TRADABLE_PASSIVE_COMPARATOR",
        tuple(curve),
        {
            "equity_policy": "CASH_BASIS_EXCLUDES_UNPAID_RECEIVABLES",
            "unpaid_dividend_receivables": {
                key: str(value) for key, value in sorted(dividend_rights.items())
            },
            "paid_dividend_event_ids": tuple(sorted(paid_dividend_ids)),
            "initial_entry_deferred_sessions": tuple(deferred_sessions),
            "uninvested_reinvestment_budget": str(reinvest_budget),
            "account_daily": tuple(account_daily),
            "transactions": tuple(transactions),
        },
    )


def run_index_line(
    index_values: (
        Mapping[date, Decimal | int | float | str]
        | Sequence[IndexObservation]
    ),
    sessions: Sequence[date],
    *,
    line_id: str = "GOLD_ER_OFFICIAL_TRACKED_INDEX",
    alignment_policy: str = "STRICT_SAME_SESSION",
) -> ComparisonLine:
    policies = {"STRICT_SAME_SESSION", "COMMON_PUBLISHED_DAYS", "LAST_KNOWN_PUBLISHED"}
    if alignment_policy not in policies:
        raise ValueError(f"unsupported index alignment policy: {alignment_policy}")
    if isinstance(index_values, Mapping):
        observations = [
            IndexObservation(day, day, decimal(value), "UNSPECIFIED_LEGACY_MAPPING")
            for day, value in sorted(index_values.items())
        ]
    else:
        observations = sorted(index_values, key=lambda item: (item.available_date, item.source_date))
    if len({item.source_date for item in observations}) != len(observations):
        raise ValueError("duplicate official index source_date")
    aligned: list[tuple[date, Decimal]] = []
    ledger: list[dict[str, object]] = []
    for session in sessions:
        candidates = [item for item in observations if item.available_date <= session]
        selected = candidates[-1] if candidates else None
        if alignment_policy == "STRICT_SAME_SESSION":
            selected = next(
                (
                    item for item in observations
                    if item.available_date == session and item.source_date == session
                ),
                None,
            )
            if selected is None:
                raise ValueError(f"official index missing aligned sessions: {[session]}")
        elif alignment_policy == "COMMON_PUBLISHED_DAYS":
            selected = next(
                (item for item in observations if item.available_date == session),
                None,
            )
            if selected is None:
                ledger.append({"session": session, "status": "NO_COMMON_PUBLISHED_VALUE"})
                continue
        elif selected is None:
            ledger.append({"session": session, "status": "NO_PUBLISHED_VALUE_YET"})
            continue
        aligned.append((session, selected.value))
        ledger.append({
            "session": session,
            "status": (
                "EXACT_AVAILABLE_DATE"
                if selected.available_date == session
                else "LAST_KNOWN_PUBLISHED"
            ),
            "source_date": selected.source_date,
            "available_date": selected.available_date,
            "source_timezone": selected.source_timezone,
            "staleness_calendar_days": (session - selected.available_date).days,
        })
    common = aligned
    if not common or common[0][1] <= 0 or any(value <= 0 for _, value in common):
        raise ValueError("official index has no positive common-date values")
    base = common[0][1]
    return ComparisonLine(
        line_id,
        "NON_TRADABLE_MARKET_BENCHMARK",
        tuple((day, INITIAL_CAPITAL * value / base) for day, value in common),
        {
            "alignment_policy": alignment_policy,
            "alignment_ledger": tuple(ledger),
            "silent_forward_fill": False,
        },
    )


def run_seven_lines(
    rows: Sequence[IndicatorRow],
    gold_er_values: (
        Mapping[date, Decimal | int | float | str]
        | Sequence[IndexObservation]
    ),
    *,
    slippage_bps: int = 20,
    gold_alignment_policy: str = "STRICT_SAME_SESSION",
) -> tuple[ComparisonLine, ...]:
    output: list[ComparisonLine] = []
    for strategy in StrategyVersion:
        for timing in ExecutionTiming:
            config = ETFResearchConfig(
                strategy=strategy,
                timing=timing,
                slippage_bps=slippage_bps,
            )
            result = run_strategy(rows, config)
            output.append(
                ComparisonLine(
                    f"STRATEGY_{strategy.name}_{timing.value}",
                    "ACTIVE_STRATEGY",
                    tuple((item["date"], item["equity"]) for item in result.state.daily),
                    {
                        "trades": tuple(result.state.trades),
                        "events": tuple(result.state.events),
                    },
                )
            )
    benchmark_config = ETFResearchConfig(slippage_bps=slippage_bps)
    output.append(run_buy_hold(rows, reinvest=False, config=benchmark_config))
    output.append(run_buy_hold(rows, reinvest=True, config=benchmark_config))
    output.append(run_index_line(
        gold_er_values,
        [row.day for row in rows],
        alignment_policy=gold_alignment_policy,
    ))
    common_dates = set(day for day, _value in output[0].curve)
    for line in output[1:]:
        common_dates &= {day for day, _value in line.curve}
    if not common_dates:
        raise ValueError("main comparison lines have no common period")
    ordered_common = sorted(common_dates)
    if ordered_common != [row.day for row in rows]:
        raise ValueError(
            "Gold ER must have a causally available value from the first ETF session"
        )
    aligned = []
    for line in output:
        values = dict(line.curve)
        base = values[ordered_common[0]]
        rebased = tuple(
            (day, INITIAL_CAPITAL * values[day] / base) for day in ordered_common
        )
        aligned.append(ComparisonLine(
            line.line_id,
            line.role,
            rebased,
            {
                **dict(line.audit),
                "comparison_start": ordered_common[0],
                "comparison_end": ordered_common[-1],
                "comparison_observations": len(ordered_common),
                "rebased_to": INITIAL_CAPITAL,
            },
        ))
    output = aligned
    if tuple(line.line_id for line in output) != expected_comparison_lines():
        raise AssertionError("main comparison line order changed")
    return tuple(output)


def parameter_grid() -> tuple[tuple[int, int, int, int, int], ...]:
    if len(PARAMETER_GRID) != GRID_SIZE:
        raise AssertionError("parameter grid must contain exactly 243 cells")
    return PARAMETER_GRID


def run_parameter_grid(
    indicator_builder: Callable[[tuple[int, int, int, int, int]], Sequence[IndicatorRow]],
    *,
    strategy: StrategyVersion,
    timing: ExecutionTiming,
    slippage_bps: int = 20,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for parameters in parameter_grid():
        result = run_strategy(
            indicator_builder(parameters),
            ETFResearchConfig(
                strategy=strategy,
                timing=timing,
                slippage_bps=slippage_bps,
            ),
        )
        output.append(
            {
                "ma_fast": parameters[0],
                "ma_slow": parameters[1],
                "macd_fast": parameters[2],
                "macd_slow": parameters[3],
                "macd_signal": parameters[4],
                "kd": "9/9/55",
                "two_r": "2R",
                "ending_equity": result.final_equity,
            }
        )
    return output

def cost_sensitivity(
    rows: Sequence[IndicatorRow],
) -> list[dict[str, object]]:
    output = []
    for bps in STRESS_SLIPPAGE_BPS:
        for strategy in StrategyVersion:
            for timing in ExecutionTiming:
                result = run_strategy(
                    rows,
                    ETFResearchConfig(
                        strategy=strategy,
                        timing=timing,
                        slippage_bps=bps,
                    ),
                )
                curve = tuple((item["date"], item["equity"]) for item in result.state.daily)
                metrics = performance_metrics(curve, result.state.trades)
                output.append(
                    {
                        "slippage_bps": bps,
                        "strategy": strategy.value,
                        "timing": timing.value,
                        "ending_equity": result.final_equity,
                        "cagr": metrics["cagr"],
                        "max_drawdown": metrics["max_drawdown"],
                        "completed_rounds": metrics["closed_positions"],
                    }
                )
    return output


def slice_curve(
    curve: Sequence[tuple[date, Decimal]],
    start: date,
    end: date,
    *,
    initial_capital: Decimal = INITIAL_CAPITAL,
) -> dict[str, object]:
    selected = [(day, value) for day, value in curve if start <= day <= end]
    if not selected:
        return {"start": start, "end": end, "status": "INSUFFICIENT_DATA"}
    prior = [value for day, value in curve if day < selected[0][0]]
    starting_equity = prior[-1] if prior else initial_capital
    return {
        "start": selected[0][0],
        "end": selected[-1][0],
        "status": "OK",
        "starting_equity": starting_equity,
        "ending_equity": selected[-1][1],
        "return": selected[-1][1] / starting_equity - Decimal("1"),
        "cagr": Decimal(str(
            float(selected[-1][1] / starting_equity)
            ** float(Decimal("365.25") / Decimal(max((selected[-1][0] - selected[0][0]).days + 1, 1)))
            - 1
        )),
        "max_drawdown": max_drawdown(
            ((selected[0][0], starting_equity), *selected)
        ),
    }


def annual_slices(
    curve: Sequence[tuple[date, Decimal]],
    *,
    initial_capital: Decimal = INITIAL_CAPITAL,
) -> list[dict[str, object]]:
    years = sorted({day.year for day, _ in curve})
    return [
        slice_curve(
            curve,
            date(year, 1, 1),
            date(year, 12, 31),
            initial_capital=initial_capital,
        )
        for year in years
    ]


def rolling_windows(
    curve: Sequence[tuple[date, Decimal]], years: int
) -> list[dict[str, object]]:
    if years not in (3, 5, 10):
        raise ValueError("rolling window must be 3, 5, or 10 years")
    if not curve:
        return [{"years": years, "status": "INSUFFICIENT_DATA"}]
    monthly = month_end_curve(curve)
    output: list[dict[str, object]] = []
    for index, (start, starting) in enumerate(monthly):
        try:
            target = start.replace(year=start.year + years)
        except ValueError:
            target = start.replace(year=start.year + years, day=28)
        candidates = [(day, value) for day, value in monthly[index + 1 :] if day >= target]
        if not candidates:
            continue
        end, ending = candidates[0]
        window = [(day, value) for day, value in curve if start <= day <= end]
        output.append(
            {
                "years": years,
                "start": start,
                "end": end,
                "status": "OK",
                "total_return": ending / starting - Decimal("1"),
                "cagr": Decimal(
                    str(float(ending / starting) ** (1 / years) - 1)
                ),
                "max_drawdown": max_drawdown(window),
                "sampling": "MONTH_END",
            }
        )
    return output or [{"years": years, "status": "INSUFFICIENT_DATA"}]


def month_end_curve(
    curve: Sequence[tuple[date, Decimal]],
) -> tuple[tuple[date, Decimal], ...]:
    by_month: dict[tuple[int, int], tuple[date, Decimal]] = {}
    for day, value in curve:
        by_month[(day.year, day.month)] = (day, value)
    return tuple(by_month[key] for key in sorted(by_month))


def max_drawdown(curve: Sequence[tuple[date, Decimal]]) -> Decimal:
    peak: Decimal | None = None
    worst = Decimal("0")
    for _day, value in curve:
        peak = value if peak is None else max(peak, value)
        if peak:
            worst = min(worst, value / peak - Decimal("1"))
    return worst


def performance_metrics(
    curve: Sequence[tuple[date, Decimal]],
    trades: Sequence[Mapping[str, object]] = (),
    *,
    initial_capital: Decimal = INITIAL_CAPITAL,
) -> dict[str, object]:
    if not curve:
        raise ValueError("performance curve cannot be empty")
    start, end = curve[0][0], curve[-1][0]
    ending = curve[-1][1]
    elapsed_years = max(Decimal((end - start).days) / Decimal("365.25"), Decimal("1") / Decimal("365.25"))
    cagr = Decimal(str(float(ending / initial_capital) ** (1 / float(elapsed_years)) - 1))
    annual = annual_slices(curve, initial_capital=initial_capital)
    entries = [row for row in trades if row.get("role") == "ENTRY"]
    exits = [row for row in trades if row.get("role") == "EXIT"]
    open_positions = max(len(entries) - len(exits), 0)
    return {
        "starting_capital": initial_capital,
        "ending_equity": ending,
        "total_return": ending / initial_capital - Decimal("1"),
        "cagr": cagr,
        "max_drawdown": max_drawdown(curve),
        "profitable_years": sum(item.get("return", 0) > 0 for item in annual),
        "losing_years": sum(item.get("return", 0) < 0 for item in annual),
        "flat_years": sum(item.get("return", 0) == 0 for item in annual),
        "closed_positions": len(exits),
        "open_positions": open_positions,
        "maximum_single_entry_cash": max(
            (decimal(row["cash_amount"]) for row in entries),
            default=Decimal("0"),
        ),
    }


def relative_to_benchmark(
    strategy_metrics: Mapping[str, object],
    benchmark_metrics: Mapping[str, object],
) -> dict[str, Decimal]:
    return {
        "ending_equity_difference": decimal(strategy_metrics["ending_equity"])
        - decimal(benchmark_metrics["ending_equity"]),
        "total_return_difference": decimal(strategy_metrics["total_return"])
        - decimal(benchmark_metrics["total_return"]),
        "cagr_difference": decimal(strategy_metrics["cagr"])
        - decimal(benchmark_metrics["cagr"]),
        "max_drawdown_difference": decimal(strategy_metrics["max_drawdown"])
        - decimal(benchmark_metrics["max_drawdown"]),
    }


def path_diagnostics(curve: Sequence[tuple[date, Decimal]]) -> dict[str, object]:
    monthly = month_end_curve(curve)
    monthly_returns = [
        (current[0], current[1] / previous[1] - Decimal("1"))
        for previous, current in zip(monthly, monthly[1:])
    ]
    longest_losing = current_losing = 0
    for _day, value in monthly_returns:
        current_losing = current_losing + 1 if value < 0 else 0
        longest_losing = max(longest_losing, current_losing)
    peak_value: Decimal | None = None
    peak_day: date | None = None
    underwater_start: date | None = None
    longest_underwater = 0
    for day, value in curve:
        if peak_value is None or value >= peak_value:
            if underwater_start is not None:
                longest_underwater = max(longest_underwater, (day - underwater_start).days)
            peak_value, peak_day, underwater_start = value, day, None
        elif underwater_start is None:
            underwater_start = peak_day
    if underwater_start is not None:
        longest_underwater = max(longest_underwater, (curve[-1][0] - underwater_start).days)
    return {
        "longest_consecutive_losing_months": longest_losing,
        "longest_underwater_calendar_days": longest_underwater,
        "best_month": max(monthly_returns, key=lambda item: item[1], default=None),
        "worst_month": min(monthly_returns, key=lambda item: item[1], default=None),
    }


def regime_summary(daily: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    output = []
    for regime in ("HIGH", "LOW"):
        rows = [row for row in daily if row.get("regime") == regime]
        changes = [
            current["equity"] / previous["equity"] - Decimal("1")
            for previous, current in zip(daily, daily[1:])
            if current.get("regime") == regime and previous["equity"]
        ]
        output.append(
            {
                "regime": regime,
                "sessions": len(rows),
                "compounded_return": (
                    Decimal("0")
                    if not changes
                    else _compound(changes) - Decimal("1")
                ),
            }
        )
    return output


def _compound(values: Sequence[Decimal]) -> Decimal:
    result = Decimal("1")
    for value in values:
        result *= Decimal("1") + value
    return result


def trade_stability(trades: Sequence[Mapping[str, object]]) -> dict[str, object]:
    pnls = [decimal(row["net_pnl"]) for row in trades if row.get("role") == "EXIT"]
    gains = sorted((value for value in pnls if value > 0), reverse=True)
    total_gain = sum(gains, Decimal("0"))
    consecutive = longest = 0
    for value in pnls:
        consecutive = consecutive + 1 if value < 0 else 0
        longest = max(longest, consecutive)
    sorted_pnls = sorted(pnls, reverse=True)
    return {
        "closed_trades": len(pnls),
        "win_rate": (
            Decimal("0") if not pnls else Decimal(sum(value > 0 for value in pnls)) / len(pnls)
        ),
        "total_net_pnl": sum(pnls, Decimal("0")),
        "top1_gain_concentration": (
            Decimal("0") if not total_gain else sum(gains[:1], Decimal("0")) / total_gain
        ),
        "top3_gain_concentration": (
            Decimal("0") if not total_gain else sum(gains[:3], Decimal("0")) / total_gain
        ),
        "longest_consecutive_losses": longest,
        "best_trade": max(pnls, default=None),
        "worst_trade": min(pnls, default=None),
        "pnl_sum_after_removing_best_1": sum(sorted_pnls[1:], Decimal("0")),
        "pnl_sum_after_removing_best_3": sum(sorted_pnls[3:], Decimal("0")),
        "pnl_sum_after_removing_best_5": sum(sorted_pnls[5:], Decimal("0")),
    }


def trade_removal_cash_counterfactual(
    curve: Sequence[tuple[date, Decimal]],
    trades: Sequence[Mapping[str, object]],
    remove_best: int,
) -> dict[str, object]:
    """Subtract selected realized cash PnL after exit; does not rerun sizing/signals."""

    exits = sorted(
        (row for row in trades if row.get("role") == "EXIT"),
        key=lambda row: decimal(row["net_pnl"]),
        reverse=True,
    )[:remove_best]
    adjusted = []
    for day, equity in curve:
        removed = sum(
            (decimal(row["net_pnl"]) for row in exits if row["fill_date"] <= day),
            Decimal("0"),
        )
        adjusted.append((day, equity - removed))
    metrics = performance_metrics(adjusted)
    return {
        "method": "REALIZED_CASH_PNL_SUBTRACTION_NO_RESIZING",
        "removed_trades": len(exits),
        "ending_equity": metrics["ending_equity"],
        "cagr": metrics["cagr"],
        "max_drawdown": metrics["max_drawdown"],
    }


def parameter_grid_summary(
    rows: Sequence[Mapping[str, object]],
    *,
    baseline: tuple[int, int, int, int, int] = (21, 144, 21, 55, 55),
) -> dict[str, object]:
    if len(rows) != GRID_SIZE:
        raise ValueError("parameter summary requires exactly 243 rows")
    equities = sorted(decimal(row["ending_equity"]) for row in rows)
    def quantile(numerator: int, denominator: int) -> Decimal:
        index = (len(equities) - 1) * numerator // denominator
        return equities[index]
    keys = ("ma_fast", "ma_slow", "macd_fast", "macd_slow", "macd_signal")
    baseline_row = next(
        row for row in rows if tuple(int(row[key]) for key in keys) == baseline
    )
    neighborhood = [
        row for row in rows
        if sum(int(row[key]) != baseline[index] for index, key in enumerate(keys)) <= 1
    ]
    rolling_values = [
        decimal(row["rolling_5y_worst_cagr"])
        for row in rows
        if row.get("rolling_5y_worst_cagr") is not None
    ]
    return {
        "cells": len(rows),
        "minimum_ending_equity": equities[0],
        "q25_ending_equity": quantile(1, 4),
        "median_ending_equity": quantile(1, 2),
        "q75_ending_equity": quantile(3, 4),
        "maximum_ending_equity": equities[-1],
        "positive_expectation_cell_ratio": Decimal(
            sum(value > INITIAL_CAPITAL for value in equities)
        ) / Decimal(len(equities)),
        "baseline_ending_equity": decimal(baseline_row["ending_equity"]),
        "local_neighborhood_cells": len(neighborhood),
        "local_neighborhood_median": sorted(
            decimal(row["ending_equity"]) for row in neighborhood
        )[len(neighborhood) // 2],
        "rolling_5y_worst_cagr": min(rolling_values) if rolling_values else None,
    }


def core_allocation_curves(
    strategy_curve: Sequence[tuple[date, Decimal]],
    core_curve: Sequence[tuple[date, Decimal]],
) -> dict[int, tuple[tuple[date, Decimal], ...]]:
    strategy = dict(strategy_curve)
    core = dict(core_curve)
    common = sorted(set(strategy) & set(core))
    if not common:
        raise ValueError("strategy and core curves have no common dates")
    output = {}
    for weight in CORE_ALLOCATIONS:
        core_weight = Decimal(weight) / Decimal("100")
        output[weight] = tuple(
            (
                day,
                strategy[day] * (Decimal("1") - core_weight)
                + core[day] * core_weight,
            )
            for day in common
        )
    return output


def run_core_allocations(
    rows: Sequence[IndicatorRow],
    *,
    strategy: StrategyVersion,
    timing: ExecutionTiming,
    slippage_bps: int = 20,
) -> dict[int, tuple[tuple[date, Decimal], ...]]:
    """Run separate cash-safe core/satellite accounts, then add daily equity."""

    output: dict[int, tuple[tuple[date, Decimal], ...]] = {}
    for weight in CORE_ALLOCATIONS:
        core_capital = INITIAL_CAPITAL * Decimal(weight) / Decimal("100")
        satellite_capital = INITIAL_CAPITAL - core_capital
        satellite = run_strategy(
            rows,
            ETFResearchConfig(
                initial_capital=satellite_capital,
                strategy=strategy,
                timing=timing,
                slippage_bps=slippage_bps,
            ),
        )
        satellite_curve = {
            item["date"]: item["equity"] for item in satellite.state.daily
        }
        if core_capital:
            core = run_buy_hold(
                rows,
                reinvest=True,
                config=ETFResearchConfig(
                    initial_capital=core_capital,
                    slippage_bps=slippage_bps,
                ),
            )
            core_curve = dict(core.curve)
        else:
            core_curve = {day: Decimal("0") for day in satellite_curve}
        common = sorted(set(satellite_curve) & set(core_curve))
        output[weight] = tuple(
            (day, satellite_curve[day] + core_curve[day]) for day in common
        )
    return output


def run_core_allocation_study(
    rows: Sequence[IndicatorRow],
    *,
    strategy: StrategyVersion,
    timing: ExecutionTiming,
    slippage_bps: int = 20,
) -> dict[str, list[dict[str, object]]]:
    """Publish reconstructable no-rebalance core/tactical sleeve accounts."""

    summaries: list[dict[str, object]] = []
    daily: list[dict[str, object]] = []
    ledger: list[dict[str, object]] = []
    for weight in CORE_ALLOCATIONS:
        core_capital = INITIAL_CAPITAL * Decimal(weight) / Decimal("100")
        tactical_capital = INITIAL_CAPITAL - core_capital
        tactical = run_strategy(
            rows,
            ETFResearchConfig(
                initial_capital=tactical_capital,
                strategy=strategy,
                timing=timing,
                slippage_bps=slippage_bps,
            ),
        )
        tactical_by_day = {item["date"]: item for item in tactical.state.daily}
        if core_capital:
            core = run_buy_hold(
                rows,
                reinvest=True,
                config=ETFResearchConfig(initial_capital=core_capital, slippage_bps=slippage_bps),
            )
            core_by_day = {item["date"]: item for item in core.audit["account_daily"]}
            ledger.extend({
                "core_pct": weight, "sleeve": "CORE", **item
            } for item in core.audit["transactions"])
        else:
            core_by_day = {
                item.day: {
                    "date": item.day, "cash": Decimal("0"), "shares": 0,
                    "liquidation_value": Decimal("0"), "equity": Decimal("0"),
                }
                for item in rows
            }
        ledger.extend({
            "core_pct": weight, "sleeve": "TACTICAL", **item
        } for item in tactical.state.trades)
        for day in sorted(set(core_by_day) & set(tactical_by_day)):
            core_row, tactical_row = core_by_day[day], tactical_by_day[day]
            combined = core_row["equity"] + tactical_row["equity"]
            daily.append({
                "core_pct": weight, "rebalance": "NONE_MAIN_METHOD", "date": day,
                "core_cash": core_row["cash"], "core_shares": core_row["shares"],
                "core_liquidation_value": core_row["liquidation_value"],
                "core_equity": core_row["equity"],
                "tactical_cash": tactical_row["cash"],
                "tactical_shares": tactical_row["shares"],
                "tactical_liquidation_value": tactical_row["liquidation_value"],
                "tactical_equity": tactical_row["equity"],
                "combined_equity": combined,
                "reconstruction_error": combined - core_row["equity"] - tactical_row["equity"],
            })
        last = next(item for item in reversed(daily) if item["core_pct"] == weight)
        allocation_curve = tuple(
            (item["date"], item["combined_equity"])
            for item in daily if item["core_pct"] == weight
        )
        metrics = performance_metrics(allocation_curve)
        allocation_ledger = [item for item in ledger if item["core_pct"] == weight]
        summaries.append({
            "core_pct": weight, "rebalance": "NONE_MAIN_METHOD",
            "ending_equity": last["combined_equity"],
            "cagr": metrics["cagr"], "max_drawdown": metrics["max_drawdown"],
            "core_ending_equity": last["core_equity"],
            "tactical_ending_equity": last["tactical_equity"],
            "maximum_reconstruction_error": max(
                abs(item["reconstruction_error"])
                for item in daily if item["core_pct"] == weight
            ),
            "transaction_count": len(allocation_ledger),
            "total_commission": sum(
                (decimal(item.get("commission", 0)) for item in allocation_ledger),
                Decimal("0"),
            ),
            "total_tax": sum(
                (decimal(item.get("tax", 0)) for item in allocation_ledger),
                Decimal("0"),
            ),
            "annual_rebalance_sensitivity": "SEPARATE_OPTION_NOT_MAIN_RESULT",
        })
    return {"summary": summaries, "daily": daily, "ledger": ledger}
