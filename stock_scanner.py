from __future__ import annotations

import json
import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import yfinance as yf

from fugle_data import fetch_fugle_history

from progress_logger import now_timestamp


ROOT_DIR = Path(__file__).resolve().parent
CACHE_DIR = ROOT_DIR / ".cache"
MONTHLY_CACHE_DIR = CACHE_DIR / "monthly_revenue"
PRICE_CACHE_PATH = CACHE_DIR / "price_metrics.json"
GROSS_MARGIN_CACHE_PATH = CACHE_DIR / "gross_margin.json"
STOCK_LIST_PATH = ROOT_DIR / "stock_list.json"
REVENUE_VALUE_MULTIPLIER = 1000.0
YF_PRICE_CHUNK_SIZE = 40
YF_PRICE_CHUNK_RETRIES = 3
YF_PRICE_BASE_RETRY_SECONDS = 2.0
YF_PRICE_CHUNK_PAUSE_SECONDS = 1.5
YF_PRICE_SINGLE_PAUSE_SECONDS = 0.8

TWSE_NAME_API_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_NAME_API_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
UNCLASSIFIED_INDUSTRY = "未分類"

INDUSTRY_CODE_LABELS = {
    "00": UNCLASSIFIED_INDUSTRY,
    "01": "水泥工業",
    "02": "食品工業",
    "03": "塑膠工業",
    "04": "紡織纖維",
    "05": "電機機械",
    "06": "電器電纜",
    "08": "玻璃陶瓷",
    "09": "造紙工業",
    "10": "鋼鐵工業",
    "11": "橡膠工業",
    "12": "汽車工業",
    "14": "建材營造",
    "15": "航運業",
    "16": "觀光餐旅",
    "17": "金融保險",
    "18": "貿易百貨",
    "19": "綜合",
    "20": "其他",
    "21": "化學工業",
    "22": "生技醫療業",
    "23": "油電燃氣業",
    "24": "半導體業",
    "25": "電腦及週邊設備業",
    "26": "光電業",
    "27": "通信網路業",
    "28": "電子零組件業",
    "29": "電子通路業",
    "30": "資訊服務業",
    "31": "其他電子業",
    "32": "文化創意業",
    "33": "農業科技業",
    "34": "電子商務",
    "35": "綠能環保",
    "36": "數位雲端",
    "37": "運動休閒",
    "38": "居家生活",
    "80": "管理股票",
}

GROUP_LABELS = {
    "group_1": "營收第一組：連4月成長",
    "group_2": "營收第二組：動能轉強",
}

RATING_LABELS = {
    "A": "A級 (毛利連增)",
    "B": "B級 (毛利穩健)",
    "C": "C級 (轉虧為盈)",
    "D": "D級 (其他)",
}

REPORT_GROUP_TITLES = {
    "group_1": "📂 【營收第一組：連 4 月穩健成長】",
    "group_2": "📂 【營收第二組：動能轉強】",
}

REPORT_RATING_TITLES = {
    "A": "🥇 A級：毛利連三季遞增",
    "B": "🥈 B級：毛利穩定波動",
    "C": "🥉 C級：毛利轉虧為盈",
    "D": "▫️ D級：其他",
}

DEFAULT_SCAN_SETTINGS = {
    "min_price": 10.0,
    "max_price": 100.0,
    "min_avg_volume_20d": 500.0,
    "min_monthly_revenue": 50_000_000.0,
}


@dataclass(frozen=True)
class StockUniverseEntry:
    code: str
    symbol: str
    market: str
    name: str
    industry: str = UNCLASSIFIED_INDUSTRY

    @property
    def display_name(self) -> str:
        return f"{self.code} {self.name}".strip()


@dataclass(frozen=True)
class RevenuePoint:
    month: str
    revenue: float
    yoy: float


@dataclass(frozen=True)
class GrossMarginPoint:
    quarter: str
    gross_margin: float


@dataclass(frozen=True)
class ScanCandidate:
    code: str
    symbol: str
    market: str
    name: str
    industry: str
    revenue_group: str
    gross_margin_rating: str
    price: float
    avg_volume_20d: float
    latest_monthly_revenue: float
    revenue_history: list[RevenuePoint]
    gross_margins: list[GrossMarginPoint]

    @property
    def display_name(self) -> str:
        return f"{self.code} {self.name}".strip()


