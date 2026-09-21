"""Shared price-adjustment and technical-indicator calculations."""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd


INDICATOR_VERSION = "2026-09-16-adjusted-kd-alpha-v1"
PRICE_BASIS_ADJUSTED = "fully_adjusted_point_in_time"
PRICE_BASIS_SOURCE_FALLBACK = "source_price_fallback"

MACD_FAST = 21
MACD_SLOW = 55
MACD_SIGNAL = 55
KD_RSV_PERIOD = 9
KD_K_PERIOD = 9
KD_D_PERIOD = 55

PRICE_COLUMNS = ("open", "high", "low", "close")


def _numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(float("nan"), index=frame.index, dtype="float64")
    value = frame[column]
    if isinstance(value, pd.DataFrame):
        value = value.iloc[:, 0]
    return pd.to_numeric(value, errors="coerce")


def apply_point_in_time_adjustment(
    frame: pd.DataFrame,
    *,
    enabled: bool = True,
) -> pd.DataFrame:
    """Return OHLC adjusted only with corporate actions known by the frame end.

    Yahoo ``Close`` can already contain split/stock-dividend adjustments while
    ``Adj Close`` also includes cash distributions. Rebasing their ratio to the
    last row keeps the last bar unchanged and removes adjustment changes that
    occur after a historical replay's target date.
    """

    result = frame.copy()
    if result.empty or not set(PRICE_COLUMNS).issubset(result.columns):
        return result

    for column in PRICE_COLUMNS:
        source_column = f"source_{column}"
        if source_column not in result.columns:
            result[source_column] = _numeric_series(result, column)
        result[column] = _numeric_series(result, source_column)

    result["adjustment_factor"] = 1.0
    result["price_basis"] = PRICE_BASIS_SOURCE_FALLBACK
    result["indicator_version"] = INDICATOR_VERSION
    if not enabled or "adj_close" not in result.columns:
        return result

    source_close = _numeric_series(result, "source_close")
    adj_close = _numeric_series(result, "adj_close")
    ratio = (adj_close / source_close.where(source_close > 0)).where(adj_close > 0)
    if ratio.dropna().empty:
        return result

    ratio = ratio.ffill().bfill()
    reference_ratio = ratio.dropna().iloc[-1]
    if pd.isna(reference_ratio) or float(reference_ratio) <= 0:
        return result

    factor = (ratio / float(reference_ratio)).where(lambda values: values > 0).fillna(1.0)
    for column in PRICE_COLUMNS:
        result[column] = _numeric_series(result, f"source_{column}") * factor
    result["adjustment_factor"] = factor
    result["price_basis"] = PRICE_BASIS_ADJUSTED
    return result


def _atr(frame: pd.DataFrame, period: int) -> pd.Series:
    high_low = frame["high"] - frame["low"]
    high_close = (frame["high"] - frame["close"].shift(1)).abs()
    low_close = (frame["low"] - frame["close"].shift(1)).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(period).mean()


def apply_technical_indicators(
    frame: pd.DataFrame,
    *,
    adjust_prices: bool = True,
    ma_periods: Iterable[int] = (5, 13, 21, 55, 60, 105, 144),
    atr_period: int | None = 14,
    volume_ma_period: int | None = 20,
    rsv_min_periods: int | None = None,
    fill_flat_rsv: bool = False,
) -> pd.DataFrame:
    """Apply the project's canonical MA, MACD (21/55/55), and KD (9/9/55)."""

    result = apply_point_in_time_adjustment(frame, enabled=adjust_prices)
    if result.empty or not set(PRICE_COLUMNS).issubset(result.columns):
        return result

    for period in ma_periods:
        period = int(period)
        result[f"MA{period}"] = result["close"].rolling(period).mean()

    ema_fast = result["close"].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = result["close"].ewm(span=MACD_SLOW, adjust=False).mean()
    result["DIF"] = ema_fast - ema_slow
    result["DEA"] = result["DIF"].ewm(span=MACD_SIGNAL, adjust=False).mean()
    result["MACD_HIST"] = result["DIF"] - result["DEA"]
    result["Histogram"] = result["MACD_HIST"]

    min_periods = KD_RSV_PERIOD if rsv_min_periods is None else max(1, int(rsv_min_periods))
    low_min = result["low"].rolling(KD_RSV_PERIOD, min_periods=min_periods).min()
    high_max = result["high"].rolling(KD_RSV_PERIOD, min_periods=min_periods).max()
    price_range = high_max - low_min
    price_range = price_range.mask(price_range == 0)
    rsv = ((result["close"] - low_min) / price_range * 100.0).clip(0, 100)
    result["RSV"] = rsv.fillna(50.0) if fill_flat_rsv else rsv
    result["K"] = result["RSV"].ewm(alpha=1.0 / KD_K_PERIOD, adjust=False).mean()
    result["D"] = result["K"].ewm(alpha=1.0 / KD_D_PERIOD, adjust=False).mean()

    if atr_period is not None:
        result[f"ATR{int(atr_period)}"] = _atr(result, int(atr_period))
    if volume_ma_period is not None and "volume" in result.columns:
        result[f"volume_ma{int(volume_ma_period)}"] = result["volume"].rolling(int(volume_ma_period)).mean()
    return result
