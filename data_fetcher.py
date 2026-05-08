from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import StringIO
from typing import Any

import httpx
import pandas as pd
import yfinance as yf

from fugle_data import fetch_fugle_history


TWSE_NAME_API_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_NAME_API_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
MOPS_API_BASE_URL = "https://mops.twse.com.tw/mops/api/"
FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"


class StockExportError(Exception):
    pass


class StockNotFoundError(StockExportError):
    pass


@dataclass(frozen=True)
class StockMeta:
    code: str
    symbol: str
    market: str
    name: str

    @property
    def display_name(self) -> str:
        return f"{self.code} {self.name}".strip()


def _month_starts_between(start_date: date, end_date: date) -> list[date]:
    cursor = date(start_date.year, start_date.month, 1)
    months: list[date] = []
    while cursor <= end_date:
        months.append(cursor)
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return months


def _roc_to_gregorian(raw_value: str) -> date:
    raw_value = str(raw_value).strip()
    if not raw_value:
        raise ValueError("empty ROC date")

    if "/" in raw_value:
        year_part, month_part, day_part = raw_value.split("/")
        return date(int(year_part) + 1911, int(month_part), int(day_part))

    if len(raw_value) == 7:
        return date(int(raw_value[:3]) + 1911, int(raw_value[3:5]), int(raw_value[5:7]))

    if len(raw_value) == 5:
        return date(int(raw_value[:3]) + 1911, int(raw_value[3:5]), 1)

    raise ValueError(f"unsupported ROC date: {raw_value}")


def _flatten_columns(columns: Any) -> list[str]:
    flattened: list[str] = []
    if isinstance(columns, pd.MultiIndex):
        for column in columns:
            parts = [str(part).strip() for part in column if str(part).strip() and "Unnamed" not in str(part)]
            flattened.append("_".join(parts))
        return flattened

    return [str(column).strip() for column in columns]


