"""Official-data backfills for the isolated dual-MA historical replay.

This module is deliberately separate from the running selector.  It writes
only to ``.cache/historical_scan`` and to immutable audit manifests under
``data/dual_ma``; the live price, chip, TDCC, scheduler and command paths are
never modified.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterable, Mapping
import hashlib
import json
import time

import httpx
import pandas as pd
import yfinance as yf

from stock_ai_bot.data_sources import historical_price_service
from stock_ai_bot.data_sources.data_source_manager import FinMindQuotaManager, SourceHealthManager
from stock_ai_bot.data_sources.finmind_client import FinMindClient
from stock_ai_bot.selection.historical_universe_service import (
    HistoricalUniverse,
    MarketMembershipInterval,
    parse_exchange_date,
)


ROOT_DIR = Path(__file__).resolve().parents[2]
HISTORICAL_DAILY_CHIP_CACHE_DIR = ROOT_DIR / ".cache" / "historical_scan" / "chip_daily"
DEFAULT_MANIFEST_ROOT = ROOT_DIR / "data" / "dual_ma" / "data_backfills"

TWSE_MONTHLY_PRICE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
TPEX_MONTHLY_PRICE_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
TWSE_DAILY_INSTITUTIONAL_URL = "https://www.twse.com.tw/fund/T86"
TPEX_DAILY_INSTITUTIONAL_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
FINMIND_PRICE_URL = "https://api.finmindtrade.com/api/v4/data"

SCHEMA_VERSION = "dual-ma-historical-data-backfill-v1"
ProgressCallback = Callable[[int, int, str], None]
_SOURCE_RATE_LOCK = Lock()
_LAST_SOURCE_REQUEST_AT: dict[str, float] = {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT_DIR.resolve()))
    except ValueError:
        return str(path.resolve())


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "--", "---", "-", "nan", "None"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _normalise_code(value: Any) -> str:
    text = str(value or "").strip()
    return text.split(".", 1)[0]


def _wait_for_source_slot(source: str, minimum_interval_seconds: float) -> None:
    if minimum_interval_seconds <= 0:
        return
    with _SOURCE_RATE_LOCK:
        now = time.monotonic()
        wait_seconds = minimum_interval_seconds - (
            now - _LAST_SOURCE_REQUEST_AT.get(source, 0.0)
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _LAST_SOURCE_REQUEST_AT[source] = time.monotonic()


def _months_between(start_date: date, end_date: date) -> list[date]:
    if start_date > end_date:
        return []
    current = start_date.replace(day=1)
    result: list[date] = []
    while current <= end_date:
        result.append(current)
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return result


def parse_twse_monthly_price_payload(payload: Mapping[str, Any]) -> pd.DataFrame:
    """Normalise one official TWSE monthly response into shared cache columns."""

    if str(payload.get("stat") or "") != "OK":
        return pd.DataFrame(columns=historical_price_service.REQUIRED_COLUMNS)
    rows: list[dict[str, Any]] = []
    for raw in payload.get("data") or []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 7:
            continue
        trade_date = parse_exchange_date(raw[0])
        values = [_to_float(raw[index]) for index in (3, 4, 5, 6)]
        if trade_date is None or any(value is None for value in values):
            continue
        rows.append(
            {
                "date": trade_date,
                "open": values[0],
                "high": values[1],
                "low": values[2],
                "close": values[3],
                "volume": _to_float(raw[1]) or 0.0,
            }
        )
    return historical_price_service.standardize_history(pd.DataFrame(rows))


def parse_tpex_monthly_price_payload(payload: Mapping[str, Any]) -> pd.DataFrame:
    """Normalise one official TPEx monthly response.

    The TPEx ``tradingStock`` volume field is expressed in thousands of
    shares, so it is multiplied by 1,000 to match the TWSE/shared cache unit.
    """

    tables = payload.get("tables") or []
    if not isinstance(tables, list) or not tables:
        return pd.DataFrame(columns=historical_price_service.REQUIRED_COLUMNS)
    rows: list[dict[str, Any]] = []
    for raw in tables[0].get("data") or []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 7:
            continue
        trade_date = parse_exchange_date(raw[0])
        values = [_to_float(raw[index]) for index in (3, 4, 5, 6)]
        if trade_date is None or any(value is None for value in values):
            continue
        rows.append(
            {
                "date": trade_date,
                "open": values[0],
                "high": values[1],
                "low": values[2],
                "close": values[3],
                "volume": (_to_float(raw[1]) or 0.0) * 1000.0,
            }
        )
    return historical_price_service.standardize_history(pd.DataFrame(rows))


def parse_finmind_price_payload(payload: Mapping[str, Any]) -> pd.DataFrame:
    """Normalise FinMind ``TaiwanStockPrice`` rows into replay cache columns."""

    if payload.get("status") not in (None, 200, "200"):
        return pd.DataFrame(columns=historical_price_service.REQUIRED_COLUMNS)
    rows: list[dict[str, Any]] = []
    for raw in payload.get("data") or []:
        if not isinstance(raw, Mapping):
            continue
        trade_date = parse_exchange_date(raw.get("date"))
        values = [
            _to_float(raw.get("open")),
            _to_float(raw.get("max")),
            _to_float(raw.get("min")),
            _to_float(raw.get("close")),
        ]
        if trade_date is None or any(value is None for value in values):
            continue
        rows.append(
            {
                "date": trade_date,
                "open": values[0],
                "high": values[1],
                "low": values[2],
                "close": values[3],
                "volume": _to_float(raw.get("Trading_Volume")) or 0.0,
            }
        )
    return historical_price_service.standardize_history(pd.DataFrame(rows))


def parse_twse_daily_institutional_payload(
    payload: Mapping[str, Any],
    target_date: date,
    candidate_codes: set[str],
) -> pd.DataFrame:
    """Parse TWSE T86; net-buy fields are shares and are stored as lots."""

    if str(payload.get("stat") or "") != "OK":
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for raw in payload.get("data") or []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 11:
            continue
        code = _normalise_code(raw[0])
        if not code or (candidate_codes and code not in candidate_codes):
            continue
        rows.append(
            {
                "date": target_date,
                "code": code,
                "market": "TWSE",
                "foreign_net_lots": (_to_float(raw[4]) or 0.0) / 1000.0,
                "trust_net_lots": (_to_float(raw[10]) or 0.0) / 1000.0,
                "foreign_ratio_pct": pd.NA,
                "source": "TWSE_T86_OFFICIAL",
            }
        )
    return pd.DataFrame(rows)


def parse_tpex_daily_institutional_payload(
    payload: Mapping[str, Any],
    target_date: date,
    candidate_codes: set[str],
) -> pd.DataFrame:
    """Parse TPEx's combined all-market daily institutional response.

    Columns 4 and 13 are respectively foreign-investor (excluding foreign
    dealers) and investment-trust net shares.  This is the same definition
    used by the existing four-request fallback in ``chip_strategies``.
    """

    tables = payload.get("tables") or []
    if not isinstance(tables, list) or not tables:
        return pd.DataFrame()
    table = tables[0]
    response_date = parse_exchange_date(table.get("date"))
    if response_date != target_date:
        raise ValueError(
            f"TPEx response date mismatch: requested {target_date}, got {response_date}"
        )
    rows: list[dict[str, Any]] = []
    for raw in table.get("data") or []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 14:
            continue
        code = _normalise_code(raw[0])
        if not code or (candidate_codes and code not in candidate_codes):
            continue
        rows.append(
            {
                "date": target_date,
                "code": code,
                "market": "TPEX",
                "foreign_net_lots": (_to_float(raw[4]) or 0.0) / 1000.0,
                "trust_net_lots": (_to_float(raw[13]) or 0.0) / 1000.0,
                "foreign_ratio_pct": pd.NA,
                "source": "TPEX_DAILY_TRADE_OFFICIAL",
            }
        )
    return pd.DataFrame(rows)


def _request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    data: Mapping[str, str] | None = None,
    attempts: int = 3,
) -> Mapping[str, Any]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = client.request(method, url, params=params, data=data)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise ValueError("official endpoint did not return a JSON object")
            return payload
        except Exception as exc:  # pragma: no cover - exact failures are network dependent
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.6 * (2**attempt))
    assert last_error is not None
    raise last_error


def _interval_bounds(
    interval: MarketMembershipInterval,
    start_date: date,
    end_date: date,
) -> tuple[date, date] | None:
    effective_start = max(interval.listed_on, start_date)
    effective_end = min(
        end_date,
        interval.delisted_on.fromordinal(interval.delisted_on.toordinal() - 1)
        if interval.delisted_on
        else end_date,
    )
    if effective_start > effective_end:
        return None
    return effective_start, effective_end


def _price_targets(
    universe: HistoricalUniverse,
    start_date: date,
    end_date: date,
    symbols: Iterable[str] | None,
    only_missing: bool,
) -> list[tuple[MarketMembershipInterval, date, date]]:
    requested_symbols = {str(value).strip() for value in symbols or () if str(value).strip()}
    targets: list[tuple[MarketMembershipInterval, date, date]] = []
    for interval in universe.intervals:
        if requested_symbols and interval.symbol not in requested_symbols:
            continue
        bounds = _interval_bounds(interval, start_date, end_date)
        if bounds is None:
            continue
        if only_missing and historical_price_service.historical_price_cache_path(interval.symbol).exists():
            continue
        targets.append((interval, *bounds))
    return sorted(targets, key=lambda value: value[0].symbol)


def _fetch_price_target(
    target: tuple[MarketMembershipInterval, date, date],
    *,
    request_delay_seconds: float,
) -> dict[str, Any]:
    interval, effective_start, effective_end = target
    frames: list[pd.DataFrame] = []
    month_count = 0
    try:
        with httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/140 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://www.twse.com.tw/",
            },
        ) as client:
            for month in _months_between(effective_start, effective_end):
                if interval.market == "TWSE":
                    _wait_for_source_slot("twse_price", request_delay_seconds)
                    payload = _request_json(
                        client,
                        "GET",
                        TWSE_MONTHLY_PRICE_URL,
                        params={
                            "response": "json",
                            "date": month.strftime("%Y%m01"),
                            "stockNo": interval.code,
                        },
                    )
                    frame = parse_twse_monthly_price_payload(payload)
                elif interval.market == "TPEX":
                    _wait_for_source_slot("tpex_price", request_delay_seconds)
                    form = {
                        "response": "json",
                        "date": month.strftime("%Y/%m/01"),
                        "code": interval.code,
                    }
                    payload = _request_json(
                        client,
                        "POST",
                        TPEX_MONTHLY_PRICE_URL,
                        data=form,
                    )
                    frame = parse_tpex_monthly_price_payload(payload)
                else:
                    raise ValueError(f"unsupported market: {interval.market}")
                month_count += 1
                if not frame.empty:
                    frames.append(frame)
    except Exception as exc:
        return {
            "symbol": interval.symbol,
            "market": interval.market,
            "status": "FAILED",
            "requested_start": effective_start.isoformat(),
            "requested_end": effective_end.isoformat(),
            "months_completed": month_count,
            "error": f"{type(exc).__name__}: {exc}",
        }

    if not frames:
        return {
            "symbol": interval.symbol,
            "market": interval.market,
            "status": "EMPTY",
            "requested_start": effective_start.isoformat(),
            "requested_end": effective_end.isoformat(),
            "months_completed": month_count,
            "row_count": 0,
        }

    combined = historical_price_service.standardize_history(
        pd.concat(frames, ignore_index=True)
    )
    combined = combined[
        (combined["date"].dt.date >= effective_start)
        & (combined["date"].dt.date <= effective_end)
    ].reset_index(drop=True)
    if combined.empty:
        return {
            "symbol": interval.symbol,
            "market": interval.market,
            "status": "EMPTY",
            "requested_start": effective_start.isoformat(),
            "requested_end": effective_end.isoformat(),
            "months_completed": month_count,
            "row_count": 0,
        }
    cache_path = historical_price_service.save_history(interval.symbol, combined)
    if cache_path is None:
        raise RuntimeError(f"normalised official history unexpectedly empty: {interval.symbol}")
    historical_price_service._mark_coverage(  # isolated cache metadata used by replay only
        interval.symbol,
        effective_start,
        effective_end,
        has_adjusted=False,
    )
    return {
        "symbol": interval.symbol,
        "market": interval.market,
        "status": "SUCCESS",
        "requested_start": effective_start.isoformat(),
        "requested_end": effective_end.isoformat(),
        "months_completed": month_count,
        "row_count": len(combined),
        "first_trade_date": combined["date"].min().date().isoformat(),
        "last_trade_date": combined["date"].max().date().isoformat(),
        "cache_path": _manifest_path(cache_path),
        "cache_sha256": _file_sha256(cache_path),
        "source_url": TWSE_MONTHLY_PRICE_URL if interval.market == "TWSE" else TPEX_MONTHLY_PRICE_URL,
    }


@dataclass(frozen=True)
class BackfillRun:
    kind: str
    requested_start: date
    requested_end: date
    universe_source_id: str
    results: tuple[dict[str, Any], ...]

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": self.kind,
            "requested_start": self.requested_start.isoformat(),
            "requested_end": self.requested_end.isoformat(),
            "universe_source_id": self.universe_source_id,
            "official_sources": {
                "twse_monthly_price": TWSE_MONTHLY_PRICE_URL,
                "tpex_monthly_price": TPEX_MONTHLY_PRICE_URL,
                "twse_daily_institutional": TWSE_DAILY_INSTITUTIONAL_URL,
                "tpex_daily_institutional": TPEX_DAILY_INSTITUTIONAL_URL,
                "finmind_price_fallback": FINMIND_PRICE_URL,
            },
            "results": list(self.results),
        }

    @property
    def content_sha256(self) -> str:
        return _sha256_json(self.content_payload())

    @property
    def run_id(self) -> str:
        return f"dual-ma-{self.kind}-backfill#{self.content_sha256[:16]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_payload(),
            "run_id": self.run_id,
            "content_sha256": self.content_sha256,
            "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }


def freeze_backfill_run(
    run: BackfillRun,
    *,
    root: Path = DEFAULT_MANIFEST_ROOT,
) -> Path:
    target_dir = root / run.kind / f"{run.requested_start.year:04d}-{run.requested_end.year:04d}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{run.run_id}.json"
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing.get("content_sha256") != run.content_sha256:
            raise ValueError(f"content digest mismatch at existing path: {target}")
        return target
    target.write_text(
        json.dumps(run.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def backfill_official_price_history(
    universe: HistoricalUniverse,
    *,
    start_date: date,
    end_date: date,
    symbols: Iterable[str] | None = None,
    only_missing: bool = True,
    workers: int = 4,
    request_delay_seconds: float = 0.05,
    progress: ProgressCallback | None = None,
) -> BackfillRun:
    """Fill missing isolated price files from TWSE/TPEx official month data."""

    targets = _price_targets(universe, start_date, end_date, symbols, only_missing)
    results: list[dict[str, Any]] = []
    total = len(targets)
    if total:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(
                    _fetch_price_target,
                    target,
                    request_delay_seconds=request_delay_seconds,
                ): target[0].symbol
                for target in targets
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                result = future.result()
                results.append(result)
                if progress:
                    progress(completed, total, f"{result['symbol']} {result['status']}")
    return BackfillRun(
        kind="official-prices",
        requested_start=start_date,
        requested_end=end_date,
        universe_source_id=universe.source_id,
        results=tuple(sorted(results, key=lambda item: str(item.get("symbol") or ""))),
    )


def _fetch_finmind_price_target(
    target: tuple[MarketMembershipInterval, date, date],
) -> dict[str, Any]:
    interval, effective_start, effective_end = target
    try:
        client = FinMindClient(
            health_manager=SourceHealthManager(),
            quota_manager=FinMindQuotaManager(),
            timeout=30.0,
            allow_anonymous=True,
        )
        payload = client.request_dataset(
            dataset="TaiwanStockPrice",
            params={
                "stock_id": interval.code,
                "start_date": effective_start.isoformat(),
                "end_date": effective_end.isoformat(),
            },
            scope="backfill",
        )
        if not payload:
            raise RuntimeError("FinMind returned no payload (quota, cooldown, or unavailable source)")
        frame = parse_finmind_price_payload(payload)
        if frame.empty:
            return {
                "symbol": interval.symbol,
                "market": interval.market,
                "status": "EMPTY",
                "requested_start": effective_start.isoformat(),
                "requested_end": effective_end.isoformat(),
                "row_count": 0,
                "source": "FinMind",
                "source_url": FINMIND_PRICE_URL,
            }
        frame = frame[
            (frame["date"].dt.date >= effective_start)
            & (frame["date"].dt.date <= effective_end)
        ].reset_index(drop=True)
        if frame.empty:
            return {
                "symbol": interval.symbol,
                "market": interval.market,
                "status": "EMPTY",
                "requested_start": effective_start.isoformat(),
                "requested_end": effective_end.isoformat(),
                "row_count": 0,
                "source": "FinMind",
                "source_url": FINMIND_PRICE_URL,
            }
        cache_path = historical_price_service.save_history(interval.symbol, frame)
        if cache_path is None:
            raise RuntimeError(f"normalised FinMind history unexpectedly empty: {interval.symbol}")
        historical_price_service._mark_coverage(
            interval.symbol,
            effective_start,
            effective_end,
            has_adjusted=False,
        )
        return {
            "symbol": interval.symbol,
            "market": interval.market,
            "status": "SUCCESS",
            "requested_start": effective_start.isoformat(),
            "requested_end": effective_end.isoformat(),
            "row_count": len(frame),
            "first_trade_date": frame["date"].min().date().isoformat(),
            "last_trade_date": frame["date"].max().date().isoformat(),
            "cache_path": _manifest_path(cache_path),
            "cache_sha256": _file_sha256(cache_path),
            "source": "FinMind",
            "source_url": FINMIND_PRICE_URL,
        }
    except Exception as exc:
        return {
            "symbol": interval.symbol,
            "market": interval.market,
            "status": "FAILED",
            "requested_start": effective_start.isoformat(),
            "requested_end": effective_end.isoformat(),
            "source": "FinMind",
            "source_url": FINMIND_PRICE_URL,
            "error": f"{type(exc).__name__}: {exc}",
        }


def backfill_finmind_price_history(
    universe: HistoricalUniverse,
    *,
    start_date: date,
    end_date: date,
    symbols: Iterable[str] | None = None,
    only_missing: bool = True,
    workers: int = 4,
    progress: ProgressCallback | None = None,
) -> BackfillRun:
    """Backfill remaining price gaps via the bot's existing FinMind client."""

    targets = _price_targets(universe, start_date, end_date, symbols, only_missing)
    results: list[dict[str, Any]] = []
    total = len(targets)
    if total:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(_fetch_finmind_price_target, target): target[0].symbol
                for target in targets
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                result = future.result()
                results.append(result)
                if progress:
                    progress(completed, total, f"{result['symbol']} {result['status']}")
    return BackfillRun(
        kind="finmind-prices",
        requested_start=start_date,
        requested_end=end_date,
        universe_source_id=universe.source_id,
        results=tuple(sorted(results, key=lambda item: str(item.get("symbol") or ""))),
    )


