import json
import math
import tempfile
import zipfile
import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Iterable
from uuid import uuid4

import httpx
import pandas as pd


TAIFEX_DAILY_CSV_URL = "https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_{date_str}.zip"
CACHE_DIR = Path(".cache") / "tmf_daily"
TMP_DIR = Path(tempfile.gettempdir()) / "stock_tg_bot"
PRODUCT_CODE = "TMF"
SESSION_DAY = "日盤"
SESSION_NIGHT = "夜盤"
SESSION_FULL = "全日盤"
SUPPORTED_SESSIONS = {SESSION_DAY, SESSION_NIGHT, SESSION_FULL}
SUPPORTED_FREQUENCIES = {"1m": 1, "5m": 5, "15m": 15, "60m": 60}


class TmfChartError(Exception):
    pass


@dataclass(frozen=True)
class TmfChartRequest:
    start_date: date
    end_date: date
    session: str
    frequency: str

    @property
    def frequency_minutes(self) -> int:
        return SUPPORTED_FREQUENCIES[self.frequency]


def parse_tmf_chart_args(args: list[str]) -> TmfChartRequest:
    if len(args) < 2:
        raise TmfChartError("請輸入開始與結束日期，例如 /tmf_chart 2026-05-01 2026-05-05 全日盤 1m")

    try:
        start_date = datetime.strptime(args[0], "%Y-%m-%d").date()
        end_date = datetime.strptime(args[1], "%Y-%m-%d").date()
    except ValueError as exc:
        raise TmfChartError("日期格式必須是 YYYY-MM-DD。") from exc

    if start_date > end_date:
        raise TmfChartError("開始日期不可晚於結束日期。")

    session = args[2] if len(args) >= 3 else SESSION_FULL
    frequency = args[3].lower() if len(args) >= 4 else "1m"

    if session not in SUPPORTED_SESSIONS:
        raise TmfChartError("盤別只支援 日盤、夜盤、全日盤。")
    if frequency not in SUPPORTED_FREQUENCIES:
        raise TmfChartError("頻率只支援 1m、5m、15m、60m。")

    return TmfChartRequest(
        start_date=start_date,
        end_date=end_date,
        session=session,
        frequency=frequency,
    )


def build_tmf_chart_report(start_date: str, end_date: str, session: str, frequency: str = "1m") -> Path:
    request = parse_tmf_chart_args([start_date, end_date, session, frequency])
    ticks = load_tmf_ticks(request)
    bars = build_resampled_bars(ticks, request)

    if bars.empty:
        raise TmfChartError("指定區間找不到 TMF 可用資料。")

    bars = apply_indicators(bars)
    display_bars = bars[
        (bars["session_date"] >= pd.Timestamp(request.start_date))
        & (bars["session_date"] <= pd.Timestamp(request.end_date))
    ].copy()

    if display_bars.empty:
        raise TmfChartError("指定區間沒有可顯示的 TMF K 線。")

    return write_html_report(display_bars, request)


def load_tmf_ticks(request: TmfChartRequest) -> pd.DataFrame:
    frames = []
    warmup_calendar_days = estimate_warmup_calendar_days(request)
    fetch_start = request.start_date - timedelta(days=warmup_calendar_days)
    fetch_end = request.end_date + timedelta(days=1)

    for current_day in date_range(fetch_start, fetch_end):
        daily_frame = load_tmf_ticks_for_day(current_day)
        if not daily_frame.empty:
            frames.append(daily_frame)

    if not frames:
        raise TmfChartError("無法從期交所下載 TMF 逐筆成交資料。")

    merged = pd.concat(frames, ignore_index=True)
    merged.sort_values("actual_datetime", inplace=True)
    return merged


