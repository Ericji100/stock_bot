from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from io import BytesIO
import json
import zipfile

from bs4 import BeautifulSoup
import httpx
import pandas as pd
import pytz
import yfinance as yf


TW_TIMEZONE = pytz.timezone("Asia/Taipei")
US_INDEX_SYMBOLS = [
    ("^DJI", "道瓊工業", 2),
    ("^GSPC", "標普 500", 2),
    ("^IXIC", "納斯達克", 2),
    ("^SOX", "費城半導", 2),
]
TW_INDEX_CHANNELS = [
    ("tse_t00.tw", "加權指數", 2),
    ("otc_o00.tw", "櫃買指數", 2),
]
TAIFEX_DAILY_CSV_URL = "https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_{date_str}.zip"
TWSE_INDEX_API_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
YAHOO_TX_FUTURES_URL = "https://tw.stock.yahoo.com/future/WTX%40"
TX_PRODUCT_CODE = "TX"
SESSION_DAY = "day"
SESSION_NIGHT = "night"
MORNING_START = time(hour=6, minute=0)
MORNING_END = time(hour=9, minute=0)


class MarketSummaryError(Exception):
    pass


@dataclass(frozen=True)
class QuoteSnapshot:
    label: str
    close: float
    change: float
    percent_change: float
    quote_date: date
    decimals: int = 2


def get_tw_now() -> datetime:
    return datetime.now(TW_TIMEZONE)


def is_morning_push_window(now: datetime | None = None) -> bool:
    current = now.astimezone(TW_TIMEZONE) if now else get_tw_now()
    current_time = current.time().replace(tzinfo=None)
    return MORNING_START <= current_time <= MORNING_END


def build_morning_market_report(reference_time: datetime | None = None) -> str:
    report_time = reference_time.astimezone(TW_TIMEZONE) if reference_time else get_tw_now()
    us_quotes = [fetch_latest_yfinance_quote(symbol, label, decimals) for symbol, label, decimals in US_INDEX_SYMBOLS]
    try:
        tx_night_quote = fetch_latest_tx_night_session_quote(report_time.date())
        tx_lines = [format_quote_line(tx_night_quote)]
    except MarketSummaryError:
        tx_lines = ["• 最新夜盤資料暫時無法取得"]

    us_lines = [format_quote_line(quote) for quote in us_quotes]

    return "\n".join(
        [
            "🌅 【晨間市場速報】",
            f"📅 日期：{report_time.date().isoformat()}",
            "",
            "🇺🇸 美股四大指數：",
            *us_lines,
            "",
            "🇹🇼 台指期 (夜盤收盤)：",
            *tx_lines,
            "",
            f"資料日期：{report_time.date().isoformat()}",
            "資料來源：Yahoo Finance / TAIFEX / 本機快取",
        ]
    )


def build_noon_market_report(reference_time: datetime | None = None) -> str:
    report_time = reference_time.astimezone(TW_TIMEZONE) if reference_time else get_tw_now()
    report_date = report_time.date()
    tw_quotes = [fetch_latest_tw_index_quote(channel, label, decimals) for channel, label, decimals in TW_INDEX_CHANNELS]

    if any(quote.quote_date != report_date for quote in tw_quotes):
        raise MarketSummaryError("今日尚無完整台股收盤資料，若為非交易日將自動略過。")

    tx_day_quote = fetch_latest_tx_session_quote(SESSION_DAY, report_date)
    if tx_day_quote.quote_date != report_date:
        raise MarketSummaryError("今日台指期日盤收盤資料尚未更新。")

    tw_lines = [format_quote_line(quote) for quote in tw_quotes]
    tx_lines = [format_quote_line(tx_day_quote)]

    return "\n".join(
        [
            "📊 【台股收盤總結】",
            f"📅 日期：{report_date.isoformat()}",
            "",
            "🇹🇼 台灣現貨指數：",
            *tw_lines,
            "",
            "🇹🇼 台指期 (日盤收盤)：",
            *tx_lines,
            "",
            f"資料日期：{report_date.isoformat()}",
            "資料來源：TWSE MIS / TAIFEX / 本機快取",
        ]
    )


