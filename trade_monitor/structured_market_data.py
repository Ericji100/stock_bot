from __future__ import annotations

import json
import math
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .pivot_replay import Candle, detect_local_pivots, dow_state, pair_pivots, secondary_pivots


TAIPEI = ZoneInfo("Asia/Taipei")
SUPPORTED_INSTRUMENTS = {"TX", "MTX", "TMF"}


class StructuredMarketDataError(ValueError):
    pass


def load_structured_market_snapshot(
    path: Path,
    *,
    expected_latest_closed_k_iso: str,
    max_age_seconds: int = 90,
) -> dict[str, Any]:
    """Read a closed-bar JSON snapshot without ever inventing missing prices.

    The provider is optional.  A non-fresh result is returned to the model as
    provenance only and must never become an exact price authority.
    """
    expected = _parse_aware_time(expected_latest_closed_k_iso, "expected_latest_closed_k_iso").astimezone(TAIPEI)
    if not isinstance(max_age_seconds, int) or isinstance(max_age_seconds, bool) or max_age_seconds <= 0:
        raise StructuredMarketDataError("max_age_seconds must be a positive integer")
    if not path.is_file():
        return _unavailable("UNAVAILABLE", "結構化行情來源檔案不存在。", expected)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _unavailable("INVALID", "結構化行情來源無法讀取或不是有效 JSON。", expected)
    try:
        source, instrument, candles, volumes = _validate_payload(payload)
    except StructuredMarketDataError as exc:
        return _unavailable("INVALID", str(exc), expected)
    latest = candles[-1].at.astimezone(TAIPEI)
    age_seconds = int((expected - latest).total_seconds())
    provenance = {
        "source": source,
        "instrument": instrument,
        "timezone": "Asia/Taipei",
        "expected_latest_closed_k_iso": expected.isoformat(),
        "source_latest_closed_k_iso": latest.isoformat(),
        "age_seconds": age_seconds,
        "bar_count": len(candles),
    }
    if latest > expected:
        return {"ok": False, "status": "FUTURE", "reason": "來源含預期收盤時間之後的 K 棒。", "provenance": provenance}
    if age_seconds > max_age_seconds or latest != expected:
        return {"ok": False, "status": "STALE", "reason": "來源尚未更新到本輪最新已收盤 K。", "provenance": provenance}

    exact = [candle for candle in candles if candle.at.astimezone(TAIPEI) <= expected]
    latest_candle = exact[-1]
    session = _current_session_bars(exact, expected)
    local_pivots, _ = detect_local_pivots(session, 2) if len(session) >= 5 else ([], {})
    paired, _, _ = pair_pivots(local_pivots)
    finalized = paired[:-1]
    secondaries = secondary_pivots(finalized)
    return {
        "ok": True,
        "status": "FRESH",
        "reason": "精確已收盤一分 K 已通過時間與格式驗證。",
        "provenance": provenance,
        "latest_closed_k": {
            "time": latest_candle.at.astimezone(TAIPEI).isoformat(),
            "open": latest_candle.open,
            "high": latest_candle.high,
            "low": latest_candle.low,
            "close": latest_candle.close,
            "volume": volumes[-1],
        },
        "indicators": {
            "sma21": _sma(exact, 21),
            "sma105": _sma(exact, 105),
            "atr14": _wilder_atr(exact, 14),
        },
        "opening_ranges": {
            "or5": _opening_range(session, 5),
            "or15": _opening_range(session, 15),
        },
        "causal_structure_n2": {
            "dow_small": dow_state(finalized),
            "dow_large": dow_state(secondaries),
            "recent_confirmed_pivots": [_pivot_view(item) for item in finalized[-6:]],
            "recent_large_pivots": [_pivot_view(item) for item in secondaries[-4:]],
        },
    }

