from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
import pandas as pd
import pytz
import yfinance as yf

from chip_strategies import get_tw_today
from stock_ai_bot.data_sources.fugle_data import fetch_fugle_history
from technical_scanner import (
    MA_BREAKOUT_PERIODS,
    MA_BREAKOUT_SIGNAL_LABELS,
    MA_RECLAIM_SIGNAL_LABELS,
    MA_SIGNAL_TRIGGER_BREAKOUT,
    MA_SIGNAL_TRIGGER_RECLAIM,
    ma_breakout_signal_label,
    ma_signal_label_from_triggers,
)
from technical_indicator_service import apply_technical_indicators

from progress_logger import now_timestamp
from stock_ai_bot.telegram.telegram_stock_formatting import mark_stock_text


OFFICIAL_NAME_CACHE: dict[str, str] = {}
OFFICIAL_SYMBOL_CACHE: dict[str, str] = {}
OFFICIAL_NAME_CACHE_EXPIRES_AT: datetime | None = None
OFFICIAL_NAME_CACHE_TTL = timedelta(hours=12)
TWSE_NAME_API_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_NAME_API_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
TWSE_SECURITY_DAILY_API_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_MIS_QUOTE_API_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
MONITOR_DATA_SOURCE = "Yahoo Finance / TWSE MIS / Fugle / 本機快取"
MONITOR_MA_PERIODS = MA_BREAKOUT_PERIODS


def _looks_like_twse_etf_code(code: str) -> bool:
    normalized_code = str(code).upper().strip()
    return normalized_code.startswith("00") and len(normalized_code) >= 5 and normalized_code.isalnum()


def _cache_symbol_name(target_cache: dict[str, str], code: str, suffix: str, name: str) -> None:
    normalized_code = str(code).upper().strip()
    normalized_name = str(name).strip()
    if not normalized_code or not normalized_name:
        return

    target_cache[f"{normalized_code}.{suffix}"] = normalized_name
    target_cache.setdefault(normalized_code, normalized_name)


def _remember_official_symbol_name(code: str, suffix: str, name: str) -> str:
    normalized_code = str(code).upper().strip()
    symbol = f"{normalized_code}.{suffix}"
    _cache_symbol_name(OFFICIAL_NAME_CACHE, normalized_code, suffix, name)
    OFFICIAL_SYMBOL_CACHE[normalized_code] = symbol
    return symbol


def fetch_official_stock_name_cache() -> dict[str, str]:
    global OFFICIAL_NAME_CACHE_EXPIRES_AT

    now = datetime.now()
    if OFFICIAL_NAME_CACHE and OFFICIAL_NAME_CACHE_EXPIRES_AT and now < OFFICIAL_NAME_CACHE_EXPIRES_AT:
        return OFFICIAL_NAME_CACHE

    updated_cache: dict[str, str] = {}
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True, verify=False) as client:
            twse_response = client.get(TWSE_NAME_API_URL)
            twse_response.raise_for_status()
            for item in twse_response.json():
                code = str(item.get("公司代號", "")).strip()
                name = str(item.get("公司簡稱") or item.get("公司名稱") or "").strip()
                if code and name:
                    _cache_symbol_name(updated_cache, code, "TW", name)

            tpex_response = client.get(TPEX_NAME_API_URL)
            tpex_response.raise_for_status()
            for item in tpex_response.json():
                code = str(item.get("SecuritiesCompanyCode", "")).strip()
                name = str(item.get("CompanyAbbreviation") or item.get("CompanyName") or "").strip()
                if code and name:
                    _cache_symbol_name(updated_cache, code, "TWO", name)

            try:
                twse_daily_response = client.get(TWSE_SECURITY_DAILY_API_URL)
                twse_daily_response.raise_for_status()
                for item in twse_daily_response.json():
                    code = str(item.get("Code", "")).strip()
                    name = str(item.get("Name") or "").strip()
                    if _looks_like_twse_etf_code(code) and name:
                        _cache_symbol_name(updated_cache, code, "TW", name)
            except Exception as exc:
                print(f"[{now_timestamp()}] ⚠️ 取得 TWSE ETF 名稱清單失敗，改用 MIS 單檔查詢備援：{exc}")

        OFFICIAL_NAME_CACHE.clear()
        OFFICIAL_NAME_CACHE.update(updated_cache)

        OFFICIAL_SYMBOL_CACHE.clear()
        for symbol in updated_cache:
            if "." in symbol:
                OFFICIAL_SYMBOL_CACHE[symbol.split(".", 1)[0]] = symbol

        OFFICIAL_NAME_CACHE_EXPIRES_AT = now + OFFICIAL_NAME_CACHE_TTL
    except Exception as exc:
        print(f"[{now_timestamp()}] ⚠️ 取得官方股票名稱清單失敗，改用既有快取或代號：{exc}")

    return OFFICIAL_NAME_CACHE


