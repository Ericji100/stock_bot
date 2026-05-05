from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx
import pandas as pd
import pytz
import yfinance as yf

from stock_scanner import UNCLASSIFIED_INDUSTRY, load_price_metrics, load_recent_revenue_history, load_stock_universe


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


def _is_source_available(source_key: str) -> bool:
    cooldown_until = SOURCE_COOLDOWNS.get(source_key)
    if cooldown_until is None:
        return True
    return datetime.now(TIMEZONE) >= cooldown_until


def _mark_source_cooldown(source_key: str) -> None:
    SOURCE_COOLDOWNS[source_key] = datetime.now(TIMEZONE) + timedelta(seconds=SOURCE_COOLDOWN_SECONDS)


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


def _to_lots_from_shares(value: Any) -> float | None:
    parsed = _to_float(value)
    if parsed is None:
        return None
    return parsed / 1000.0


def _normalize_code(value: Any) -> str:
    return str(value or "").strip()


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
    lines = [title]
    if not industry_groups:
        lines.extend([_format_report_members([]), ""])
        return lines

    for industry in sorted(industry_groups, key=lambda value: (value == UNCLASSIFIED_INDUSTRY, value)):
        lines.append(f"  【{industry}】")
        lines.extend(_wrap_report_members(industry_groups[industry]))
    lines.append("")
    return lines


def _load_holiday_dates(target_year: int) -> set[date]:
    try:
        rows = httpx.get(TWSE_HOLIDAY_URL, timeout=20.0, follow_redirects=True, verify=False).json()
    except Exception:
        return set()

    holidays: set[date] = set()
    for row in rows:
        raw_date = str(row.get("Date") or "").strip()
        if not raw_date:
            continue
        try:
            parsed = datetime.strptime(raw_date, "%Y%m%d").date()
        except ValueError:
            continue
        if parsed.year == target_year:
            holidays.add(parsed)
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


def _daily_chip_cache_path(target_date: date) -> Path:
    return DAILY_CHIP_CACHE_DIR / f"{target_date.strftime('%Y%m%d')}.csv"


def _normalize_daily_chip_frame(frame: pd.DataFrame, target_date: date | None = None) -> pd.DataFrame:
    columns = ["date", "code", "market", "foreign_net_lots", "trust_net_lots", "foreign_ratio_pct"]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    normalized = frame.copy()
    for column in columns:
        if column not in normalized.columns:
            normalized[column] = pd.NA

    if target_date is not None:
        normalized["date"] = target_date
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce").dt.date
    normalized["code"] = normalized["code"].astype(str).str.strip()
    normalized["market"] = normalized["market"].astype(str).replace({"nan": ""})
    for column in ("foreign_net_lots", "trust_net_lots", "foreign_ratio_pct"):
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

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


def _finmind_payload(client: httpx.Client, params: dict[str, Any]) -> list[dict[str, Any]]:
    response = client.get(FINMIND_API_URL, params=params)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != 200:
        raise RuntimeError(str(payload.get("msg") or "FinMind request failed"))
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
    if frame.empty:
        return frame
    return frame.sort_values("code").reset_index(drop=True)


def _fetch_twse_net_buy_for_date(client: httpx.Client, target_date: date, candidate_codes: set[str]) -> pd.DataFrame:
    payload = _fetch_json(
        client,
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
            }
        )
    return pd.DataFrame(rows)


def _fetch_twse_foreign_ratio_for_date(client: httpx.Client, target_date: date, candidate_codes: set[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    remaining_codes = set(candidate_codes)
    for select_type in TWSE_FOREIGN_RATIO_SELECT_TYPES:
        if not remaining_codes:
            break
        payload = _fetch_json(
            client,
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
            rows.append({"date": target_date, "code": code, "foreign_ratio_pct": ratio})
            remaining_codes.discard(code)
    return pd.DataFrame(rows)


def _fetch_tpex_net_payload(client: httpx.Client, target_date: date, url: str, sort_key: str) -> dict[str, float]:
    roc_date = f"{target_date.year - 1911:03d}/{target_date.month:02d}/{target_date.day:02d}"
    payload = _fetch_json(
        client,
        url,
        params={"date": roc_date, "type": "Daily", sort_key: "buy"},
    )
    table = (payload.get("tables") or [{}])[0]
    net_map: dict[str, float] = {}
    for row in table.get("data", []):
        code = _normalize_code(row[1])
        net_value = _to_float(row[-1])
        if code and net_value is not None:
            net_map[code] = float(net_value)
    return net_map


def _fetch_tpex_net_buy_for_date(client: httpx.Client, target_date: date, candidate_codes: set[str]) -> pd.DataFrame:
    try:
        foreign_map = _fetch_tpex_net_payload(client, target_date, TPEX_QFII_URL, "searchType")
        trust_map = _fetch_tpex_net_payload(client, target_date, TPEX_SITC_URL, "searchType")
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
            }
        )
    return pd.DataFrame(rows)


def _fetch_finmind_net_buy_for_stock(client: httpx.Client, target_date: date, code: str, market: str) -> pd.DataFrame:
    data = _finmind_payload(
        client,
        {
            "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
            "data_id": code,
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
        },
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
            }
        ]
    )


