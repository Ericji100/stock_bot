from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pandas as pd


TAIPEI = ZoneInfo("Asia/Taipei")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_ROOT = PROJECT_ROOT / ".cache" / "trade_monitor_replay" / "twse"
INTRADAY_URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_INDEX"
DAILY_URL = "https://www.twse.com.tw/indicesReport/MI_5MINS_HIST"


class TwseSpotDataError(ValueError):
    pass


@dataclass(frozen=True)
class TwseSpotReplayData:
    target_date: date
    previous_trading_date: date
    previous_close: float
    official_open: float
    official_high: float
    official_low: float
    official_close: float
    bars: pd.DataFrame
    source_sha256: dict[str, str]


JsonLoader = Callable[[str, Mapping[str, str]], tuple[Mapping[str, Any], bytes]]


def load_twse_spot_replay_data(
    target_date: date,
    *,
    json_loader: JsonLoader | None = None,
) -> TwseSpotReplayData:
    loader = json_loader or _fetch_json_cached
    daily_payload, daily_raw = loader(
        DAILY_URL,
        {"date": target_date.replace(day=1).strftime("%Y%m%d"), "response": "json"},
    )
    intraday_payload, intraday_raw = loader(
        INTRADAY_URL,
        {"date": target_date.strftime("%Y%m%d"), "response": "json"},
    )
    daily_rows = _parse_daily_rows(daily_payload)
    target_rows = [row for row in daily_rows if row[0] == target_date]
    previous_rows = [row for row in daily_rows if row[0] < target_date]
    if len(target_rows) != 1 or not previous_rows:
        raise TwseSpotDataError("TWSE月資料缺少目標日或前一交易日指數。")
    _, official_open, official_high, official_low, official_close = target_rows[0]
    previous_date, _, _, _, previous_close = max(previous_rows, key=lambda row: row[0])
    points = _parse_intraday_points(intraday_payload, target_date)
    if points.empty:
        raise TwseSpotDataError("TWSE目標日沒有每5秒加權指數資料。")
    actual_open = points[points["index_value"].sub(official_open).abs() <= 0.01]
    if actual_open.empty:
        raise TwseSpotDataError("TWSE每5秒資料找不到官方開盤指數。")
    open_position = actual_open.index[0]
    points = points.loc[open_position:].reset_index(drop=True)
    if points.iloc[0]["timestamp"].time() > time(9, 0, 30):
        raise TwseSpotDataError("TWSE第一個當日指數時間異常。")
    bars = _aggregate_one_minute(points, target_date)
    if bars.empty:
        raise TwseSpotDataError("TWSE每5秒指數無法聚合一分K。")
    _verify_daily_ohlc(
        bars,
        official_open=official_open,
        official_high=official_high,
        official_low=official_low,
        official_close=official_close,
    )
    return TwseSpotReplayData(
        target_date=target_date,
        previous_trading_date=previous_date,
        previous_close=previous_close,
        official_open=official_open,
        official_high=official_high,
        official_low=official_low,
        official_close=official_close,
        bars=bars,
        source_sha256={
            "daily_history": hashlib.sha256(daily_raw).hexdigest(),
            "five_second_index": hashlib.sha256(intraday_raw).hexdigest(),
        },
    )


def spot_context(data: TwseSpotReplayData | None, *, latest_futures_bar: datetime) -> dict[str, Any]:
    if data is None:
        return {
            "status": "UNAVAILABLE",
            "source": "not configured",
            "previous_close": None,
            "current_session": None,
            "bars": [],
        }
    previous = {
        "date": data.previous_trading_date.isoformat(),
        "close": _number(data.previous_close),
    }
    if latest_futures_bar.time() < time(9, 0):
        return {
            "status": "PREOPEN",
            "source": "TWSE official five-second TAIEX",
            "previous_close": previous,
            "current_session": None,
            "bars": [],
            "note": "現貨尚未開盤；不得提前使用當日開高低收。",
        }
    visible = data.bars[data.bars["bar_time"] <= pd.Timestamp(latest_futures_bar)].copy()
    if visible.empty:
        return {
            "status": "PREOPEN",
            "source": "TWSE official five-second TAIEX",
            "previous_close": previous,
            "current_session": None,
            "bars": [],
            "note": "尚無已完成的現貨一分K。",
        }
    first = visible.iloc[0]
    last = visible.iloc[-1]
    return {
        "status": "FRESH",
        "source": "TWSE official five-second TAIEX aggregated causally to one minute",
        "previous_close": previous,
        "current_session": {
            "open": _number(first["open"]),
            "opening_gap_points": _number(float(first["open"]) - data.previous_close),
            "latest_bar_time": last["bar_time"].isoformat(),
            "latest_close": _number(last["close"]),
            "high_so_far": _number(visible["high"].max()),
            "low_so_far": _number(visible["low"].min()),
        },
        "bars": _bar_records(visible.tail(60)),
    }