def load_tmf_ticks_for_day(target_date: date) -> pd.DataFrame:
    zip_bytes = download_daily_zip(target_date)
    if zip_bytes is None:
        return pd.DataFrame()

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
    frame = frame[frame["product_code"] == PRODUCT_CODE].copy()
    frame = frame[frame["expiry_month"].str.fullmatch(r"\d{6}")]

    if frame.empty:
        return frame

    frame["trade_date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d", errors="coerce")
    frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    frame = frame.dropna(subset=["trade_date", "price", "volume"])

    if frame.empty:
        return frame

    time_values = frame["trade_time"].astype(int)
    frame["actual_datetime"] = pd.to_datetime(
        frame["trade_date"].dt.strftime("%Y%m%d") + frame["trade_time"],
        format="%Y%m%d%H%M%S",
        errors="coerce",
    )
    frame["session_type"] = pd.NA
    frame.loc[(time_values >= 84500) & (time_values <= 134500), "session_type"] = "day"
    frame.loc[(time_values >= 150000) | (time_values <= 50000), "session_type"] = "night"
    frame = frame.dropna(subset=["actual_datetime", "session_type"])

    if frame.empty:
        return frame

    frame["session_date"] = frame["trade_date"]
    frame.loc[time_values <= 50000, "session_date"] = frame.loc[time_values <= 50000, "trade_date"] - pd.Timedelta(days=1)
    frame["expiry_rank"] = pd.to_numeric(frame["expiry_month"], errors="coerce")
    frame["near_expiry"] = frame.groupby("session_date")["expiry_rank"].transform("min")
    frame = frame[frame["expiry_rank"] == frame["near_expiry"]].copy()

    return frame[["actual_datetime", "session_date", "session_type", "price", "volume"]]


def download_daily_zip(target_date: date) -> bytes | None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"Daily_{target_date.strftime('%Y_%m_%d')}.zip"
    if cache_path.exists():
        cached_bytes = cache_path.read_bytes()
        if is_zip_bytes(cached_bytes):
            return cached_bytes
        cache_path.unlink(missing_ok=True)

    url = TAIFEX_DAILY_CSV_URL.format(date_str=target_date.strftime("%Y_%m_%d"))
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, verify=False) as client:
            response = client.get(url)
            if response.status_code == 404:
                return None
            response.raise_for_status()
    except Exception:
        return None

    if not is_zip_bytes(response.content):
        return None

    cache_path.write_bytes(response.content)
    return response.content