def _fetch_twse_mis_stock_name(symbol: str) -> str:
    normalized_symbol = str(symbol).upper().strip()
    if not normalized_symbol:
        return ""

    base_symbol = normalized_symbol.split(".", 1)[0]
    if not _looks_like_twse_etf_code(base_symbol):
        return ""

    try:
        with httpx.Client(timeout=10.0, follow_redirects=True, verify=False) as client:
            response = client.get(
                TWSE_MIS_QUOTE_API_URL,
                params={
                    "ex_ch": build_official_realtime_channel(f"{base_symbol}.TW"),
                    "json": "1",
                    "delay": "0",
                },
                headers={"Referer": "https://mis.twse.com.tw/stock/fibest.jsp"},
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        print(f"[{now_timestamp()}] ⚠️ 取得 TWSE MIS 名稱失敗 {symbol}: {exc}")
        return ""

    msg = (data.get("msgArray") or [{}])[0]
    returned_code = str(msg.get("c") or "").upper().strip()
    name = str(msg.get("n") or "").strip()
    if returned_code == base_symbol and name:
        return name
    return ""


def get_official_stock_name(symbol: str) -> str:
    normalized_symbol = str(symbol).upper().strip()
    if not normalized_symbol:
        return ""

    cache = fetch_official_stock_name_cache()
    if normalized_symbol in cache:
        return cache[normalized_symbol]

    base_symbol = normalized_symbol.split(".", 1)[0]
    cached_name = cache.get(base_symbol, "")
    if cached_name:
        return cached_name

    if _looks_like_twse_etf_code(base_symbol):
        etf_name = _fetch_twse_mis_stock_name(f"{base_symbol}.TW")
        if etf_name:
            _remember_official_symbol_name(base_symbol, "TW", etf_name)
            return etf_name

    return ""


def get_canonical_stock_symbol(symbol: str) -> str:
    normalized_symbol = str(symbol).upper().strip()
    if not normalized_symbol:
        return ""

    fetch_official_stock_name_cache()
    base_symbol = normalized_symbol.split(".", 1)[0]
    canonical_symbol = OFFICIAL_SYMBOL_CACHE.get(base_symbol)

    if "." in normalized_symbol:
        suffix = normalized_symbol.split(".", 1)[1]
        if suffix == "TW" and _looks_like_twse_etf_code(base_symbol) and normalized_symbol not in OFFICIAL_NAME_CACHE:
            etf_name = _fetch_twse_mis_stock_name(normalized_symbol)
            if etf_name:
                _remember_official_symbol_name(base_symbol, "TW", etf_name)
                return normalized_symbol
        return normalized_symbol if normalized_symbol in OFFICIAL_NAME_CACHE else (canonical_symbol or normalized_symbol)

    if not canonical_symbol and _looks_like_twse_etf_code(base_symbol):
        etf_name = _fetch_twse_mis_stock_name(f"{base_symbol}.TW")
        if etf_name:
            canonical_symbol = _remember_official_symbol_name(base_symbol, "TW", etf_name)

    return canonical_symbol or normalized_symbol


def normalize_stock_entry(stock: Any) -> dict[str, str] | None:
    if isinstance(stock, str):
        symbol = get_canonical_stock_symbol(stock)
        return {"symbol": symbol, "name": ""} if symbol else None

    if isinstance(stock, dict):
        symbol = get_canonical_stock_symbol(stock.get("symbol", ""))
        name = str(stock.get("name", "")).strip()
        if symbol:
            return {"symbol": symbol, "name": name}

    return None


def get_monitor_stocks(config: dict[str, Any]) -> list[dict[str, str]]:
    normalized_stocks = []
    seen_symbols: set[str] = set()
    for stock in config.get("monitor_stocks", []):
        normalized_stock = normalize_stock_entry(stock)
        if normalized_stock:
            symbol = normalized_stock["symbol"]
            if symbol in seen_symbols:
                continue
            seen_symbols.add(symbol)
            if not normalized_stock["name"]:
                normalized_stock["name"] = get_official_stock_name(normalized_stock["symbol"])
            normalized_stocks.append(normalized_stock)
    return normalized_stocks


def format_stock_display(stock: dict[str, str] | None) -> str:
    if not stock:
        return ""
    if stock.get("name"):
        return f"{stock['symbol']} ({stock['name']})"
    return stock.get("symbol", "")


def find_stock_index(stocks: list[Any], symbol: str) -> int:
    symbol = get_canonical_stock_symbol(symbol)
    for index, stock in enumerate(stocks):
        normalized_stock = normalize_stock_entry(stock)
        if normalized_stock and normalized_stock["symbol"] == symbol:
            return index
    return -1


def build_monitor_list_message(config: dict[str, Any]) -> str:
    stocks = get_monitor_stocks(config)
    stock_lines = [mark_stock_text(format_stock_display(stock)) for stock in stocks]
    return "📋 目前監控清單：\n" + ("\n".join(stock_lines) if stock_lines else "監控清單空白")


def add_monitor_stock_to_config(config: dict[str, Any], args: list[str]) -> tuple[bool, str]:
    if not args:
        return False, "請輸入股票代號，例如：/add_m 2330 台積電"

    stock = get_canonical_stock_symbol(args[0])
    stock_name = " ".join(args[1:]).strip() or get_official_stock_name(stock)
    stock_display = mark_stock_text(format_stock_display({"symbol": stock, "name": stock_name}))
    if find_stock_index(config.get("monitor_stocks", []), stock) != -1:
        return False, f"⚠️ 已在監控清單：{stock_display}"

    entry: dict[str, str] | str = {"symbol": stock, "name": stock_name} if stock_name else stock
    config.setdefault("monitor_stocks", []).append(entry)
    return True, f"✅ 已加入：{stock_display}"


def remove_monitor_stock_from_config(config: dict[str, Any], args: list[str]) -> tuple[bool, str]:
    if not args:
        return False, "請輸入股票代號，例如：/del_m 2330"

    stock_index = find_stock_index(config.get("monitor_stocks", []), args[0])
    if stock_index == -1:
        return False, f"⚠️ 監控清單找不到：{args[0].upper()}"

    removed_stock = normalize_stock_entry(config["monitor_stocks"][stock_index])
    config["monitor_stocks"].pop(stock_index)
    return True, f"🗑️ 已從監控清單刪除：{mark_stock_text(format_stock_display(removed_stock))}"


def get_tw_local_now() -> datetime:
    return datetime.now(pytz.timezone("Asia/Taipei"))


def build_official_realtime_channel(symbol: str) -> str:
    normalized_symbol = str(symbol).upper().strip()
    code = normalized_symbol.split(".", 1)[0]
    suffix = normalized_symbol.split(".", 1)[1] if "." in normalized_symbol else ""
    market = "otc" if suffix == "TWO" else "tse"
    return f"{market}_{code}.tw"


def parse_official_price_value(raw_value: Any) -> float | None:
    if raw_value in (None, "", "-"):
        return None

    first_value = str(raw_value).split("_", 1)[0].strip()
    if first_value in ("", "-"):
        return None

    try:
        return float(first_value)
    except (TypeError, ValueError):
        return None


def pick_official_quote_price(msg: dict[str, Any]) -> tuple[float | None, str | None]:
    trade_price = parse_official_price_value(msg.get("z"))
    if trade_price is not None:
        return trade_price, "TWSE MIS 即時成交價"

    best_bid = parse_official_price_value(msg.get("b"))
    best_ask = parse_official_price_value(msg.get("a"))
    if best_bid is not None and best_ask is not None:
        return (best_bid + best_ask) / 2.0, "TWSE MIS 買賣中間價"
    if best_bid is not None:
        return best_bid, "TWSE MIS 最佳買價"
    if best_ask is not None:
        return best_ask, "TWSE MIS 最佳賣價"

    return None, None


def get_official_realtime_price(symbol: str) -> tuple[float | None, str | None, str | None]:
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True, verify=False) as client:
            response = client.get(
                TWSE_MIS_QUOTE_API_URL,
                params={
                    "ex_ch": build_official_realtime_channel(symbol),
                    "json": "1",
                    "delay": "0",
                },
                headers={"Referer": "https://mis.twse.com.tw/stock/fibest.jsp"},
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        print(f"[{now_timestamp()}] ⚠️ 取得官方即時報價失敗 {symbol}: {exc}")
        return None, None, None

    msg = (data.get("msgArray") or [{}])[0]
    official_price, official_source = pick_official_quote_price(msg)
    quote_date = str(msg.get("d") or "").strip()
    return official_price, quote_date, official_source


def get_current_market_price(symbol: str, fallback_price: float | None = None) -> tuple[float | None, str]:
    today_tw = get_tw_local_now().strftime("%Y%m%d")
    yahoo_price = None
    yahoo_quote_date = None

    try:
        ticker = yf.Ticker(symbol)
        try:
            fast_info = ticker.fast_info
            last_price = fast_info.get("lastPrice") if fast_info else None
            if last_price not in (None, 0):
                yahoo_price = float(last_price)
        except Exception:
            pass

        try:
            info = ticker.info
            market_timestamp = info.get("regularMarketTime")
            if market_timestamp:
                yahoo_quote_date = datetime.fromtimestamp(
                    market_timestamp,
                    tz=pytz.utc,
                ).astimezone(pytz.timezone("Asia/Taipei")).strftime("%Y%m%d")

            for key in ("regularMarketPrice", "currentPrice"):
                value = info.get(key)
                if value not in (None, 0):
                    yahoo_price = float(value)
                    break
        except Exception:
            pass
    except Exception as exc:
        print(f"[{now_timestamp()}] ⚠️ 取得 Yahoo 即時報價失敗 {symbol}: {exc}")

    if yahoo_price is not None and yahoo_quote_date == today_tw:
        return yahoo_price, "Yahoo Finance"

    official_price, official_quote_date, official_source = get_official_realtime_price(symbol)
    if official_price is not None and official_quote_date == today_tw:
        return official_price, official_source or "TWSE MIS"

    return fallback_price, "日K收盤價 fallback"


def download_daily_history_with_fugle_fallback(symbol: str, period: str = "500d") -> pd.DataFrame:
    try:
        frame = yf.download(symbol, period=period, interval="1d", progress=False, auto_adjust=False)
    except Exception:
        frame = pd.DataFrame()

    if not frame.empty:
        return frame

    end_date = get_tw_today()
    start_date = end_date - timedelta(days=760)
    fugle_frame = fetch_fugle_history(symbol, start_date, end_date, "1d")
    if fugle_frame.empty:
        return pd.DataFrame()

    converted = fugle_frame.rename(
        columns={
            "datetime": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    ).set_index("Date")
    return converted[["Open", "High", "Low", "Close", "Volume"]]


def _prepare_daily_frame(symbol: str, minimum_rows: int) -> pd.DataFrame:
    frame = download_daily_history_with_fugle_fallback(symbol, period="500d")
    if frame.empty or len(frame) < minimum_rows:
        return pd.DataFrame()

    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    source = frame.copy().reset_index()
    date_column = "Date" if "Date" in source.columns else source.columns[0]
    source = source.rename(
        columns={
            date_column: "date",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Adj Close": "adj_close",
            "Volume": "volume",
        }
    )
    required = {"date", "open", "high", "low", "close", "volume"}
    if not required.issubset(source.columns):
        return pd.DataFrame()
    source["date"] = pd.to_datetime(source["date"], errors="coerce")
    source = source.dropna(subset=["date"]).sort_values("date")
    adjusted = apply_technical_indicators(source)
    adjusted = adjusted.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "adj_close": "Adj Close",
            "volume": "Volume",
        }
    ).set_index("date")
    return adjusted