def _price_window_targets(
    universe: HistoricalUniverse,
    start_date: date,
    end_date: date,
    *,
    minimum_coverage_ratio: float,
    force_all: bool,
) -> list[tuple[MarketMembershipInterval, date, date, int, int]]:
    trading_dates = trading_dates_from_index_cache(start_date, end_date)
    targets: list[tuple[MarketMembershipInterval, date, date, int, int]] = []
    for interval in universe.intervals:
        bounds = _interval_bounds(interval, start_date, end_date)
        if bounds is None:
            continue
        effective_start, effective_end = bounds
        expected = sum(effective_start <= value <= effective_end for value in trading_dates)
        if expected <= 0:
            continue
        cached = historical_price_service.load_cached_history(interval.symbol)
        if cached.empty:
            actual = 0
        else:
            cached_dates = cached["date"].dt.date
            actual = int(
                cached_dates[
                    (cached_dates >= effective_start) & (cached_dates <= effective_end)
                ].nunique()
            )
        if force_all or actual / expected < minimum_coverage_ratio:
            targets.append((interval, effective_start, effective_end, actual, expected))
    return sorted(targets, key=lambda value: value[0].symbol)


def backfill_yahoo_price_window(
    universe: HistoricalUniverse,
    *,
    start_date: date,
    end_date: date,
    minimum_coverage_ratio: float = 0.75,
    force_all: bool = False,
    chunk_size: int = 40,
    progress: ProgressCallback | None = None,
) -> BackfillRun:
    """Repair a broad missing price window through the existing Yahoo backup.

    A window is selected only when cached observations cover less than the
    configured fraction of market trading days during that stock's membership
    interval.  Legitimate short suspensions therefore do not trigger a retry,
    while the missing 2024 cache window is detected reliably.
    """

    if not 0 < minimum_coverage_ratio <= 1:
        raise ValueError("minimum_coverage_ratio must be in (0, 1]")
    targets = _price_window_targets(
        universe,
        start_date,
        end_date,
        minimum_coverage_ratio=minimum_coverage_ratio,
        force_all=force_all,
    )
    by_symbol = {item[0].symbol: item for item in targets}
    symbols = sorted(by_symbol)
    results: list[dict[str, Any]] = []
    completed = 0
    for offset in range(0, len(symbols), max(1, chunk_size)):
        chunk = symbols[offset : offset + max(1, chunk_size)]
        try:
            dataset = yf.download(
                tickers=chunk,
                start=start_date.isoformat(),
                end=date.fromordinal(end_date.toordinal() + 1).isoformat(),
                interval="1d",
                progress=False,
                auto_adjust=False,
                group_by="ticker",
                threads=True,
            )
            chunk_error: str | None = None
        except Exception as exc:  # pragma: no cover - network dependent
            dataset = pd.DataFrame()
            chunk_error = f"{type(exc).__name__}: {exc}"
        for symbol in chunk:
            interval, effective_start, effective_end, previous_rows, expected_rows = by_symbol[symbol]
            frame = historical_price_service.standardize_history(
                historical_price_service._frame_for_symbol(dataset, symbol, len(chunk))
            )
            if not frame.empty:
                frame = frame[
                    (frame["date"].dt.date >= effective_start)
                    & (frame["date"].dt.date <= effective_end)
                ].reset_index(drop=True)
            if frame.empty:
                result = {
                    "symbol": symbol,
                    "market": interval.market,
                    "status": "EMPTY" if chunk_error is None else "FAILED",
                    "requested_start": effective_start.isoformat(),
                    "requested_end": effective_end.isoformat(),
                    "previous_row_count": previous_rows,
                    "expected_market_days": expected_rows,
                    "row_count": 0,
                    "source": "Yahoo Finance",
                }
                if chunk_error:
                    result["error"] = chunk_error
            else:
                cache_path = historical_price_service.save_history(symbol, frame)
                if cache_path is None:
                    raise RuntimeError(f"normalised Yahoo history unexpectedly empty: {symbol}")
                historical_price_service._mark_coverage(
                    symbol,
                    effective_start,
                    effective_end,
                    has_adjusted=(
                        "adj_close" in frame.columns
                        and frame["adj_close"].notna().mean() >= 0.9
                    ),
                )
                result = {
                    "symbol": symbol,
                    "market": interval.market,
                    "status": "SUCCESS",
                    "requested_start": effective_start.isoformat(),
                    "requested_end": effective_end.isoformat(),
                    "previous_row_count": previous_rows,
                    "expected_market_days": expected_rows,
                    "row_count": len(frame),
                    "first_trade_date": frame["date"].min().date().isoformat(),
                    "last_trade_date": frame["date"].max().date().isoformat(),
                    "cache_path": _manifest_path(cache_path),
                    "cache_sha256": _file_sha256(cache_path),
                    "source": "Yahoo Finance",
                }
            results.append(result)
            completed += 1
        if progress:
            success_count = sum(item["status"] == "SUCCESS" for item in results[-len(chunk) :])
            progress(
                completed,
                len(symbols),
                f"Yahoo batch {offset // max(1, chunk_size) + 1}: {success_count}/{len(chunk)} success",
            )
    return BackfillRun(
        kind="yahoo-prices",
        requested_start=start_date,
        requested_end=end_date,
        universe_source_id=universe.source_id,
        results=tuple(sorted(results, key=lambda item: str(item.get("symbol") or ""))),
    )