def build_resampled_bars(ticks: pd.DataFrame, request: TmfChartRequest) -> pd.DataFrame:
    frame = ticks.copy()
    if request.session == SESSION_DAY:
        frame = frame[frame["session_type"] == "day"].copy()
    elif request.session == SESSION_NIGHT:
        frame = frame[frame["session_type"] == "night"].copy()

    if frame.empty:
        return frame

    frame["anchor"] = frame.apply(build_session_anchor, axis=1)
    offset_minutes = ((frame["actual_datetime"] - frame["anchor"]).dt.total_seconds() // 60).astype(int)
    frame["bucket_start"] = frame["anchor"] + pd.to_timedelta(
        (offset_minutes // request.frequency_minutes) * request.frequency_minutes,
        unit="m",
    )

    grouped = (
        frame.sort_values("actual_datetime")
        .groupby("bucket_start", as_index=False)
        .agg(
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
            volume=("volume", "sum"),
            session_date=("session_date", "first"),
        )
    )
    grouped.sort_values("bucket_start", inplace=True)
    grouped.reset_index(drop=True, inplace=True)
    grouped["time"] = grouped["bucket_start"].map(to_unix_timestamp)
    return grouped


def build_session_anchor(row: pd.Series) -> pd.Timestamp:
    session_day = pd.Timestamp(row["session_date"])
    if row["session_type"] == "day":
        return session_day + pd.Timedelta(hours=8, minutes=45)
    return session_day + pd.Timedelta(hours=15)


def apply_indicators(bars: pd.DataFrame) -> pd.DataFrame:
    frame = bars.copy()
    frame["MA21"] = frame["close"].rolling(window=21).mean()
    frame["MA105"] = frame["close"].rolling(window=105).mean()
    lowest_low = frame["low"].rolling(window=9, min_periods=1).min()
    highest_high = frame["high"].rolling(window=9, min_periods=1).max()
    range_value = highest_high - lowest_low
    frame["RSV"] = ((frame["close"] - lowest_low) / range_value.replace(0, pd.NA) * 100.0).fillna(50.0)
    frame["K"] = frame["RSV"].ewm(alpha=9 / 55, adjust=False).mean()
    frame["D"] = frame["K"].ewm(alpha=9 / 55, adjust=False).mean()
    ema21 = frame["close"].ewm(span=21, adjust=False).mean()
    ema55 = frame["close"].ewm(span=55, adjust=False).mean()
    frame["DIF"] = ema21 - ema55
    frame["DEA"] = frame["DIF"].ewm(span=55, adjust=False).mean()
    frame["Histogram"] = frame["DIF"] - frame["DEA"]
    return frame


def write_html_report(bars: pd.DataFrame, request: TmfChartRequest) -> Path:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    output_path = TMP_DIR / f"tmf_chart_{uuid4().hex}.html"
    payload = build_chart_payload(bars, request)
    output_path.write_text(build_html_template(payload), encoding="utf-8")
    return output_path


def build_chart_payload(bars: pd.DataFrame, request: TmfChartRequest) -> dict:
    precision = infer_precision(bars)
    candle_data = []
    ma21_data = []
    ma105_data = []
    volume_data = []
    dif_data = []
    dea_data = []
    histogram_data = []
    k_data = []
    d_data = []

    for row in bars.itertuples(index=False):
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

    return {
        "meta": {
            "title": f"TMF 交互式分析圖表 {request.start_date} ~ {request.end_date}",
            "session": request.session,
            "frequency": request.frequency,
            "barCount": len(candle_data),
        },
        "candles": candle_data,
        "ma21": ma21_data,
        "ma105": ma105_data,
        "volume": volume_data,
        "dif": dif_data,
        "dea": dea_data,
        "histogram": histogram_data,
        "k": k_data,
        "d": d_data,
    }


def build_html_template(payload: dict) -> str:
    payload_json = json.dumps(payload, ensure_ascii=False)
    title_suffix = payload["meta"]["title"].replace("TMF 交互式分析圖表 ", "")
    return f"""<!DOCTYPE html>
<html lang=\"zh-Hant\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">
  <title>TMF 交互式分析圖表</title>
  <script src=\"https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js\"></script>
  <style>
    :root {{
            --bg: #05070b;
            --panel: #0a0e14;
            --panel-soft: #0d131c;
            --line: rgba(150, 176, 214, 0.18);
            --text: #edf3ff;
            --muted: #8ea0be;
            --pill-bg: rgba(15, 22, 33, 0.92);
            --hero-bg: rgba(7, 11, 17, 0.94);
    }}
    * {{ box-sizing: border-box; }}
        html {{ height: 100%; background: var(--bg); }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Microsoft JhengHei", "PingFang TC", sans-serif;
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
    .chart {{ width: 100%; height: 100%; }}
    @media (max-width: 768px) {{
            .page {{ padding: 4px; }}
            .hero {{ border-radius: 10px; padding: 8px 10px 7px; margin-bottom: 4px; }}
            .board {{ padding: 3px; border-radius: 10px; }}
            .chart-stack {{ gap: 3px; min-height: 560px; height: calc(100vh - 92px); }}
    }}
  </style>
</head>
<body>
  <div class=\"page\">
    <section class=\"hero\">
      <h1>TMF 交互式分析圖表</h1>
      <div class=\"meta\">
        <div class=\"pill\">區間: {title_suffix}</div>
        <div class=\"pill\">盤別: {payload['meta']['session']}</div>
        <div class=\"pill\">頻率: {payload['meta']['frequency']}</div>
        <div class=\"pill\">K 線數: {payload['meta']['barCount']}</div>
      </div>
    </section>
    <section class=\"board\">
      <div class=\"chart-stack\">
        <div class=\"pane price-pane\" data-pane=\"price\"><div class=\"pane-label\">Price + MA</div><div class=\"pane-controls\"><button id=\"priceScaleToggle\" class=\"control-btn\" type=\"button\">價格軸：自動</button></div><div id=\"mainChart\" class=\"chart\"></div></div>
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
                timeVisible: true,
                secondsVisible: false,
                rightOffset: 6,
                barSpacing: 8,
                                borderColor: 'rgba(122, 148, 186, 0.18)',
                tickMarkFormatter: (time, tickMarkType, locale) => formatTickMark(time, tickMarkType, locale),
            }},
            rightPriceScale: {{ borderColor: 'rgba(122, 148, 186, 0.18)' }},
      crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
    }};

        function normalizeDate(time) {{
            if (typeof time === 'number') {{
                return new Date(time * 1000);
            }}
            if (time && typeof time === 'object' && 'timestamp' in time) {{
                return new Date(time.timestamp * 1000);
            }}
            return new Date(0);
        }}

        function pad2(value) {{
            return String(value).padStart(2, '0');
        }}

        function getSessionLabel(date) {{
            const hour = date.getUTCHours();
            const minute = date.getUTCMinutes();
            const timeNumber = hour * 100 + minute;
            if (timeNumber >= 845 && timeNumber <= 1345) {{
                return '日盤';
            }}
            return '夜盤';
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
            return `${{formatDatePart(date)}} ${{getSessionLabel(date)}} ${{formatClock(date)}}`;
        }}

        function formatTickMark(time, tickMarkType, locale) {{
            const date = normalizeDate(time);
            const monthDay = `${{pad2(date.getUTCMonth() + 1)}}/${{pad2(date.getUTCDate())}}`;
            const clock = formatClock(date);
            const hour = date.getUTCHours();
            const minute = date.getUTCMinutes();

            if (hour === 8 && minute === 45) {{
                return `${{monthDay}} 日盤`;
            }}
            if (hour === 15 && minute === 0) {{
                return `${{monthDay}} 夜盤`;
            }}
            if (tickMarkType === LightweightCharts.TickMarkType.DayOfMonth) {{
                return monthDay;
            }}
            if (tickMarkType === LightweightCharts.TickMarkType.Month) {{
                return `${{date.getUTCFullYear()}}/${{pad2(date.getUTCMonth() + 1)}}`;
            }}
            if (tickMarkType === LightweightCharts.TickMarkType.Year) {{
                return String(date.getUTCFullYear());
            }}
            if (hour < 5) {{
                return `翌日 ${{clock}}`;
            }}
            return clock;
        }}

    function makeChart(container, extraOptions = {{}}) {{
      return LightweightCharts.createChart(container, {{
        width: container.clientWidth,
        height: container.clientHeight,
        ...commonOptions,
        ...extraOptions,
      }});
    }}

    const mainContainer = document.getElementById('mainChart');
    const volumeContainer = document.getElementById('volumeChart');
    const kdContainer = document.getElementById('kdChart');
    const macdContainer = document.getElementById('macdChart');
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
            {{ color: '#29d3c2', lineWidth: 2, priceLineVisible: false }},
            'addLineSeries',
        );
        const deaSeries = addSeriesCompat(
            macdChart,
            'LineSeries',
            {{ color: '#d54bcb', lineWidth: 2, priceLineVisible: false }},
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

    function syncVisibleRange(source, targets) {{
      let syncing = false;
      source.timeScale().subscribeVisibleLogicalRangeChange((range) => {{
        if (syncing || !range) return;
        syncing = true;
        targets.forEach((chart) => chart.timeScale().setVisibleLogicalRange(range));
        syncing = false;
      }});
    }}

    syncVisibleRange(mainChart, [volumeChart, kdChart, macdChart]);
    syncVisibleRange(volumeChart, [mainChart, kdChart, macdChart]);
    syncVisibleRange(kdChart, [mainChart, volumeChart, macdChart]);
    syncVisibleRange(macdChart, [mainChart, volumeChart, kdChart]);

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


def estimate_warmup_calendar_days(request: TmfChartRequest) -> int:
    bars_per_session_day = {
        SESSION_DAY: math.ceil(301 / request.frequency_minutes),
        SESSION_NIGHT: math.ceil(841 / request.frequency_minutes),
        SESSION_FULL: math.ceil(1142 / request.frequency_minutes),
    }
    trading_days = math.ceil(200 / max(1, bars_per_session_day[request.session])) + 5
    return min(max(int(trading_days * 1.6), 7), 120)


def infer_precision(bars: pd.DataFrame) -> int:
    closes = bars["close"].dropna()
    if closes.empty:
        return 0
    fractional = closes % 1
    return 1 if (fractional != 0).any() else 0


def round_number(value: float, precision: int) -> float | None:
    if pd.isna(value):
        return None
    return round(float(value), precision)


def to_unix_timestamp(value: pd.Timestamp) -> int:
    timestamp = pd.Timestamp(value).to_pydatetime()
    return calendar.timegm(timestamp.utctimetuple())


def is_zip_bytes(content: bytes) -> bool:
    return content.startswith(b"PK")


def date_range(start_date: date, end_date: date) -> Iterable[date]:
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)