def _format_price(value: object) -> str:
    if value is None:
        return "未知"
    try:
        if pd.isna(value):
            return "未知"
        return f"{float(value):,.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "未知"


def _monitor_minimum_rows(period: int) -> int:
    return max(int(period) + 2, 30)


def check_ma_breakout_signal(stock: dict[str, str], period: int) -> dict[str, object] | None:
    symbol = stock["symbol"]
    stock_display = format_stock_display(stock)
    label = ma_breakout_signal_label(period)
    print(f"[{now_timestamp()}] 檢查監控策略：{label} {stock_display}...")
    try:
        frame = _prepare_daily_frame(symbol, _monitor_minimum_rows(period))
        if frame.empty or len(frame) < period + 2:
            return None

        ma_column = f"MA{period}"
        if ma_column not in frame.columns:
            frame[ma_column] = frame["Close"].rolling(window=period).mean()
        latest_close = frame["Close"].iloc[-1].item()
        current_price, price_source = get_current_market_price(symbol, fallback_price=latest_close)
        if current_price is None:
            return None

        today_ma = frame[ma_column].iloc[-1].item()
        yesterday_price = frame["Close"].iloc[-2].item()
        yesterday_ma = frame[ma_column].iloc[-2].item()
        today_low = frame["Low"].iloc[-1].item()
        if pd.isna(today_ma) or pd.isna(yesterday_ma):
            return None

        triggers: list[str] = []
        if float(yesterday_price) <= float(yesterday_ma) and float(current_price) > float(today_ma):
            triggers.append(MA_SIGNAL_TRIGGER_BREAKOUT)
        if float(today_low) < float(today_ma) and float(current_price) > float(today_ma):
            triggers.append(MA_SIGNAL_TRIGGER_RECLAIM)
        if not triggers:
            return None

        return {
            "symbol": symbol,
            "stock_display": stock_display,
            "period": period,
            "label": ma_signal_label_from_triggers(period, triggers),
            "trigger_type": " / ".join(triggers),
            "current_price": float(current_price),
            "ma_value": float(today_ma),
            "stop_loss": float(frame["Low"].iloc[-3:].min().item()),
            "price_source": price_source,
        }
    except Exception as exc:
        print(f"[{now_timestamp()}] ⚠️ {label} 策略檢查失敗 {stock_display}: {exc}")
    return None