def trading_dates_from_index_cache(start_date: date, end_date: date) -> list[date]:
    frame = historical_price_service.load_cached_history("^TWII")
    if frame.empty:
        raise FileNotFoundError("isolated ^TWII history is required to build the trading calendar")
    dates = frame["date"].dt.date
    return sorted({value for value in dates if start_date <= value <= end_date})


def _daily_chip_cache_path(target_date: date) -> Path:
    return HISTORICAL_DAILY_CHIP_CACHE_DIR / f"{target_date:%Y%m%d}.csv"


def _valid_daily_chip_cache(path: Path, target_date: date) -> bool:
    if not path.exists():
        return False
    try:
        frame = pd.read_csv(path, dtype={"code": str})
    except Exception:
        return False
    required = {
        "date",
        "code",
        "market",
        "foreign_net_lots",
        "trust_net_lots",
        "foreign_ratio_pct",
        "source",
    }
    if frame.empty or not required.issubset(frame.columns):
        return False
    parsed_dates = pd.to_datetime(frame["date"], errors="coerce").dt.date.dropna().unique().tolist()
    if parsed_dates != [target_date]:
        return False
    sources = set(frame["source"].astype(str))
    return sources.issubset({"TWSE_T86_OFFICIAL", "TPEX_DAILY_TRADE_OFFICIAL"})