def _validate_payload(payload: Any) -> tuple[str, str, list[Candle], list[float | None]]:
    if not isinstance(payload, Mapping) or payload.get("version") != 1:
        raise StructuredMarketDataError("結構化行情版本無效。")
    source = payload.get("source")
    instrument = payload.get("instrument")
    timezone_name = payload.get("timezone")
    bars = payload.get("bars")
    if not isinstance(source, str) or not source.strip() or len(source) > 120:
        raise StructuredMarketDataError("結構化行情來源名稱無效。")
    if instrument not in SUPPORTED_INSTRUMENTS:
        raise StructuredMarketDataError("商品必須明確為 TX、MTX 或 TMF。")
    if timezone_name != "Asia/Taipei":
        raise StructuredMarketDataError("結構化行情時區必須為 Asia/Taipei。")
    if not isinstance(bars, list) or not bars:
        raise StructuredMarketDataError("結構化行情沒有 K 棒。")
    candles: list[Candle] = []
    volumes: list[float | None] = []
    previous: datetime | None = None
    for index, item in enumerate(bars):
        if not isinstance(item, Mapping):
            raise StructuredMarketDataError(f"第 {index + 1} 根 K 棒格式無效。")
        at = _parse_aware_time(item.get("time"), f"bars[{index}].time").astimezone(TAIPEI)
        if at.second != 0 or at.microsecond != 0:
            raise StructuredMarketDataError("一分 K 時間必須對齊整分鐘。")
        if previous is not None and at <= previous:
            raise StructuredMarketDataError("K 棒時間必須嚴格遞增且不可重複。")
        previous = at
        open_, high, low, close = (_finite_number(item.get(key), f"bars[{index}].{key}") for key in ("open", "high", "low", "close"))
        if high < max(open_, close) or low > min(open_, close) or high < low:
            raise StructuredMarketDataError(f"第 {index + 1} 根 K 棒的 OHLC 關係無效。")
        volume_value = item.get("volume")
        volume = None if volume_value is None else _finite_number(volume_value, f"bars[{index}].volume")
        if volume is not None and volume < 0:
            raise StructuredMarketDataError("成交量不得為負數。")
        candles.append(Candle(at=at, open=open_, high=high, low=low, close=close))
        volumes.append(volume)
    return source.strip(), str(instrument), candles, volumes


def _parse_aware_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise StructuredMarketDataError(f"{field} 必須是含時區的時間。")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StructuredMarketDataError(f"{field} 不是有效時間。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StructuredMarketDataError(f"{field} 必須包含時區。")
    return parsed


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StructuredMarketDataError(f"{field} 必須是數字。")
    number = float(value)
    if not math.isfinite(number):
        raise StructuredMarketDataError(f"{field} 必須是有限數字。")
    return number


def _sma(candles: list[Candle], period: int) -> float | None:
    if len(candles) < period:
        return None
    return round(sum(item.close for item in candles[-period:]) / period, 4)


def _wilder_atr(candles: list[Candle], period: int) -> float | None:
    if len(candles) < period + 1:
        return None
    true_ranges: list[float] = []
    for previous, current in zip(candles, candles[1:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    if len(true_ranges) < period:
        return None
    atr = sum(true_ranges[:period]) / period
    for value in true_ranges[period:]:
        atr = ((period - 1) * atr + value) / period
    return round(atr, 4)


def _session_start(at: datetime) -> datetime | None:
    local = at.astimezone(TAIPEI)
    clock = local.time().replace(tzinfo=None)
    if time(8, 45) <= clock <= time(13, 45):
        return local.replace(hour=8, minute=45, second=0, microsecond=0)
    if clock >= time(15, 0):
        return local.replace(hour=15, minute=0, second=0, microsecond=0)
    if clock < time(5, 0):
        prior = local - timedelta(days=1)
        return prior.replace(hour=15, minute=0, second=0, microsecond=0)
    return None


def _current_session_bars(candles: list[Candle], expected: datetime) -> list[Candle]:
    start = _session_start(expected)
    if start is None:
        return []
    return [item for item in candles if start <= item.at.astimezone(TAIPEI) <= expected]


def _opening_range(candles: list[Candle], minutes: int) -> dict[str, Any] | None:
    if len(candles) < minutes:
        return None
    opening = candles[:minutes]
    expected_times = [opening[0].at + timedelta(minutes=index) for index in range(minutes)]
    if [item.at for item in opening] != expected_times:
        return None
    return {
        "start": opening[0].at.astimezone(TAIPEI).isoformat(),
        "end": opening[-1].at.astimezone(TAIPEI).isoformat(),
        "high": max(item.high for item in opening),
        "low": min(item.low for item in opening),
    }


def _pivot_view(pivot: Any) -> dict[str, Any]:
    return {
        "kind": pivot.kind,
        "bar_time": pivot.bar_time,
        "confirmation_time": pivot.confirmation_time,
        "price": pivot.price,
    }


def _unavailable(status: str, reason: str, expected: datetime) -> dict[str, Any]:
    return {
        "ok": False,
        "status": status,
        "reason": reason,
        "provenance": {
            "expected_latest_closed_k_iso": expected.isoformat(),
            "source_latest_closed_k_iso": None,
            "age_seconds": None,
        },
    }