def _format_monitor_signal(signal: dict[str, object]) -> str:
    period = int(signal.get("period") or 0)
    stock_display = mark_stock_text(signal.get("stock_display") or "")
    label = str(signal.get("label") or ma_signal_label_from_triggers(period, str(signal.get("trigger_type") or "").split(" / ")))
    return (
        f"{stock_display} | "
        f"現價：{_format_price(signal.get('current_price'))} | "
        f"停損：{_format_price(signal.get('stop_loss'))} | "
        f"來源：{signal.get('price_source') or '未知'}\n"
        f"訊號：{label}\n"
        f"MA：MA{period} {_format_price(signal.get('ma_value'))}"
    )


def _unique_texts(values: list[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _merge_monitor_stock_signals(signals: list[dict[str, object]]) -> dict[str, object]:
    first = signals[0]
    stop_losses = [
        float(signal["stop_loss"])
        for signal in signals
        if signal.get("stop_loss") is not None and pd.notna(signal.get("stop_loss"))
    ]
    return {
        "symbol": first.get("symbol"),
        "stock_display": first.get("stock_display"),
        "current_price": first.get("current_price"),
        "stop_loss": min(stop_losses) if stop_losses else first.get("stop_loss"),
        "price_source": " / ".join(_unique_texts([signal.get("price_source") for signal in signals])),
        "signals": _unique_texts([signal.get("label") for signal in signals]),
        "ma_values": [
            {
                "period": int(signal.get("period") or 0),
                "ma_value": signal.get("ma_value"),
                "trigger_type": signal.get("trigger_type"),
                "label": signal.get("label"),
            }
            for signal in signals
        ],
    }


def _format_monitor_signal_item(item: dict[str, object]) -> str:
    ma_values = item.get("ma_values") if isinstance(item.get("ma_values"), list) else []
    ma_text = "、".join(
        f"MA{int(row.get('period') or 0)} {_format_price(row.get('ma_value'))}"
        for row in ma_values
        if isinstance(row, dict)
    )
    signal_text = "、".join(_unique_texts(list(item.get("signals") or []))) or "未標示"
    stock_display = mark_stock_text(item.get("stock_display") or "")
    return (
        f"{stock_display} | "
        f"現價：{_format_price(item.get('current_price'))} | "
        f"停損：{_format_price(item.get('stop_loss'))} | "
        f"來源：{item.get('price_source') or '未知'}\n"
        f"訊號：{signal_text}\n"
        f"MA：{ma_text or '未標示'}"
    )


def check_signal(stock: dict[str, str]) -> str | None:
    signal = check_ma_breakout_signal(stock, 21)
    return _format_monitor_signal(signal) if signal else None


def check_advanced_signal(stock: dict[str, str]) -> str | None:
    symbol = stock["symbol"]
    stock_display = format_stock_display(stock)
    print(f"[{now_timestamp()}] 🔎 檢查監控策略：MACD 紅柱突破 {stock_display}...")
    try:
        frame = _prepare_daily_frame(symbol, 150)
        if frame.empty:
            return None

        if "MA21" not in frame.columns:
            frame["MA21"] = frame["Close"].rolling(window=21).mean()
        frame["MACD_Hist"] = frame["MACD_HIST"]

        if frame["MACD_Hist"].iloc[-1] <= 0:
            return None

        green_days = frame[frame["MACD_Hist"] <= 0]
        if green_days.empty:
            return None

        red_start_date = green_days.index[-1] + pd.Timedelta(days=1)
        red_zone_past = frame.loc[red_start_date: frame.index[-2]]
        if red_zone_past.empty:
            return None

        touch_days = red_zone_past.index[red_zone_past["Low"] <= red_zone_past["MA21"]]
        if len(touch_days) == 0:
            return None

        touch_pos = red_zone_past.index.get_loc(touch_days[-1])
        pre_touch_zone = red_zone_past.iloc[:touch_pos]
        if pre_touch_zone.empty:
            return None

        breakout_high = pre_touch_zone["High"].max()
        period_high = breakout_high
        previous_close = frame["Close"].iloc[-2].item()
        latest_close = frame["Close"].iloc[-1].item()
        current_price, price_source = get_current_market_price(symbol, fallback_price=latest_close)
        above_ma21 = bool(current_price and current_price > frame["MA21"].iloc[-1].item())

        if previous_close <= breakout_high and current_price and current_price > breakout_high and above_ma21:
            stop_loss = frame["Low"].iloc[-3:].min().item()
            return (
                "🚨 MACD 紅柱突破訊號\n"
                f"股票：{mark_stock_text(stock_display)}\n"
                f"現價：{current_price:,.2f}\n"
                f"價格來源：{price_source}\n"
                f"條件：紅柱期間曾回測 21MA，今日第一次突破回測前高 {period_high:,.2f}\n"
                f"參考停損：{stop_loss:,.2f} (近 3 日低點)"
            )
    except Exception as exc:
        print(f"[{now_timestamp()}] ⚠️ MACD 策略檢查失敗 {stock_display}: {exc}")
    return None


def check_ma105_signal(stock: dict[str, str]) -> str | None:
    signal = check_ma_breakout_signal(stock, 105)
    return _format_monitor_signal(signal) if signal else None


def collect_monitor_signal_groups(config: dict[str, Any]) -> dict[str, list[dict[str, object]]]:
    labels = {
        label
        for period in MONITOR_MA_PERIODS
        for label in (MA_BREAKOUT_SIGNAL_LABELS[period], MA_RECLAIM_SIGNAL_LABELS[period])
    }
    grouped_signals: dict[str, list[dict[str, object]]] = {label: [] for label in labels}
    for stock in get_monitor_stocks(config):
        for period in MONITOR_MA_PERIODS:
            signal = check_ma_breakout_signal(stock, period)
            if signal:
                grouped_signals.setdefault(str(signal["label"]), []).append(signal)
    return {label: items for label, items in grouped_signals.items() if items}


def collect_monitor_signal_items(config: dict[str, Any]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for stock in get_monitor_stocks(config):
        stock_signals: list[dict[str, object]] = []
        for period in MONITOR_MA_PERIODS:
            signal = check_ma_breakout_signal(stock, period)
            if signal:
                stock_signals.append(signal)
        if stock_signals:
            items.append(_merge_monitor_stock_signals(stock_signals))
    return items


def _format_monitor_signal_groups(groups: dict[str, list[dict[str, object]]]) -> list[str]:
    blocks: list[str] = []
    for period in MONITOR_MA_PERIODS:
        for label in (MA_BREAKOUT_SIGNAL_LABELS[period], MA_RECLAIM_SIGNAL_LABELS[period]):
            signals = groups.get(label) or []
            if not signals:
                continue
            lines = [f"📂 {label}"]
            lines.extend(_format_monitor_signal(signal) for signal in signals)
            blocks.append("\n".join(lines))
    return blocks


def collect_monitor_signals(config: dict[str, Any]) -> list[str]:
    return [_format_monitor_signal_item(item) for item in collect_monitor_signal_items(config)]


def append_monitor_data_footer(text: str) -> str:
    return (
        f"{text.rstrip()}\n\n"
        f"資料日期：{get_tw_today().isoformat()}\n"
        f"資料來源：{MONITOR_DATA_SOURCE}"
    )


def build_monitor_scan_report(
    config: dict[str, Any],
    title: str | None = None,
    no_signal_text: str = "目前無突破訊號。",
) -> str:
    signals = collect_monitor_signals(config)
    body = f"觸發：{len(signals)} 檔\n\n" + "\n\n".join(signals) if signals else no_signal_text
    if title:
        body = f"{title}\n\n{body}"
    return append_monitor_data_footer(body)