def _to_number(value: Any) -> float | None:
    if value is None:
        return None

    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "---", "nan", "None"}:
        return None
    if text.startswith("X"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_lots(value: Any) -> float | None:
    parsed = _to_number(value)
    if parsed is None:
        return None
    return parsed / 1000.0


def _safe_ratio(numerator: Any, denominator: Any) -> float | None:
    numerator_value = _to_number(numerator)
    denominator_value = _to_number(denominator)
    if numerator_value is None or denominator_value in (None, 0):
        return None
    return numerator_value / denominator_value


def _quarter_sequence(start_year: int, end_year: int, end_quarter: int = 4) -> list[tuple[int, int]]:
    quarters: list[tuple[int, int]] = []
    for year in range(start_year, end_year + 1):
        final_quarter = end_quarter if year == end_year else 4
        for quarter in range(1, final_quarter + 1):
            quarters.append((year, quarter))
    return quarters


class StockDataFetcher:
    def __init__(self, twse_delay_seconds: float = 0.4, timeout_seconds: float = 20.0):
        self.client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            verify=False,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        self.twse_delay_seconds = twse_delay_seconds
        self._last_twse_request_at = 0.0
        self._twse_name_cache: dict[str, str] | None = None
        self._tpex_name_cache: dict[str, str] | None = None
        self._tpex_institutional_cache: dict[tuple[str, str, str], dict[str, float] | None] = {}
        self.notes: list[str] = []

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "StockDataFetcher":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _throttle_twse(self) -> None:
        elapsed = time.monotonic() - self._last_twse_request_at
        wait_seconds = self.twse_delay_seconds - elapsed
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        self._last_twse_request_at = time.monotonic()

    def _append_note_once(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    def _get_json(self, url: str, params: dict[str, Any] | None = None, *, is_twse: bool = False) -> Any:
        last_error: httpx.HTTPStatusError | None = None
        for attempt in range(3):
            if is_twse:
                self._throttle_twse()
            try:
                response = self.client.get(url, params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if status_code not in {403, 429, 500, 502, 503, 504} or attempt == 2:
                    raise
                last_error = exc
                time.sleep(0.6 * (attempt + 1))

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"無法取得 JSON 資料：{url}")

    def _get_text(self, url: str, *, is_twse: bool = False) -> str:
        if is_twse:
            self._throttle_twse()
        response = self.client.get(url)
        response.raise_for_status()
        if "mopsov.twse.com.tw" in url:
            return response.content.decode("big5-hkscs", errors="ignore")
        return response.text

    def _load_name_cache(self) -> None:
        if self._twse_name_cache is not None and self._tpex_name_cache is not None:
            return

        self._twse_name_cache = {}
        self._tpex_name_cache = {}

        for item in self._get_json(TWSE_NAME_API_URL):
            code = str(item.get("公司代號", "")).strip()
            name = str(item.get("公司簡稱") or item.get("公司名稱") or "").strip()
            if code and name:
                self._twse_name_cache[code] = name

        for item in self._get_json(TPEX_NAME_API_URL):
            code = str(item.get("SecuritiesCompanyCode", "")).strip()
            name = str(item.get("CompanyAbbreviation") or item.get("CompanyName") or "").strip()
            if code and name:
                self._tpex_name_cache[code] = name

    def resolve_stock(self, symbol_or_code: str) -> StockMeta:
        self._load_name_cache()
        normalized = str(symbol_or_code).strip().upper()
        if not normalized:
            raise StockNotFoundError("請輸入股票代碼，例如 /export 2330")

        suffix = None
        code = normalized
        if normalized.endswith(".TW"):
            suffix = ".TW"
            code = normalized[:-3]
        elif normalized.endswith(".TWO"):
            suffix = ".TWO"
            code = normalized[:-4]

        if suffix == ".TW" or (suffix is None and code in self._twse_name_cache):
            name = (self._twse_name_cache or {}).get(code, "")
            return StockMeta(code=code, symbol=f"{code}.TW", market="TWSE", name=name)

        if suffix == ".TWO" or (suffix is None and code in self._tpex_name_cache):
            name = (self._tpex_name_cache or {}).get(code, "")
            return StockMeta(code=code, symbol=f"{code}.TWO", market="TPEX", name=name)

        raise StockNotFoundError(f"找不到股票代碼：{symbol_or_code}")

    def fetch_price_history(self, meta: StockMeta, months: int = 6) -> pd.DataFrame:
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=months * 31)

        if meta.market == "TWSE":
            official_error: Exception | None = None
            try:
                return self._fetch_twse_price_history(meta, start_date, end_date)
            except Exception as exc:
                official_error = exc
                fallback = self._fetch_fugle_price_history(meta, start_date, end_date)
                if not fallback.empty:
                    self._append_note_once("上市股價歷史資料改用 Fugle 備援補齊。")
                    return fallback
                yahoo_fallback = self._fetch_yahoo_price_history(meta, start_date, end_date)
                if not yahoo_fallback.empty:
                    self._append_note_once("上市股價歷史資料改用 Yahoo Finance 備援補齊。")
                    return yahoo_fallback
                raise official_error

        return self._fetch_tpex_price_history_with_fallback(meta, start_date, end_date)

    def _fetch_twse_price_history(self, meta: StockMeta, start_date: date, end_date: date) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        months = _month_starts_between(start_date, end_date)
        for month_start in months:
            payload = self._get_json(
                "https://www.twse.com.tw/exchangeReport/STOCK_DAY",
                params={
                    "response": "json",
                    "date": month_start.strftime("%Y%m01"),
                    "stockNo": meta.code,
                },
                is_twse=True,
            )
            if payload.get("stat") != "OK" or not payload.get("data"):
                continue

            rows = []
            for row in payload["data"]:
                rows.append(
                    {
                        "Date": _roc_to_gregorian(row[0]),
                        "Close": _to_number(row[6]),
                        "Volume_Lots": _to_lots(row[1]),
                    }
                )
            frames.append(pd.DataFrame(rows))

        if not frames:
            raise StockExportError(f"無法取得 {meta.display_name} 的股價資料")

        price_df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["Date"]).sort_values("Date")
        price_df = price_df[(price_df["Date"] >= start_date) & (price_df["Date"] <= end_date)].reset_index(drop=True)
        return price_df

    def _fetch_tpex_price_history_with_fallback(self, meta: StockMeta, start_date: date, end_date: date) -> pd.DataFrame:
        history = yf.download(
            meta.symbol,
            start=start_date.isoformat(),
            end=(end_date + timedelta(days=1)).isoformat(),
            interval="1d",
            progress=False,
            auto_adjust=False,
        )
        if history.empty:
            fallback = self._fetch_fugle_price_history(meta, start_date, end_date)
            if not fallback.empty:
                self._append_note_once("上櫃股價歷史資料改用 Fugle 備援補齊。")
                return fallback
            raise StockExportError(f"無法取得 {meta.display_name} 的上櫃歷史股價資料")

        if isinstance(history.columns, pd.MultiIndex):
            history.columns = history.columns.get_level_values(0)

        self.notes.append("上櫃個股歷史價量改用 Yahoo Finance 補齊 6 個月日線，因 TPEx 可直接存取的官方 API 在此環境僅提供當日快照。")

        price_df = history.reset_index()[["Date", "Close", "Volume"]].rename(columns={"Volume": "Volume_Lots"})
        price_df["Date"] = pd.to_datetime(price_df["Date"]).dt.date
        price_df["Volume_Lots"] = price_df["Volume_Lots"].astype(float) / 1000.0
        return price_df

    def _fetch_yahoo_price_history(self, meta: StockMeta, start_date: date, end_date: date) -> pd.DataFrame:
        try:
            history = yf.download(
                meta.symbol,
                start=start_date.isoformat(),
                end=(end_date + timedelta(days=1)).isoformat(),
                interval="1d",
                progress=False,
                auto_adjust=False,
                threads=False,
            )
        except Exception:
            return pd.DataFrame()
        if history.empty:
            return pd.DataFrame()
        if isinstance(history.columns, pd.MultiIndex):
            history.columns = history.columns.get_level_values(0)
        required = {"Date", "Close", "Volume"}
        reset = history.reset_index()
        if not required.issubset(set(reset.columns)):
            return pd.DataFrame()
        price_df = reset[["Date", "Close", "Volume"]].rename(columns={"Volume": "Volume_Lots"})
        price_df["Date"] = pd.to_datetime(price_df["Date"], errors="coerce").dt.date
        price_df["Volume_Lots"] = pd.to_numeric(price_df["Volume_Lots"], errors="coerce") / 1000.0
        price_df["Close"] = pd.to_numeric(price_df["Close"], errors="coerce")
        return price_df.dropna(subset=["Date", "Close"]).reset_index(drop=True)
    def _fetch_fugle_price_history(self, meta: StockMeta, start_date: date, end_date: date) -> pd.DataFrame:
        history = fetch_fugle_history(meta.symbol, start_date, end_date, "1d")
        if history.empty:
            return pd.DataFrame()
        price_df = history.rename(
            columns={
                "datetime": "Date",
                "close": "Close",
                "volume": "Volume_Lots",
            }
        )[["Date", "Close", "Volume_Lots"]].copy()
        price_df["Date"] = pd.to_datetime(price_df["Date"]).dt.date
        price_df["Volume_Lots"] = pd.to_numeric(price_df["Volume_Lots"], errors="coerce") / 1000.0
        return price_df.dropna(subset=["Date", "Close"]).reset_index(drop=True)

    def fetch_institutional_daily(self, meta: StockMeta, trading_dates: list[date]) -> pd.DataFrame:
        if meta.market == "TWSE":
            return self._fetch_twse_institutional_daily(meta, trading_dates)

        return self._fetch_tpex_institutional_history(meta, trading_dates)

    def _fetch_twse_institutional_daily(self, meta: StockMeta, trading_dates: list[date]) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for trading_date in trading_dates:
            try:
                payload = self._get_json(
                    "https://www.twse.com.tw/fund/T86",
                    params={
                        "response": "json",
                        "date": trading_date.strftime("%Y%m%d"),
                        "selectType": "ALL",
                    },
                    is_twse=True,
                )
            except httpx.HTTPError:
                fallback_row = self._fetch_finmind_institutional_daily(meta, trading_date)
                if fallback_row:
                    rows.append(fallback_row)
                continue
            if payload.get("stat") != "OK":
                fallback_row = self._fetch_finmind_institutional_daily(meta, trading_date)
                if fallback_row:
                    rows.append(fallback_row)
                continue

            found = False
            for row in payload.get("data", []):
                if str(row[0]).strip() != meta.code:
                    continue
                rows.append(
                    {
                        "Date": trading_date,
                        "Foreign_Net_Lots": _to_lots(row[4]),
                        "Investment_Trust_Net_Lots": _to_lots(row[10]),
                        "Dealer_Net_Lots": _to_lots(row[11]),
                    }
                )
                found = True
                break
            if not found:
                fallback_row = self._fetch_finmind_institutional_daily(meta, trading_date)
                if fallback_row:
                    rows.append(fallback_row)

        return pd.DataFrame(rows)

    def _fetch_finmind_institutional_daily(self, meta: StockMeta, trading_date: date) -> dict[str, Any] | None:
        try:
            payload = self._get_json(
                FINMIND_API_URL,
                params={
                    "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
                    "data_id": meta.code,
                    "start_date": trading_date.isoformat(),
                    "end_date": trading_date.isoformat(),
                },
            )
        except httpx.HTTPError:
            self._append_note_once("部分上市法人歷史資料改用 FinMind 備援；若仍缺漏，可能為免費額度限制或資料源暫時不可用。")
            return None

        if payload.get("status") != 200:
            self._append_note_once("部分上市法人歷史資料改用 FinMind 備援；若仍缺漏，可能為免費額度限制或資料源暫時不可用。")
            return None

        foreign = 0.0
        investment_trust = 0.0
        dealer = 0.0
        has_row = False
        for row in payload.get("data") or []:
            if str(row.get("date")) != trading_date.isoformat():
                continue
            name = str(row.get("name") or "")
            buy = _to_number(row.get("buy")) or 0.0
            sell = _to_number(row.get("sell")) or 0.0
            net_lots = (buy - sell) / 1000.0
            if name == "Foreign_Investor":
                foreign += net_lots
                has_row = True
            elif name == "Investment_Trust":
                investment_trust += net_lots
                has_row = True
            elif name in {"Dealer_self", "Dealer_Hedging"}:
                dealer += net_lots
                has_row = True

        if not has_row:
            return None

        self._append_note_once("部分上市法人歷史資料改用 FinMind 備援補齊。")
        return {
            "Date": trading_date,
            "Foreign_Net_Lots": foreign,
            "Investment_Trust_Net_Lots": investment_trust,
            "Dealer_Net_Lots": dealer,
        }

    def _fetch_tpex_institutional_history(self, meta: StockMeta, trading_dates: list[date]) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for trading_date in trading_dates:
            rows.append(
                {
                    "Date": trading_date,
                    "Foreign_Net_Lots": self._fetch_tpex_institutional_value("insti/qfiiStat", "searchType", meta.code, trading_date),
                    "Investment_Trust_Net_Lots": self._fetch_tpex_institutional_value("insti/sitcStat", "searchType", meta.code, trading_date),
                    "Dealer_Net_Lots": self._fetch_tpex_institutional_value("insti/dealerStat", "stype", meta.code, trading_date),
                }
            )

        self._append_note_once("上櫃三大法人改用 TPEx 官方歷史日報 JSON 端點補齊匯出範圍內的日序列。")
        return pd.DataFrame(rows)

    def _fetch_tpex_institutional_value(self, action: str, sort_key_name: str, stock_code: str, trading_date: date) -> float | None:
        cache_key = (action, sort_key_name, trading_date.isoformat())
        if cache_key not in self._tpex_institutional_cache:
            roc_date = f"{trading_date.year - 1911:03d}/{trading_date.month:02d}/{trading_date.day:02d}"
            net_map: dict[str, float] = {}
            try:
                for order in ("buy", "sell"):
                    payload = self._get_json(
                        f"https://www.tpex.org.tw/www/zh-tw/{action}",
                        params={
                            "date": roc_date,
                            "type": "Daily",
                            sort_key_name: order,
                        },
                    )
                    table = (payload.get("tables") or [{}])[0]
                    for row in table.get("data", []):
                        code = str(row[1]).strip()
                        net_value = _to_number(row[-1])
                        if code and net_value is not None:
                            net_map[code] = net_value
                self._tpex_institutional_cache[cache_key] = net_map
            except httpx.HTTPError:
                self._tpex_institutional_cache[cache_key] = None
                self._append_note_once("部分上櫃法人歷史資料因 TPEx 單日端點暫時拒絕存取而留白，不影響其餘分頁匯出。")

        cached_map = self._tpex_institutional_cache[cache_key]
        if cached_map is None:
            return None
        return cached_map.get(stock_code, 0.0)

    def fetch_margin_daily(self, meta: StockMeta, trading_dates: list[date]) -> pd.DataFrame:
        if meta.market == "TWSE":
            return self._fetch_twse_margin_daily(meta, trading_dates)
        return self._fetch_tpex_margin_daily(meta, trading_dates)

    def _fetch_twse_margin_daily(self, meta: StockMeta, trading_dates: list[date]) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for trading_date in trading_dates:
            payload = self._get_json(
                "https://www.twse.com.tw/exchangeReport/TWT93U",
                params={
                    "response": "json",
                    "date": trading_date.strftime("%Y%m%d"),
                },
                is_twse=True,
            )
            if payload.get("stat") != "OK":
                continue
            for row in payload.get("data", []):
                if str(row[0]).strip() != meta.code:
                    continue
                financing_buy = _to_lots(row[4])
                financing_sell = _to_lots(row[3])
                cash_redemption = _to_lots(row[5])
                short_sale = _to_lots(row[9])
                short_covering = _to_lots(row[10])
                stock_redemption = _to_lots(row[11])
                financing_balance = _to_lots(row[6])
                short_balance = _to_lots(row[12])
                rows.append(
                    {
                        "Date": trading_date,
                        "Financing_Balance_Lots": financing_balance,
                        "Financing_Buy_Lots": financing_buy,
                        "Financing_Sell_Lots": financing_sell,
                        "Cash_Redemption_Lots": cash_redemption,
                        "Financing_Net_Change_Lots": (financing_buy or 0) - (financing_sell or 0) - (cash_redemption or 0),
                        "Short_Balance_Lots": short_balance,
                        "Short_Sell_Lots": short_sale,
                        "Short_Covering_Lots": short_covering,
                        "Stock_Redemption_Lots": stock_redemption,
                        "Short_Net_Change_Lots": (short_sale or 0) - (short_covering or 0) - (stock_redemption or 0),
                        "Short_Margin_Ratio": _safe_ratio(short_balance, financing_balance),
                    }
                )
                break

        return pd.DataFrame(rows)

    def _fetch_tpex_margin_daily(self, meta: StockMeta, trading_dates: list[date]) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for trading_date in trading_dates:
            roc_date = f"{trading_date.year - 1911:03d}/{trading_date.month:02d}/{trading_date.day:02d}"
            try:
                payload = self._get_json(
                    "https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php",
                    params={
                        "l": "zh-tw",
                        "o": "json",
                        "d": roc_date,
                        "s": "0,asc,0",
                    },
                )
            except (httpx.HTTPError, ValueError):
                self._append_note_once("部分上櫃融資融券歷史資料因 TPEx 單日端點暫時回傳異常內容而留白，不影響其餘分頁匯出。")
                continue
            table = (payload.get("tables") or [{}])[0]
            for row in table.get("data", []):
                if str(row[0]).strip() != meta.code:
                    continue
                financing_buy = _to_number(row[3])
                financing_sell = _to_number(row[4])
                cash_redemption = _to_number(row[5])
                short_sale = _to_number(row[12])
                short_covering = _to_number(row[13])
                stock_redemption = _to_number(row[14])
                financing_balance = _to_number(row[6])
                short_balance = _to_number(row[15])
                rows.append(
                    {
                        "Date": trading_date,
                        "Financing_Balance_Lots": financing_balance,
                        "Financing_Buy_Lots": financing_buy,
                        "Financing_Sell_Lots": financing_sell,
                        "Cash_Redemption_Lots": cash_redemption,
                        "Financing_Net_Change_Lots": (financing_buy or 0) - (financing_sell or 0) - (cash_redemption or 0),
                        "Short_Balance_Lots": short_balance,
                        "Short_Sell_Lots": short_sale,
                        "Short_Covering_Lots": short_covering,
                        "Stock_Redemption_Lots": stock_redemption,
                        "Short_Net_Change_Lots": (short_sale or 0) - (short_covering or 0) - (stock_redemption or 0),
                        "Short_Margin_Ratio": _safe_ratio(short_balance, financing_balance),
                    }
                )
                break

        return pd.DataFrame(rows)

    def fetch_monthly_revenue(self, meta: StockMeta, start_year: int) -> pd.DataFrame:
        folder = "sii" if meta.market == "TWSE" else "otc"
        start_date = date(start_year, 1, 1)
        end_date = datetime.now().date().replace(day=1)
        rows: list[dict[str, Any]] = []

        for month_start in _month_starts_between(start_date, end_date):
            roc_year = month_start.year - 1911
            url = f"https://mopsov.twse.com.tw/nas/t21/{folder}/t21sc03_{roc_year}_{month_start.month}_0.html"
            try:
                html = self._get_text(url)
            except httpx.HTTPError:
                continue

            try:
                tables = pd.read_html(StringIO(html), flavor="lxml")
            except ValueError:
                continue

            revenue_row = self._find_revenue_row(tables, meta.code)
            if revenue_row is None:
                continue

            rows.append(
                {
                    "Month": month_start,
                    "Monthly_Revenue": _to_number(revenue_row.get("營業收入-當月營收")),
                    "Prior_Month_Revenue": _to_number(revenue_row.get("營業收入-上月營收")),
                    "Prior_Year_Revenue": _to_number(revenue_row.get("營業收入-去年當月營收")),
                    "Cumulative_Revenue": _to_number(revenue_row.get("累計營業收入-當月累計營收")),
                }
            )

        if not rows:
            raise StockExportError(f"無法取得 {meta.display_name} 的月營收資料")

        revenue_df = pd.DataFrame(rows).drop_duplicates(subset=["Month"]).sort_values("Month").reset_index(drop=True)
        revenue_df["MoM%"] = revenue_df["Monthly_Revenue"].pct_change() * 100
        revenue_df["YoY%"] = revenue_df["Monthly_Revenue"].pct_change(12) * 100
        return revenue_df

    def _find_revenue_row(self, tables: list[pd.DataFrame], stock_code: str) -> dict[str, Any] | None:
        def find_column(columns: list[str], *fragments: str) -> str | None:
            normalized_columns = [re.sub(r"\s+", "", column) for column in columns]
            normalized_fragments = [re.sub(r"\s+", "", fragment) for fragment in fragments]
            for index, normalized in enumerate(normalized_columns):
                if all(fragment in normalized for fragment in normalized_fragments):
                    return columns[index]
            return None

        for table in tables:
            candidate = table.copy()
            candidate.columns = _flatten_columns(candidate.columns)
            code_column = find_column(candidate.columns, "公司", "代號")
            if code_column is None:
                continue

            current_revenue_column = find_column(candidate.columns, "當月營收")
            prior_month_column = find_column(candidate.columns, "上月營收")
            prior_year_column = find_column(candidate.columns, "去年當月營收")
            cumulative_column = find_column(candidate.columns, "當月累計營收")
            if not current_revenue_column:
                continue

            candidate[code_column] = candidate[code_column].astype(str).str.strip()
            matched = candidate[candidate[code_column] == stock_code]
            if matched.empty:
                continue

            row = matched.iloc[0]
            return {
                "營業收入-當月營收": row.get(current_revenue_column),
                "營業收入-上月營收": row.get(prior_month_column) if prior_month_column else None,
                "營業收入-去年當月營收": row.get(prior_year_column) if prior_year_column else None,
                "累計營業收入-當月累計營收": row.get(cumulative_column) if cumulative_column else None,
            }

        return None

    def fetch_quarterly_financials(self, meta: StockMeta) -> pd.DataFrame:
        quarterly_rows: list[dict[str, Any]] = []
        for gregorian_year, quarter in _quarter_sequence(2023, datetime.now().year, 4):
            quarter_row = self._fetch_mops_quarter_income_statement(meta, gregorian_year - 1911, quarter)
            if quarter_row:
                quarterly_rows.append(quarter_row)

        if not quarterly_rows:
            return pd.DataFrame(columns=["Quarter", "Revenue", "Gross_Profit", "Operating_Income", "Net_Income", "EPS"])

        financial_df = pd.DataFrame(quarterly_rows)
        financial_df = financial_df.drop_duplicates(subset=["Quarter"]).sort_values("Quarter").reset_index(drop=True)
        if len(financial_df) > 12:
            financial_df = financial_df.tail(12).reset_index(drop=True)

        self.notes.append("季財報改用 MOPS Plus 官方 API 逐季回溯，已涵蓋自 2023 年起的 12 季資料。")
        return financial_df

    def _fetch_mops_quarter_income_statement(self, meta: StockMeta, roc_year: int, quarter: int) -> dict[str, Any] | None:
        payload = {
            "companyId": meta.code,
            "dataType": "2",
            "season": str(quarter),
            "year": str(roc_year),
            "subsidiaryCompanyId": "",
        }
        try:
            response = self.client.post(
                f"{MOPS_API_BASE_URL}t164sb04",
                json=payload,
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            data = response.json()
        except Exception:
            return None

        report_list = (data.get("result") or {}).get("reportList") or []
        if not report_list:
            return None

        report_map: dict[str, float | None] = {}
        for row in report_list:
            if not row:
                continue
            label = str(row[0]).strip()
            report_map[label] = _to_number(row[1]) if len(row) > 1 else None

        return {
            "Quarter": f"{roc_year + 1911}Q{quarter}",
            "Company_Name": meta.name,
            "Revenue": report_map.get("營業收入合計"),
            "Gross_Profit": report_map.get("營業毛利（毛損）淨額") or report_map.get("營業毛利（毛損）"),
            "Operating_Income": report_map.get("營業利益（損失）"),
            "Net_Income": report_map.get("本期淨利（淨損）") or report_map.get("母公司業主（淨利／損）"),
            "EPS": report_map.get("　基本每股盈餘") or report_map.get("基本每股盈餘"),
        }

    def merge_daily_frames(
        self,
        price_df: pd.DataFrame,
        institutional_df: pd.DataFrame,
        margin_df: pd.DataFrame,
    ) -> pd.DataFrame:
        merged = price_df.copy()
        for frame in (institutional_df, margin_df):
            if frame.empty:
                continue
            merged = merged.merge(frame, on="Date", how="left")
        return merged.sort_values("Date").reset_index(drop=True)

    def build_strategy_summary(
        self,
        meta: StockMeta,
        daily_df: pd.DataFrame,
        revenue_df: pd.DataFrame,
        financial_df: pd.DataFrame,
    ) -> pd.DataFrame:
        summary_rows = [{"Item": "股票", "Value": meta.display_name}]

        if not daily_df.empty:
            latest_daily = daily_df.dropna(subset=["Date"]).iloc[-1]
            summary_rows.append({"Item": "最新交易日", "Value": latest_daily["Date"].isoformat()})
            financing_delta = latest_daily.get("Financing_Net_Change_Lots")
            short_delta = latest_daily.get("Short_Net_Change_Lots")
            if pd.notna(financing_delta) and pd.notna(short_delta) and financing_delta > 0 and short_delta > 0:
                summary_rows.append({"Item": "警示", "Value": "資券同增"})

        if not revenue_df.empty:
            latest_revenue = revenue_df.iloc[-1]
            rolling_max = revenue_df["Monthly_Revenue"].max()
            if latest_revenue["Monthly_Revenue"] == rolling_max:
                summary_rows.append({"Item": "營收訊號", "Value": "最新月營收創樣本期新高"})
            summary_rows.append({"Item": "最新月營收", "Value": f"{latest_revenue['Monthly_Revenue']:,.0f}"})

        if not financial_df.empty:
            latest_financial = financial_df.iloc[-1]
            quarter_value = latest_financial.get("Quarter", "")
            eps_value = latest_financial.get("EPS")
            if quarter_value:
                summary_rows.append({"Item": "最新財報季度", "Value": quarter_value})
            if pd.notna(eps_value):
                summary_rows.append({"Item": "最新 EPS", "Value": f"{eps_value:.2f}"})

        for note in self.notes:
            summary_rows.append({"Item": "備註", "Value": note})

        return pd.DataFrame(summary_rows)