@dataclass(frozen=True)
class ScanReport:
    generated_at: str
    total_symbols: int
    hard_filter_passed: int
    candidates: list[ScanCandidate]
    scan_settings: dict[str, float]


def _ensure_cache_dirs() -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    MONTHLY_CACHE_DIR.mkdir(exist_ok=True)


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def _is_fresh(path: Path, ttl_seconds: int) -> bool:
    if not path.exists():
        return False
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds < ttl_seconds


def _month_offsets(months: int) -> list[date]:
    today = datetime.now().date().replace(day=1)
    results: list[date] = []
    year = today.year
    month = today.month
    for _ in range(months):
        results.append(date(year, month, 1))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return results


def _normalize_column(text: str) -> str:
    normalized = re.sub(r"\s+", "", str(text))
    replacements = {
        "營業收入": "營收",
        "資料年月": "年月",
        "公司代號": "代號",
        "公司名稱": "名稱",
        "公司簡稱": "名稱",
        "公司代碼": "代號",
        "當月營收": "當月營收",
        "去年當月營收": "去年當月營收",
        "年增率": "年增率",
        "年增": "年增率",
        "去年同月增減(%)": "年增率(%)",
        "去年同月增減": "年增率(%)",
    }
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    return normalized


def _find_column(columns: list[str], *tokens: str) -> str | None:
    normalized_tokens = [_normalize_column(token) for token in tokens]
    for column in columns:
        normalized = _normalize_column(column)
        if all(token in normalized for token in normalized_tokens):
            return column
    return None


def _flatten_columns(columns: Any) -> list[str]:
    if isinstance(columns, pd.MultiIndex):
        flattened: list[str] = []
        for column in columns:
            parts = [str(part).strip() for part in column if str(part).strip() and "Unnamed" not in str(part)]
            flattened.append("_".join(parts))
        return flattened
    return [str(column).strip() for column in columns]


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "---", "nan", "None", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _column_as_series(frame: pd.DataFrame, column: str) -> pd.Series:
    value = frame[column]
    if isinstance(value, pd.DataFrame):
        if value.empty:
            return pd.Series(pd.NA, index=frame.index)
        value = value.iloc[:, 0]
    if isinstance(value, pd.Series):
        return value
    return pd.Series(value, index=frame.index)


def _extract_price_metric(frame: pd.DataFrame) -> dict[str, float] | None:
    if frame.empty or "Close" not in frame.columns or "Volume" not in frame.columns:
        return None

    candidate = pd.DataFrame(index=frame.index)
    candidate["Close"] = _column_as_series(frame, "Close")
    candidate["Volume"] = _column_as_series(frame, "Volume")
    candidate = candidate.dropna()
    if len(candidate) < 20:
        return None
    close = pd.to_numeric(candidate["Close"], errors="coerce").dropna()
    volume = pd.to_numeric(candidate["Volume"], errors="coerce").dropna()
    if len(close) < 20 or len(volume) < 20:
        return None

    latest_close = float(close.iloc[-1])
    previous_close = float(close.iloc[-2]) if len(close) >= 2 else None
    latest_volume_lots = float(volume.iloc[-1] / 1000.0)
    avg_volume_20d = float(volume.tail(20).mean() / 1000.0)
    latest_date = _latest_frame_date(candidate)
    high_20d = float(close.tail(20).max()) if len(close) >= 20 else None
    pullback_from_high_pct = round((latest_close / high_20d - 1) * 100, 2) if high_20d else None
    metric = {
        "price": latest_close,
        "price_date": latest_date,
        "previous_close": previous_close,
        "change_pct": round((latest_close / previous_close - 1) * 100, 2) if previous_close else None,
        "change_pct_5d": _window_return_pct(close, 5),
        "change_pct_10d": _window_return_pct(close, 10),
        "change_pct_20d": _window_return_pct(close, 20),
        "volume": latest_volume_lots,
        "avg_volume_20d": avg_volume_20d,
        "volume_ratio": round(latest_volume_lots / avg_volume_20d, 2) if avg_volume_20d else None,
        "turnover": round(latest_close * latest_volume_lots, 2),
        "new_high_days": _latest_extreme_days(close, high=True),
        "new_low_days": _latest_extreme_days(close, high=False),
        "days_since_high": _days_since_extreme(close, high=True),
        "near_high_20d": bool(high_20d and latest_close >= high_20d * 0.95),
        "pullback_from_high_pct": pullback_from_high_pct,
        "above_ma5": _above_ma(close, 5),
        "above_ma10": _above_ma(close, 10),
        "above_ma20": _above_ma(close, 20),
    }
    return {key: value for key, value in metric.items() if value is not None}


