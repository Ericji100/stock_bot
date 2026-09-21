from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from io import BytesIO
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from stock_ai_bot.charts.tmf_chart_service import download_daily_zip

from .twse_spot_source import TwseSpotReplayData, load_twse_spot_replay_data


TAIPEI = ZoneInfo("Asia/Taipei")


class ReplayDataError(ValueError):
    pass


@dataclass(frozen=True)
class ReplayDataset:
    target_date: date
    instrument: str
    expiry_month: str
    night_bars: pd.DataFrame
    day_bars: pd.DataFrame
    source_sha256: dict[str, str]
    spot: TwseSpotReplayData | None = None

    @property
    def all_bars(self) -> pd.DataFrame:
        return pd.concat([self.night_bars, self.day_bars], ignore_index=True).sort_values("bar_time")


ZipLoader = Callable[[date], bytes | None]
SpotLoader = Callable[[date], TwseSpotReplayData]


def load_replay_dataset(
    target_date: date,
    *,
    instrument: str = "TMF",
    zip_loader: ZipLoader = download_daily_zip,
    spot_loader: SpotLoader | None = load_twse_spot_replay_data,
) -> ReplayDataset:
    instrument = str(instrument).strip().upper()
    if instrument != "TMF":
        raise ReplayDataError("第一版歷史回放只支援TMF。")
    # TAIFEX names the ZIP by trading date, not by every calendar date covered
    # by the session.  A Monday ZIP therefore contains Friday afternoon,
    # Saturday early morning and Monday day-session trades.  Requiring a
    # Sunday ZIP would incorrectly reject every Monday replay (and holidays).
    raw = zip_loader(target_date)
    if raw is None:
        raise ReplayDataError(f"找不到{target_date.isoformat()}期交所逐筆成交資料。")
    ticks = _parse_daily_zip(raw, instrument).sort_values("actual_datetime")
    if ticks.empty:
        raise ReplayDataError("指定日期沒有TMF逐筆成交資料。")
    day_start = datetime.combine(target_date, time(8, 45))
    day_end = datetime.combine(target_date, time(13, 45))
    night_start, night_end = _prior_night_bounds(ticks, target_date=target_date)
    wanted = ticks[
        ((ticks["actual_datetime"] >= night_start) & (ticks["actual_datetime"] <= night_end))
        | ((ticks["actual_datetime"] >= day_start) & (ticks["actual_datetime"] <= day_end))
    ].copy()
    if wanted.empty:
        raise ReplayDataError("指定交易日沒有前夜與日盤資料。")
    expiry_values = sorted(wanted["expiry_month"].dropna().astype(str).unique())
    if not expiry_values:
        raise ReplayDataError("無法判定TMF近月合約。")
    expiry_month = expiry_values[0]
    wanted = wanted[wanted["expiry_month"] == expiry_month].copy()
    night_ticks = wanted[(wanted["actual_datetime"] >= night_start) & (wanted["actual_datetime"] <= night_end)]
    day_ticks = wanted[(wanted["actual_datetime"] >= day_start) & (wanted["actual_datetime"] <= day_end)]
    night_bars = _aggregate_one_minute(night_ticks, session_end=night_end)
    day_bars = _aggregate_one_minute(day_ticks, session_end=day_end)
    if night_bars.empty:
        raise ReplayDataError("前一夜盤沒有可用的一分K。")
    if day_bars.empty:
        raise ReplayDataError("指定日盤沒有可用的一分K。")
    if day_bars.iloc[0]["bar_time"].time() != time(8, 45):
        raise ReplayDataError("日盤第一根一分K不是08:45。")
    spot = spot_loader(target_date) if spot_loader is not None else None
    hashes = {target_date.isoformat(): hashlib.sha256(raw).hexdigest()}
    if spot is not None:
        hashes.update({f"twse:{key}": value for key, value in spot.source_sha256.items()})
    return ReplayDataset(
        target_date=target_date,
        instrument=instrument,
        expiry_month=expiry_month,
        night_bars=_add_indicators(night_bars),
        day_bars=_add_indicators(pd.concat([night_bars, day_bars], ignore_index=True)).tail(len(day_bars)).reset_index(drop=True),
        source_sha256=hashes,
        spot=spot,
    )