def _fetch_finmind_net_buy_for_codes(client: httpx.Client, target_date: date, code_market_map: dict[str, str]) -> pd.DataFrame:
    if not _is_source_available("finmind"):
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for code, market in sorted(code_market_map.items())[:FINMIND_MAX_FALLBACK_STOCKS_PER_DATE]:
        try:
            frame = _fetch_finmind_net_buy_for_stock(client, target_date, code, market)
        except Exception:
            _mark_source_cooldown("finmind")
            continue
        if not frame.empty:
            frames.append(frame)
            _save_daily_chip_cache(target_date, frame)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _fetch_finmind_foreign_ratio_for_stock(client: httpx.Client, target_date: date, code: str) -> pd.DataFrame:
    data = _finmind_payload(
        client,
        {
            "dataset": "TaiwanStockShareholding",
            "data_id": code,
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
        },
    )
    for row in data:
        if str(row.get("date")) != target_date.isoformat():
            continue
        ratio = (
            _to_float(row.get("ForeignInvestmentSharesRatio"))
            or _to_float(row.get("foreign_investment_shares_ratio"))
        )
        if ratio is not None:
            return pd.DataFrame([{"date": target_date, "code": code, "foreign_ratio_pct": ratio}])
    return pd.DataFrame()


def _fetch_finmind_foreign_ratio_for_codes(client: httpx.Client, target_date: date, codes: set[str]) -> pd.DataFrame:
    if not _is_source_available("finmind"):
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for code in sorted(codes)[:FINMIND_MAX_FALLBACK_STOCKS_PER_DATE]:
        try:
            frame = _fetch_finmind_foreign_ratio_for_stock(client, target_date, code)
        except Exception:
            _mark_source_cooldown("finmind")
            continue
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _fetch_recent_daily_chip_data(report_date: date, candidates: pd.DataFrame) -> tuple[pd.DataFrame, date | None]:
    if candidates.empty:
        return pd.DataFrame(), None

    candidate_codes = set(candidates["code"].astype(str).tolist())
    twse_codes = set(candidates.loc[candidates["market"] == "TWSE", "code"].tolist())
    tpex_codes = set(candidates.loc[candidates["market"] == "TPEX", "code"].tolist())
    code_market_map = dict(zip(candidates["code"].astype(str), candidates["market"].astype(str)))
    collected_dates: list[date] = []
    collected_frames: list[pd.DataFrame] = []

    calendar = pd.bdate_range(end=pd.Timestamp(report_date), periods=TRADING_DAY_LOOKBACK).date[::-1]
    with httpx.Client(timeout=20.0, follow_redirects=True, verify=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for target_date in calendar:
            cached_net = _load_daily_chip_cache(target_date, candidate_codes)
            cached_codes = set(cached_net["code"].tolist()) if not cached_net.empty else set()
            missing_twse_codes = twse_codes - cached_codes
            missing_tpex_codes = tpex_codes - cached_codes

            fetched_frames: list[pd.DataFrame] = []
            if missing_twse_codes:
                if _is_source_available("twse_t86"):
                    try:
                        twse_net = _fetch_twse_net_buy_for_date(client, target_date, missing_twse_codes)
                    except Exception:
                        _mark_source_cooldown("twse_t86")
                        twse_net = pd.DataFrame()
                else:
                    twse_net = pd.DataFrame()
                if not twse_net.empty:
                    twse_net = twse_net.assign(market="TWSE")
                    fetched_frames.append(twse_net)
                    _save_daily_chip_cache(target_date, twse_net)

                fetched_twse_codes = set(twse_net["code"].tolist()) if not twse_net.empty else set()
                finmind_twse_codes = missing_twse_codes - fetched_twse_codes
                if finmind_twse_codes:
                    finmind_map = {code: code_market_map[code] for code in finmind_twse_codes if code in code_market_map}
                    finmind_net = _fetch_finmind_net_buy_for_codes(client, target_date, finmind_map)
                    if not finmind_net.empty:
                        fetched_frames.append(finmind_net)

            if missing_tpex_codes:
                tpex_net = _fetch_tpex_net_buy_for_date(client, target_date, missing_tpex_codes)
                if not tpex_net.empty:
                    tpex_net = tpex_net.assign(market="TPEX")
                    fetched_frames.append(tpex_net)
                    _save_daily_chip_cache(target_date, tpex_net)

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

            twse_date_codes = set(date_net.loc[date_net["market"] == "TWSE", "code"].tolist())
            cached_ratio_codes = set(
                date_net.loc[
                    (date_net["market"] == "TWSE") & date_net["foreign_ratio_pct"].notna(),
                    "code",
                ].tolist()
            )
            ratio_missing_codes = twse_date_codes - cached_ratio_codes
            if ratio_missing_codes:
                if _is_source_available("twse_mi_qfiis"):
                    try:
                        foreign_ratio = _fetch_twse_foreign_ratio_for_date(client, target_date, ratio_missing_codes)
                    except Exception:
                        _mark_source_cooldown("twse_mi_qfiis")
                        foreign_ratio = pd.DataFrame()
                else:
                    foreign_ratio = pd.DataFrame()
                fetched_ratio_codes = set(foreign_ratio["code"].tolist()) if not foreign_ratio.empty else set()
                finmind_ratio_codes = ratio_missing_codes - fetched_ratio_codes
                if finmind_ratio_codes:
                    finmind_ratio = _fetch_finmind_foreign_ratio_for_codes(client, target_date, finmind_ratio_codes)
                    if not finmind_ratio.empty:
                        foreign_ratio = pd.concat([foreign_ratio, finmind_ratio], ignore_index=True)
                if not foreign_ratio.empty:
                    ratio_update = foreign_ratio.drop_duplicates(["date", "code"], keep="last").rename(
                        columns={"foreign_ratio_pct": "foreign_ratio_pct_update"}
                    )
                    date_net = date_net.merge(
                        ratio_update,
                        on=["date", "code"],
                        how="left",
                    )
                    date_net["foreign_ratio_pct"] = date_net["foreign_ratio_pct"].fillna(
                        date_net["foreign_ratio_pct_update"]
                    )
                    date_net = date_net.drop(columns=["foreign_ratio_pct_update"])

            _save_daily_chip_cache(target_date, date_net)
            collected_frames.append(date_net)

            collected_dates.append(target_date)
            if len(collected_dates) >= TARGET_DAILY_TRADING_DAYS:
                break

    if not collected_frames:
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
    daily_df["foreign_ratio_pct"] = daily_df["foreign_ratio_pct"].fillna(estimated_foreign_ratio)
    daily_df["combined_ratio_pct"] = daily_df["foreign_ratio_pct"].fillna(0.0) + daily_df["trust_ratio_pct"].fillna(0.0)

    latest_trading_date = max(collected_dates) if collected_dates else None
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


def build_market_context(force_refresh: bool = False, report_date: date | None = None, include_daily_data: bool = True) -> ChipMarketContext:
    report_date = report_date or get_tw_today()
    candidates = _build_hard_filter_candidates(report_date, force_refresh=force_refresh)
    if include_daily_data:
        daily_data, latest_trading_date = _fetch_recent_daily_chip_data(report_date, candidates)
    else:
        daily_data, latest_trading_date = pd.DataFrame(), None
    weekly_data = _build_weekly_distribution(candidates)
    return ChipMarketContext(
        report_date=report_date,
        latest_trading_date=latest_trading_date,
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


def _render_strategy_template(
    title: str,
    context: ChipMarketContext,
    legend_lines: list[str],
    section_title: str,
    members: dict[str, dict[str, list[str]]],
    labels: dict[str, str],
    latest_line: str | None = None,
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
    )


REPORT_BUILDERS: dict[str, Callable[[ChipMarketContext], str]] = {
    "chip_1": build_chip_strategy_one_report,
    "chip_2": build_chip_strategy_two_report,
    "chip_3": build_chip_strategy_three_report,
    "chip_4": build_chip_strategy_four_report,
}


def build_chip_reports(strategy_keys: list[str], force_refresh: bool = False, report_date: date | None = None) -> tuple[dict[str, str], ChipMarketContext]:
    include_daily_data = any(key != "chip_4" for key in strategy_keys)
    context = build_market_context(force_refresh=force_refresh, report_date=report_date, include_daily_data=include_daily_data)
    reports = {key: REPORT_BUILDERS[key](context) for key in strategy_keys}
    return reports, context


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