def _save_daily_chip_frame(target_date: date, frame: pd.DataFrame) -> Path:
    columns = [
        "date",
        "code",
        "market",
        "foreign_net_lots",
        "trust_net_lots",
        "foreign_ratio_pct",
        "source",
    ]
    normalised = frame.copy()
    normalised["date"] = target_date.isoformat()
    normalised["code"] = normalised["code"].astype(str).str.strip()
    normalised = normalised[columns].sort_values(["code", "market"]).drop_duplicates(
        ["date", "code", "market"], keep="last"
    )
    target = _daily_chip_cache_path(target_date)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".csv.tmp")
    normalised.to_csv(temporary, index=False, encoding="utf-8")
    temporary.replace(target)
    return target


def _fetch_institutional_target(
    universe: HistoricalUniverse,
    target_date: date,
    *,
    twse_min_interval_seconds: float,
    tpex_min_interval_seconds: float,
) -> dict[str, Any]:
    members = universe.members_as_of(target_date, strict=True)
    twse_codes = {item.code for item in members if item.market == "TWSE"}
    tpex_codes = {item.code for item in members if item.market == "TPEX"}
    try:
        with httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/140 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://www.twse.com.tw/",
            },
        ) as client:
            _wait_for_source_slot("twse_daily_institutional", twse_min_interval_seconds)
            twse_payload = _request_json(
                client,
                "GET",
                TWSE_DAILY_INSTITUTIONAL_URL,
                params={
                    "response": "json",
                    "date": target_date.strftime("%Y%m%d"),
                    "selectType": "ALL",
                },
            )
            _wait_for_source_slot("tpex_daily_institutional", tpex_min_interval_seconds)
            tpex_payload = _request_json(
                client,
                "GET",
                TPEX_DAILY_INSTITUTIONAL_URL,
                params={
                    "response": "json",
                    "date": target_date.strftime("%Y/%m/%d"),
                    "type": "Daily",
                    "sect": "EW",
                },
            )
        twse = parse_twse_daily_institutional_payload(twse_payload, target_date, twse_codes)
        tpex = parse_tpex_daily_institutional_payload(tpex_payload, target_date, tpex_codes)
        if twse_codes and twse.empty:
            raise ValueError("TWSE official response contained no active-universe rows")
        if tpex_codes and tpex.empty:
            raise ValueError("TPEx official response contained no active-universe rows")
        combined = pd.concat([twse, tpex], ignore_index=True)
        path = _save_daily_chip_frame(target_date, combined)
        return {
            "date": target_date.isoformat(),
            "status": "SUCCESS",
            "row_count": len(combined),
            "twse_rows": len(twse),
            "tpex_rows": len(tpex),
            "cache_path": _manifest_path(path),
            "cache_sha256": _file_sha256(path),
        }
    except Exception as exc:
        return {
            "date": target_date.isoformat(),
            "status": "FAILED",
            "error": f"{type(exc).__name__}: {exc}",
        }


