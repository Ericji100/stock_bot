"""Data-agnostic research surfaces for the frozen 00635U preparation.

All functions require caller-supplied, already validated rows.  They never
download data and therefore cannot bypass the manifest gate in prep v1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import product
from typing import Callable, Mapping, Sequence

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

    @property
    def final_equity(self) -> Decimal:
        return self.curve[-1][1]


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
    shares = _affordable_shares(cash, rows[0].raw_open, model)
    if shares < 1:
        raise ValueError("initial capital cannot buy one share")
    cash -= model.buy(rows[0].raw_open, shares).cash_amount
    curve: list[tuple[date, Decimal]] = []
    dividend_rights: dict[str, Decimal] = {}
    for row in rows:
        if row.share_factor != 1:
            changed = Decimal(shares) * row.share_factor
            if changed != changed.to_integral_value():
                raise ValueError("share factor must preserve whole shares")
            shares = int(changed)
        dividend_cash = Decimal("0")
        if row.dividend_stage == "ENTITLEMENT":
            if row.dividend_event_id in dividend_rights:
                raise ValueError("duplicate dividend entitlement id")
            dividend_rights[row.dividend_event_id] = row.cash_dividend_per_share * shares
        elif row.dividend_stage == "PAYMENT":
            dividend_cash = dividend_rights.pop(row.dividend_event_id, Decimal("0"))
        elif row.cash_dividend_per_share:
            dividend_cash = row.cash_dividend_per_share * shares
        cash += dividend_cash
        if reinvest and dividend_cash:
            added = _affordable_shares(cash, row.raw_close, model)
            if added:
                cash -= model.buy(row.raw_close, added).cash_amount
                shares += added
        liquidation = model.sell(row.raw_close, shares, InstrumentType.ETF).cash_amount
        curve.append((row.day, cash + liquidation))
    return ComparisonLine(
        "00635U_BUY_HOLD_REINVEST" if reinvest else "00635U_BUY_HOLD_CASH",
        "TRADABLE_PASSIVE_COMPARATOR",
        tuple(curve),
    )


def run_index_line(
    index_values: Mapping[date, Decimal | int | float | str],
    sessions: Sequence[date],
    *,
    line_id: str = "GOLD_ER_OFFICIAL_TRACKED_INDEX",
) -> ComparisonLine:
    missing = [day for day in sessions if day not in index_values]
    if missing:
        raise ValueError(f"official index missing aligned sessions: {missing[:3]}")
    common = [(day, decimal(index_values[day])) for day in sessions if day in index_values]
    if not common or common[0][1] <= 0 or any(value <= 0 for _, value in common):
        raise ValueError("official index has no positive common-date values")
    base = common[0][1]
    return ComparisonLine(
        line_id,
        "NON_TRADABLE_MARKET_BENCHMARK",
        tuple((day, INITIAL_CAPITAL * value / base) for day, value in common),
    )


def run_seven_lines(
    rows: Sequence[IndicatorRow],
    gold_er_values: Mapping[date, Decimal | int | float | str],
    *,
    slippage_bps: int = 20,
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
                )
            )
    benchmark_config = ETFResearchConfig(slippage_bps=slippage_bps)
    output.append(run_buy_hold(rows, reinvest=False, config=benchmark_config))
    output.append(run_buy_hold(rows, reinvest=True, config=benchmark_config))
    output.append(run_index_line(gold_er_values, [row.day for row in rows]))
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
                output.append(
                    {
                        "slippage_bps": bps,
                        "strategy": strategy.value,
                        "timing": timing.value,
                        "ending_equity": result.final_equity,
                    }
                )
    return output


def slice_curve(
    curve: Sequence[tuple[date, Decimal]],
    start: date,
    end: date,
) -> dict[str, object]:
    selected = [(day, value) for day, value in curve if start <= day <= end]
    if len(selected) < 2:
        return {"start": start, "end": end, "status": "INSUFFICIENT_DATA"}
    return {
        "start": selected[0][0],
        "end": selected[-1][0],
        "status": "OK",
        "starting_equity": selected[0][1],
        "ending_equity": selected[-1][1],
        "return": selected[-1][1] / selected[0][1] - Decimal("1"),
    }


def annual_slices(curve: Sequence[tuple[date, Decimal]]) -> list[dict[str, object]]:
    years = sorted({day.year for day, _ in curve})
    return [slice_curve(curve, date(year, 1, 1), date(year, 12, 31)) for year in years]


def rolling_windows(
    curve: Sequence[tuple[date, Decimal]], years: int
) -> list[dict[str, object]]:
    if years not in (3, 5, 10):
        raise ValueError("rolling window must be 3, 5, or 10 years")
    if not curve:
        return [{"years": years, "status": "INSUFFICIENT_DATA"}]
    output: list[dict[str, object]] = []
    for index, (start, starting) in enumerate(curve):
        try:
            target = start.replace(year=start.year + years)
        except ValueError:
            target = start.replace(year=start.year + years, day=28)
        candidates = [(day, value) for day, value in curve[index + 1 :] if day >= target]
        if not candidates:
            continue
        end, ending = candidates[0]
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
            }
        )
    return output or [{"years": years, "status": "INSUFFICIENT_DATA"}]


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