def _prior_night_bounds(ticks: pd.DataFrame, *, target_date: date) -> tuple[datetime, datetime]:
    """Locate the night session bundled into a TAIFEX trading-date file."""
    day_start = datetime.combine(target_date, time(8, 45))
    prior_ticks = ticks[ticks["actual_datetime"] < day_start]
    afternoon_dates = prior_ticks.loc[
        prior_ticks["actual_datetime"].dt.time >= time(15, 0),
        "actual_datetime",
    ].dt.date
    if afternoon_dates.empty:
        raise ReplayDataError("最近前一夜盤沒有可用的一分K。")
    night_date = max(afternoon_dates)
    return (
        datetime.combine(night_date, time(15, 0)),
        datetime.combine(night_date + timedelta(days=1), time(5, 0)),
    )


def bar_records(frame: pd.DataFrame) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for row in frame.itertuples(index=False):
        records.append(
            {
                "time": row.bar_time.isoformat(),
                "open": _number(row.open),
                "high": _number(row.high),
                "low": _number(row.low),
                "close": _number(row.close),
                "volume": _number(row.volume),
                "sma21": _optional_number(getattr(row, "sma21", None)),
                "sma105": _optional_number(getattr(row, "sma105", None)),
                "atr14": _optional_number(getattr(row, "atr14", None)),
            }
        )
    return records


def compact_bar_table(frame: pd.DataFrame) -> dict[str, object]:
    """Return the same one-minute facts without repeating JSON field names.

    Keeping the column declaration beside the rows makes the representation
    lossless while substantially reducing the dynamic prompt sent on every
    replay step.
    """
    columns = ["time", "open", "high", "low", "close", "volume", "sma21", "sma105", "atr14"]
    rows: list[list[object]] = []
    for row in frame.itertuples(index=False):
        rows.append(
            [
                row.bar_time.isoformat(),
                _number(row.open),
                _number(row.high),
                _number(row.low),
                _number(row.close),
                _number(row.volume),
                _optional_number(getattr(row, "sma21", None)),
                _optional_number(getattr(row, "sma105", None)),
                _optional_number(getattr(row, "atr14", None)),
            ]
        )
    return {"columns": columns, "rows": rows}


def completed_overview(frame: pd.DataFrame, *, minutes: int, available_at: datetime) -> list[dict[str, object]]:
    if frame.empty:
        return []
    local = frame.copy()
    local["bucket"] = local["bar_time"].dt.floor(f"{minutes}min")
    grouped = (
        local.groupby("bucket", as_index=False)
        .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), volume=("volume", "sum"), count=("bar_time", "count"))
        .sort_values("bucket")
    )
    available = pd.Timestamp(available_at)
    grouped = grouped[(grouped["bucket"] + pd.Timedelta(minutes=minutes) <= available) & (grouped["count"] == minutes)]
    return [
        {
            "time": row.bucket.isoformat(),
            "open": _number(row.open),
            "high": _number(row.high),
            "low": _number(row.low),
            "close": _number(row.close),
            "volume": _number(row.volume),
        }
        for row in grouped.itertuples(index=False)
    ]


def compact_completed_overview(
    frame: pd.DataFrame,
    *,
    minutes: int,
    available_at: datetime,
) -> dict[str, object]:
    records = completed_overview(frame, minutes=minutes, available_at=available_at)
    columns = ["time", "open", "high", "low", "close", "volume"]
    return {
        "minutes": minutes,
        "columns": columns,
        "rows": [[record[column] for column in columns] for record in records],
    }