def fetch_latest_yfinance_quote(symbol: str, label: str, decimals: int = 2) -> QuoteSnapshot:
    history = yf.Ticker(symbol).history(period="10d", interval="1d", auto_adjust=False)
    history = history.dropna(subset=["Close"])

    if len(history.index) < 2:
        raise MarketSummaryError(f"{label} 歷史報價不足。")

    latest_row = history.iloc[-1]
    previous_row = history.iloc[-2]
    close_price = float(latest_row["Close"])
    previous_close = float(previous_row["Close"])
    change = close_price - previous_close
    percent_change = (change / previous_close * 100.0) if previous_close else 0.0
    quote_date = normalize_history_date(history.index[-1])

    return QuoteSnapshot(
        label=label,
        close=close_price,
        change=change,
        percent_change=percent_change,
        quote_date=quote_date,
        decimals=decimals,
    )


def fetch_latest_tw_index_quote(channel: str, label: str, decimals: int = 2) -> QuoteSnapshot:
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, verify=False) as client:
            response = client.get(
                TWSE_INDEX_API_URL,
                params={
                    "ex_ch": channel,
                    "json": "1",
                    "delay": "0",
                },
                headers={
                    "Referer": "https://mis.twse.com.tw/stock/fibest.jsp",
                    "User-Agent": "Mozilla/5.0",
                },
            )
            response.raise_for_status()
            payload = json.loads(response.text.strip())
    except Exception as exc:
        raise MarketSummaryError(f"{label} 官方報價取得失敗。") from exc

    quote = (payload.get("msgArray") or [{}])[0]
    close_price = parse_official_number(quote.get("z"))
    previous_close = parse_official_number(quote.get("y"))
    quote_date = parse_quote_date(quote.get("d"))

    if close_price is None or previous_close is None or quote_date is None:
        raise MarketSummaryError(f"{label} 官方報價資料不足。")

    change = close_price - previous_close
    percent_change = (change / previous_close * 100.0) if previous_close else 0.0

    return QuoteSnapshot(
        label=label,
        close=close_price,
        change=change,
        percent_change=percent_change,
        quote_date=quote_date,
        decimals=decimals,
    )


def normalize_history_date(raw_value) -> date:
    if isinstance(raw_value, pd.Timestamp):
        if raw_value.tzinfo is None:
            return raw_value.date()
        return raw_value.tz_convert(TW_TIMEZONE).date()

    if isinstance(raw_value, datetime):
        return raw_value.astimezone(TW_TIMEZONE).date() if raw_value.tzinfo else raw_value.date()

    if isinstance(raw_value, date):
        return raw_value

    raise MarketSummaryError("無法辨識報價日期格式。")


def parse_quote_date(raw_value: str | None) -> date | None:
    if not raw_value:
        return None

    raw_text = str(raw_value).strip()
    if len(raw_text) != 8 or not raw_text.isdigit():
        return None

    return datetime.strptime(raw_text, "%Y%m%d").date()


def parse_official_number(raw_value) -> float | None:
    if raw_value in (None, "", "-", "--", "---", "----"):
        return None

    raw_text = str(raw_value).strip().replace(",", "")
    if raw_text in ("", "-", "--", "---", "----"):
        return None

    try:
        return float(raw_text)
    except ValueError:
        return None


def fetch_latest_tx_session_quote(session_type: str, reference_date: date) -> QuoteSnapshot:
    sessions = load_tx_session_closes(reference_date)
    current_session = pick_latest_session(sessions, session_type, reference_date)
    if current_session is None:
        raise MarketSummaryError("台指期近月資料不足。")

    previous_session = pick_previous_session(sessions, session_type, current_session.quote_date)
    if previous_session is None:
        raise MarketSummaryError("台指期近月資料不足。")

    change = current_session.close - previous_session.close
    percent_change = (change / previous_session.close * 100.0) if previous_session.close else 0.0

    return QuoteSnapshot(
        label="台指期近月",
        close=current_session.close,
        change=change,
        percent_change=percent_change,
        quote_date=current_session.quote_date,
        decimals=0,
    )


def fetch_latest_tx_night_session_quote(reference_date: date) -> QuoteSnapshot:
    yahoo_quote = fetch_latest_tx_quote_from_yahoo(reference_date)
    if yahoo_quote is not None:
        return yahoo_quote

    return fetch_latest_tx_night_session_quote_from_taifex(reference_date)


def fetch_latest_tx_quote_from_yahoo(reference_date: date) -> QuoteSnapshot | None:
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, verify=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
            response = client.get(YAHOO_TX_FUTURES_URL)
            response.raise_for_status()
    except Exception:
        return None

    quote = parse_yahoo_tx_futures_header(response.text)
    if quote is None or quote.quote_date != reference_date:
        return None

    return quote


