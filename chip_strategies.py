from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Callable

import httpx
import pandas as pd
import pytz
import yfinance as yf

from data_source_manager import SourceHealthManager, FinMindQuotaManager
from stock_scanner import UNCLASSIFIED_INDUSTRY, load_price_metrics, load_recent_revenue_history, load_stock_universe

from progress_logger import now_timestamp

# Singletons for health and quota tracking
_CHIP_HEALTH = SourceHealthManager()
_FINMIND_QUOTA = FinMindQuotaManager()
_HOLIDAY_DATES_CACHE: dict[int, set[date]] = {}


ROOT_DIR = Path(__file__).resolve().parent
CACHE_DIR = ROOT_DIR / ".cache"
TDCC_CACHE_DIR = CACHE_DIR / "tdcc"
DAILY_CHIP_CACHE_DIR = CACHE_DIR / "chip_daily"
STATE_PATH = CACHE_DIR / "chip_strategy_state.json"
TIMEZONE = pytz.timezone("Asia/Taipei")

TWSE_FUND_T86_URL = "https://www.twse.com.tw/fund/T86"
TWSE_FOREIGN_HOLDING_URL = "https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS"
TWSE_NAME_API_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_NAME_API_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
TPEX_QFII_URL = "https://www.tpex.org.tw/www/zh-tw/insti/qfiiStat"
TPEX_SITC_URL = "https://www.tpex.org.tw/www/zh-tw/insti/sitcStat"
TPEX_OPENAPI_DAILY_TRADING_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
TPEX_OPENAPI_QFII_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_qfii"
TPEX_MAINBOARD_QUOTES_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TPEX_ESB_QUOTES_URL = "https://www.tpex.org.tw/openapi/v1/tpex_esb_daily_close_quotes"
TWSE_HOLIDAY_URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
TDCC_DISTRIBUTION_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"

HARD_FILTERS = {
    "min_price": 10.0,
    "max_price": 80.0,
    "min_avg_volume_20d": 500.0,
    "min_monthly_revenue": 50_000_000.0,
}

TARGET_DAILY_TRADING_DAYS = 60
TRADING_DAY_LOOKBACK = 120
TDCC_TARGET_WEEKS = 8
SOURCE_COOLDOWN_SECONDS = 30 * 60
FINMIND_MAX_FALLBACK_STOCKS_PER_DATE = 50
DAILY_NET_DIRECT_FINMIND_THRESHOLD = 3
TWSE_FOREIGN_RATIO_DIRECT_FINMIND_THRESHOLD = 3
CHIP_PROGRESS_DEFAULT_LABEL = "籌碼資料"
SOURCE_MIN_INTERVAL_SECONDS = {
    "twse_t86": 3.0,
    "twse_mi_qfiis": 3.0,
    "tpex_openapi_daily_trading": 3.0,
    "tpex_openapi_qfii": 3.0,
    "tpex_qfii": 3.0,
    "tpex_sitc": 3.0,
    "finmind": 1.2,
    "tdcc": 3.0,
}
SOURCE_BACKOFF_SECONDS = [60, 300, SOURCE_COOLDOWN_SECONDS]
SOURCE_LABELS = {
    "twse_t86": "TWSE",
    "twse_mi_qfiis": "TWSE",
    "tpex_openapi_daily_trading": "TPEX OpenAPI",
    "tpex_openapi_qfii": "TPEX OpenAPI",
    "tpex_qfii": "TPEX",
    "tpex_sitc": "TPEX",
    "finmind": "FinMind",
    "tdcc": "TDCC",
}


def _print_chip_progress(label: str, progress: float, message: str) -> None:
    print(f"[{now_timestamp()}] [選股進度][{label}] {progress:.2f}% {message}", flush=True)


def _chip_progress_value(
    progress_start: float,
    progress_end: float,
    collected_days: int,
    checked_days: int,
    target_days: int = TARGET_DAILY_TRADING_DAYS,
) -> float:
    collected_fraction = min(1.0, collected_days / max(1, target_days))
    checked_fraction = min(1.0, checked_days / max(1, TRADING_DAY_LOOKBACK))
    fraction = min(0.98, collected_fraction * 0.85 + checked_fraction * 0.15)
    return progress_start + (progress_end - progress_start) * fraction

TDCC_RETAIL_BUCKETS = {1, 2, 3, 4, 5, 6, 7, 8}
TDCC_BIG_BUCKETS = {12, 13, 14, 15}
TWSE_FOREIGN_RATIO_SELECT_TYPES = [
    "01",
    "02",
    "03",
    "04",
    "05",
    "06",
    "08",
    "09",
    "10",
    "11",
    "12",
    "14",
    "15",
    "16",
    "17",
    "18",
    "20",
    "21",
    "22",
    "23",
    "24",
    "25",
    "26",
    "27",
    "28",
    "29",
    "30",
    "31",
    "32",
    "33",
    "34",
    "35",
    "36",
    "37",
    "38",
    "80",
]

STRATEGY_DEFINITIONS = {
    "financial": {"menu": "財報營收選股"},
    "chip_1": {"menu": "60 日法人動態選股"},
    "chip_2": {"menu": "投信認養股"},
    "chip_3": {"menu": "法人持股比例增加"},
    "chip_4": {"menu": "每週大戶持股選股"},
    "all": {"menu": "全部執行"},
}


@dataclass(frozen=True)
class HardFilterCandidate:
    code: str
    symbol: str
    market: str
    name: str
    industry: str
    price: float
    avg_volume_20d: float
    monthly_revenue: float
    issued_shares: float

    @property
    def display_name(self) -> str:
        return f"{self.code} {self.name}".strip()


@dataclass(frozen=True)
class ChipMarketContext:
    report_date: date
    latest_trading_date: date | None
    total_symbols: int
    scan_settings: dict[str, float]
    candidates: pd.DataFrame
    daily_data: pd.DataFrame
    weekly_data: pd.DataFrame


def get_tw_today() -> date:
    return datetime.now(TIMEZONE).date()


def _ensure_cache_dirs() -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    TDCC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_CHIP_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


SOURCE_COOLDOWNS: dict[str, datetime] = {}
SOURCE_FAILURE_COUNTS: dict[str, int] = {}
SOURCE_LAST_REQUEST_AT: dict[str, float] = {}
TPEX_OPENAPI_AVAILABLE_DATES: dict[str, set[date]] = {}
SOURCE_LOCK = Lock()


def _is_source_available(source_key: str) -> bool:
    cooldown_until = SOURCE_COOLDOWNS.get(source_key)
    if cooldown_until is None:
        return True
    return datetime.now(TIMEZONE) >= cooldown_until


def _mark_source_cooldown(source_key: str) -> None:
    SOURCE_COOLDOWNS[source_key] = datetime.now(TIMEZONE) + timedelta(seconds=SOURCE_COOLDOWN_SECONDS)


def _mark_source_failure(source_key: str) -> None:
    failures = SOURCE_FAILURE_COUNTS.get(source_key, 0) + 1
    SOURCE_FAILURE_COUNTS[source_key] = failures
    backoff_seconds = SOURCE_BACKOFF_SECONDS[min(failures - 1, len(SOURCE_BACKOFF_SECONDS) - 1)]
    SOURCE_COOLDOWNS[source_key] = datetime.now(TIMEZONE) + timedelta(seconds=backoff_seconds)
    print(
        f"[{now_timestamp()}] [資料來源][{SOURCE_LABELS.get(source_key, source_key)}] 失敗 {failures} 次，暫停 {backoff_seconds // 60 if backoff_seconds >= 60 else backoff_seconds} {'分鐘' if backoff_seconds >= 60 else '秒'}",
        flush=True,
    )


def _mark_source_success(source_key: str) -> None:
    SOURCE_FAILURE_COUNTS.pop(source_key, None)
    SOURCE_COOLDOWNS.pop(source_key, None)


