from __future__ import annotations

import calendar
import json
import math
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Iterable

import httpx
import pandas as pd
import yfinance as yf

from fugle_data import fetch_fugle_history


TWSE_NAME_API_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_NAME_API_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
TWSE_STOCK_DAY_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
TPEX_DAILY_CLOSE_QUOTES_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TW_TZ = "Asia/Taipei"
TMP_DIR = Path(tempfile.gettempdir()) / "stock_tg_bot"
SUPPORTED_FREQUENCIES = {"1d", "1m", "5m", "15m", "60m"}
YFINANCE_INTRADAY_LIMIT_DAYS = {
    "1m": 30,
    "5m": 60,
    "15m": 60,
    "60m": 730,
}


class StockChartError(Exception):
    pass


@dataclass(frozen=True)
class StockChartRequest:
    code: str
    start_date: date
    end_date: date
    frequency: str = "1d"


@dataclass(frozen=True)
class StockChartMeta:
    code: str
    symbol: str
    market: str
    name: str

    @property
    def display_name(self) -> str:
        return f"{self.code} {self.name}".strip()


def parse_stock_chart_args(args: list[str]) -> StockChartRequest:
    if len(args) < 3:
        raise StockChartError(
            "請輸入股票代號、開始日期、結束日期，例如 /stock_chart 2330 2026-01-01 2026-05-01 1d"
        )

    code = str(args[0]).strip()
    if not code.isdigit() or len(code) not in {4, 6}:
        raise StockChartError("股票代號必須是 4 碼或 6 碼數字，例如 2330 或 0050。")

    try:
        start_date = datetime.strptime(args[1], "%Y-%m-%d").date()
        end_date = datetime.strptime(args[2], "%Y-%m-%d").date()
    except ValueError as exc:
        raise StockChartError("日期格式必須是 YYYY-MM-DD。") from exc

    if start_date > end_date:
        raise StockChartError("開始日期不可晚於結束日期。")

    frequency = args[3].lower() if len(args) >= 4 else "1d"
    if frequency not in SUPPORTED_FREQUENCIES:
        raise StockChartError("頻率只支援 1d、1m、5m、15m、60m。")

    if frequency != "1d":
        max_days = YFINANCE_INTRADAY_LIMIT_DAYS[frequency]
        warmup_days = estimate_intraday_warmup_days(frequency)
        requested_days = (end_date - start_date).days + 1
        if (date.today() - end_date).days > max_days or requested_days + warmup_days > max_days:
            raise StockChartError(
                f"{frequency} 分鐘資料目前僅支援最近 {max_days} 天內的區間，請縮短查詢範圍或改用 1d。"
            )

    return StockChartRequest(
        code=code,
        start_date=start_date,
        end_date=end_date,
        frequency=frequency,
    )


def build_stock_chart_document(
    code: str,
    start_date: str,
    end_date: str,
    frequency: str = "1d",
) -> tuple[BytesIO, str, StockChartMeta]:
    request = parse_stock_chart_args([code, start_date, end_date, frequency])
    meta = resolve_stock_meta(request.code)
    bars = load_chart_bars(request, meta)
    bars = apply_indicators(bars)
    display_bars = slice_display_bars(bars, request)

    if display_bars.empty:
        raise StockChartError("指定區間沒有可顯示的 K 線資料。")

    html = build_html_template(build_chart_payload(display_bars, request, meta))
    filename = build_output_filename(meta, request)
    buffer = BytesIO(html.encode("utf-8"))
    buffer.seek(0)
    return buffer, filename, meta


def write_stock_chart_temp_file(
    code: str,
    start_date: str,
    end_date: str,
    frequency: str = "1d",
) -> Path:
    buffer, filename, _meta = build_stock_chart_document(code, start_date, end_date, frequency)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    output_path = TMP_DIR / filename
    output_path.write_bytes(buffer.getvalue())
    return output_path


def resolve_stock_meta(code: str) -> StockChartMeta:
    code = str(code).strip()
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, verify=False) as client:
            twse_rows = client.get(TWSE_NAME_API_URL)
            twse_rows.raise_for_status()
            for item in twse_rows.json():
                item_code = str(item.get("公司代號", "")).strip()
                if item_code == code:
                    return StockChartMeta(
                        code=code,
                        symbol=f"{code}.TW",
                        market="TWSE",
                        name=str(item.get("公司簡稱") or item.get("公司名稱") or "").strip(),
                    )

            tpex_rows = client.get(TPEX_NAME_API_URL)
            tpex_rows.raise_for_status()
            for item in tpex_rows.json():
                item_code = str(item.get("SecuritiesCompanyCode", "")).strip()
                if item_code == code:
                    return StockChartMeta(
                        code=code,
                        symbol=f"{code}.TWO",
                        market="TPEX",
                        name=str(item.get("CompanyAbbreviation") or item.get("CompanyName") or "").strip(),
                    )

            quote_meta = resolve_stock_meta_from_quote(client, code)
            if quote_meta is not None:
                return quote_meta
    except httpx.HTTPError as exc:
        raise StockChartError(f"查詢股票代碼時無法連線官方資料源：{exc}") from exc

    raise StockChartError(f"找不到股票代號：{code}")


def resolve_stock_meta_from_quote(client: httpx.Client, code: str) -> StockChartMeta | None:
    response = client.get(
        "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
        params={
            "ex_ch": f"tse_{code}.tw|otc_{code}.tw",
            "json": "1",
            "delay": "0",
        },
        headers={"Referer": "https://mis.twse.com.tw/stock/api/"},
    )
    response.raise_for_status()

    for item in response.json().get("msgArray", []):
        if str(item.get("c", "")).strip() != code:
            continue

        exchange = str(item.get("ex", "")).strip().lower()
        if exchange not in {"tse", "otc"}:
            continue

        market = "TWSE" if exchange == "tse" else "TPEX"
        symbol = f"{code}.TW" if market == "TWSE" else f"{code}.TWO"
        name = str(item.get("n") or item.get("nf") or "").strip()
        return StockChartMeta(code=code, symbol=symbol, market=market, name=name)

    return None