def _latest_frame_date(frame: pd.DataFrame) -> str | None:
    if frame.empty:
        return None
    try:
        latest_index = frame.index[-1]
        if hasattr(latest_index, "date"):
            return latest_index.date().isoformat()
        return pd.to_datetime(latest_index).date().isoformat()
    except Exception:
        return None


def _latest_extreme_days(close: pd.Series, *, high: bool) -> int | None:
    latest = float(close.iloc[-1])
    matched = 0
    for window in (20, 60, 120):
        if len(close) < window:
            continue
        window_values = close.tail(window)
        extreme = float(window_values.max() if high else window_values.min())
        if (high and latest >= extreme) or (not high and latest <= extreme):
            matched = window
    return matched or None


def _window_return_pct(close: pd.Series, window: int) -> float | None:
    if len(close) <= window:
        return None
    base = float(close.iloc[-window - 1])
    latest = float(close.iloc[-1])
    if not base:
        return None
    return round((latest / base - 1) * 100, 2)


def _above_ma(close: pd.Series, window: int) -> bool | None:
    if len(close) < window:
        return None
    ma = float(close.tail(window).mean())
    return bool(float(close.iloc[-1]) >= ma) if ma else None


def _days_since_extreme(close: pd.Series, *, high: bool) -> int | None:
    if close.empty:
        return None
    window = close.tail(min(60, len(close)))
    extreme = window.max() if high else window.min()
    matches = window[window == extreme]
    if matches.empty:
        return None
    return int(len(window) - 1 - window.index.get_loc(matches.index[-1]))


def _download_single_symbol_price_metric(symbol: str) -> dict[str, float] | None:
    for attempt in range(YF_PRICE_CHUNK_RETRIES):
        try:
            history = yf.Ticker(symbol).history(period="3mo", interval="1d", auto_adjust=False)
        except Exception:
            history = pd.DataFrame()

        if isinstance(history.columns, pd.MultiIndex):
            history.columns = history.columns.get_level_values(0)

        metric = _extract_price_metric(history)
        if metric:
            return metric

        time.sleep(YF_PRICE_BASE_RETRY_SECONDS * (attempt + 1))

    return None


def _download_fugle_price_metric(symbol: str) -> dict[str, float] | None:
    history = fetch_fugle_history(symbol, datetime.now().date() - timedelta(days=120), datetime.now().date(), "1d")
    if history.empty:
        return None
    frame = history.rename(
        columns={
            "close": "Close",
            "volume": "Volume",
        }
    )
    return _extract_price_metric(frame)


def _download_chunk_price_metrics(chunk: list[str]) -> dict[str, dict[str, float]]:
    chunk_metrics: dict[str, dict[str, float]] = {}
    missing_symbols = list(chunk)

    for attempt in range(YF_PRICE_CHUNK_RETRIES):
        try:
            dataset = yf.download(
                tickers=missing_symbols,
                period="3mo",
                interval="1d",
                progress=False,
                auto_adjust=False,
                group_by="ticker",
                threads=False,
            )
        except Exception:
            dataset = pd.DataFrame()

        if not dataset.empty:
            for symbol in list(missing_symbols):
                try:
                    frame = dataset[symbol] if isinstance(dataset.columns, pd.MultiIndex) else dataset
                except KeyError:
                    continue

                metric = _extract_price_metric(frame)
                if metric:
                    chunk_metrics[symbol] = metric

        missing_symbols = [symbol for symbol in chunk if symbol not in chunk_metrics]
        if not missing_symbols:
            return chunk_metrics

        time.sleep(YF_PRICE_BASE_RETRY_SECONDS * (attempt + 1))

    for symbol in missing_symbols:
        metric = _download_single_symbol_price_metric(symbol)
        if not metric:
            metric = _download_fugle_price_metric(symbol)
        if metric:
            chunk_metrics[symbol] = metric
        time.sleep(YF_PRICE_SINGLE_PAUSE_SECONDS)

    return chunk_metrics


def _format_price_display(price: float) -> str:
    return f"{price:,.2f}".rstrip("0").rstrip(".")