def parse_yahoo_tx_futures_header(html_text: str) -> QuoteSnapshot | None:
    soup = BeautifulSoup(html_text, "html.parser")
    header = soup.find(id="main-1-FutureHeader-Proxy")
    if header is None:
        return None

    label_node = header.find("h1")
    price_node = header.find("span", class_=lambda value: value and "Fz(32px)" in value)
    change_node = header.find("span", class_=lambda value: value and "Fz(20px)" in value and "Mend(4px)" in value)
    percent_node = header.find("span", string=re.compile(r"\([-+]?\d+(?:\.\d+)?%\)"))
    time_node = header.find("span", string=re.compile(r"\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}"))

    label = label_node.get_text(strip=True) if label_node else ""
    close_price = parse_official_number(price_node.get_text(strip=True) if price_node else None)
    change = parse_signed_yahoo_number(change_node)
    percent_change = parse_signed_yahoo_percent(percent_node, change)
    quote_date = parse_yahoo_update_date(time_node.get_text(" ", strip=True) if time_node else None)

    if not label or close_price is None or change is None or percent_change is None or quote_date is None:
        return None

    return QuoteSnapshot(
        label=label,
        close=close_price,
        change=change,
        percent_change=percent_change,
        quote_date=quote_date,
        decimals=0,
    )


def parse_signed_yahoo_number(node) -> float | None:
    if node is None:
        return None

    value = parse_official_number(node.get_text(strip=True))
    if value is None:
        return None

    text = node.get_text(strip=True)
    if text.startswith("-"):
        return value

    class_text = " ".join(node.get("class") or []).lower()
    return -abs(value) if "down" in class_text else abs(value)


def parse_signed_yahoo_percent(node, change: float | None) -> float | None:
    if node is None:
        return None

    text = node.get_text(strip=True).strip("()")
    if text.endswith("%"):
        text = text[:-1]

    value = parse_official_number(text)
    if value is None:
        return None

    if text.startswith("-"):
        return value

    if change is not None and change < 0:
        return -abs(value)

    class_text = " ".join(node.get("class") or []).lower()
    return -abs(value) if "down" in class_text else abs(value)


def parse_yahoo_update_date(raw_value: str | None) -> date | None:
    if not raw_value:
        return None

    match = re.search(r"(\d{4})/(\d{2})/(\d{2})\s+\d{2}:\d{2}", raw_value)
    if not match:
        return None

    return datetime.strptime(match.group(0), "%Y/%m/%d %H:%M").date()


def fetch_latest_tx_night_session_quote_from_taifex(reference_date: date) -> QuoteSnapshot:
    sessions = load_tx_session_closes(reference_date)
    current_night_session = pick_latest_session(sessions, SESSION_NIGHT, reference_date)
    if current_night_session is None:
        raise MarketSummaryError("台指期近月夜盤資料不足。")

    if current_night_session.quote_date != expected_taifex_night_session_date(reference_date):
        raise MarketSummaryError("台指期近月夜盤資料尚未更新。")

    same_date_day_session = pick_exact_session(sessions, SESSION_DAY, current_night_session.quote_date)
    if same_date_day_session is None:
        raise MarketSummaryError("台指期近月日盤基準資料不足。")

    change = current_night_session.close - same_date_day_session.close
    percent_change = (change / same_date_day_session.close * 100.0) if same_date_day_session.close else 0.0

    return QuoteSnapshot(
        label="台指期近月",
        close=current_night_session.close,
        change=change,
        percent_change=percent_change,
        quote_date=current_night_session.quote_date,
        decimals=0,
    )


def expected_taifex_night_session_date(reference_date: date) -> date:
    return reference_date - timedelta(days=1)


def load_tx_session_closes(reference_date: date) -> list[QuoteSnapshot]:
    frames = []
    start_date = reference_date - timedelta(days=10)

    for current_day in date_range(start_date, reference_date):
        daily_frame = load_tx_ticks_for_day(current_day)
        if not daily_frame.empty:
            frames.append(daily_frame)

    if not frames:
        raise MarketSummaryError("無法取得台指期近月報價。")

    merged = pd.concat(frames, ignore_index=True)
    merged.sort_values(["session_date", "actual_datetime"], inplace=True)
    session_closes = (
        merged.groupby(["session_type", "session_date"], as_index=False)
        .tail(1)
        .sort_values(["session_type", "session_date"])
    )

    return [
        QuoteSnapshot(
            label=row["session_type"],
            close=float(row["price"]),
            change=0.0,
            percent_change=0.0,
            quote_date=row["session_date"].date(),
            decimals=0,
        )
        for _, row in session_closes.iterrows()
    ]