def load_chart_bars(request: StockChartRequest, meta: StockChartMeta) -> pd.DataFrame:
    if request.frequency == "1d":
        bars = load_daily_bars(request, meta)
    else:
        bars = load_intraday_bars(request, meta)

    if bars.empty:
        raise StockChartError("查無可用行情資料。")

    bars = bars.sort_values("datetime").drop_duplicates(subset=["datetime"]).reset_index(drop=True)
    return bars


def load_daily_bars(request: StockChartRequest, meta: StockChartMeta) -> pd.DataFrame:
    fetch_start = request.start_date - timedelta(days=260)
    fetch_end = request.end_date

    if meta.market == "TWSE":
        bars = fetch_twse_daily_history(meta.code, meta.symbol, fetch_start, fetch_end)
    else:
        bars = fetch_tpex_daily_history(meta.code, meta.symbol, fetch_start, fetch_end)

    if bars.empty:
        raise StockChartError(f"無法取得 {meta.display_name} 的日線資料。")

    return standardize_ohlcv_frame(bars, is_intraday=False)


def fetch_twse_daily_history(code: str, symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    months = month_starts_between(start_date, end_date)
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, verify=False) as client:
            for month_start in months:
                response = client.get(
                    TWSE_STOCK_DAY_URL,
                    params={
                        "response": "json",
                        "date": month_start.strftime("%Y%m01"),
                        "stockNo": code,
                    },
                )
                if response.status_code in {301, 302, 303, 307, 308}:
                    return fetch_yfinance_daily_history(symbol, start_date, end_date)
                response.raise_for_status()
                payload = response.json()
                if payload.get("stat") != "OK" or not payload.get("data"):
                    continue

                rows = []
                for row in payload["data"]:
                    rows.append(
                        {
                            "datetime": roc_date_to_timestamp(row[0]),
                            "open": to_number(row[3]),
                            "high": to_number(row[4]),
                            "low": to_number(row[5]),
                            "close": to_number(row[6]),
                            "volume": to_number(row[1]),
                        }
                    )

                frame = pd.DataFrame(rows)
                frames.append(frame)
    except Exception:
        return fetch_yfinance_daily_history(symbol, start_date, end_date)

    if not frames:
        return fetch_yfinance_daily_history(symbol, start_date, end_date)

    merged = pd.concat(frames, ignore_index=True)
    merged = merged.dropna(subset=["datetime", "open", "high", "low", "close"])
    merged = merged[(merged["datetime"].dt.date >= start_date) & (merged["datetime"].dt.date <= end_date)]
    return merged


def fetch_tpex_daily_history(code: str, symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
    official_snapshot = fetch_tpex_latest_snapshot(code)
    frame = fetch_yfinance_daily_history(symbol, start_date, end_date)

    if frame.empty:
        raise StockChartError(f"無法取得 {code} 的上櫃日線資料。")

    if official_snapshot:
        snapshot_date = official_snapshot["datetime"]
        mask = frame["datetime"] == snapshot_date
        if mask.any():
            for field in ("open", "high", "low", "close", "volume"):
                frame.loc[mask, field] = official_snapshot[field]

    return frame


def fetch_yfinance_daily_history(symbol: str, start_date: date, end_date: date) -> pd.DataFrame:
    yf_frame = download_yfinance_history(symbol, start_date, end_date + timedelta(days=1), interval="1d")
    if yf_frame.empty or "Date" not in yf_frame.columns:
        fugle_frame = fetch_fugle_history(symbol, start_date, end_date, "1d")
        if not fugle_frame.empty:
            return fugle_frame.rename(
                columns={
                    "datetime": "datetime",
                    "open": "open",
                    "high": "high",
                    "low": "low",
                    "close": "close",
                    "volume": "volume",
                }
            )[["datetime", "open", "high", "low", "close", "volume"]]
        return pd.DataFrame()

    frame = yf_frame.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    frame["datetime"] = pd.to_datetime(frame["Date"], errors="coerce").dt.normalize()
    return frame[["datetime", "open", "high", "low", "close", "volume"]]


def fetch_tpex_latest_snapshot(code: str) -> dict | None:
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, verify=False) as client:
            response = client.get(TPEX_DAILY_CLOSE_QUOTES_URL)
            response.raise_for_status()
            for item in response.json():
                if str(item.get("SecuritiesCompanyCode", "")).strip() != code:
                    continue
                return {
                    "datetime": roc_date_to_timestamp(item.get("Date", "")),
                    "open": to_number(item.get("Open")),
                    "high": to_number(item.get("High")),
                    "low": to_number(item.get("Low")),
                    "close": to_number(item.get("Close")),
                    "volume": to_number(item.get("TradingShares")),
                }
    except Exception:
        return None

    return None


def load_intraday_bars(request: StockChartRequest, meta: StockChartMeta) -> pd.DataFrame:
    fetch_start = request.start_date - timedelta(days=estimate_intraday_warmup_days(request.frequency))
    fetch_end = request.end_date + timedelta(days=1)
    base_frame = pd.DataFrame()

    if request.frequency == "1m":
        base_frame = download_yfinance_history(meta.symbol, fetch_start, fetch_end, interval="1m")
    else:
        base_frame = download_yfinance_history(meta.symbol, fetch_start, fetch_end, interval="1m")
        if base_frame.empty:
            base_frame = download_yfinance_history(meta.symbol, fetch_start, fetch_end, interval=request.frequency)

    if base_frame.empty:
        fugle_frame = fetch_fugle_history(meta.symbol, fetch_start, fetch_end, request.frequency)
        if not fugle_frame.empty:
            return standardize_ohlcv_frame(fugle_frame, is_intraday=True)
        raise StockChartError(
            f"無法取得 {meta.display_name} 的 {request.frequency} 分鐘資料，請縮短日期區間或改用 1d。"
        )

    frame = normalize_intraday_history(base_frame)
    if frame.empty:
        raise StockChartError("分鐘資料下載成功，但沒有落在台股 09:00 到 13:30 的有效交易時段。")

    if request.frequency == "1m":
        return frame

    if is_one_minute_frame(frame):
        return resample_intraday_bars(frame, request.frequency)

    return frame


