from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


TAIPEI = ZoneInfo("Asia/Taipei")
DAY = "DAY"
NIGHT_US = "NIGHT_US"
SESSION_BOUNDS = {
    DAY: (time(8, 45), time(13, 44), 300),
    NIGHT_US: (time(21, 30), time(0, 59), 210),
}


class PureAIDataError(ValueError):
    pass


@dataclass(frozen=True)
class PureAISessionData:
    source_path: Path
    source_sha256: str
    source_meta: dict[str, Any]
    session_date: date
    session_kind: str
    session_key: str
    background_bars: pd.DataFrame
    session_bars: pd.DataFrame


def load_chart_payload(path: Path) -> dict[str, Any]:
    source = Path(path).resolve()
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PureAIDataError("TMF圖表HTML無法讀取。") from exc
    marker = "const payload = "
    position = text.find(marker)
    if position < 0:
        raise PureAIDataError("TMF圖表HTML找不到payload。")
    candidate = text[position + len(marker):].lstrip()
    try:
        payload, _ = json.JSONDecoder().raw_decode(candidate)
    except json.JSONDecodeError as exc:
        raise PureAIDataError("TMF圖表payload不是有效JSON。") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("candles"), list):
        raise PureAIDataError("TMF圖表payload格式錯誤。")
    return payload