def backfill_official_daily_institutional(
    universe: HistoricalUniverse,
    *,
    start_date: date,
    end_date: date,
    trading_dates: Iterable[date] | None = None,
    only_missing: bool = True,
    workers: int = 4,
    twse_min_interval_seconds: float = 0.4,
    tpex_min_interval_seconds: float = 0.05,
    progress: ProgressCallback | None = None,
) -> BackfillRun:
    """Fill the isolated per-date chip cache from two official bulk endpoints."""

    dates = sorted(
        set(trading_dates or trading_dates_from_index_cache(start_date, end_date))
    )
    dates = [value for value in dates if start_date <= value <= end_date]
    if only_missing:
        dates = [
            value
            for value in dates
            if not _valid_daily_chip_cache(_daily_chip_cache_path(value), value)
        ]
    results: list[dict[str, Any]] = []
    total = len(dates)
    if total:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(
                    _fetch_institutional_target,
                    universe,
                    value,
                    twse_min_interval_seconds=twse_min_interval_seconds,
                    tpex_min_interval_seconds=tpex_min_interval_seconds,
                ): value
                for value in dates
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                result = future.result()
                results.append(result)
                if progress:
                    progress(completed, total, f"{result['date']} {result['status']}")
    return BackfillRun(
        kind="official-daily-institutional",
        requested_start=start_date,
        requested_end=end_date,
        universe_source_id=universe.source_id,
        results=tuple(sorted(results, key=lambda item: str(item.get("date") or ""))),
    )