def download_yfinance_history(symbol: str, start_date: date, end_date: date, interval: str) -> pd.DataFrame:
    history = yf.download(
        symbol,
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        interval=interval,
        progress=False,
        auto_adjust=False,
        threads=False,
    )

    if history.empty:
        return history

    if isinstance(history.columns, pd.MultiIndex):
        history.columns = history.columns.get_level_values(0)

    return history.reset_index()


def normalize_intraday_history(history: pd.DataFrame) -> pd.DataFrame:
    if "Datetime" in history.columns:
        timestamp_column = "Datetime"
    elif "Date" in history.columns:
        timestamp_column = "Date"
    else:
        return pd.DataFrame()

    frame = history.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    ).copy()
    frame["datetime"] = pd.to_datetime(frame[timestamp_column], errors="coerce")
    frame = frame.dropna(subset=["datetime", "open", "high", "low", "close"])

    if frame.empty:
        return frame

    if pd.api.types.is_datetime64tz_dtype(frame["datetime"]):
        frame["datetime"] = frame["datetime"].dt.tz_convert(TW_TZ).dt.tz_localize(None)
    else:
        frame["datetime"] = frame["datetime"].dt.tz_localize(TW_TZ, nonexistent="shift_forward", ambiguous="NaT").dt.tz_localize(None)

    frame = frame.sort_values("datetime")
    frame = frame[frame["datetime"].dt.dayofweek < 5]
    frame = frame[frame["datetime"].dt.time >= datetime.strptime("09:00", "%H:%M").time()]
    frame = frame[frame["datetime"].dt.time <= datetime.strptime("13:30", "%H:%M").time()]
    return standardize_ohlcv_frame(frame[["datetime", "open", "high", "low", "close", "volume"]], is_intraday=True)


def resample_intraday_bars(frame: pd.DataFrame, frequency: str) -> pd.DataFrame:
    rule = frequency.upper()
    indexed = frame.copy().set_index("datetime")
    grouped = (
        indexed.groupby(indexed.index.date, group_keys=False)
        .apply(
            lambda day_frame: day_frame.resample(rule, label="left", closed="left").agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
        )
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )
    return standardize_ohlcv_frame(grouped, is_intraday=True)