def load_chart_frame(path: Path) -> tuple[pd.DataFrame, dict[str, Any], str]:
    source = Path(path).resolve()
    payload = load_chart_payload(source)
    candles = payload["candles"]
    vectors: dict[str, dict[int, float]] = {}
    for source_key, target_key in (("volume", "volume"), ("ma21", "sma21"), ("ma105", "sma105")):
        raw_items = payload.get(source_key)
        if not isinstance(raw_items, list):
            raise PureAIDataError(f"TMF圖表缺少{source_key}。")
        vectors[target_key] = {
            int(item["time"]): float(item["value"])
            for item in raw_items
            if isinstance(item, dict) and "time" in item and "value" in item
        }
    rows: list[dict[str, Any]] = []
    for candle in candles:
        if not isinstance(candle, dict):
            raise PureAIDataError("TMF K棒格式錯誤。")
        timestamp = int(candle["time"])
        # The generated chart intentionally renders UTC clock fields as Taiwan
        # wall-clock fields. Preserve those fields and attach Asia/Taipei.
        wall_clock = datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=TAIPEI)
        try:
            rows.append(
                {
                    "bar_time": wall_clock,
                    "open": float(candle["open"]),
                    "high": float(candle["high"]),
                    "low": float(candle["low"]),
                    "close": float(candle["close"]),
                    "volume": vectors["volume"][timestamp],
                    "sma21": vectors["sma21"][timestamp],
                    "sma105": vectors["sma105"][timestamp],
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PureAIDataError("TMF K棒或指標資料不完整。") from exc
    frame = pd.DataFrame(rows).sort_values("bar_time").drop_duplicates("bar_time").reset_index(drop=True)
    if frame.empty:
        raise PureAIDataError("TMF圖表沒有K棒。")
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["atr14"] = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return frame, dict(meta), digest


def load_session(path: Path, *, session_date: date, session_kind: str) -> PureAISessionData:
    kind = str(session_kind).strip().upper()
    if kind not in SESSION_BOUNDS:
        raise PureAIDataError("session_kind只允許DAY或NIGHT_US。")
    frame, meta, digest = load_chart_frame(path)
    session = _session_frame(frame, session_date=session_date, session_kind=kind)
    background = _background_frame(frame, session_date=session_date, session_kind=kind)
    _assert_complete(session, session_date=session_date, session_kind=kind)
    if background.empty:
        raise PureAIDataError("找不到前一時段盤前背景。")
    return PureAISessionData(
        source_path=Path(path).resolve(),
        source_sha256=digest,
        source_meta=meta,
        session_date=session_date,
        session_kind=kind,
        session_key=f"{session_date.isoformat()}:{kind}",
        background_bars=background.reset_index(drop=True),
        session_bars=session.reset_index(drop=True),
    )


def compact_bars(frame: pd.DataFrame, *, tail: int | None = None) -> dict[str, Any]:
    selected = frame.tail(tail) if tail is not None else frame
    columns = ["time", "open", "high", "low", "close", "volume", "sma21", "sma105", "atr14"]
    rows: list[list[Any]] = []
    for row in selected.itertuples(index=False):
        rows.append(
            [
                row.bar_time.isoformat(),
                _number(row.open),
                _number(row.high),
                _number(row.low),
                _number(row.close),
                _number(row.volume),
                _optional_number(row.sma21),
                _optional_number(row.sma105),
                _optional_number(row.atr14),
            ]
        )
    return {"columns": columns, "rows": rows}


def completed_overview(frame: pd.DataFrame, *, minutes: int = 15) -> dict[str, Any]:
    if frame.empty:
        return {"minutes": minutes, "columns": [], "rows": []}
    local = frame.copy()
    local["bucket"] = local["bar_time"].dt.floor(f"{minutes}min")
    grouped = (
        local.groupby("bucket", as_index=False)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            count=("bar_time", "count"),
        )
        .sort_values("bucket")
    )
    grouped = grouped[grouped["count"] == minutes]
    # A 15-minute row is only a compact overview.  Its bucket timestamp is the
    # exact OPEN time, but it is not necessarily the minute that printed the
    # bucket HIGH/LOW.  Preserve the exact source-minute timestamps so the AI
    # can cite a real 1-minute K and the causal validator can verify it.
    columns = [
        "bucket_start",
        "open",
        "high",
        "high_time",
        "low",
        "low_time",
        "close",
        "close_time",
        "volume",
    ]
    rows: list[list[Any]] = []
    for row in grouped.itertuples(index=False):
        bucket_frame = local[local["bucket"] == row.bucket]
        high_row = bucket_frame.loc[bucket_frame["high"].idxmax()]
        low_row = bucket_frame.loc[bucket_frame["low"].idxmin()]
        close_row = bucket_frame.iloc[-1]
        rows.append(
            [
                row.bucket.isoformat(),
                _number(row.open),
                _number(row.high),
                high_row["bar_time"].isoformat(),
                _number(row.low),
                low_row["bar_time"].isoformat(),
                _number(row.close),
                close_row["bar_time"].isoformat(),
                _number(row.volume),
            ]
        )
    return {
        "minutes": minutes,
        "columns": columns,
        "citation_note": (
            "bucket_start只對應OPEN；引用HIGH/LOW/CLOSE時必須使用"
            "high_time/low_time/close_time所列的實際1分K時間。"
        ),
        "rows": rows,
    }


def session_facts(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {}
    first = frame.iloc[0]
    last = frame.iloc[-1]
    high = frame.loc[frame["high"].idxmax()]
    low = frame.loc[frame["low"].idxmin()]
    result: dict[str, Any] = {
        "bar_count": len(frame),
        "first_bar_time": first["bar_time"].isoformat(),
        "latest_bar_time": last["bar_time"].isoformat(),
        "session_open": _number(first["open"]),
        "high_so_far": {"time": high["bar_time"].isoformat(), "price": _number(high["high"])},
        "low_so_far": {"time": low["bar_time"].isoformat(), "price": _number(low["low"])},
        "latest_close": _number(last["close"]),
    }
    if len(frame) >= 5:
        opening = frame.head(5)
        result["opening_5m"] = {
            "start": opening.iloc[0]["bar_time"].isoformat(),
            "end": opening.iloc[-1]["bar_time"].isoformat(),
            "high": _number(opening["high"].max()),
            "low": _number(opening["low"].min()),
        }
    else:
        result["opening_5m"] = None
    if len(frame) >= 15:
        opening = frame.head(15)
        result["opening_15m"] = {
            "start": opening.iloc[0]["bar_time"].isoformat(),
            "end": opening.iloc[-1]["bar_time"].isoformat(),
            "high": _number(opening["high"].max()),
            "low": _number(opening["low"].min()),
        }
    else:
        result["opening_15m"] = None
    return result


def _session_frame(frame: pd.DataFrame, *, session_date: date, session_kind: str) -> pd.DataFrame:
    if session_kind == DAY:
        start = datetime.combine(session_date, time(8, 45), TAIPEI)
        end = datetime.combine(session_date, time(13, 44), TAIPEI)
    else:
        start = datetime.combine(session_date, time(21, 30), TAIPEI)
        end = datetime.combine(session_date + timedelta(days=1), time(0, 59), TAIPEI)
    return frame[(frame["bar_time"] >= start) & (frame["bar_time"] <= end)].copy()


def _background_frame(frame: pd.DataFrame, *, session_date: date, session_kind: str) -> pd.DataFrame:
    if session_kind == NIGHT_US:
        start = datetime.combine(session_date, time(15, 0), TAIPEI)
        end = datetime.combine(session_date, time(21, 29), TAIPEI)
        return frame[(frame["bar_time"] >= start) & (frame["bar_time"] <= end)].copy()
    candidates = frame[frame["bar_time"] < datetime.combine(session_date, time(8, 45), TAIPEI)].copy()
    candidates["night_key"] = candidates["bar_time"].map(_night_key)
    candidates = candidates[candidates["night_key"].notna()]
    candidates = candidates[candidates["night_key"] < session_date]
    if candidates.empty:
        return candidates.drop(columns=["night_key"], errors="ignore")
    key = max(candidates["night_key"])
    return candidates[candidates["night_key"] == key].drop(columns=["night_key"])


def _night_key(value: pd.Timestamp) -> date | None:
    local_time = value.time()
    if local_time >= time(15, 0):
        return value.date()
    if local_time < time(5, 0):
        return value.date() - timedelta(days=1)
    return None


def _assert_complete(frame: pd.DataFrame, *, session_date: date, session_kind: str) -> None:
    _, _, expected_count = SESSION_BOUNDS[session_kind]
    if len(frame) != expected_count:
        raise PureAIDataError(
            f"{session_date.isoformat()} {session_kind}只有{len(frame)}根，預期{expected_count}根。"
        )
    differences = frame["bar_time"].diff().dropna()
    if not (differences == pd.Timedelta(minutes=1)).all():
        raise PureAIDataError(f"{session_date.isoformat()} {session_kind}存在1分K缺口。")


def _number(value: Any) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else round(number, 4)


def _optional_number(value: Any) -> int | float | None:
    if value is None or pd.isna(value):
        return None
    return _number(value)