def _wait_for_source_slot(source_key: str) -> None:
    interval = SOURCE_MIN_INTERVAL_SECONDS.get(source_key, 3.0)
    with SOURCE_LOCK:
        now = time.monotonic()
        wait_seconds = interval - (now - SOURCE_LAST_REQUEST_AT.get(source_key, 0.0))
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        SOURCE_LAST_REQUEST_AT[source_key] = time.monotonic()


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text or text in {"--", "---", "nan", "None", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_lots_from_shares(value: Any) -> float | None:
    parsed = _to_float(value)
    if parsed is None:
        return None
    return parsed / 1000.0


def _normalize_code(value: Any) -> str:
    return str(value or "").strip()


def _row_value(row: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    for key in candidates:
        if key in row:
            return row.get(key)
    normalized = {str(key).replace(" ", "").replace("\n", "").strip(): value for key, value in row.items()}
    for key in candidates:
        compact = key.replace(" ", "").replace("\n", "").strip()
        if compact in normalized:
            return normalized[compact]
    return None


def _parse_tpex_openapi_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    compact = text.replace("/", "").replace("-", "")
    if compact.isdigit() and len(compact) == 7:
        try:
            return date(int(compact[:3]) + 1911, int(compact[3:5]), int(compact[5:7]))
        except ValueError:
            return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    parts = text.replace("-", "/").split("/")
    if len(parts) == 3:
        try:
            year = int(parts[0])
            if year < 1911:
                year += 1911
            return date(year, int(parts[1]), int(parts[2]))
        except ValueError:
            return None
    return None


def _remember_tpex_openapi_dates(source_key: str, payload: Any) -> set[date]:
    if not isinstance(payload, list):
        return set()
    dates = {
        parsed
        for row in payload
        if isinstance(row, dict)
        for parsed in [_parse_tpex_openapi_date(_row_value(row, ("Date", "date", "日期", "資料日期")))]
        if parsed is not None
    }
    if dates:
        TPEX_OPENAPI_AVAILABLE_DATES[source_key] = dates
    return dates


def _should_try_tpex_openapi(source_key: str, target_date: date) -> bool:
    known_dates = TPEX_OPENAPI_AVAILABLE_DATES.get(source_key)
    return not known_dates or target_date in known_dates


def _format_price(price: float) -> str:
    return f"{price:,.1f}".rstrip("0").rstrip(".")


def _format_report_members(members: list[str], empty_text: str = "無符合標的") -> str:
    if not members:
        return empty_text
    return " | ".join(members)


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


def _render_bucket(title: str, industry_groups: dict[str, list[str]]) -> list[str]:
    lines = ["", title]
    if not industry_groups:
        lines.extend([_format_report_members([]), ""])
        return lines

    for industry in sorted(industry_groups, key=lambda value: (value == UNCLASSIFIED_INDUSTRY, value)):
        lines.append(f"  【{industry}】")
        lines.extend(_wrap_report_members(industry_groups[industry]))
        lines.append("")
    return lines


def _load_holiday_dates(target_year: int) -> set[date]:
    if target_year in _HOLIDAY_DATES_CACHE:
        return _HOLIDAY_DATES_CACHE[target_year]
    try:
        rows = httpx.get(TWSE_HOLIDAY_URL, timeout=20.0, follow_redirects=True, verify=False).json()
    except Exception:
        return set()

    holidays: set[date] = set()
    for row in rows:
        raw_date = str(row.get("Date") or "").strip()
        if not raw_date:
            continue
        description_text = f"{row.get('Name') or ''} {row.get('Description') or ''}"
        if "開始交易" in description_text or "最後交易" in description_text:
            continue
        parsed = _parse_tpex_openapi_date(raw_date)
        if parsed is None:
            continue
        if parsed.year == target_year:
            holidays.add(parsed)
    _HOLIDAY_DATES_CACHE[target_year] = holidays
    return holidays


def is_possible_trading_day(target_date: date) -> bool:
    if target_date.weekday() >= 5:
        return False
    holidays = _load_holiday_dates(target_date.year)
    return target_date not in holidays


def _fetch_json(client: httpx.Client, url: str, params: dict[str, Any] | None = None) -> Any:
    response = client.get(url, params=params)
    response.raise_for_status()
    return response.json()


def _fetch_source_json(
    client: httpx.Client,
    source_key: str,
    url: str,
    params: dict[str, Any] | None = None,
) -> Any:
    if not _is_source_available(source_key):
        raise RuntimeError(f"{source_key} is cooling down")
    _wait_for_source_slot(source_key)
    response = client.get(
        url,
        params=params,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.twse.com.tw/",
        },
    )
    if response.status_code in {301, 302, 303, 307, 308, 429}:
        _mark_source_failure(source_key)
        raise RuntimeError(f"{source_key} blocked or redirected: HTTP {response.status_code}")
    try:
        response.raise_for_status()
        payload = response.json()
    except Exception:
        _mark_source_failure(source_key)
        raise
    _mark_source_success(source_key)
    return payload


def _daily_chip_cache_path(target_date: date) -> Path:
    return DAILY_CHIP_CACHE_DIR / f"{target_date.strftime('%Y%m%d')}.csv"


def _chip_column_as_series(frame: pd.DataFrame, column: str) -> pd.Series:
    value = frame[column]
    if isinstance(value, pd.DataFrame):
        if value.empty:
            return pd.Series(pd.NA, index=frame.index)
        value = value.iloc[:, 0]
    if isinstance(value, pd.Series):
        return value
    return pd.Series(value, index=frame.index)


def _normalize_daily_chip_frame(frame: pd.DataFrame, target_date: date | None = None) -> pd.DataFrame:
    columns = ["date", "code", "market", "foreign_net_lots", "trust_net_lots", "foreign_ratio_pct", "source"]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    source_frame = frame.copy()
    for column in columns:
        if column not in source_frame.columns:
            source_frame[column] = pd.NA

    if target_date is not None:
        source_frame["date"] = target_date

    normalized = pd.DataFrame(index=source_frame.index)
    normalized["date"] = pd.to_datetime(_chip_column_as_series(source_frame, "date"), errors="coerce").dt.date
    normalized["code"] = _chip_column_as_series(source_frame, "code").astype(str).str.strip()
    normalized["market"] = _chip_column_as_series(source_frame, "market").astype(str).replace({"nan": ""})
    normalized["source"] = _chip_column_as_series(source_frame, "source").fillna("cache").astype(str).str.strip()
    normalized.loc[normalized["source"].isin(["", "nan", "None"]), "source"] = "cache"
    for column in ("foreign_net_lots", "trust_net_lots", "foreign_ratio_pct"):
        normalized[column] = pd.to_numeric(_chip_column_as_series(source_frame, column), errors="coerce")

    normalized = normalized[normalized["code"] != ""]
    normalized = normalized.dropna(subset=["date", "code"])
    return normalized[columns].sort_values(["date", "code", "market"]).reset_index(drop=True)


def _load_daily_chip_cache(target_date: date, candidate_codes: set[str]) -> pd.DataFrame:
    _ensure_cache_dirs()
    cache_path = _daily_chip_cache_path(target_date)
    if not cache_path.exists():
        return pd.DataFrame()
    try:
        frame = pd.read_csv(cache_path, dtype={"code": str})
    except Exception:
        return pd.DataFrame()
    frame = _normalize_daily_chip_frame(frame, target_date=target_date)
    if candidate_codes:
        frame = frame[frame["code"].isin(candidate_codes)].copy()
    return frame.reset_index(drop=True)


def _save_daily_chip_cache(target_date: date, frame: pd.DataFrame) -> None:
    if frame.empty:
        return
    _ensure_cache_dirs()
    cache_path = _daily_chip_cache_path(target_date)
    new_frame = _normalize_daily_chip_frame(frame, target_date=target_date)
    if new_frame.empty:
        return

    if cache_path.exists():
        try:
            old_frame = _normalize_daily_chip_frame(pd.read_csv(cache_path, dtype={"code": str}), target_date=target_date)
        except Exception:
            old_frame = pd.DataFrame()
        merged = pd.concat([old_frame, new_frame], ignore_index=True)
    else:
        merged = new_frame

    merged = merged.sort_values(["code", "market"]).drop_duplicates(["date", "code", "market"], keep="last")
    merged.to_csv(cache_path, index=False, encoding="utf-8")


def _finmind_payload(client: httpx.Client, params: dict[str, Any], scope: str = "default") -> list[dict[str, Any]]:
    # Check health cooldown and quota before sending request
    if not _CHIP_HEALTH.is_available("finmind"):
        raise RuntimeError("FinMind source currently in cooldown")
    if not _FINMIND_QUOTA.can_use(cost=1, scope=scope):
        raise RuntimeError("FinMind quota exceeded (500/hour safe limit)")
    payload = _fetch_source_json(client, "finmind", FINMIND_API_URL, params=params)
    if payload.get("status") != 200:
        _mark_source_failure("finmind")
        raise RuntimeError(str(payload.get("msg") or "FinMind request failed"))
    _FINMIND_QUOTA.record_use(cost=1, scope=scope)
    data = payload.get("data") or []
    return data if isinstance(data, list) else []


def _fallback_shares_outstanding(symbols: list[str]) -> dict[str, float]:
    results: dict[str, float] = {}
    for symbol in symbols:
        try:
            shares_outstanding = yf.Ticker(symbol).info.get("sharesOutstanding")
        except Exception:
            shares_outstanding = None
        if shares_outstanding:
            results[symbol] = float(shares_outstanding)
    return results


def _load_issued_shares_map(universe: list[Any]) -> dict[str, float]:
    issued_shares: dict[str, float] = {}
    missing_tpex_symbols: list[str] = []

    with httpx.Client(timeout=20.0, follow_redirects=True, verify=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
        try:
            for row in _fetch_json(client, TWSE_NAME_API_URL):
                code = _normalize_code(row.get("公司代號"))
                raw_shares = row.get("已發行普通股數或TDR原股發行股數") or row.get("已發行普通股數")
                shares = _to_float(raw_shares)
                if code and shares:
                    issued_shares[code] = shares
        except Exception:
            pass

        tpex_quote_rows: list[dict[str, Any]] = []
        for url in (TPEX_MAINBOARD_QUOTES_URL, TPEX_ESB_QUOTES_URL):
            try:
                tpex_quote_rows.extend(_fetch_json(client, url))
            except Exception:
                continue

        for row in tpex_quote_rows:
            code = _normalize_code(row.get("SecuritiesCompanyCode"))
            shares = _to_float(row.get("Capitals"))
            if code and shares:
                issued_shares[code] = shares

        if len(issued_shares) < len(universe):
            try:
                for row in _fetch_json(client, TPEX_NAME_API_URL):
                    code = _normalize_code(row.get("SecuritiesCompanyCode"))
                    shares = _to_float(
                        row.get("Capitals")
                        or row.get("股本")
                        or row.get("已發行股份總數")
                        or row.get("IssuedShares")
                    )
                    if code and shares:
                        issued_shares[code] = shares
            except Exception:
                pass

    for entry in universe:
        if entry.code not in issued_shares and entry.market == "TPEX":
            missing_tpex_symbols.append(entry.symbol)

    if missing_tpex_symbols:
        fallback_map = _fallback_shares_outstanding(missing_tpex_symbols)
        for entry in universe:
            shares = fallback_map.get(entry.symbol)
            if shares:
                issued_shares[entry.code] = shares

    return issued_shares


def _build_hard_filter_candidates(report_date: date, force_refresh: bool = False) -> pd.DataFrame:
    universe = load_stock_universe(force_refresh=force_refresh)
    revenue_history = load_recent_revenue_history(universe)
    price_metrics = load_price_metrics(universe, force_refresh=force_refresh)
    issued_shares = _load_issued_shares_map(universe)

    rows: list[dict[str, Any]] = []
    for entry in universe:
        revenue_points = revenue_history.get(entry.code)
        price_metric = price_metrics.get(entry.symbol)
        latest_revenue = revenue_points[0].revenue if revenue_points else None
        price = price_metric.get("price") if price_metric else None
        avg_volume_20d = price_metric.get("avg_volume_20d") if price_metric else None
        shares = issued_shares.get(entry.code)

        if latest_revenue is None or price is None or avg_volume_20d is None or not shares:
            continue
        if not (HARD_FILTERS["min_price"] < float(price) < HARD_FILTERS["max_price"]):
            continue
        if float(avg_volume_20d) <= HARD_FILTERS["min_avg_volume_20d"]:
            continue
        if float(latest_revenue) <= HARD_FILTERS["min_monthly_revenue"]:
            continue

        rows.append(
            {
                "code": entry.code,
                "symbol": entry.symbol,
                "market": entry.market,
                "name": entry.name,
                "industry": entry.industry or UNCLASSIFIED_INDUSTRY,
                "price": round(float(price), 2),
                "avg_volume_20d": round(float(avg_volume_20d), 2),
                "monthly_revenue": float(latest_revenue),
                "issued_shares": float(shares),
            }
        )

    frame = pd.DataFrame(rows)
    frame.attrs["total_symbols"] = len(universe)
    frame.attrs["scan_settings"] = dict(HARD_FILTERS)
    if frame.empty:
        return frame
    return frame.sort_values("code").reset_index(drop=True)


def _fetch_twse_net_buy_for_date(client: httpx.Client, target_date: date, candidate_codes: set[str]) -> pd.DataFrame:
    payload = _fetch_source_json(
        client,
        "twse_t86",
        TWSE_FUND_T86_URL,
        params={"response": "json", "date": target_date.strftime("%Y%m%d"), "selectType": "ALL"},
    )
    if payload.get("stat") != "OK":
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for row in payload.get("data", []):
        code = _normalize_code(row[0])
        if code not in candidate_codes:
            continue
        rows.append(
            {
                "date": target_date,
                "code": code,
                "foreign_net_lots": _to_lots_from_shares(row[4]) or 0.0,
                "trust_net_lots": _to_lots_from_shares(row[10]) or 0.0,
                "source": "TWSE",
            }
        )
    return pd.DataFrame(rows)


def _fetch_twse_foreign_ratio_for_date(client: httpx.Client, target_date: date, candidate_codes: set[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    remaining_codes = set(candidate_codes)
    for select_type in TWSE_FOREIGN_RATIO_SELECT_TYPES:
        if not remaining_codes:
            break
        payload = _fetch_source_json(
            client,
            "twse_mi_qfiis",
            TWSE_FOREIGN_HOLDING_URL,
            params={"response": "json", "date": target_date.strftime("%Y%m%d"), "selectType": select_type},
        )
        if payload.get("stat") != "OK":
            continue

        for row in payload.get("data", []):
            code = _normalize_code(row[0])
            if code not in remaining_codes:
                continue
            ratio = _to_float(row[7])
            rows.append({"date": target_date, "code": code, "foreign_ratio_pct": ratio, "source": "TWSE"})
            remaining_codes.discard(code)
    return pd.DataFrame(rows)


def _fetch_tpex_openapi_net_buy_for_date(
    client: httpx.Client,
    target_date: date,
    candidate_codes: set[str],
) -> pd.DataFrame:
    source_key = "tpex_openapi_daily_trading"
    payload = _fetch_source_json(
        client,
        source_key,
        TPEX_OPENAPI_DAILY_TRADING_URL,
    )
    if not isinstance(payload, list):
        return pd.DataFrame()
    available_dates = _remember_tpex_openapi_dates(source_key, payload)
    if available_dates and target_date not in available_dates:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        row_date = _parse_tpex_openapi_date(
            _row_value(row, ("Date", "date", "日期", "資料日期"))
        )
        if row_date != target_date:
            continue
        code = _normalize_code(
            _row_value(
                row,
                ("SecuritiesCompanyCode", "SecuritiesCode", "Code", "code", "股票代號", "證券代號", "代號"),
            )
        )
        if code not in candidate_codes:
            continue
        foreign_net = _to_lots_from_shares(
            _row_value(
                row,
                (
                    "ForeignNetBuySell",
                    "ForeignNetBuySellShares",
                    "NetForeignPurchasesSales",
                    "ForeignInvestorsInclude MainlandAreaInvestors-Difference",
                    "ForeignInvestorsIncludeMainlandAreaInvestors-Difference",
                    "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference",
                    "外資及陸資買賣超股數",
                    "外資及陸資買賣超",
                    "外資買賣超股數",
                    "外資買賣超",
                ),
            )
        )
        trust_net = _to_lots_from_shares(
            _row_value(
                row,
                (
                    "InvestmentTrustNetBuySell",
                    "InvestmentTrustNetBuySellShares",
                    "NetInvestmentTrustPurchasesSales",
                    "SecuritiesInvestmentTrustCompanies-Difference",
                    "投信買賣超股數",
                    "投信買賣超",
                ),
            )
        )
        if foreign_net is None and trust_net is None:
            continue
        rows.append(
            {
                "date": target_date,
                "code": code,
                "market": "TPEX",
                "foreign_net_lots": foreign_net or 0.0,
                "trust_net_lots": trust_net or 0.0,
                "source": "TPEX_OpenAPI",
            }
        )
    return pd.DataFrame(rows)


def _fetch_tpex_openapi_foreign_ratio_for_date(
    client: httpx.Client,
    target_date: date,
    candidate_codes: set[str],
) -> pd.DataFrame:
    source_key = "tpex_openapi_qfii"
    payload = _fetch_source_json(
        client,
        source_key,
        TPEX_OPENAPI_QFII_URL,
    )
    if not isinstance(payload, list):
        return pd.DataFrame()
    available_dates = _remember_tpex_openapi_dates(source_key, payload)
    if available_dates and target_date not in available_dates:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        row_date = _parse_tpex_openapi_date(
            _row_value(row, ("Date", "date", "日期", "資料日期"))
        )
        if row_date != target_date:
            continue
        code = _normalize_code(
            _row_value(
                row,
                ("SecuritiesCompanyCode", "SecuritiesCode", "Code", "code", "股票代號", "證券代號", "代號"),
            )
        )
        if code not in candidate_codes:
            continue
        ratio = _to_float(
            _row_value(
                row,
                (
                    "ForeignHoldingRatio",
                    "QFIIRatio",
                    "ForeignInvestmentRatio",
                    "PercentageOfSharesOC/FMIHeld",
                    "僑外資及陸資持股比率",
                    "外資持股比率",
                    "持股比率",
                    "比率",
                ),
            )
        )
        if ratio is None:
            continue
        rows.append(
            {
                "date": target_date,
                "code": code,
                "foreign_ratio_pct": ratio,
                "source": "TPEX_OpenAPI",
            }
        )
    return pd.DataFrame(rows)


def _fetch_tpex_net_payload(client: httpx.Client, target_date: date, url: str, sort_key: str, source_key: str) -> dict[str, float]:
    roc_date = f"{target_date.year - 1911:03d}/{target_date.month:02d}/{target_date.day:02d}"
    net_map: dict[str, float] = {}
    for search_type in ("buy", "sell"):
        try:
            payload = _fetch_source_json(
                client,
                source_key,
                url,
                params={"date": roc_date, "type": "Daily", sort_key: search_type},
            )
        except Exception:
            if net_map:
                continue
            raise
        table = (payload.get("tables") or [{}])[0]
        for row in table.get("data", []):
            code = _normalize_code(row[1])
            net_value = _to_float(row[-1])
            if code and net_value is not None:
                net_map[code] = float(net_value)
    return net_map


def _fetch_tpex_net_buy_for_date(client: httpx.Client, target_date: date, candidate_codes: set[str]) -> pd.DataFrame:
    try:
        foreign_map = _fetch_tpex_net_payload(client, target_date, TPEX_QFII_URL, "searchType", "tpex_qfii")
        trust_map = _fetch_tpex_net_payload(client, target_date, TPEX_SITC_URL, "searchType", "tpex_sitc")
    except Exception:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for code in sorted(candidate_codes):
        if code not in foreign_map and code not in trust_map:
            continue
        rows.append(
            {
                "date": target_date,
                "code": code,
                "foreign_net_lots": float(foreign_map.get(code, 0.0)),
                "trust_net_lots": float(trust_map.get(code, 0.0)),
                "source": "TPEX",
            }
        )
    return pd.DataFrame(rows)


def _fetch_finmind_net_buy_for_stock(
    client: httpx.Client,
    target_date: date,
    code: str,
    market: str,
    scope: str = "default",
) -> pd.DataFrame:
    data = _finmind_payload(
        client,
        {
            "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
            "data_id": code,
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
        },
        scope=scope,
    )
    foreign_net_shares = 0.0
    trust_net_shares = 0.0
    has_row = False
    for row in data:
        if str(row.get("date")) != target_date.isoformat():
            continue
        name = str(row.get("name") or "")
        buy = _to_float(row.get("buy")) or 0.0
        sell = _to_float(row.get("sell")) or 0.0
        if name == "Foreign_Investor":
            foreign_net_shares += buy - sell
            has_row = True
        elif name == "Investment_Trust":
            trust_net_shares += buy - sell
            has_row = True

    if not has_row:
        return pd.DataFrame()
    return pd.DataFrame(
        [
            {
                "date": target_date,
                "code": code,
                "market": market,
                "foreign_net_lots": foreign_net_shares / 1000.0,
                "trust_net_lots": trust_net_shares / 1000.0,
                "source": "FinMind",
            }
        ]
    )


def _fetch_finmind_net_buy_for_codes(
    client: httpx.Client,
    target_date: date,
    code_market_map: dict[str, str],
    progress_callback: Callable[[int, int, str], None] | None = None,
    scope: str = "default",
) -> pd.DataFrame:
    if not _is_source_available("finmind"):
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    items = sorted(code_market_map.items())[:FINMIND_MAX_FALLBACK_STOCKS_PER_DATE]
    total = len(items)
    for index, (code, market) in enumerate(items, start=1):
        if progress_callback and (index == 1 or index % 5 == 0 or index == total):
            progress_callback(index, total, f"FinMind 法人買賣超補資料 {target_date.isoformat()} {index}/{total}")
        try:
            frame = _fetch_finmind_net_buy_for_stock(client, target_date, code, market, scope=scope)
        except Exception:
            continue
        if not frame.empty:
            frames.append(frame)
            _save_daily_chip_cache(target_date, frame)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _fetch_finmind_foreign_ratio_for_stock(
    client: httpx.Client,
    target_date: date,
    code: str,
    scope: str = "default",
) -> pd.DataFrame:
    data = _finmind_payload(
        client,
        {
            "dataset": "TaiwanStockShareholding",
            "data_id": code,
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
        },
        scope=scope,
    )
    for row in data:
        if str(row.get("date")) != target_date.isoformat():
            continue
        ratio = (
            _to_float(row.get("ForeignInvestmentSharesRatio"))
            or _to_float(row.get("foreign_investment_shares_ratio"))
        )
        if ratio is not None:
            return pd.DataFrame([{"date": target_date, "code": code, "foreign_ratio_pct": ratio, "source": "FinMind"}])
    return pd.DataFrame()


def _fetch_finmind_foreign_ratio_for_codes(
    client: httpx.Client,
    target_date: date,
    codes: set[str],
    progress_callback: Callable[[int, int, str], None] | None = None,
    scope: str = "default",
) -> pd.DataFrame:
    if not _is_source_available("finmind"):
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    code_list = sorted(codes)[:FINMIND_MAX_FALLBACK_STOCKS_PER_DATE]
    total = len(code_list)
    for index, code in enumerate(code_list, start=1):
        if progress_callback and (index == 1 or index % 5 == 0 or index == total):
            progress_callback(index, total, f"FinMind 外資持股比例補資料 {target_date.isoformat()} {index}/{total}")
        try:
            frame = _fetch_finmind_foreign_ratio_for_stock(client, target_date, code, scope=scope)
        except Exception:
            continue
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _fetch_recent_daily_chip_data(
    report_date: date,
    candidates: pd.DataFrame,
    progress_label: str = CHIP_PROGRESS_DEFAULT_LABEL,
    progress_start: float = 0.0,
    progress_end: float = 100.0,
    target_trading_days: int = TARGET_DAILY_TRADING_DAYS,
    include_foreign_ratio: bool = True,
    scope: str = "default",
) -> tuple[pd.DataFrame, date | None]:
    if candidates.empty:
        return pd.DataFrame(), None

    _print_chip_progress(progress_label, progress_start, f"準備法人日資料，候選 {len(candidates)} 檔")
    if not include_foreign_ratio:
        _print_chip_progress(progress_label, progress_start, "本次策略不需要外資持股比例，略過 MI_QFIIS / FinMind 持股比例補資料")
    candidate_codes = set(candidates["code"].astype(str).tolist())
    twse_codes = set(candidates.loc[candidates["market"] == "TWSE", "code"].tolist())
    tpex_codes = set(candidates.loc[candidates["market"] == "TPEX", "code"].tolist())
    code_market_map = dict(zip(candidates["code"].astype(str), candidates["market"].astype(str)))
    collected_dates: list[date] = []
    collected_frames: list[pd.DataFrame] = []

    calendar = pd.bdate_range(end=pd.Timestamp(report_date), periods=TRADING_DAY_LOOKBACK).date[::-1]
    with httpx.Client(timeout=20.0, follow_redirects=True, verify=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for checked_index, target_date in enumerate(calendar, start=1):
            # Extra safety: skip weekends and Taiwan market holidays if calendar ever contains them.
            if not is_possible_trading_day(target_date):
                _print_chip_progress(progress_label, _chip_progress_value(progress_start, progress_end, len(collected_dates), checked_index, target_trading_days), f"略過週末非交易日 {target_date.isoformat()}")
                continue
            progress = _chip_progress_value(progress_start, progress_end, len(collected_dates), checked_index, target_trading_days)
            _print_chip_progress(
                progress_label,
                progress,
                f"檢查 {target_date.isoformat()}，已收集 {len(collected_dates)}/{target_trading_days} 個交易日",
            )

            def progress_callback(index: int, total: int, message: str) -> None:
                finmind_fraction = index / max(1, total)
                callback_progress = min(progress_end - 0.01, progress + (progress_end - progress) * 0.015 * finmind_fraction)
                _print_chip_progress(progress_label, callback_progress, message)

            cached_net = _load_daily_chip_cache(target_date, candidate_codes)
            cached_codes = set(cached_net["code"].tolist()) if not cached_net.empty else set()
            missing_twse_codes = twse_codes - cached_codes
            missing_tpex_codes = tpex_codes - cached_codes
            if cached_codes:
                _print_chip_progress(
                    progress_label,
                    progress,
                    f"本機快取 {target_date.isoformat()} 已有 {len(cached_codes)}/{len(candidate_codes)} 檔，僅補缺口",
                )
            else:
                _print_chip_progress(
                    progress_label,
                    progress,
                    f"本機快取 {target_date.isoformat()} 無可用資料，準備抓官方資料",
                )

            fetched_frames: list[pd.DataFrame] = []
            if missing_twse_codes:
                if len(missing_twse_codes) <= DAILY_NET_DIRECT_FINMIND_THRESHOLD:
                    _print_chip_progress(
                        progress_label,
                        progress,
                        f"上市法人買賣超缺口僅 {len(missing_twse_codes)} 檔，略過 TWSE T86 批次查詢，直接改用 FinMind 單檔補資料",
                    )
                    twse_net = pd.DataFrame()
                elif _is_source_available("twse_t86"):
                    _print_chip_progress(
                        progress_label,
                        progress,
                        f"嘗試 TWSE T86 {target_date.isoformat()}，上市缺口 {len(missing_twse_codes)} 檔",
                    )
                    try:
                        twse_net = _fetch_twse_net_buy_for_date(client, target_date, missing_twse_codes)
                    except Exception as exc:
                        _print_chip_progress(
                            progress_label,
                            progress,
                            f"TWSE T86 {target_date.isoformat()} 失敗，將改用 FinMind 補上市缺口：{exc}",
                        )
                        twse_net = pd.DataFrame()
                else:
                    _print_chip_progress(
                        progress_label,
                        progress,
                        f"TWSE T86 目前冷卻中，{target_date.isoformat()} 上市缺口改用 FinMind",
                    )
                    twse_net = pd.DataFrame()
                if not twse_net.empty:
                    twse_net = twse_net.assign(market="TWSE")
                    fetched_frames.append(twse_net)
                    _save_daily_chip_cache(target_date, twse_net)
                    _print_chip_progress(
                        progress_label,
                        progress,
                        f"TWSE T86 {target_date.isoformat()} 成功，補到 {len(twse_net)}/{len(missing_twse_codes)} 檔",
                    )
                else:
                    _print_chip_progress(
                        progress_label,
                        progress,
                        f"TWSE T86 {target_date.isoformat()} 未補到上市缺口",
                    )

                fetched_twse_codes = set(twse_net["code"].tolist()) if not twse_net.empty else set()
                finmind_twse_codes = missing_twse_codes - fetched_twse_codes
                if finmind_twse_codes:
                    _print_chip_progress(
                        progress_label,
                        progress,
                        f"剩餘上市缺口 {len(finmind_twse_codes)} 檔，改用 FinMind 單檔補資料",
                    )
                    finmind_map = {code: code_market_map[code] for code in finmind_twse_codes if code in code_market_map}
                    finmind_net = _fetch_finmind_net_buy_for_codes(
                        client,
                        target_date,
                        finmind_map,
                        progress_callback,
                        scope=scope,
                    )
                    if not finmind_net.empty:
                        fetched_frames.append(finmind_net)

            if missing_tpex_codes:
                _print_chip_progress(
                    progress_label,
                    progress,
                    f"嘗試 TPEX 官方法人資料 {target_date.isoformat()}，上櫃缺口 {len(missing_tpex_codes)} 檔",
                )
                if not _should_try_tpex_openapi("tpex_openapi_daily_trading", target_date):
                    _print_chip_progress(
                        progress_label,
                        progress,
                        f"TPEx OpenAPI 法人資料已知不含 {target_date.isoformat()}，略過 OpenAPI，直接改用舊版 TPEx fallback",
                    )
                    tpex_net = pd.DataFrame()
                else:
                    try:
                        _print_chip_progress(
                            progress_label,
                            progress,
                            f"嘗試 TPEx OpenAPI 法人資料 {target_date.isoformat()}，上櫃缺口 {len(missing_tpex_codes)} 檔",
                        )
                        tpex_net = _fetch_tpex_openapi_net_buy_for_date(client, target_date, missing_tpex_codes)
                    except Exception as exc:
                        _print_chip_progress(
                            progress_label,
                            progress,
                            f"TPEx OpenAPI 法人資料 {target_date.isoformat()} 失敗，將改用舊版 TPEx fallback：{exc}",
                        )
                        tpex_net = pd.DataFrame()
                if tpex_net.empty:
                    _print_chip_progress(
                        progress_label,
                        progress,
                        f"TPEx OpenAPI 法人資料 {target_date.isoformat()} 未補到上櫃缺口，改用舊版 TPEx fallback",
                    )
                    tpex_net = _fetch_tpex_net_buy_for_date(client, target_date, missing_tpex_codes)
                else:
                    _print_chip_progress(
                        progress_label,
                        progress,
                        f"TPEx OpenAPI 法人資料 {target_date.isoformat()} 成功，補到 {len(tpex_net)}/{len(missing_tpex_codes)} 檔",
                    )
                if not tpex_net.empty:
                    tpex_net = tpex_net.assign(market="TPEX")
                    fetched_frames.append(tpex_net)
                    _save_daily_chip_cache(target_date, tpex_net)
                    _print_chip_progress(
                        progress_label,
                        progress,
                        f"TPEX 官方法人資料 {target_date.isoformat()} 成功，補到 {len(tpex_net)}/{len(missing_tpex_codes)} 檔",
                    )
                else:
                    _print_chip_progress(
                        progress_label,
                        progress,
                        f"TPEX 官方法人資料 {target_date.isoformat()} 未補到上櫃缺口",
                    )

            date_frames = []
            if not cached_net.empty:
                date_frames.append(cached_net)
            date_frames.extend(fetched_frames)
            if not date_frames:
                continue

            date_net = pd.concat(date_frames, ignore_index=True)
            date_net = _normalize_daily_chip_frame(date_net, target_date=target_date)
            date_net = date_net.sort_values(["code", "market"]).drop_duplicates(["date", "code", "market"], keep="last")
            if date_net.empty:
                continue

            if include_foreign_ratio:
                tpex_date_codes = set(date_net.loc[date_net["market"] == "TPEX", "code"].tolist())
                tpex_cached_ratio_codes = set(
                    date_net.loc[
                        (date_net["market"] == "TPEX") & date_net["foreign_ratio_pct"].notna(),
                        "code",
                    ].tolist()
                )
                tpex_ratio_missing_codes = tpex_date_codes - tpex_cached_ratio_codes
                if tpex_ratio_missing_codes:
                    if not _should_try_tpex_openapi("tpex_openapi_qfii", target_date):
                        _print_chip_progress(
                            progress_label,
                            progress,
                            f"TPEx OpenAPI 外資持股比例已知不含 {target_date.isoformat()}，略過 OpenAPI，保留估算流程",
                        )
                        tpex_foreign_ratio = pd.DataFrame()
                    else:
                        try:
                            _print_chip_progress(
                                progress_label,
                                progress,
                                f"嘗試 TPEx OpenAPI 外資持股比例 {target_date.isoformat()}，上櫃缺口 {len(tpex_ratio_missing_codes)} 檔",
                            )
                            tpex_foreign_ratio = _fetch_tpex_openapi_foreign_ratio_for_date(
                                client,
                                target_date,
                                tpex_ratio_missing_codes,
                            )
                        except Exception as exc:
                            _print_chip_progress(
                                progress_label,
                                progress,
                                f"TPEx OpenAPI 外資持股比例 {target_date.isoformat()} 失敗，保留估算流程：{exc}",
                            )
                            tpex_foreign_ratio = pd.DataFrame()
                    if not tpex_foreign_ratio.empty:
                        ratio_update = tpex_foreign_ratio.drop_duplicates(["date", "code"], keep="last").rename(
                            columns={
                                "foreign_ratio_pct": "foreign_ratio_pct_update",
                                "source": "foreign_ratio_source",
                            }
                        )
                        date_net = date_net.merge(
                            ratio_update[["date", "code", "foreign_ratio_pct_update", "foreign_ratio_source"]],
                            on=["date", "code"],
                            how="left",
                        )
                        missing_ratio_mask = date_net["foreign_ratio_pct"].isna() & date_net["foreign_ratio_pct_update"].notna()
                        date_net["foreign_ratio_pct"] = date_net["foreign_ratio_pct"].fillna(
                            date_net["foreign_ratio_pct_update"]
                        )
                        date_net.loc[missing_ratio_mask, "source"] = (
                            date_net.loc[missing_ratio_mask, "source"].astype(str)
                            + "+"
                            + date_net.loc[missing_ratio_mask, "foreign_ratio_source"].astype(str)
                        )
                        date_net = date_net.drop(columns=["foreign_ratio_pct_update", "foreign_ratio_source"])
                        _print_chip_progress(
                            progress_label,
                            progress,
                            f"TPEx OpenAPI 外資持股比例 {target_date.isoformat()} 成功，補到 {len(tpex_foreign_ratio)}/{len(tpex_ratio_missing_codes)} 檔",
                        )
                    else:
                        _print_chip_progress(
                            progress_label,
                            progress,
                            f"TPEx OpenAPI 外資持股比例 {target_date.isoformat()} 未補到上櫃缺口，保留估算流程",
                        )
                twse_date_codes = set(date_net.loc[date_net["market"] == "TWSE", "code"].tolist())
                cached_ratio_codes = set(
                    date_net.loc[
                        (date_net["market"] == "TWSE") & date_net["foreign_ratio_pct"].notna(),
                        "code",
                    ].tolist()
                )
                ratio_missing_codes = twse_date_codes - cached_ratio_codes
                if ratio_missing_codes:
                    if len(ratio_missing_codes) <= TWSE_FOREIGN_RATIO_DIRECT_FINMIND_THRESHOLD:
                        _print_chip_progress(
                            progress_label,
                            progress,
                            f"外資持股比例缺口僅 {len(ratio_missing_codes)} 檔，略過 TWSE MI_QFIIS 分類掃描，直接改用 FinMind 單檔補資料",
                        )
                        foreign_ratio = pd.DataFrame()
                    else:
                        if _is_source_available("twse_mi_qfiis"):
                            _print_chip_progress(
                                progress_label,
                                progress,
                                f"嘗試 TWSE MI_QFIIS {target_date.isoformat()}，外資持股比例缺口 {len(ratio_missing_codes)} 檔",
                            )
                            try:
                                foreign_ratio = _fetch_twse_foreign_ratio_for_date(client, target_date, ratio_missing_codes)
                            except Exception as exc:
                                _print_chip_progress(
                                    progress_label,
                                    progress,
                                    f"TWSE MI_QFIIS {target_date.isoformat()} 失敗，將改用 FinMind 補持股比例：{exc}",
                                )
                                foreign_ratio = pd.DataFrame()
                        else:
                            _print_chip_progress(
                                progress_label,
                                progress,
                                f"TWSE MI_QFIIS 目前冷卻中，{target_date.isoformat()} 外資持股比例改用 FinMind",
                            )
                            foreign_ratio = pd.DataFrame()
                    fetched_ratio_codes = set(foreign_ratio["code"].tolist()) if not foreign_ratio.empty else set()
                    if fetched_ratio_codes:
                        _print_chip_progress(
                            progress_label,
                            progress,
                            f"TWSE MI_QFIIS {target_date.isoformat()} 成功，補到 {len(fetched_ratio_codes)}/{len(ratio_missing_codes)} 檔",
                        )
                    else:
                        _print_chip_progress(
                            progress_label,
                            progress,
                            f"TWSE MI_QFIIS {target_date.isoformat()} 未補到外資持股比例缺口",
                        )
                    finmind_ratio_codes = ratio_missing_codes - fetched_ratio_codes
                    if finmind_ratio_codes:
                        _print_chip_progress(
                            progress_label,
                            progress,
                            f"剩餘外資持股比例缺口 {len(finmind_ratio_codes)} 檔，改用 FinMind 單檔補資料",
                        )
                        finmind_ratio = _fetch_finmind_foreign_ratio_for_codes(
                            client,
                            target_date,
                            finmind_ratio_codes,
                            progress_callback,
                            scope=scope,
                        )
                        if not finmind_ratio.empty:
                            foreign_ratio = pd.concat([foreign_ratio, finmind_ratio], ignore_index=True)
                    if not foreign_ratio.empty:
                        ratio_update = foreign_ratio.drop_duplicates(["date", "code"], keep="last").rename(
                            columns={
                                "foreign_ratio_pct": "foreign_ratio_pct_update",
                                "source": "foreign_ratio_source",
                            }
                        )
                        date_net = date_net.merge(
                            ratio_update[["date", "code", "foreign_ratio_pct_update", "foreign_ratio_source"]],
                            on=["date", "code"],
                            how="left",
                        )
                        missing_ratio_mask = date_net["foreign_ratio_pct"].isna() & date_net["foreign_ratio_pct_update"].notna()
                        date_net["foreign_ratio_pct"] = date_net["foreign_ratio_pct"].fillna(
                            date_net["foreign_ratio_pct_update"]
                        )
                        date_net.loc[missing_ratio_mask, "source"] = (
                            date_net.loc[missing_ratio_mask, "source"].astype(str)
                            + "+"
                            + date_net.loc[missing_ratio_mask, "foreign_ratio_source"].astype(str)
                        )
                        date_net = date_net.drop(columns=["foreign_ratio_pct_update", "foreign_ratio_source"])

            _save_daily_chip_cache(target_date, date_net)
            collected_frames.append(date_net)

            collected_dates.append(target_date)
            progress = _chip_progress_value(progress_start, progress_end, len(collected_dates), checked_index, target_trading_days)
            _print_chip_progress(
                progress_label,
                progress,
                f"完成 {target_date.isoformat()}，已收集 {len(collected_dates)}/{target_trading_days} 個交易日",
            )
            if len(collected_dates) >= target_trading_days:
                break

    if not collected_frames:
        _print_chip_progress(progress_label, progress_end, "無可用法人日資料")
        return pd.DataFrame(), None

    daily_df = pd.concat(collected_frames, ignore_index=True)
    if "foreign_ratio_pct" not in daily_df.columns:
        daily_df["foreign_ratio_pct"] = pd.NA

    daily_df = daily_df.merge(
        candidates[["code", "market", "issued_shares"]],
        on=["code", "market"],
        how="left",
    )

    daily_df = daily_df.sort_values(["code", "date"]).reset_index(drop=True)
    daily_df["trust_ratio_pct"] = (
        daily_df["trust_net_lots"].fillna(0.0) * 1000.0 / daily_df["issued_shares"] * 100.0
    )
    daily_df["trust_ratio_pct"] = daily_df.groupby("code")["trust_ratio_pct"].cumsum()

    estimated_foreign_ratio = (
        daily_df["foreign_net_lots"].fillna(0.0) * 1000.0 / daily_df["issued_shares"] * 100.0
    )
    estimated_foreign_ratio = estimated_foreign_ratio.groupby(daily_df["code"]).cumsum()
    estimated_mask = daily_df["foreign_ratio_pct"].isna()
    daily_df["foreign_ratio_pct"] = daily_df["foreign_ratio_pct"].fillna(estimated_foreign_ratio)
    daily_df.loc[estimated_mask, "source"] = daily_df.loc[estimated_mask, "source"].astype(str) + "+estimated"
    daily_df["combined_ratio_pct"] = daily_df["foreign_ratio_pct"].fillna(0.0) + daily_df["trust_ratio_pct"].fillna(0.0)

    latest_trading_date = max(collected_dates) if collected_dates else None
    _print_chip_progress(
        progress_label,
        progress_end,
        f"完成，最新交易日 {latest_trading_date.isoformat() if latest_trading_date else '無資料'}",
    )
    return daily_df, latest_trading_date


def _parse_tdcc_text(raw_text: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 6 or not parts[0].isdigit() or len(parts[0]) != 8:
            continue
        snapshot_date = datetime.strptime(parts[0], "%Y%m%d").date()
        level = _to_float(parts[2])
        holder_count = _to_float(parts[3])
        shares = _to_float(parts[4])
        pct = _to_float(parts[5])
        if level is None or pct is None:
            continue
        rows.append(
            {
                "snapshot_date": snapshot_date,
                "code": parts[1].strip(),
                "level": int(level),
                "holder_count": holder_count or 0.0,
                "shares": shares or 0.0,
                "pct": float(pct),
            }
        )
    return pd.DataFrame(rows)


def update_tdcc_snapshot_cache() -> Path | None:
    _ensure_cache_dirs()
    try:
        response = httpx.get(TDCC_DISTRIBUTION_URL, timeout=30.0, follow_redirects=True, verify=False)
        response.raise_for_status()
    except Exception:
        return None

    raw_text = response.text
    frame = _parse_tdcc_text(raw_text)
    if frame.empty:
        return None

    snapshot_date = frame["snapshot_date"].max()
    cache_path = TDCC_CACHE_DIR / f"{snapshot_date.strftime('%Y%m%d')}.csv"
    if not cache_path.exists():
        cache_path.write_text(raw_text, encoding="utf-8")
    return cache_path


def _load_cached_tdcc_frames() -> pd.DataFrame:
    _ensure_cache_dirs()
    frames: list[pd.DataFrame] = []
    for path in sorted(TDCC_CACHE_DIR.glob("*.csv"), reverse=True)[:TDCC_TARGET_WEEKS]:
        try:
            frame = _parse_tdcc_text(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True)
    return merged.sort_values(["snapshot_date", "code", "level"]).reset_index(drop=True)


def _build_weekly_distribution(candidates: pd.DataFrame) -> pd.DataFrame:
    update_tdcc_snapshot_cache()
    tdcc_df = _load_cached_tdcc_frames()
    if tdcc_df.empty or candidates.empty:
        return pd.DataFrame()

    candidate_codes = set(candidates["code"].tolist())
    tdcc_df = tdcc_df[tdcc_df["code"].isin(candidate_codes)].copy()
    if tdcc_df.empty:
        return tdcc_df

    aggregated = (
        tdcc_df.groupby(["snapshot_date", "code"]).apply(
            lambda group: pd.Series(
                {
                    "big_holder_pct": group.loc[group["level"].isin(TDCC_BIG_BUCKETS), "pct"].sum(),
                    "retail_holder_pct": group.loc[group["level"].isin(TDCC_RETAIL_BUCKETS), "pct"].sum(),
                }
            )
        )
    ).reset_index()
    return aggregated.sort_values(["code", "snapshot_date"]).reset_index(drop=True)


def build_market_context(
    force_refresh: bool = False,
    report_date: date | None = None,
    include_daily_data: bool = True,
    include_foreign_ratio: bool = True,
    progress_label: str = CHIP_PROGRESS_DEFAULT_LABEL,
    progress_start: float = 0.0,
    progress_end: float = 100.0,
    target_trading_days: int = TARGET_DAILY_TRADING_DAYS,
    scope: str = "default",
    extra_candidates: list[dict[str, Any]] | None = None,
) -> ChipMarketContext:
    report_date = report_date or get_tw_today()
    candidates = _build_hard_filter_candidates(report_date, force_refresh=force_refresh)
    if extra_candidates:
        extra_frame = pd.DataFrame(extra_candidates)
        if not extra_frame.empty and "code" in extra_frame.columns:
            for column in candidates.columns:
                if column not in extra_frame.columns:
                    if column in {"price", "avg_volume_20d", "monthly_revenue", "issued_shares"}:
                        extra_frame[column] = 0.0
                    elif column == "industry":
                        extra_frame[column] = UNCLASSIFIED_INDUSTRY
                    else:
                        extra_frame[column] = ""
            extra_frame = extra_frame[candidates.columns].copy()
            extra_frame["code"] = extra_frame["code"].astype(str).str.strip()
            merged = pd.concat([candidates, extra_frame], ignore_index=True)
            merged = merged.sort_values("code").drop_duplicates("code", keep="first").reset_index(drop=True)
            merged.attrs["total_symbols"] = candidates.attrs.get("total_symbols", len(candidates))
            merged.attrs["scan_settings"] = dict(candidates.attrs.get("scan_settings", HARD_FILTERS))
            candidates = merged
    if include_daily_data:
        daily_data, latest_trading_date = _fetch_recent_daily_chip_data(
            report_date,
            candidates,
            progress_label=progress_label,
            progress_start=progress_start,
            progress_end=progress_end,
            target_trading_days=target_trading_days,
            include_foreign_ratio=include_foreign_ratio,
            scope=scope,
        )
    else:
        daily_data, latest_trading_date = pd.DataFrame(), None
    weekly_data = _build_weekly_distribution(candidates)
    return ChipMarketContext(
        report_date=report_date,
        latest_trading_date=latest_trading_date,
        total_symbols=int(candidates.attrs.get("total_symbols", len(candidates))),
        scan_settings=dict(candidates.attrs.get("scan_settings", HARD_FILTERS)),
        candidates=candidates,
        daily_data=daily_data,
        weekly_data=weekly_data,
    )


def _candidate_members(context: ChipMarketContext, selector: Callable[[str], str | None], sort_key: Callable[[str], Any] | None = None) -> dict[str, dict[str, list[str]]]:
    members: dict[str, dict[str, list[str]]] = {"S": {}, "A": {}, "B": {}}
    if context.candidates.empty:
        return members

    indexed_candidates = context.candidates.set_index("code")
    codes = indexed_candidates.index.tolist()
    if sort_key is None:
        ordered_codes = sorted(codes)
    else:
        ordered_codes = sorted(codes, key=sort_key)

    for code in ordered_codes:
        grade = selector(code)
        if grade not in members:
            continue
        row = indexed_candidates.loc[code]
        industry = str(row.get("industry") or UNCLASSIFIED_INDUSTRY)
        members[grade].setdefault(industry, []).append(f"{code} {row['name']} ({_format_price(float(row['price']))})")
    return members


def _count_strategy_members(members: dict[str, dict[str, list[str]]]) -> int:
    return sum(len(items) for industry_groups in members.values() for items in industry_groups.values())


def _format_context_sources(context: ChipMarketContext) -> str:
    source_order = ["cache", "TWSE", "TPEX_OpenAPI", "TPEX", "FinMind", "estimated", "TDCC"]
    source_labels = {
        "cache": "本機快取",
        "TWSE": "TWSE",
        "TPEX_OpenAPI": "TPEx OpenAPI",
        "TPEX": "TPEX",
        "FinMind": "FinMind",
        "estimated": "估算",
        "TDCC": "TDCC",
    }
    found: set[str] = set()
    if not context.daily_data.empty and "source" in context.daily_data.columns:
        for raw_source in context.daily_data["source"].dropna().astype(str):
            for source in raw_source.replace("/", "+").split("+"):
                cleaned = source.strip()
                if cleaned:
                    found.add(cleaned)
    if not context.weekly_data.empty:
        found.add("TDCC")
        found.add("cache")
    if not found:
        return "本機快取 / TWSE / TPEX / FinMind / 估算"
    ordered = [source_labels[source] for source in source_order if source in found]
    extras = sorted(source_labels.get(source, source) for source in found if source not in source_order)
    return " / ".join(ordered + extras)


def _format_scan_statistics(context: ChipMarketContext, matched_count: int, data_date_text: str | None = None) -> list[str]:
    settings = context.scan_settings or HARD_FILTERS
    min_price = _format_price(float(settings.get("min_price", HARD_FILTERS["min_price"])))
    max_price = _format_price(float(settings.get("max_price", HARD_FILTERS["max_price"])))
    min_volume = _format_price(float(settings.get("min_avg_volume_20d", HARD_FILTERS["min_avg_volume_20d"])))
    resolved_data_date = data_date_text or (context.latest_trading_date.isoformat() if context.latest_trading_date else context.report_date.isoformat())
    return [
        "",
        "掃描統計",
        f"總掃描範圍：{context.total_symbols} 檔",
        (
            f"通過硬篩標的：{len(context.candidates)} 檔 "
            f"(股價 {min_price}~{max_price} / 均量 > {min_volume})"
        ),
        f"符合選股邏輯：{matched_count} 檔",
        f"資料日期：{resolved_data_date}",
        f"資料來源：{_format_context_sources(context)}",
    ]


def _render_strategy_template(
    title: str,
    context: ChipMarketContext,
    legend_lines: list[str],
    section_title: str,
    members: dict[str, dict[str, list[str]]],
    labels: dict[str, str],
    latest_line: str | None = None,
    data_date_text: str | None = None,
) -> str:
    latest_date_text = context.latest_trading_date.isoformat() if context.latest_trading_date else "無資料"
    lines = [
        title,
        f"📅 日期：{context.report_date.isoformat()}",
        latest_line or f"📈 最新交易日：{latest_date_text}",
        "",
        "📌 分類定義說明：",
        *[f"* {line}" for line in legend_lines],
        "",
        section_title,
        "",
    ]
    for grade in ("S", "A", "B"):
        lines.extend(_render_bucket(labels[grade], members.get(grade, [])))
    lines.extend(_format_scan_statistics(context, _count_strategy_members(members), data_date_text))
    return "\n".join(lines).strip()


def _strategy_one_grades(context: ChipMarketContext) -> dict[str, str]:
    if context.daily_data.empty:
        return {}

    grades: dict[str, str] = {}
    for code, group in context.daily_data.groupby("code"):
        window = group.sort_values("date").tail(TARGET_DAILY_TRADING_DAYS)
        if len(window) < TARGET_DAILY_TRADING_DAYS:
            continue
        combined_net = window["foreign_net_lots"].fillna(0.0) + window["trust_net_lots"].fillna(0.0)
        positive_days = int((combined_net > 0).sum())
        recent_positive_days = int((combined_net.tail(10) > 0).sum())
        positive_values = combined_net[combined_net > 0]
        average_buy = float(positive_values.mean()) if not positive_values.empty else 0.0
        max_sell = float((-combined_net[combined_net < 0]).max()) if (combined_net < 0).any() else 0.0
        today_trigger = bool(window.iloc[-1]["foreign_net_lots"] > 0 or window.iloc[-1]["trust_net_lots"] > 0)

        if (
            today_trigger
            and positive_days >= 18
            and recent_positive_days >= 5
            and average_buy > 0
            and max_sell < average_buy * 1.20
        ):
            grades[code] = "S"
            continue
        if (
            today_trigger
            and positive_days >= 10
            and recent_positive_days >= 4
            and average_buy > 0
            and max_sell < average_buy * 1.80
        ):
            grades[code] = "A"
            continue
        if today_trigger and positive_days >= 5 and recent_positive_days >= 4:
            grades[code] = "B"
    return grades


def build_chip_strategy_one_report(context: ChipMarketContext) -> str:
    grades = _strategy_one_grades(context)
    members = _candidate_members(context, grades.get)
    return _render_strategy_template(
        "🔍 今日 60 日法人動態選股掃描報告",
        context,
        [
            "S級：今日外資或投信買超；近 60 個交易日外資+投信合計買超 >= 18 日，最近 10 日買超 >= 5 日，且最大單日賣超 < 買超日平均買超的 120%。",
            "A級：今日外資或投信買超；近 60 個交易日外資+投信合計買超 >= 10 日，最近 10 日買超 >= 4 日，且最大單日賣超 < 買超日平均買超的 180%。",
            "B級：今日外資或投信買超；近 60 個交易日外資+投信合計買超 >= 5 日，且最近 10 日內買超 >= 4 日。",
        ],
        "📂 【策略一：60 日法人動態】",
        members,
        {
            "S": "🥇 S級 (強勢主升段型)",
            "A": "🥈 A級 (底部轉機發動型)",
            "B": "🥉 B級 (溫和試單觀察型)",
        },
    )


def _strategy_two_grades(context: ChipMarketContext) -> dict[str, str]:
    if context.daily_data.empty:
        return {}

    grades: dict[str, str] = {}
    for code, group in context.daily_data.groupby("code"):
        window = group.sort_values("date").tail(TARGET_DAILY_TRADING_DAYS)
        if len(window) < TARGET_DAILY_TRADING_DAYS:
            continue
        trust_net = window["trust_net_lots"].fillna(0.0)
        trust_ratio = window["trust_ratio_pct"].fillna(0.0)
        today_trigger = bool(trust_net.iloc[-1] > 0)
        if not today_trigger:
            continue

        if float(trust_ratio.head(45).mean()) >= 0.10:
            continue

        if abs(float(trust_net.head(45).sum())) < 80 and int((trust_net.tail(15) > 0).sum()) >= 7:
            grades[code] = "S"
            continue
        if abs(float(trust_net.head(40).sum())) < 250 and int((trust_net.tail(20) > 0).sum()) >= 5:
            grades[code] = "A"
            continue
        if abs(float(trust_net.head(50).sum())) < 100 and bool((trust_net.tail(10) > 0).any()):
            grades[code] = "B"
    return grades


def build_chip_strategy_two_report(context: ChipMarketContext) -> str:
    grades = _strategy_two_grades(context)
    members = _candidate_members(context, grades.get)
    return _render_strategy_template(
        "🔍 今日投信認養股掃描報告",
        context,
        [
            "共同條件：今日投信買超，且前 45 日投信估計持股比例平均 < 0.10%。",
            "S級：前 45 日投信合計買賣超絕對值 < 80 張，最近 15 日投信買超 >= 7 日。",
            "A級：前 40 日投信合計買賣超絕對值 < 250 張，最近 20 日投信買超 >= 5 日。",
            "B級：前 50 日投信合計買賣超絕對值 < 100 張，最近 10 日內至少 1 日投信買超。",
        ],
        "📂 【策略二：投信認養股】",
        members,
        {
            "S": "🥇 S級 (急行軍強勢認養)",
            "A": "🥈 A級 (穩步波段建倉)",
            "B": "🥉 B級 (初綻火花雷達現身)",
        },
    )


def _strategy_three_grades(context: ChipMarketContext) -> dict[str, str]:
    if context.daily_data.empty:
        return {}

    grades: dict[str, str] = {}
    for code, group in context.daily_data.groupby("code"):
        window = group.sort_values("date").tail(TARGET_DAILY_TRADING_DAYS)
        if len(window) < TARGET_DAILY_TRADING_DAYS:
            continue
        combined_ratio = window["combined_ratio_pct"].fillna(0.0)
        combined_net_today = window.iloc[-1]["foreign_net_lots"] > 0 or window.iloc[-1]["trust_net_lots"] > 0
        if not combined_net_today:
            continue

        increase = float(combined_ratio.iloc[-1] - combined_ratio.iloc[0])
        running_max = combined_ratio.cummax()
        drawdown = (running_max - combined_ratio).max()
        distance_from_high = float(combined_ratio.max() - combined_ratio.iloc[-1])
        latest_above_20d_mean = bool(combined_ratio.iloc[-1] > combined_ratio.tail(20).mean())

        if increase > 2.0 and distance_from_high <= 0.2 and drawdown <= 0.5:
            grades[code] = "S"
            continue
        if increase > 1.0 and distance_from_high <= 0.4 and drawdown <= 0.8:
            grades[code] = "A"
            continue
        if increase > 0.5 and latest_above_20d_mean:
            grades[code] = "B"
    return grades


def build_chip_strategy_three_report(context: ChipMarketContext) -> str:
    grades = _strategy_three_grades(context)
    members = _candidate_members(context, grades.get)
    return _render_strategy_template(
        "🔍 今日法人持股比例增加掃描報告",
        context,
        [
            "共同條件：今日外資或投信買超；法人持股比例 = 外資持股比例 + 投信估計持股比例。",
            "S級：近 60 日法人持股比例增加 > 2.0 個百分點，最新值距 60 日高點 <= 0.2 個百分點，期間最大回落 <= 0.5 個百分點。",
            "A級：近 60 日法人持股比例增加 > 1.0 個百分點，最新值距 60 日高點 <= 0.4 個百分點，期間最大回落 <= 0.8 個百分點。",
            "B級：近 60 日法人持股比例增加 > 0.5 個百分點，且最新值高於最近 20 日平均。",
        ],
        "📂 【策略三：法人持股比例增加】",
        members,
        {
            "S": "🥇 S級 (45 度角強力鎖碼)",
            "A": "🥈 A級 (階梯式認養)",
            "B": "🥉 B級 (底部溫和吸籌)",
        },
    )


def _passes_weekly_s(big_values: list[float], retail_values: list[float]) -> bool:
    if len(big_values) < 3 or len(retail_values) < 3:
        return False
    if big_values[-3] < big_values[-2] < big_values[-1] and retail_values[-3] > retail_values[-2] > retail_values[-1]:
        return True
    if len(big_values) < 4 or len(retail_values) < 4:
        return False
    for index in range(1, len(big_values) - 1):
        drop = big_values[index - 1] - big_values[index]
        recovered = big_values[index + 1] > big_values[index - 1]
        if drop > 0 and drop < 0.5 and recovered and retail_values[-3] > retail_values[-2] > retail_values[-1]:
            return True
    return False


def _strategy_four_grades(context: ChipMarketContext) -> dict[str, str]:
    if context.weekly_data.empty:
        return {}

    grades: dict[str, str] = {}
    for code, group in context.weekly_data.groupby("code"):
        ordered = group.sort_values("snapshot_date")
        big_values = ordered["big_holder_pct"].tolist()
        retail_values = ordered["retail_holder_pct"].tolist()
        if len(big_values) >= 4 and _passes_weekly_s(big_values[-4:], retail_values[-4:]):
            grades[code] = "S"
            continue
        if len(big_values) >= 3:
            latest_big = big_values[-1]
            previous_big = big_values[-2]
            two_weeks_ago_big = big_values[-3]
            latest_retail = retail_values[-1]
            previous_retail = retail_values[-2]
            two_weeks_ago_retail = retail_values[-3]
            if latest_big > previous_big > two_weeks_ago_big and (latest_big - two_weeks_ago_big) > 1.0 and latest_retail < two_weeks_ago_retail:
                grades[code] = "A"
                continue
            if (latest_big - previous_big) > 1.5 and latest_retail < previous_retail:
                grades[code] = "B"
    return grades


def build_chip_strategy_four_report(context: ChipMarketContext) -> str:
    grades = _strategy_four_grades(context)
    members = _candidate_members(context, grades.get)
    latest_snapshot = context.weekly_data["snapshot_date"].max().isoformat() if not context.weekly_data.empty else "無資料"
    return _render_strategy_template(
        "🔍 本週大戶持股選股掃描報告",
        context,
        [
            "大戶定義：集保 400 張以上級距；散戶定義：集保 50 張以下級距。",
            "S級：最近 3 週大戶持股比例連續上升、散戶持股比例連續下降；或大戶短暫回落 < 0.5 個百分點後突破回落前高點，且散戶最近 3 週連續下降。",
            "A級：最近 3 週大戶持股比例連續上升，兩週累計增加 > 1.0 個百分點，且散戶持股比例低於兩週前。",
            "B級：最新一週大戶持股比例增加 > 1.5 個百分點，且散戶持股比例同步下降。",
        ],
        "📂 【策略四：每週大戶持股】",
        members,
        {
            "S": "🥇 S級 (絕對籌碼集中)",
            "A": "🥈 A級 (偏多格局確認)",
            "B": "🥉 B級 (突發性大戶卡位)",
        },
        latest_line=f"📦 最新集保快照：{latest_snapshot}",
        data_date_text=latest_snapshot,
    )


REPORT_BUILDERS: dict[str, Callable[[ChipMarketContext], str]] = {
    "chip_1": build_chip_strategy_one_report,
    "chip_2": build_chip_strategy_two_report,
    "chip_3": build_chip_strategy_three_report,
    "chip_4": build_chip_strategy_four_report,
}

CHIP_STRATEGY_NAMES = {
    "chip_1": "60 日法人動態",
    "chip_2": "投信認養股",
    "chip_3": "法人持股比例增加",
    "chip_4": "大戶持股週變化",
}

CHIP_GRADE_BUILDERS: dict[str, Callable[[ChipMarketContext], dict[str, str]]] = {
    "chip_1": _strategy_one_grades,
    "chip_2": _strategy_two_grades,
    "chip_3": _strategy_three_grades,
    "chip_4": _strategy_four_grades,
}


def build_chip_grade_maps(context: ChipMarketContext, strategy_keys: list[str]) -> dict[str, dict[str, str]]:
    return {key: CHIP_GRADE_BUILDERS[key](context) for key in strategy_keys}


def build_chip_reports(
    strategy_keys: list[str],
    force_refresh: bool = False,
    report_date: date | None = None,
    progress_label: str = CHIP_PROGRESS_DEFAULT_LABEL,
    progress_start: float = 0.0,
    progress_end: float = 100.0,
    target_trading_days: int = TARGET_DAILY_TRADING_DAYS,
) -> tuple[dict[str, str], ChipMarketContext]:
    include_daily_data = any(key != "chip_4" for key in strategy_keys)
    include_foreign_ratio = "chip_3" in strategy_keys
    context = build_market_context(
        force_refresh=force_refresh,
        report_date=report_date,
        include_daily_data=include_daily_data,
        include_foreign_ratio=include_foreign_ratio,
        progress_label=progress_label,
        progress_start=progress_start,
        progress_end=progress_end,
        target_trading_days=target_trading_days,
    )
    reports = {key: REPORT_BUILDERS[key](context) for key in strategy_keys}
    return reports, context


def warmup_chip_data_cache(
    report_date: date | None = None,
    full_backfill: bool = False,
    force_refresh: bool = False,
    progress_label: str = "籌碼快取回補",
    strategy_keys: list[str] | None = None,
    scope: str = "default",
    extra_candidates: list[dict[str, Any]] | None = None,
) -> ChipMarketContext:
    target_date = report_date or get_tw_today()
    target_days = TARGET_DAILY_TRADING_DAYS if full_backfill else 1
    warmup_strategy_keys = strategy_keys or ["chip_1", "chip_2", "chip_3"]
    context = build_market_context(
        force_refresh=force_refresh,
        report_date=target_date,
        include_daily_data=True,
        include_foreign_ratio="chip_3" in warmup_strategy_keys,
        progress_label=progress_label,
        progress_start=0.0,
        progress_end=95.0,
        target_trading_days=target_days,
        scope=scope,
        extra_candidates=extra_candidates,
    )
    update_tdcc_snapshot_cache()
    _print_chip_progress(
        progress_label,
        100.0,
        (
            f"回補完成，法人最新交易日 "
            f"{context.latest_trading_date.isoformat() if context.latest_trading_date else '無資料'}，"
            f"候選 {len(context.candidates)} 檔"
        ),
    )
    return context


def load_push_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return _read_json(STATE_PATH)
    except Exception:
        return {}


def save_push_state(state: dict[str, Any]) -> None:
    _write_json(STATE_PATH, state)


def _target_weekly_slot(report_date: date, fallback: bool = False) -> date:
    if fallback:
        days_back = (report_date.weekday() - 0) % 7 + 2
        return report_date - timedelta(days=days_back)
    weekday = report_date.weekday()
    days_until_saturday = (5 - weekday) % 7
    return report_date + timedelta(days=days_until_saturday)


def has_weekly_report_been_sent(report_date: date, fallback: bool = False) -> bool:
    state = load_push_state()
    weekly_state = state.get("weekly_big_holders", {})
    target_slot = _target_weekly_slot(report_date, fallback=fallback).isoformat()
    return weekly_state.get("slot_date") == target_slot


def mark_weekly_report_sent(report_date: date, latest_snapshot_date: date | None, fallback: bool = False) -> None:
    state = load_push_state()
    state["weekly_big_holders"] = {
        "slot_date": _target_weekly_slot(report_date, fallback=fallback).isoformat(),
        "sent_at": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
        "latest_snapshot_date": latest_snapshot_date.isoformat() if latest_snapshot_date else None,
    }
    save_push_state(state)


def should_run_startup_weekly_fallback(report_date: date | None = None) -> bool:
    report_date = report_date or get_tw_today()
    if report_date.weekday() != 0:
        return False
    return not has_weekly_report_been_sent(report_date, fallback=True)


def latest_weekly_snapshot_date(context: ChipMarketContext) -> date | None:
    if context.weekly_data.empty:
        return None
    return context.weekly_data["snapshot_date"].max()