def _parse_daily_zip(raw: bytes, instrument: str) -> pd.DataFrame:
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if not names:
                return pd.DataFrame()
            with archive.open(names[0]) as csv_file:
                frame = pd.read_csv(csv_file, encoding="cp950", usecols=[0, 1, 2, 3, 4, 5], dtype=str)
    except (zipfile.BadZipFile, UnicodeError, ValueError) as exc:
        raise ReplayDataError("期交所逐筆成交ZIP格式無效。") from exc
    frame.columns = ["trade_date", "product_code", "expiry_month", "trade_time", "price", "volume"]
    frame["product_code"] = frame["product_code"].str.strip()
    frame["expiry_month"] = frame["expiry_month"].str.strip()
    frame["trade_time"] = frame["trade_time"].str.strip().str.zfill(6)
    frame = frame[(frame["product_code"] == instrument) & frame["expiry_month"].str.fullmatch(r"\d{6}", na=False)].copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d", errors="coerce")
    frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    frame = frame.dropna(subset=["trade_date", "trade_time", "price", "volume"])
    frame["actual_datetime"] = pd.to_datetime(
        frame["trade_date"].dt.strftime("%Y%m%d") + frame["trade_time"],
        format="%Y%m%d%H%M%S",
        errors="coerce",
    )
    return frame.dropna(subset=["actual_datetime"])[
        ["actual_datetime", "expiry_month", "price", "volume"]
    ]


def _aggregate_one_minute(ticks: pd.DataFrame, *, session_end: datetime) -> pd.DataFrame:
    if ticks.empty:
        return pd.DataFrame(columns=["bar_time", "open", "high", "low", "close", "volume"])
    frame = ticks.copy().sort_values("actual_datetime")
    exact_close = frame["actual_datetime"] == session_end
    frame.loc[exact_close, "actual_datetime"] = frame.loc[exact_close, "actual_datetime"] - pd.Timedelta(microseconds=1)
    frame["bar_time"] = frame["actual_datetime"].dt.floor("min")
    grouped = (
        frame.groupby("bar_time", as_index=False)
        .agg(open=("price", "first"), high=("price", "max"), low=("price", "min"), close=("price", "last"), volume=("volume", "sum"))
        .sort_values("bar_time")
        .reset_index(drop=True)
    )
    grouped = _fill_isolated_no_trade_minutes(grouped)
    grouped["bar_time"] = grouped["bar_time"].dt.tz_localize(TAIPEI)
    return grouped


def _fill_isolated_no_trade_minutes(grouped: pd.DataFrame) -> pd.DataFrame:
    """Materialize one isolated no-trade minute without concealing larger gaps.

    TAIFEX tick files omit a minute when no trade occurs.  A causal one-minute
    feed represents that minute with the prior close and zero volume.  Only a
    single-minute hole bracketed by observed minutes is safe to infer; longer
    gaps remain visible so the deterministic continuity guard rejects them.
    """

    if len(grouped) < 2:
        return grouped
    observed = set(grouped["bar_time"].tolist())
    full_index = pd.date_range(
        start=grouped.iloc[0]["bar_time"],
        end=grouped.iloc[-1]["bar_time"],
        freq="min",
    )
    fillable = [
        at
        for at in full_index
        if at not in observed
        and at - pd.Timedelta(minutes=1) in observed
        and at + pd.Timedelta(minutes=1) in observed
    ]
    if not fillable:
        return grouped
    closes = grouped.set_index("bar_time")["close"]
    synthetic = []
    for at in fillable:
        prior_close = float(closes.loc[at - pd.Timedelta(minutes=1)])
        synthetic.append(
            {
                "bar_time": at,
                "open": prior_close,
                "high": prior_close,
                "low": prior_close,
                "close": prior_close,
                "volume": 0.0,
            }
        )
    return (
        pd.concat([grouped, pd.DataFrame(synthetic)], ignore_index=True)
        .sort_values("bar_time")
        .reset_index(drop=True)
    )


def _add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy().sort_values("bar_time").reset_index(drop=True)
    result["sma21"] = result["close"].rolling(21).mean()
    result["sma105"] = result["close"].rolling(105).mean()
    previous_close = result["close"].shift(1)
    true_range = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - previous_close).abs(),
            (result["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    result["atr14"] = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    return result


def _number(value: object) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else round(number, 4)


def _optional_number(value: object) -> int | float | None:
    if value is None or pd.isna(value):
        return None
    return _number(value)