def _wrap_report_members(members: list[str], max_line_length: int = 180) -> list[str]:
    wrapped_lines: list[str] = []
    current_line = ""

    for member in members:
        if not current_line:
            current_line = member
            continue

        candidate_line = f"{current_line} | {member}"
        if len(candidate_line) <= max_line_length:
            current_line = candidate_line
            continue

        wrapped_lines.append(current_line)
        current_line = member

    if current_line:
        wrapped_lines.append(current_line)

    return wrapped_lines


def _safe_margin_drop(previous: float, current: float) -> float:
    return current - previous


def _first_text(mapping: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return default


def _format_industry(raw_value: str) -> str:
    normalized = str(raw_value or "").strip()
    if not normalized:
        return UNCLASSIFIED_INDUSTRY

    code = normalized.zfill(2) if normalized.isdigit() else normalized
    return INDUSTRY_CODE_LABELS.get(code, normalized)


def _stock_universe_entry_from_mapping(item: dict[str, Any]) -> StockUniverseEntry:
    payload = {
        "code": str(item.get("code", "")).strip(),
        "symbol": str(item.get("symbol", "")).strip(),
        "market": str(item.get("market", "")).strip(),
        "name": str(item.get("name", "")).strip(),
        "industry": _format_industry(str(item.get("industry") or UNCLASSIFIED_INDUSTRY)),
    }
    return StockUniverseEntry(**payload)


def load_stock_universe(force_refresh: bool = False, ttl_seconds: int = 24 * 60 * 60) -> list[StockUniverseEntry]:
    cached_stocks: list[dict[str, Any]] = []
    if not force_refresh and STOCK_LIST_PATH.exists():
        try:
            payload = _read_json(STOCK_LIST_PATH)
            cached_stocks = payload.get("stocks", [])
        except Exception:
            cached_stocks = []
    if not force_refresh and _is_fresh(STOCK_LIST_PATH, ttl_seconds):
        if cached_stocks and all("industry" in item for item in cached_stocks):
            return [_stock_universe_entry_from_mapping(item) for item in cached_stocks]

    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, verify=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
            twse_rows = client.get(TWSE_NAME_API_URL)
            twse_rows.raise_for_status()
            tpex_rows = client.get(TPEX_NAME_API_URL)
            tpex_rows.raise_for_status()

            stocks: list[dict[str, str]] = []
            for item in twse_rows.json():
                code = str(item.get("公司代號", "")).strip()
                name = str(item.get("公司簡稱") or item.get("公司名稱") or "").strip()
                industry = _format_industry(
                    _first_text(item, "產業別", "Industry", "IndustryName", default=UNCLASSIFIED_INDUSTRY)
                )
                if code:
                    stocks.append(
                        {
                            "code": code,
                            "symbol": f"{code}.TW",
                            "market": "TWSE",
                            "name": name,
                            "industry": industry,
                        }
                    )

            for item in tpex_rows.json():
                code = str(item.get("SecuritiesCompanyCode", "")).strip()
                name = str(item.get("CompanyAbbreviation") or item.get("CompanyName") or "").strip()
                industry = _first_text(
                    item,
                    "產業別",
                    "Industry",
                    "IndustryName",
                    "IndustryType",
                    "IndustryCategory",
                    "SecuritiesIndustryCode",
                    default=UNCLASSIFIED_INDUSTRY,
                )
                industry = _format_industry(industry)
                if code:
                    stocks.append(
                        {
                            "code": code,
                            "symbol": f"{code}.TWO",
                            "market": "TPEX",
                            "name": name,
                            "industry": industry,
                        }
                    )
    except Exception:
        if cached_stocks:
            return [_stock_universe_entry_from_mapping(item) for item in cached_stocks]
        raise

    stocks = sorted(stocks, key=lambda item: (item["market"], item["code"]))
    _write_json(
        STOCK_LIST_PATH,
        {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source": {"twse": TWSE_NAME_API_URL, "tpex": TPEX_NAME_API_URL},
            "stocks": stocks,
        },
    )
    return [StockUniverseEntry(**item) for item in stocks]


def _monthly_cache_path(market: str, month_start: date) -> Path:
    return MONTHLY_CACHE_DIR / f"{market.lower()}_{month_start.strftime('%Y%m')}.json"


def _load_monthly_revenue_rows(client: httpx.Client, market: str, month_start: date) -> list[dict[str, Any]]:
    cache_path = _monthly_cache_path(market, month_start)
    if _is_fresh(cache_path, 12 * 60 * 60):
        return _read_json(cache_path)

    folder = "sii" if market == "TWSE" else "otc"
    roc_year = month_start.year - 1911
    url = f"https://mopsov.twse.com.tw/nas/t21/{folder}/t21sc03_{roc_year}_{month_start.month}_0.html"
    response = client.get(url)
    response.raise_for_status()
    html = response.content.decode("big5-hkscs", errors="ignore")

    try:
        tables = pd.read_html(StringIO(html), flavor="lxml")
    except ValueError:
        _write_json(cache_path, [])
        return []

    rows: list[dict[str, Any]] = []
    for table in tables:
        candidate = table.copy()
        candidate.columns = _flatten_columns(candidate.columns)
        columns = candidate.columns.tolist()
        code_column = _find_column(columns, "代號")
        name_column = _find_column(columns, "名稱")
        current_revenue_column = _find_column(columns, "當月營收")
        prior_year_column = _find_column(columns, "去年當月營收")
        yoy_column = _find_column(columns, "年增率(%)") or _find_column(columns, "去年同月增減(%)")
        if not code_column or not current_revenue_column:
            continue

        candidate[code_column] = candidate[code_column].astype(str).str.strip()
        candidate = candidate[candidate[code_column].str.fullmatch(r"\d{4}", na=False)]
        if candidate.empty:
            continue

        for _, row in candidate.iterrows():
            current_revenue = _to_float(row.get(current_revenue_column))
            prior_year_revenue = _to_float(row.get(prior_year_column)) if prior_year_column else None
            yoy = _to_float(row.get(yoy_column)) if yoy_column else None
            if yoy is None and current_revenue not in (None, 0) and prior_year_revenue not in (None, 0):
                yoy = ((current_revenue / prior_year_revenue) - 1.0) * 100.0
            if current_revenue is None or yoy is None:
                continue
            rows.append(
                {
                    "code": str(row.get(code_column)).strip(),
                    "name": str(row.get(name_column, "")).strip() if name_column else "",
                    "month": month_start.isoformat(),
                    "monthly_revenue": current_revenue,
                    "yoy": yoy,
                }
            )

    _write_json(cache_path, rows)
    return rows


def load_recent_revenue_history(
    universe: list[StockUniverseEntry],
    months_to_fetch: int = 6,
) -> dict[str, list[RevenuePoint]]:
    universe_map = {entry.code: entry for entry in universe}
    history_map: dict[str, list[RevenuePoint]] = {}

    with httpx.Client(timeout=20.0, follow_redirects=True, verify=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for month_start in _month_offsets(months_to_fetch):
            for market in ("TWSE", "TPEX"):
                try:
                    rows = _load_monthly_revenue_rows(client, market, month_start)
                except Exception:
                    continue
                for row in rows:
                    entry = universe_map.get(row["code"])
                    if not entry or entry.market != market:
                        continue
                    history_map.setdefault(entry.code, []).append(
                        RevenuePoint(
                            month=row["month"],
                            revenue=float(row["monthly_revenue"]) * REVENUE_VALUE_MULTIPLIER,
                            yoy=float(row["yoy"]),
                        )
                    )

    trimmed_history: dict[str, list[RevenuePoint]] = {}
    for code, points in history_map.items():
        ordered = sorted(points, key=lambda item: item.month, reverse=True)
        unique_points: list[RevenuePoint] = []
        seen_months: set[str] = set()
        for point in ordered:
            if point.month in seen_months:
                continue
            seen_months.add(point.month)
            unique_points.append(point)
            if len(unique_points) == 4:
                break
        if len(unique_points) == 4:
            trimmed_history[code] = unique_points
    return trimmed_history


def load_price_metrics(
    universe: list[StockUniverseEntry],
    force_refresh: bool = False,
    ttl_seconds: int = 30 * 60,
    chunk_size: int = YF_PRICE_CHUNK_SIZE,
) -> dict[str, dict[str, float]]:
    requested_symbols = [entry.symbol for entry in universe]
    cached_metrics: dict[str, dict[str, float]] = {}
    if PRICE_CACHE_PATH.exists():
        payload = _read_json(PRICE_CACHE_PATH)
        cached_metrics = payload.get("metrics", {})
    cache_fresh = _is_fresh(PRICE_CACHE_PATH, ttl_seconds)
    if not force_refresh and cache_fresh:
        if cached_metrics and all(symbol in cached_metrics for symbol in requested_symbols) and _price_metric_schema_ready(cached_metrics, requested_symbols):
            return {symbol: cached_metrics[symbol] for symbol in requested_symbols}

    schema_ready = _price_metric_schema_ready(cached_metrics, requested_symbols)
    refresh_all = force_refresh or not cache_fresh
    metrics: dict[str, dict[str, float]] = {} if refresh_all or not schema_ready else dict(cached_metrics)
    symbols_to_fetch = requested_symbols if refresh_all else [symbol for symbol in requested_symbols if symbol not in metrics]
    downloaded_metrics: dict[str, dict[str, float]] = {}

    for index in range(0, len(symbols_to_fetch), chunk_size):
        chunk = symbols_to_fetch[index : index + chunk_size]
        chunk_metrics = _download_chunk_price_metrics(chunk)
        downloaded_metrics.update(chunk_metrics)
        metrics.update(chunk_metrics)
        time.sleep(YF_PRICE_CHUNK_PAUSE_SECONDS)

    if refresh_all and not downloaded_metrics and cached_metrics:
        return {symbol: cached_metrics[symbol] for symbol in requested_symbols if symbol in cached_metrics}

    if refresh_all and cached_metrics and len(downloaded_metrics) < max(1, int(len(requested_symbols) * 0.8)):
        return {symbol: cached_metrics[symbol] for symbol in requested_symbols if symbol in cached_metrics}

    if cached_metrics and (not refresh_all and (not metrics or len(metrics) < len(cached_metrics))):
        merged_metrics = dict(cached_metrics)
        merged_metrics.update(metrics)
        metrics = merged_metrics

    if not metrics and cached_metrics:
        return {symbol: cached_metrics[symbol] for symbol in requested_symbols if symbol in cached_metrics}
    if not metrics:
        return {}

    _write_json(
        PRICE_CACHE_PATH,
        {"generated_at": datetime.now().isoformat(timespec="seconds"), "metrics": metrics},
    )
    return {symbol: metrics[symbol] for symbol in requested_symbols if symbol in metrics}


def _price_metric_schema_ready(metrics: dict[str, dict[str, float]], requested_symbols: list[str]) -> bool:
    if not metrics:
        return False
    checked = 0
    ready = 0
    for symbol in requested_symbols[:50]:
        metric = metrics.get(symbol)
        if not isinstance(metric, dict):
            continue
        checked += 1
        if "change_pct" in metric and "volume_ratio" in metric and "price_date" in metric:
            ready += 1
    return bool(checked) and ready / checked >= 0.8


def _load_gross_margin_cache() -> dict[str, dict[str, Any]]:
    if not GROSS_MARGIN_CACHE_PATH.exists():
        return {}
    payload = _read_json(GROSS_MARGIN_CACHE_PATH)
    return payload.get("metrics", {})


def _save_gross_margin_cache(metrics: dict[str, dict[str, Any]]) -> None:
    _write_json(
        GROSS_MARGIN_CACHE_PATH,
        {"generated_at": datetime.now().isoformat(timespec="seconds"), "metrics": metrics},
    )


def load_gross_margin_series(symbol: str, cache: dict[str, dict[str, Any]], ttl_seconds: int = 12 * 60 * 60) -> list[GrossMarginPoint]:
    cached = cache.get(symbol)
    if cached and isinstance(cached.get("fetched_at"), str):
        fetched_at = datetime.fromisoformat(cached["fetched_at"])
        if (datetime.now() - fetched_at).total_seconds() < ttl_seconds:
            return [GrossMarginPoint(**item) for item in cached.get("series", [])]

    try:
        financials = yf.Ticker(symbol).quarterly_income_stmt
    except Exception:
        financials = pd.DataFrame()

    if financials is None or financials.empty:
        cache[symbol] = {"fetched_at": datetime.now().isoformat(timespec="seconds"), "series": []}
        return []

    gross_profit_label = "Gross Profit" if "Gross Profit" in financials.index else None
    revenue_label = None
    for label in ("Total Revenue", "Operating Revenue"):
        if label in financials.index:
            revenue_label = label
            break

    if not gross_profit_label or not revenue_label:
        cache[symbol] = {"fetched_at": datetime.now().isoformat(timespec="seconds"), "series": []}
        return []

    series: list[GrossMarginPoint] = []
    for column in financials.columns:
        gross_profit = _to_float(financials.at[gross_profit_label, column])
        revenue = _to_float(financials.at[revenue_label, column])
        if gross_profit is None or revenue in (None, 0):
            continue
        margin = (gross_profit / revenue) * 100.0
        if math.isnan(margin):
            continue
        quarter_text = pd.Timestamp(column).strftime("%YQ") + str(pd.Timestamp(column).quarter)
        series.append(GrossMarginPoint(quarter=quarter_text, gross_margin=round(float(margin), 4)))
        if len(series) == 3:
            break

    cache[symbol] = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "series": [asdict(item) for item in series],
    }
    return series


def classify_revenue_group(history: list[RevenuePoint]) -> str | None:
    if len(history) < 4:
        return None
    yoy_values = [point.yoy for point in history[:4]]
    if all(value >= 1.0 for value in yoy_values):
        return "group_1"
    positive_months = sum(value >= 1.0 for value in yoy_values)
    if positive_months >= 2 and all(value > -15.0 for value in yoy_values):
        return "group_2"
    return None


def classify_gross_margin(series: list[GrossMarginPoint]) -> str:
    if len(series) < 3:
        return "D"

    latest, previous, oldest = [point.gross_margin for point in series[:3]]
    if latest > 0 and previous > 0 and oldest > 0 and latest > previous > oldest:
        return "A"

    changes = [latest - previous, previous - oldest]
    if latest > 0 and previous > 0 and oldest > 0:
        positive_growth_count = sum(change > 0 for change in changes)
        acceptable_declines = all(change > -5.0 for change in changes)
        if positive_growth_count >= 1 and acceptable_declines:
            return "B"

    turning_positive = ((oldest < 0 < previous) or (previous < 0 < latest)) and latest > 0
    if turning_positive:
        return "C"

    return "D"


def scan_tw_market(
    force_refresh: bool = False,
    max_symbols: int | None = None,
    scan_settings: dict[str, float] | None = None,
) -> ScanReport:
    print(f"[{now_timestamp()}] [選股進度][財報營收] 0% 初始化掃描設定", flush=True)
    _ensure_cache_dirs()
    settings = dict(DEFAULT_SCAN_SETTINGS)
    if scan_settings:
        for key, value in scan_settings.items():
            if key in settings:
                try:
                    settings[key] = float(value)
                except (TypeError, ValueError):
                    continue

    print(f"[{now_timestamp()}] [選股進度][財報營收] 10% 讀取上市櫃股票清單", flush=True)
    universe = load_stock_universe(force_refresh=force_refresh)
    if max_symbols is not None:
        universe = universe[:max_symbols]

    print(f"[{now_timestamp()}] [選股進度][財報營收] 25% 讀取近月營收資料，共 {len(universe)} 檔", flush=True)
    revenue_history_map = load_recent_revenue_history(universe)
    print(f"[{now_timestamp()}] [選股進度][財報營收] 45% 讀取股價與 20 日均量", flush=True)
    price_metrics = load_price_metrics(universe, force_refresh=force_refresh)
    print(f"[{now_timestamp()}] [選股進度][財報營收] 60% 套用股價、均量、營收硬篩", flush=True)
    gross_margin_cache = _load_gross_margin_cache()

    hard_filter_candidates: list[tuple[StockUniverseEntry, list[RevenuePoint], dict[str, float], str]] = []
    for entry in universe:
        revenue_history = revenue_history_map.get(entry.code)
        price_metric = price_metrics.get(entry.symbol)
        if not revenue_history or not price_metric:
            continue

        latest_revenue = revenue_history[0].revenue
        price = price_metric.get("price")
        avg_volume_20d = price_metric.get("avg_volume_20d")
        if price is None or avg_volume_20d is None:
            continue
        if not (settings["min_price"] < price < settings["max_price"]):
            continue
        if avg_volume_20d <= settings["min_avg_volume_20d"]:
            continue
        if latest_revenue <= settings["min_monthly_revenue"]:
            continue

        revenue_group = classify_revenue_group(revenue_history)
        if not revenue_group:
            continue
        hard_filter_candidates.append((entry, revenue_history, price_metric, revenue_group))

    candidates: list[ScanCandidate] = []
    total_hard_filter = max(1, len(hard_filter_candidates))
    for entry, revenue_history, price_metric, revenue_group in hard_filter_candidates:
        try:
            gross_margins = load_gross_margin_series(entry.symbol, gross_margin_cache)
            rating = classify_gross_margin(gross_margins)
            candidates.append(
                ScanCandidate(
                    code=entry.code,
                    symbol=entry.symbol,
                    market=entry.market,
                    name=entry.name,
                    industry=entry.industry,
                    revenue_group=revenue_group,
                    gross_margin_rating=rating,
                    price=round(price_metric["price"], 2),
                    avg_volume_20d=round(price_metric["avg_volume_20d"], 2),
                    latest_monthly_revenue=float(revenue_history[0].revenue),
                    revenue_history=revenue_history,
                    gross_margins=gross_margins,
                )
            )
        except Exception:
            continue

        if len(candidates) % 25 == 0 or len(candidates) == total_hard_filter:
            progress = min(95, 65 + int(len(candidates) / total_hard_filter * 30))
            print(
                f"[{now_timestamp()}] [選股進度][財報營收] {progress}% 計算毛利率分級 {len(candidates)}/{len(hard_filter_candidates)}",
                flush=True,
            )

    _save_gross_margin_cache(gross_margin_cache)
    candidates.sort(key=lambda item: (item.revenue_group, item.gross_margin_rating, item.industry, item.code))
    print(f"[{now_timestamp()}] [選股進度][財報營收] 100% 完成，符合 {len(candidates)} 檔", flush=True)
    return ScanReport(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        total_symbols=len(universe),
        hard_filter_passed=len(hard_filter_candidates),
        candidates=candidates,
        scan_settings=settings,
    )


def format_scan_report(report: ScanReport) -> str:
    grouped: dict[str, dict[str, dict[str, list[str]]]] = {
        group_key: {rating: {} for rating in RATING_LABELS} for group_key in GROUP_LABELS
    }

    for candidate in report.candidates:
        industry = candidate.industry or UNCLASSIFIED_INDUSTRY
        grouped[candidate.revenue_group][candidate.gross_margin_rating].setdefault(industry, []).append(
            f"{candidate.display_name} ({_format_price_display(candidate.price)})"
        )

    settings = report.scan_settings
    lines = [
        "🔍 今日財報營收選股掃描報告",
        f"📅 日期：{report.generated_at}",
        "",
        "📌 分類定義說明",
        "【營收 G1】：連續 4 個月年增 >= 1% (穩定成長型)",
        "【營收 G2】：4 個月內至少 2 個月年增 >= 1%，且衰退 < 15% (動能轉強型)",
        "等級 A：毛利連三季成長 (成長力最強)",
        "等級 B：毛利相對穩健 (成長中有小波動)",
        "等級 C：毛利轉虧為盈 (具備強大轉機)",
        "",
    ]

    for group_key in GROUP_LABELS:
        lines.append("")
        lines.append(REPORT_GROUP_TITLES[group_key])
        has_members = False
        for rating in ("A", "B", "C", "D"):
            industry_groups = grouped[group_key][rating]
            if not industry_groups:
                continue
            has_members = True
            lines.append("")
            lines.append(REPORT_RATING_TITLES[rating])
            for industry in sorted(industry_groups, key=lambda value: (value == UNCLASSIFIED_INDUSTRY, value)):
                members = industry_groups[industry]
                lines.append(f"  【{industry}】")
                lines.extend(_wrap_report_members(members))
                lines.append("")
        if not has_members:
            lines.append("(無符合標的)")

    lines.extend(
        [
            "📊 掃描統計",
            f"總掃描範圍：{report.total_symbols} 檔",
            (
                f"通過硬篩標的：{report.hard_filter_passed} 檔 "
                f"(股價 {int(settings['min_price'])}~{int(settings['max_price'])} / 均量 > {int(settings['min_avg_volume_20d'])})"
            ),
            f"符合選股邏輯：{len(report.candidates)} 檔",
            f"資料日期：{report.generated_at.split()[0]}",
            "資料來源：本機快取 / TWSE / TPEX / FinMind / 估算",
        ]
    )
    return "\n".join(lines).strip()


def run_scan(
    force_refresh: bool = False,
    max_symbols: int | None = None,
    scan_settings: dict[str, float] | None = None,
) -> str:
    report = scan_tw_market(
        force_refresh=force_refresh,
        max_symbols=max_symbols,
        scan_settings=scan_settings,
    )
    return format_scan_report(report)


if __name__ == "__main__":
    print(run_scan())
