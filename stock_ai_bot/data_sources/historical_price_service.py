"""Isolated point-in-time daily-price cache for historical scan replays.

The live scanner cache intentionally keeps its existing short rolling window.
Historical replays use this module so fetching an old range can never replace
the files used by the running bot.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable
import json

import pandas as pd
import yfinance as yf

from stock_ai_bot.scanning.technical_indicator_service import INDICATOR_VERSION


ROOT_DIR = Path(__file__).resolve().parent
HISTORICAL_PRICE_CACHE_DIR = ROOT_DIR / ".cache" / "historical_scan" / "daily_prices"
REQUIRED_COLUMNS = ("date", "open", "high", "low", "close", "volume")


def historical_price_cache_path(symbol: str) -> Path:
    safe_symbol = str(symbol).replace(".", "_")
    return HISTORICAL_PRICE_CACHE_DIR / f"{safe_symbol}.csv"


def _empty_marker_path(symbol: str) -> Path:
    return historical_price_cache_path(symbol).with_suffix(".empty.json")


def _coverage_path(symbol: str) -> Path:
    return historical_price_cache_path(symbol).with_suffix(".coverage.json")


def _coverage_contains(symbol: str, start_date: date | None, end_date: date) -> bool:
    path = _coverage_path(symbol)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        covered_start = str(payload.get("requested_start") or "9999-12-31")
        covered_end = str(payload.get("requested_end") or "")
        return covered_end >= end_date.isoformat() and (
            start_date is None or covered_start <= start_date.isoformat()
        )
    except Exception:
        return False


def _mark_coverage(
    symbol: str,
    start_date: date,
    end_date: date,
    *,
    has_adjusted: bool = False,
) -> None:
    path = _coverage_path(symbol)
    previous: dict[str, Any] = {}
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
    previous_start = str(previous.get("requested_start") or start_date.isoformat())
    previous_end = str(previous.get("requested_end") or end_date.isoformat())
    payload = {
        "symbol": symbol,
        "requested_start": min(previous_start, start_date.isoformat()),
        "requested_end": max(previous_end, end_date.isoformat()),
        "price_basis_source": "source_ohlc_with_adj_close" if has_adjusted else "source_ohlc_only",
        "indicator_version": INDICATOR_VERSION,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _known_empty_through(symbol: str, end_date: date) -> bool:
    path = _empty_marker_path(symbol)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return str(payload.get("empty_through") or "") >= end_date.isoformat()
    except Exception:
        return False


def _mark_empty(symbol: str, end_date: date) -> None:
    path = _empty_marker_path(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"symbol": symbol, "empty_through": end_date.isoformat()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _column_as_series(frame: pd.DataFrame, column: str) -> pd.Series:
    value = frame[column]
    if isinstance(value, pd.DataFrame):
        if value.empty:
            return pd.Series(pd.NA, index=frame.index)
        value = value.iloc[:, 0]
    if isinstance(value, pd.Series):
        return value
    return pd.Series(value, index=frame.index)


def standardize_history(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=[*REQUIRED_COLUMNS, "adj_close"])
    source = frame.copy()
    if isinstance(source.columns, pd.MultiIndex):
        source.columns = source.columns.get_level_values(0)
    rename_map = {
        "Date": "date",
        "Datetime": "date",
        "datetime": "date",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Adj Close": "adj_close",
        "Adj_Close": "adj_close",
        "Volume": "volume",
    }
    if "date" not in source.columns:
        source = source.reset_index()
    source = source.rename(columns=rename_map)
    if not set(REQUIRED_COLUMNS).issubset(source.columns):
        return pd.DataFrame(columns=[*REQUIRED_COLUMNS, "adj_close"])
    result = pd.DataFrame(index=source.index)
    for column in REQUIRED_COLUMNS:
        result[column] = _column_as_series(source, column)
    if "adj_close" in source.columns:
        result["adj_close"] = _column_as_series(source, "adj_close")
    result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.normalize()
    for column in ("open", "high", "low", "close", "volume", "adj_close"):
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    return (
        result.dropna(subset=["date", "open", "high", "low", "close"])
        .sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )


@lru_cache(maxsize=4096)
def _read_cached_history(symbol: str) -> pd.DataFrame:
    path = historical_price_cache_path(symbol)
    if not path.exists():
        return pd.DataFrame(columns=[*REQUIRED_COLUMNS, "adj_close"])
    try:
        return standardize_history(pd.read_csv(path))
    except Exception:
        return pd.DataFrame(columns=[*REQUIRED_COLUMNS, "adj_close"])


def load_cached_history(symbol: str, end_date: date | None = None) -> pd.DataFrame:
    frame = _read_cached_history(str(symbol)).copy()
    if end_date is not None and not frame.empty:
        frame = frame[frame["date"].dt.date <= end_date].copy()
    return frame.reset_index(drop=True)


def save_history(symbol: str, frame: pd.DataFrame) -> Path | None:
    normalized = standardize_history(frame)
    if normalized.empty:
        return None
    path = historical_price_cache_path(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            previous = standardize_history(pd.read_csv(path))
        except Exception:
            previous = pd.DataFrame()
        if not previous.empty:
            normalized = standardize_history(pd.concat([previous, normalized], ignore_index=True))
    normalized.to_csv(path, index=False, encoding="utf-8")
    marker = _empty_marker_path(symbol)
    if marker.exists():
        marker.unlink()
    _read_cached_history.cache_clear()
    return path


def fetch_history(
    symbol: str,
    end_date: date,
    *,
    min_rows: int | None = None,
    require_adjusted: bool = False,
    lookback_days: int = 560,
) -> tuple[pd.DataFrame, str]:
    start_date = end_date - timedelta(days=max(lookback_days, 420))
    complete_cached = load_cached_history(symbol)
    if (
        not complete_cached.empty
        and complete_cached["date"].dt.date.min() > end_date
    ):
        return pd.DataFrame(columns=[*REQUIRED_COLUMNS, "adj_close"]), "歷史日線不可用（尚未上市）"
    if _known_empty_through(symbol, end_date):
        return pd.DataFrame(columns=[*REQUIRED_COLUMNS, "adj_close"]), "歷史日線不可用（已確認）"
    cached = load_cached_history(symbol, end_date)
    adjusted_ready = (
        not require_adjusted
        or (
            "adj_close" in cached.columns
            and cached["adj_close"].notna().mean() >= 0.9
            and (cached["adj_close"].dropna() > 0).all()
        )
    )
    if (
        not cached.empty
        and (cached["date"].dt.date.max() >= end_date or _coverage_contains(symbol, start_date, end_date))
        and (min_rows is None or len(cached) >= min_rows)
        and adjusted_ready
    ):
        return cached, "歷史隔離快取"

    try:
        raw = yf.download(
            symbol,
            start=start_date,
            end=end_date + timedelta(days=1),
            interval="1d",
            progress=False,
            auto_adjust=False,
            threads=False,
        )
    except Exception:
        raw = pd.DataFrame()
    normalized = standardize_history(raw)
    if not normalized.empty:
        save_history(symbol, normalized)
        _mark_coverage(
            symbol,
            start_date,
            end_date,
            has_adjusted="adj_close" in normalized.columns and normalized["adj_close"].notna().mean() >= 0.9,
        )
        normalized = load_cached_history(symbol, end_date)
    else:
        _mark_empty(symbol, end_date)
    return normalized, "Yahoo Finance（歷史隔離快取）" if not normalized.empty else "歷史日線不可用"


def _frame_for_symbol(dataset: pd.DataFrame, symbol: str, chunk_size: int) -> pd.DataFrame:
    if dataset is None or dataset.empty:
        return pd.DataFrame()
    if isinstance(dataset.columns, pd.MultiIndex):
        level_zero = {str(value) for value in dataset.columns.get_level_values(0)}
        level_one = {str(value) for value in dataset.columns.get_level_values(1)}
        try:
            if symbol in level_zero:
                return dataset[symbol]
            if symbol in level_one:
                return dataset.xs(symbol, axis=1, level=1)
        except (KeyError, ValueError):
            return pd.DataFrame()
        return pd.DataFrame()
    return dataset if chunk_size == 1 else pd.DataFrame()


def prefetch_histories(
    symbols: Iterable[str],
    *,
    start_date: date,
    end_date: date,
    chunk_size: int = 40,
    retry_workers: int = 4,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Fetch a closed historical range into the isolated cache.

    Cached symbols whose coverage already reaches ``end_date`` and starts no
    later than ``start_date`` are not downloaded again.
    """

    emit = progress or (lambda _message: None)
    requested = sorted({str(symbol).strip() for symbol in symbols if str(symbol).strip()})
    missing: list[str] = []
    for symbol in requested:
        if _known_empty_through(symbol, end_date):
            continue
        if _coverage_contains(symbol, start_date, end_date):
            continue
        frame = load_cached_history(symbol)
        if (
            frame.empty
            or frame["date"].dt.date.min() > start_date
            or frame["date"].dt.date.max() < end_date
        ):
            missing.append(symbol)
    negative_cached = sum(_known_empty_through(symbol, end_date) for symbol in requested)
    stats: dict[str, Any] = {
        "requested": len(requested),
        "already_cached": len(requested) - len(missing) - negative_cached,
        "known_empty": negative_cached,
        "downloaded": 0,
        "empty": 0,
        "errors": [],
    }
    if not missing:
        emit(f"歷史日線已完整快取：{len(requested)} 檔")
        return stats

    HISTORICAL_PRICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    chunks = [missing[index : index + max(1, chunk_size)] for index in range(0, len(missing), max(1, chunk_size))]
    failed: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        emit(f"歷史日線批次下載 {index}/{len(chunks)}（{len(chunk)} 檔）")
        try:
            dataset = yf.download(
                tickers=chunk,
                start=start_date,
                end=end_date + timedelta(days=1),
                interval="1d",
                progress=False,
                auto_adjust=False,
                group_by="ticker",
                threads=True,
            )
        except Exception as exc:
            dataset = pd.DataFrame()
            stats["errors"].append({"chunk": index, "error": f"{type(exc).__name__}: {exc}"})
        for symbol in chunk:
            frame = standardize_history(_frame_for_symbol(dataset, symbol, len(chunk)))
            if frame.empty:
                failed.append(symbol)
                continue
            save_history(symbol, frame)
            _mark_coverage(
                symbol,
                start_date,
                end_date,
                has_adjusted="adj_close" in frame.columns and frame["adj_close"].notna().mean() >= 0.9,
            )
            stats["downloaded"] += 1

    if failed:
        emit(f"歷史日線批次缺口 {len(failed)} 檔，改用單檔重試")

        def retry(symbol: str) -> tuple[str, bool, str | None]:
            try:
                frame, _ = fetch_history(
                    symbol,
                    end_date,
                    min_rows=None,
                    require_adjusted=False,
                    lookback_days=max(560, (end_date - start_date).days + 30),
                )
                return symbol, not frame.empty, None
            except Exception as exc:  # pragma: no cover - network dependent
                return symbol, False, f"{type(exc).__name__}: {exc}"

        with ThreadPoolExecutor(max_workers=max(1, retry_workers)) as executor:
            futures = {executor.submit(retry, symbol): symbol for symbol in failed}
            for number, future in enumerate(as_completed(futures), start=1):
                symbol, ok, error = future.result()
                if ok:
                    stats["downloaded"] += 1
                else:
                    stats["empty"] += 1
                    _mark_empty(symbol, end_date)
                    if error:
                        stats["errors"].append({"symbol": symbol, "error": error})
                if number % 50 == 0 or number == len(futures):
                    emit(f"歷史日線單檔重試 {number}/{len(futures)}")
    return stats