def _parse_daily_rows(payload: Mapping[str, Any]) -> list[tuple[date, float, float, float, float]]:
    if payload.get("stat") != "OK":
        raise TwseSpotDataError("TWSE發行量加權指數歷史資料回應失敗。")
    rows: list[tuple[date, float, float, float, float]] = []
    for row in payload.get("data") or []:
        if not isinstance(row, list) or len(row) < 5:
            continue
        try:
            roc_year, month, day = (int(part) for part in str(row[0]).split("/"))
            parsed_date = date(roc_year + 1911, month, day)
            values = tuple(_parse_number(value) for value in row[1:5])
        except (TypeError, ValueError):
            continue
        rows.append((parsed_date, values[0], values[1], values[2], values[3]))
    return rows


def _parse_intraday_points(payload: Mapping[str, Any], target_date: date) -> pd.DataFrame:
    if payload.get("stat") != "OK":
        raise TwseSpotDataError("TWSE每5秒指數回應失敗。")
    fields = [str(field) for field in payload.get("fields") or []]
    try:
        time_index = fields.index("時間")
        value_index = fields.index("發行量加權股價指數")
    except ValueError as exc:
        raise TwseSpotDataError("TWSE每5秒指數欄位已變更。") from exc
    records: list[dict[str, Any]] = []
    for row in payload.get("data") or []:
        if not isinstance(row, list) or max(time_index, value_index) >= len(row):
            continue
        try:
            parsed_time = datetime.strptime(str(row[time_index]), "%H:%M:%S").time()
            value = _parse_number(row[value_index])
        except (TypeError, ValueError):
            continue
        records.append(
            {
                "timestamp": pd.Timestamp(datetime.combine(target_date, parsed_time), tz=TAIPEI),
                "index_value": value,
            }
        )
    if not records:
        return pd.DataFrame(columns=["timestamp", "index_value"])
    return pd.DataFrame.from_records(records).sort_values("timestamp").reset_index(drop=True)


def _aggregate_one_minute(points: pd.DataFrame, target_date: date) -> pd.DataFrame:
    frame = points.copy()
    close_time = pd.Timestamp(datetime.combine(target_date, time(13, 30)), tz=TAIPEI)
    frame.loc[frame["timestamp"] == close_time, "timestamp"] -= pd.Timedelta(microseconds=1)
    frame["bar_time"] = frame["timestamp"].dt.floor("min")
    return (
        frame.groupby("bar_time", as_index=False)
        .agg(
            open=("index_value", "first"),
            high=("index_value", "max"),
            low=("index_value", "min"),
            close=("index_value", "last"),
            sample_count=("index_value", "count"),
        )
        .sort_values("bar_time")
        .reset_index(drop=True)
    )


def _verify_daily_ohlc(
    bars: pd.DataFrame,
    *,
    official_open: float,
    official_high: float,
    official_low: float,
    official_close: float,
) -> None:
    actual = (
        float(bars.iloc[0]["open"]),
        float(bars["high"].max()),
        float(bars["low"].min()),
        float(bars.iloc[-1]["close"]),
    )
    expected = (official_open, official_high, official_low, official_close)
    if any(abs(left - right) > 0.02 for left, right in zip(actual, expected)):
        raise TwseSpotDataError("TWSE每5秒聚合結果與官方日OHLC不一致。")


def _bar_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {
            "time": row.bar_time.isoformat(),
            "open": _number(row.open),
            "high": _number(row.high),
            "low": _number(row.low),
            "close": _number(row.close),
            "sample_count": int(row.sample_count),
        }
        for row in frame.itertuples(index=False)
    ]


def _fetch_json_cached(url: str, params: Mapping[str, str]) -> tuple[Mapping[str, Any], bytes]:
    date_value = str(params.get("date") or "unknown")
    label = "five-second" if "MI_5MINS_INDEX" in url else "daily-history"
    path = CACHE_ROOT / f"{label}-{date_value}.json"
    if path.exists():
        raw = path.read_bytes()
    else:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            response = client.get(
                url,
                params=dict(params),
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.twse.com.tw/"},
            )
            response.raise_for_status()
            raw = response.content
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_bytes(raw)
        os.replace(temporary, path)
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TwseSpotDataError("TWSE回應不是有效JSON。") from exc
    if not isinstance(payload, Mapping):
        raise TwseSpotDataError("TWSE回應不是JSON object。")
    return payload, raw


def _parse_number(value: Any) -> float:
    text = str(value).replace(",", "").strip()
    if not text or text in {"--", "---"}:
        raise ValueError("missing number")
    return float(text)


def _number(value: Any) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else round(number, 4)