def pick_latest_session(sessions: list[QuoteSnapshot], session_type: str, reference_date: date) -> QuoteSnapshot | None:
    candidates = [session for session in sessions if session.label == session_type and session.quote_date <= reference_date]
    return candidates[-1] if candidates else None


def pick_previous_session(sessions: list[QuoteSnapshot], session_type: str, reference_date: date) -> QuoteSnapshot | None:
    candidates = [session for session in sessions if session.label == session_type and session.quote_date < reference_date]
    return candidates[-1] if candidates else None


def pick_exact_session(sessions: list[QuoteSnapshot], session_type: str, quote_date: date) -> QuoteSnapshot | None:
    for session in sessions:
        if session.label == session_type and session.quote_date == quote_date:
            return session
    return None


def load_tx_ticks_for_day(target_date: date) -> pd.DataFrame:
    zip_bytes = download_taifex_zip(target_date)
    if zip_bytes is None:
        return pd.DataFrame()

    try:
        with zipfile.ZipFile(BytesIO(zip_bytes)) as archive:
            csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if not csv_names:
                return pd.DataFrame()

            with archive.open(csv_names[0]) as csv_file:
                frame = pd.read_csv(
                    csv_file,
                    encoding="cp950",
                    usecols=[0, 1, 2, 3, 4, 5],
                    dtype=str,
                )
    except zipfile.BadZipFile:
        return pd.DataFrame()

    frame.columns = [
        "trade_date",
        "product_code",
        "expiry_month",
        "trade_time",
        "price",
        "volume",
    ]
    frame["product_code"] = frame["product_code"].str.strip()
    frame["expiry_month"] = frame["expiry_month"].str.strip()
    frame["trade_time"] = frame["trade_time"].str.strip().str.zfill(6)
    frame = frame[frame["product_code"] == TX_PRODUCT_CODE].copy()
    frame = frame[frame["expiry_month"].str.fullmatch(r"\d{6}", na=False)]

    if frame.empty:
        return frame

    frame["trade_date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d", errors="coerce")
    frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
    frame = frame.dropna(subset=["trade_date", "price"])

    if frame.empty:
        return frame

    time_values = frame["trade_time"].astype(int)
    frame["actual_datetime"] = pd.to_datetime(
        frame["trade_date"].dt.strftime("%Y%m%d") + frame["trade_time"],
        format="%Y%m%d%H%M%S",
        errors="coerce",
    )
    frame["session_type"] = pd.NA
    frame.loc[(time_values >= 84500) & (time_values <= 134500), "session_type"] = SESSION_DAY
    frame.loc[(time_values >= 150000) | (time_values <= 50000), "session_type"] = SESSION_NIGHT
    frame = frame.dropna(subset=["actual_datetime", "session_type"])

    if frame.empty:
        return frame

    frame["session_date"] = frame["trade_date"]
    overnight_mask = time_values <= 50000
    frame.loc[overnight_mask, "session_date"] = frame.loc[overnight_mask, "trade_date"] - pd.Timedelta(days=1)
    frame["expiry_rank"] = pd.to_numeric(frame["expiry_month"], errors="coerce")
    frame["near_expiry"] = frame.groupby("session_date")["expiry_rank"].transform("min")
    frame = frame[frame["expiry_rank"] == frame["near_expiry"]].copy()

    return frame[["actual_datetime", "session_date", "session_type", "price"]]


def download_taifex_zip(target_date: date) -> bytes | None:
    url = TAIFEX_DAILY_CSV_URL.format(date_str=target_date.strftime("%Y_%m_%d"))
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, verify=False) as client:
            response = client.get(url)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.content if is_zip_bytes(response.content) else None
    except Exception:
        return None


def is_zip_bytes(content: bytes) -> bool:
    return content.startswith(b"PK\x03\x04")


def date_range(start_date: date, end_date: date):
    current_day = start_date
    while current_day <= end_date:
        yield current_day
        current_day += timedelta(days=1)


def format_quote_line(quote: QuoteSnapshot) -> str:
    value_text = format_number(quote.close, quote.decimals)
    change_text = format_signed_number(quote.change, quote.decimals)
    percent_text = format_signed_number(quote.percent_change, 2)
    return f"• {quote.label}：{value_text} ( {change_text} | {percent_text}% )"


def format_number(value: float, decimals: int) -> str:
    return f"{value:,.{decimals}f}"


def format_signed_number(value: float, decimals: int) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value):,.{decimals}f}"