def standardize_ohlcv_frame(frame: pd.DataFrame, *, is_intraday: bool) -> pd.DataFrame:
    standardized = frame.copy()
    standardized["datetime"] = pd.to_datetime(standardized["datetime"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        standardized[column] = pd.to_numeric(standardized[column], errors="coerce")
    standardized = standardized.dropna(subset=["datetime", "open", "high", "low", "close"])
    standardized["volume"] = standardized["volume"].fillna(0)
    standardized["time"] = standardized["datetime"].map(to_unix_timestamp)
    standardized["is_intraday"] = is_intraday
    return standardized[["datetime", "time", "open", "high", "low", "close", "volume", "is_intraday"]]


def apply_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    enriched = frame.copy()
    enriched["MA21"] = enriched["close"].rolling(window=21).mean()
    enriched["MA105"] = enriched["close"].rolling(window=105).mean()
    lowest_low = enriched["low"].rolling(window=9, min_periods=1).min()
    highest_high = enriched["high"].rolling(window=9, min_periods=1).max()
    range_value = highest_high - lowest_low
    enriched["RSV"] = ((enriched["close"] - lowest_low) / range_value.replace(0, pd.NA) * 100.0).fillna(50.0)
    enriched["K"] = enriched["RSV"].ewm(alpha=9 / 55, adjust=False).mean()
    enriched["D"] = enriched["K"].ewm(alpha=9 / 55, adjust=False).mean()
    ema21 = enriched["close"].ewm(span=21, adjust=False).mean()
    ema55_fast = enriched["close"].ewm(span=55, adjust=False).mean()
    enriched["DIF"] = ema21 - ema55_fast
    enriched["DEA"] = enriched["DIF"].ewm(span=55, adjust=False).mean()
    enriched["Histogram"] = enriched["DIF"] - enriched["DEA"]
    return enriched


def slice_display_bars(frame: pd.DataFrame, request: StockChartRequest) -> pd.DataFrame:
    display = frame[
        (frame["datetime"].dt.date >= request.start_date)
        & (frame["datetime"].dt.date <= request.end_date)
    ].copy()
    return display.reset_index(drop=True)


def build_chart_payload(frame: pd.DataFrame, request: StockChartRequest, meta: StockChartMeta) -> dict:
    precision = infer_precision(frame)
    candle_data = []
    volume_data = []
    ma21_data = []
    ma105_data = []
    dif_data = []
    dea_data = []
    histogram_data = []
    k_data = []
    d_data = []
    legend_data = []
    is_intraday = bool(frame["is_intraday"].iloc[-1])

    for row in frame.itertuples(index=False):
        candle_data.append(
            {
                "time": int(row.time),
                "open": round_number(row.open, precision),
                "high": round_number(row.high, precision),
                "low": round_number(row.low, precision),
                "close": round_number(row.close, precision),
            }
        )
        volume_data.append(
            {
                "time": int(row.time),
                "value": int(row.volume),
                "color": "#ef5350" if row.close >= row.open else "#26a69a",
            }
        )
        histogram_data.append(
            {
                "time": int(row.time),
                "value": round_number(row.Histogram, 4),
                "color": "#ef5350" if row.Histogram >= 0 else "#26a69a",
            }
        )
        dif_data.append({"time": int(row.time), "value": round_number(row.DIF, 4)})
        dea_data.append({"time": int(row.time), "value": round_number(row.DEA, 4)})
        k_data.append({"time": int(row.time), "value": round_number(row.K, 4)})
        d_data.append({"time": int(row.time), "value": round_number(row.D, 4)})
        if not pd.isna(row.MA21):
            ma21_data.append({"time": int(row.time), "value": round_number(row.MA21, precision)})
        if not pd.isna(row.MA105):
            ma105_data.append({"time": int(row.time), "value": round_number(row.MA105, precision)})
        legend_data.append(
            {
                "time": int(row.time),
                "volume": int(row.volume),
                "ma21": round_number(row.MA21, precision),
                "ma105": round_number(row.MA105, precision),
                "dif": round_number(row.DIF, 4),
                "dea": round_number(row.DEA, 4),
                "histogram": round_number(row.Histogram, 4),
                "k": round_number(row.K, 4),
                "d": round_number(row.D, 4),
            }
        )

    return {
        "meta": {
            "title": f"{meta.display_name} 交互式量化分析圖表",
            "subtitle": f"{request.start_date} ~ {request.end_date}",
            "market": meta.market,
            "frequency": request.frequency,
            "barCount": len(candle_data),
            "precision": precision,
            "isIntraday": is_intraday,
        },
        "candles": candle_data,
        "volume": volume_data,
        "ma21": ma21_data,
        "ma105": ma105_data,
        "dif": dif_data,
        "dea": dea_data,
        "histogram": histogram_data,
        "k": k_data,
        "d": d_data,
        "legend": legend_data,
    }


def build_html_template(payload: dict) -> str:
    payload_json = json.dumps(payload, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang=\"zh-Hant\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">
  <title>{payload['meta']['title']}</title>
  <script src=\"https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js\"></script>
  <style>
    :root {{
      --bg: #05070b;
      --panel: #0a0e14;
      --line: rgba(150, 176, 214, 0.18);
      --text: #edf3ff;
      --muted: #8ea0be;
      --pill-bg: rgba(15, 22, 33, 0.92);
    }}
    * {{ box-sizing: border-box; }}
    html {{ height: 100%; background: var(--bg); }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: \"Microsoft JhengHei\", \"PingFang TC\", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(36, 226, 196, 0.08), transparent 24%),
        radial-gradient(circle at top right, rgba(211, 76, 196, 0.08), transparent 26%),
        linear-gradient(180deg, #06080d 0%, #04060a 100%);
    }}
    .page {{ width: 100vw; min-height: 100vh; margin: 0; padding: 6px; }}
    .hero {{
      margin-bottom: 6px;
      padding: 10px 12px 8px;
      border: 1px solid rgba(130, 153, 190, 0.15);
      border-radius: 12px;
      background: linear-gradient(180deg, rgba(9, 13, 20, 0.96), rgba(7, 10, 16, 0.92));
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.28);
    }}
    h1 {{ margin: 0; font-size: clamp(20px, 2.2vw, 30px); letter-spacing: 0.02em; line-height: 1.15; }}
    .meta {{ display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }}
    .pill {{
      padding: 5px 9px;
      border-radius: 999px;
      background: var(--pill-bg);
      border: 1px solid rgba(130, 153, 190, 0.16);
      color: var(--muted);
      font-size: 12px;
    }}
    .board {{
      padding: 4px;
      border-radius: 12px;
      border: 1px solid rgba(130, 153, 190, 0.14);
      background: var(--panel);
      box-shadow: 0 12px 36px rgba(0, 0, 0, 0.24);
    }}
    .chart-stack {{
      display: flex;
      flex-direction: column;
      gap: 4px;
      height: calc(100vh - 104px);
      min-height: 620px;
    }}
    .pane {{
      position: relative;
      overflow: hidden;
      min-height: 54px;
      border-radius: 10px;
      border: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(14, 19, 28, 0.98), rgba(10, 14, 20, 0.98));
    }}
    .pane.price-pane {{ height: 58%; }}
    .pane.indicator-pane {{ height: 14%; }}
    .pane.macd-pane {{ height: 20%; }}
    .pane.collapsed {{ height: 34px !important; min-height: 34px; }}
    .pane.collapsed .chart {{ display: none; }}
    .pane-controls {{
      position: absolute;
      top: 7px;
      right: 8px;
      z-index: 3;
      display: flex;
      gap: 6px;
      align-items: center;
    }}
    .control-btn {{
      height: 22px;
      padding: 0 8px;
      border-radius: 999px;
      border: 1px solid rgba(130, 153, 190, 0.18);
      background: rgba(16, 24, 36, 0.96);
      color: #dbe7ff;
      font-size: 11px;
      cursor: pointer;
    }}
    .resize-handle {{
      height: 7px;
      margin: -1px 8px;
      border-radius: 999px;
      cursor: row-resize;
      background: rgba(130, 153, 190, 0.18);
    }}
    .pane-label {{
      position: absolute;
      top: 8px;
      left: 8px;
      z-index: 2;
      padding: 4px 8px;
      border-radius: 999px;
      background: rgba(16, 24, 36, 0.96);
      border: 1px solid rgba(130, 153, 190, 0.12);
      color: var(--muted);
      font-size: 11px;
      letter-spacing: 0.04em;
    }}
    .legend {{
      position: absolute;
      top: 40px;
      left: 8px;
      z-index: 2;
      min-width: min(520px, calc(100% - 16px));
      padding: 8px 10px;
      border-radius: 10px;
      background: rgba(6, 10, 16, 0.92);
      border: 1px solid rgba(130, 153, 190, 0.12);
      backdrop-filter: blur(8px);
      font-size: 12px;
      line-height: 1.5;
      color: #dbe7ff;
    }}
    .legend strong {{ color: #ffffff; }}
    .chart {{ width: 100%; height: 100%; }}
    .kv {{ display: inline-block; margin-right: 10px; white-space: nowrap; }}
    .up {{ color: #ef5350; }}
    .down {{ color: #26a69a; }}
    .ma21 {{ color: #29d3c2; }}
    .ma105 {{ color: #d54bcb; }}
    .dif {{ color: #80cbc4; }}
    .dea {{ color: #ffd54f; }}
    .kd-k {{ color: #f6c453; }}
    .kd-d {{ color: #7aa2ff; }}
    @media (max-width: 768px) {{
      .page {{ padding: 4px; }}
      .hero {{ border-radius: 10px; padding: 8px 10px 7px; margin-bottom: 4px; }}
      .board {{ padding: 3px; border-radius: 10px; }}
      .chart-stack {{ gap: 3px; min-height: 560px; height: calc(100vh - 92px); }}
      .legend {{ top: 38px; font-size: 11px; padding: 7px 8px; }}
      .kv {{ margin-right: 8px; }}
    }}
  </style>
</head>
<body>
  <div class=\"page\">
    <section class=\"hero\">
      <h1>{payload['meta']['title']}</h1>
      <div class=\"meta\">
        <div class=\"pill\">區間: {payload['meta']['subtitle']}</div>
        <div class=\"pill\">市場: {payload['meta']['market']}</div>
        <div class=\"pill\">頻率: {payload['meta']['frequency']}</div>
        <div class=\"pill\">K 線數: {payload['meta']['barCount']}</div>
      </div>
    </section>
    <section class=\"board\">
      <div class=\"chart-stack\">
        <div class=\"pane price-pane\" data-pane=\"price\">
          <div class=\"pane-label\">Price + MA</div>
          <div class=\"pane-controls\"><button id=\"priceScaleToggle\" class=\"control-btn\" type=\"button\">價格軸：自動</button></div>
          <div id=\"legend\" class=\"legend\"></div>
          <div id=\"mainChart\" class=\"chart\"></div>
        </div>
        <div class=\"resize-handle\" data-resize-before=\"price\" data-resize-after=\"volume\"></div>
        <div class=\"pane indicator-pane\" data-pane=\"volume\"><div class=\"pane-label\">Volume</div><div class=\"pane-controls\"><button class=\"control-btn\" type=\"button\" data-toggle-pane=\"volume\">收合</button></div><div id=\"volumeChart\" class=\"chart\"></div></div>
        <div class=\"resize-handle\" data-resize-before=\"volume\" data-resize-after=\"kd\"></div>
        <div class=\"pane indicator-pane\" data-pane=\"kd\"><div class=\"pane-label\">KD 9 / 9 / 55</div><div class=\"pane-controls\"><button class=\"control-btn\" type=\"button\" data-toggle-pane=\"kd\">收合</button></div><div id=\"kdChart\" class=\"chart\"></div></div>
        <div class=\"resize-handle\" data-resize-before=\"kd\" data-resize-after=\"macd\"></div>
        <div class=\"pane macd-pane\" data-pane=\"macd\"><div class=\"pane-label\">MACD 21 / 55 / 55</div><div class=\"pane-controls\"><button class=\"control-btn\" type=\"button\" data-toggle-pane=\"macd\">收合</button></div><div id=\"macdChart\" class=\"chart\"></div></div>
      </div>
    </section>
  </div>

  <script>
    const payload = {payload_json};
    const legendEl = document.getElementById('legend');
    const taiwanDateFormatter = new Intl.DateTimeFormat('zh-TW', {{
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      timeZone: 'UTC',
    }});

    const commonOptions = {{
      layout: {{
        background: {{ color: 'rgba(0,0,0,0)' }},
        textColor: '#dbe7ff',
        fontFamily: 'Microsoft JhengHei, PingFang TC, sans-serif',
      }},
      localization: {{
        locale: 'zh-TW',
        timeFormatter: (time) => formatCrosshairTime(time),
      }},
      grid: {{
        vertLines: {{ color: 'rgba(122, 148, 186, 0.12)' }},
        horzLines: {{ color: 'rgba(122, 148, 186, 0.12)' }},
      }},
      handleScroll: {{ mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true }},
      handleScale: {{ mouseWheel: true, pinch: true, axisPressedMouseMove: true }},
      timeScale: {{
        timeVisible: payload.meta.isIntraday,
        secondsVisible: false,
        rightOffset: 6,
        barSpacing: 8,
        borderColor: 'rgba(122, 148, 186, 0.18)',
        tickMarkFormatter: (time) => formatTickMark(time),
      }},
      rightPriceScale: {{ borderColor: 'rgba(122, 148, 186, 0.18)' }},
      crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
    }};

    function normalizeDate(time) {{
      if (typeof time === 'number') return new Date(time * 1000);
      if (time && typeof time === 'object' && 'timestamp' in time) return new Date(time.timestamp * 1000);
      return new Date(0);
    }}

    function pad2(value) {{
      return String(value).padStart(2, '0');
    }}

    function formatDatePart(date) {{
      const parts = taiwanDateFormatter.formatToParts(date);
      const year = parts.find((part) => part.type === 'year')?.value ?? '0000';
      const month = parts.find((part) => part.type === 'month')?.value ?? '00';
      const day = parts.find((part) => part.type === 'day')?.value ?? '00';
      return `${{year}}/${{month}}/${{day}}`;
    }}

    function formatClock(date) {{
      return `${{pad2(date.getUTCHours())}}:${{pad2(date.getUTCMinutes())}}`;
    }}

    function formatCrosshairTime(time) {{
      const date = normalizeDate(time);
      if (!payload.meta.isIntraday) return formatDatePart(date);
      return `${{formatDatePart(date)}} ${{formatClock(date)}}`;
    }}

    function formatTickMark(time) {{
      const date = normalizeDate(time);
      const monthDay = `${{pad2(date.getUTCMonth() + 1)}}/${{pad2(date.getUTCDate())}}`;
      if (!payload.meta.isIntraday) return monthDay;
      const clock = formatClock(date);
      const hour = date.getUTCHours();
      const minute = date.getUTCMinutes();
      if (hour === 9 && minute === 0) return `${{monthDay}} 開盤`;
      return clock;
    }}

    function formatNumber(value, precision = payload.meta.precision) {{
      if (value === null || value === undefined || Number.isNaN(value)) return '--';
      return Number(value).toFixed(precision);
    }}

    function formatMacd(value) {{
      if (value === null || value === undefined || Number.isNaN(value)) return '--';
      return Number(value).toFixed(4);
    }}

    function formatVolume(value) {{
      if (value === null || value === undefined || Number.isNaN(value)) return '--';
      return Number(value).toLocaleString('zh-TW');
    }}

    const mainContainer = document.getElementById('mainChart');
    const volumeContainer = document.getElementById('volumeChart');
    const kdContainer = document.getElementById('kdChart');
    const macdContainer = document.getElementById('macdChart');

    function makeChart(container, extraOptions = {{}}) {{
      return LightweightCharts.createChart(container, {{
        width: container.clientWidth,
        height: container.clientHeight,
        ...commonOptions,
        ...extraOptions,
      }});
    }}

    const mainChart = makeChart(mainContainer);
    const volumeChart = makeChart(volumeContainer, {{
      rightPriceScale: {{ scaleMargins: {{ top: 0.1, bottom: 0.15 }}, borderColor: 'rgba(122, 148, 186, 0.18)' }},
    }});
    const kdChart = makeChart(kdContainer, {{
      rightPriceScale: {{ scaleMargins: {{ top: 0.12, bottom: 0.12 }}, borderColor: 'rgba(122, 148, 186, 0.18)' }},
    }});
    const macdChart = makeChart(macdContainer, {{
      rightPriceScale: {{ scaleMargins: {{ top: 0.12, bottom: 0.12 }}, borderColor: 'rgba(122, 148, 186, 0.18)' }},
    }});

    function addSeriesCompat(chart, typeKey, options, legacyMethodName) {{
      if (typeof chart.addSeries === 'function' && LightweightCharts[typeKey]) {{
        return chart.addSeries(LightweightCharts[typeKey], options);
      }}
      return chart[legacyMethodName](options);
    }}

    const candleSeries = addSeriesCompat(
      mainChart,
      'CandlestickSeries',
      {{ upColor: '#ef5350', downColor: '#26a69a', wickUpColor: '#ef5350', wickDownColor: '#26a69a', borderVisible: false }},
      'addCandlestickSeries',
    );
    const ma21Series = addSeriesCompat(
      mainChart,
      'LineSeries',
            {{ color: '#29d3c2', lineWidth: 2, priceLineVisible: false, lastValueVisible: true }},
      'addLineSeries',
    );
    const ma105Series = addSeriesCompat(
      mainChart,
      'LineSeries',
            {{ color: '#d54bcb', lineWidth: 2, priceLineVisible: false, lastValueVisible: true }},
      'addLineSeries',
    );
    const volumeSeries = addSeriesCompat(
      volumeChart,
      'HistogramSeries',
      {{ priceFormat: {{ type: 'volume' }}, priceLineVisible: false, lastValueVisible: false }},
      'addHistogramSeries',
    );
    const difSeries = addSeriesCompat(
      macdChart,
      'LineSeries',
      {{ color: '#80cbc4', lineWidth: 2, priceLineVisible: false }},
      'addLineSeries',
    );
    const deaSeries = addSeriesCompat(
      macdChart,
      'LineSeries',
      {{ color: '#ffd54f', lineWidth: 2, priceLineVisible: false }},
      'addLineSeries',
    );
    const histSeries = addSeriesCompat(
      macdChart,
      'HistogramSeries',
      {{ priceLineVisible: false, lastValueVisible: false }},
      'addHistogramSeries',
    );
    const kSeries = addSeriesCompat(
      kdChart,
      'LineSeries',
      {{ color: '#f6c453', lineWidth: 2, priceLineVisible: false }},
      'addLineSeries',
    );
    const dSeries = addSeriesCompat(
      kdChart,
      'LineSeries',
      {{ color: '#7aa2ff', lineWidth: 2, priceLineVisible: false }},
      'addLineSeries',
    );

    candleSeries.setData(payload.candles);
    ma21Series.setData(payload.ma21);
    ma105Series.setData(payload.ma105);
    volumeSeries.setData(payload.volume);
    kSeries.setData(payload.k);
    dSeries.setData(payload.d);
    difSeries.setData(payload.dif);
    deaSeries.setData(payload.dea);
    histSeries.setData(payload.histogram);

    const candleMap = new Map(payload.candles.map((item) => [item.time, item]));
    const legendMap = new Map(payload.legend.map((item) => [item.time, item]));

    function renderLegend(time) {{
      const candle = candleMap.get(time) || payload.candles[payload.candles.length - 1];
      const extras = legendMap.get(time) || payload.legend[payload.legend.length - 1];
      if (!candle || !extras) return;
      const dateText = formatCrosshairTime(candle.time);
      const closeClass = candle.close >= candle.open ? 'up' : 'down';
      const histClass = (extras.histogram ?? 0) >= 0 ? 'up' : 'down';
      legendEl.innerHTML = [
        `<span class=\"kv\"><strong>${{dateText}}</strong></span>`,
        `<span class=\"kv\">O <strong>${{formatNumber(candle.open)}}</strong></span>`,
        `<span class=\"kv\">H <strong>${{formatNumber(candle.high)}}</strong></span>`,
        `<span class=\"kv\">L <strong>${{formatNumber(candle.low)}}</strong></span>`,
        `<span class=\"kv ${{closeClass}}\">C <strong>${{formatNumber(candle.close)}}</strong></span>`,
        `<span class=\"kv\">V <strong>${{formatVolume(extras.volume)}}</strong></span>`,
        `<span class=\"kv ma21\">MA21 <strong>${{formatNumber(extras.ma21)}}</strong></span>`,
        `<span class=\"kv ma105\">MA105 <strong>${{formatNumber(extras.ma105)}}</strong></span>`,
        `<span class=\"kv dif\">DIF <strong>${{formatMacd(extras.dif)}}</strong></span>`,
        `<span class=\"kv dea\">DEA <strong>${{formatMacd(extras.dea)}}</strong></span>`,
        `<span class=\"kv kd-k\">K <strong>${{formatMacd(extras.k)}}</strong></span>`,
        `<span class=\"kv kd-d\">D <strong>${{formatMacd(extras.d)}}</strong></span>`,
        `<span class=\"kv ${{histClass}}\">Hist <strong>${{formatMacd(extras.histogram)}}</strong></span>`,
      ].join('');
    }}

    function syncVisibleRange(source, targets) {{
      let syncing = false;
      source.timeScale().subscribeVisibleLogicalRangeChange((range) => {{
        if (syncing || !range) return;
        syncing = true;
        targets.forEach((chart) => chart.timeScale().setVisibleLogicalRange(range));
        syncing = false;
      }});
    }}

    function bindLegend(chart) {{
      chart.subscribeCrosshairMove((param) => {{
        if (!param || param.time === undefined) {{
          renderLegend(payload.candles[payload.candles.length - 1]?.time);
          return;
        }}
        renderLegend(typeof param.time === 'number' ? param.time : param.time.timestamp);
      }});
    }}

    syncVisibleRange(mainChart, [volumeChart, kdChart, macdChart]);
    syncVisibleRange(volumeChart, [mainChart, kdChart, macdChart]);
    syncVisibleRange(kdChart, [mainChart, volumeChart, macdChart]);
    syncVisibleRange(macdChart, [mainChart, volumeChart, kdChart]);

    bindLegend(mainChart);
    bindLegend(volumeChart);
    bindLegend(kdChart);
    bindLegend(macdChart);
    renderLegend(payload.candles[payload.candles.length - 1]?.time);

    mainChart.timeScale().fitContent();
    volumeChart.timeScale().fitContent();
    kdChart.timeScale().fitContent();
    macdChart.timeScale().fitContent();

    const chartsByPane = {{ price: mainChart, volume: volumeChart, kd: kdChart, macd: macdChart }};
    const containersByPane = {{ price: mainContainer, volume: volumeContainer, kd: kdContainer, macd: macdContainer }};
    const paneElements = {{
      price: document.querySelector('[data-pane="price"]'),
      volume: document.querySelector('[data-pane="volume"]'),
      kd: document.querySelector('[data-pane="kd"]'),
      macd: document.querySelector('[data-pane="macd"]'),
    }};
    const chartStack = document.querySelector('.chart-stack');
    const indicatorKeys = ['volume', 'kd', 'macd'];
    const collapsedPaneHeight = 34;
    const minPriceHeight = 180;
    const minIndicatorHeight = 70;
    const defaultPaneHeights = {{ volume: 110, kd: 110, macd: 150 }};

    function getPaneHeight(pane) {{
      return pane.getBoundingClientRect().height || parseFloat(pane.style.height) || 0;
    }}

    function setPaneHeight(pane, height) {{
      pane.style.height = `${{Math.max(34, height)}}px`;
    }}

    function getAvailablePaneHeight() {{
      const handlesHeight = Array.from(document.querySelectorAll('.resize-handle'))
        .reduce((total, handle) => total + handle.getBoundingClientRect().height, 0);
      return Math.max(360, chartStack.getBoundingClientRect().height - handlesHeight);
    }}

    function normalizePaneHeights() {{
      const availableHeight = getAvailablePaneHeight();
      const expandedKeys = indicatorKeys.filter((key) => !paneElements[key].classList.contains('collapsed'));
      let usedIndicatorHeight = 0;

      indicatorKeys.forEach((key) => {{
        const pane = paneElements[key];
        if (pane.classList.contains('collapsed')) {{
          setPaneHeight(pane, collapsedPaneHeight);
          usedIndicatorHeight += collapsedPaneHeight;
          return;
        }}
        const targetHeight = Math.max(
          minIndicatorHeight,
          getPaneHeight(pane) || Number(pane.dataset.expandedHeight) || defaultPaneHeights[key],
        );
        setPaneHeight(pane, targetHeight);
        usedIndicatorHeight += targetHeight;
      }});

      const maxIndicatorHeight = Math.max(0, availableHeight - minPriceHeight);
      if (usedIndicatorHeight > maxIndicatorHeight && expandedKeys.length) {{
        const overflow = usedIndicatorHeight - maxIndicatorHeight;
        const shrinkable = expandedKeys
          .map((key) => [key, Math.max(0, getPaneHeight(paneElements[key]) - minIndicatorHeight)])
          .filter(([, amount]) => amount > 0);
        const totalShrinkable = shrinkable.reduce((total, [, amount]) => total + amount, 0);
        shrinkable.forEach(([key, amount]) => {{
          const shrink = totalShrinkable ? overflow * (amount / totalShrinkable) : overflow / shrinkable.length;
          setPaneHeight(paneElements[key], getPaneHeight(paneElements[key]) - shrink);
        }});
        usedIndicatorHeight = indicatorKeys.reduce((total, key) => total + getPaneHeight(paneElements[key]), 0);
      }}

      setPaneHeight(paneElements.price, Math.max(minPriceHeight, availableHeight - usedIndicatorHeight));
    }}

    function toggleIndicatorPane(paneKey, button) {{
      const pane = paneElements[paneKey];
      if (!pane) return;

      if (pane.classList.contains('collapsed')) {{
        const restoreHeight = Number(pane.dataset.expandedHeight || 110);
        pane.classList.remove('collapsed');
        setPaneHeight(pane, restoreHeight);
        button.textContent = '收合';
      }} else {{
        const currentHeight = getPaneHeight(pane);
        pane.dataset.expandedHeight = String(currentHeight);
        pane.classList.add('collapsed');
        button.textContent = '展開';
      }}

      normalizePaneHeights();
      requestAnimationFrame(resizeCharts);
    }}

    function resizeCharts() {{
      Object.entries(chartsByPane).forEach(([key, chart]) => {{
        const container = containersByPane[key];
        if (!container || container.offsetParent === null) return;
        chart.applyOptions({{ width: container.clientWidth, height: container.clientHeight }});
      }});
    }}

    let autoPriceScale = true;
    const priceScaleToggle = document.getElementById('priceScaleToggle');
    priceScaleToggle.addEventListener('click', () => {{
      autoPriceScale = !autoPriceScale;
      mainChart.priceScale('right').applyOptions({{ autoScale: autoPriceScale }});
      priceScaleToggle.textContent = autoPriceScale ? '價格軸：自動' : '價格軸：手動';
    }});

    document.querySelectorAll('[data-toggle-pane]').forEach((button) => {{
      button.addEventListener('click', () => {{
        toggleIndicatorPane(button.dataset.togglePane, button);
      }});
    }});

    document.querySelectorAll('.resize-handle').forEach((handle) => {{
      handle.addEventListener('pointerdown', (event) => {{
        event.preventDefault();
        const beforePane = document.querySelector(`[data-pane="${{handle.dataset.resizeBefore}}"]`);
        const afterPane = document.querySelector(`[data-pane="${{handle.dataset.resizeAfter}}"]`);
        if (!beforePane || !afterPane || beforePane.classList.contains('collapsed') || afterPane.classList.contains('collapsed')) return;
        const startY = event.clientY;
        const beforeStart = beforePane.getBoundingClientRect().height;
        const afterStart = afterPane.getBoundingClientRect().height;
        const move = (moveEvent) => {{
          const delta = moveEvent.clientY - startY;
          const beforeHeight = Math.max(80, beforeStart + delta);
          const afterHeight = Math.max(70, afterStart - delta);
          beforePane.style.height = `${{beforeHeight}}px`;
          afterPane.style.height = `${{afterHeight}}px`;
          resizeCharts();
        }};
        const up = () => {{
          window.removeEventListener('pointermove', move);
          window.removeEventListener('pointerup', up);
        }};
        window.addEventListener('pointermove', move);
        window.addEventListener('pointerup', up);
      }});
    }});

    const resizeObserver = new ResizeObserver(() => {{
      normalizePaneHeights();
      resizeCharts();
    }});
    resizeObserver.observe(chartStack);
    requestAnimationFrame(() => {{
      normalizePaneHeights();
      resizeCharts();
    }});
  </script>
</body>
</html>
"""


def build_output_filename(meta: StockChartMeta, request: StockChartRequest) -> str:
    return (
        f"stock_chart_{meta.code}_{request.start_date.strftime('%Y%m%d')}_"
        f"{request.end_date.strftime('%Y%m%d')}_{request.frequency}.html"
    )


def month_starts_between(start_date: date, end_date: date) -> list[date]:
    cursor = date(start_date.year, start_date.month, 1)
    months: list[date] = []
    while cursor <= end_date:
        months.append(cursor)
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return months


def roc_date_to_timestamp(raw_value: str) -> pd.Timestamp:
    text = str(raw_value).strip()
    if not text:
        return pd.NaT

    if "/" in text:
        year, month, day = text.split("/")
        return pd.Timestamp(year=int(year) + 1911, month=int(month), day=int(day))

    if len(text) == 7:
        return pd.Timestamp(year=int(text[:3]) + 1911, month=int(text[3:5]), day=int(text[5:7]))

    return pd.NaT


def to_number(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "--", "---", "----", "X0.00", "除權息", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def infer_precision(frame: pd.DataFrame) -> int:
    closes = frame["close"].dropna()
    if closes.empty:
        return 2

    precisions = []
    for value in closes.tail(50):
        text = f"{float(value):.6f}".rstrip("0")
        precisions.append(len(text.split(".")[1]) if "." in text else 0)
    return min(max(max(precisions, default=0), 0), 4)


def round_number(value: float, precision: int) -> float | None:
    if pd.isna(value):
        return None
    return round(float(value), precision)


def to_unix_timestamp(value: pd.Timestamp) -> int:
    timestamp = pd.Timestamp(value).to_pydatetime()
    return calendar.timegm(timestamp.utctimetuple())


def is_one_minute_frame(frame: pd.DataFrame) -> bool:
    if len(frame) < 2:
        return False
    deltas = frame["datetime"].diff().dropna().dt.total_seconds().astype(int)
    return not deltas.empty and deltas.mode().iloc[0] <= 60


def estimate_intraday_warmup_days(frequency: str) -> int:
    if frequency == "1d":
        return 0

    minutes = {"1m": 1, "5m": 5, "15m": 15, "60m": 60}[frequency]
    bars_per_day = math.ceil(270 / minutes)
    needed_days = math.ceil(150 / max(1, bars_per_day)) + 2
    return max(needed_days, 2)


def date_range(start_date: date, end_date: date) -> Iterable[date]:
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)
