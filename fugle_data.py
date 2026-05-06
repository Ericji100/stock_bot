from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path
from threading import Lock
from typing import Any

import httpx
import pandas as pd


ROOT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ROOT_DIR / "config.json"
FUGLE_HISTORICAL_CANDLES_URL = "https://api.fugle.tw/marketdata/v1.0/stock/historical/candles/{symbol}"
FUGLE_MIN_INTERVAL_SECONDS = 1.05
FUGLE_TIMEOUT_SECONDS = 20.0
FUGLE_TIMEFRAME_MAP = {
    "1d": "D",
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "10m": "10",
    "15m": "15",
    "30m": "30",
    "60m": "60",
}

_FUGLE_LOCK = Lock()
_LAST_FUGLE_REQUEST_AT = 0.0


def get_fugle_api_key() -> str | None:
    env_key = os.getenv("FUGLE_API_KEY")
    if env_key:
        return env_key.strip()

    if not CONFIG_PATH.exists():
        return None

    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None

    key = payload.get("fugle_api_key")
    return str(key).strip() if key else None


def _wait_for_fugle_slot() -> None:
    global _LAST_FUGLE_REQUEST_AT
    with _FUGLE_LOCK:
        now = time.monotonic()
        wait_seconds = FUGLE_MIN_INTERVAL_SECONDS - (now - _LAST_FUGLE_REQUEST_AT)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _LAST_FUGLE_REQUEST_AT = time.monotonic()


def _symbol_code(symbol: str) -> str:
    return str(symbol).split(".", 1)[0].strip()


def fetch_fugle_history(symbol: str, start_date: date, end_date: date, frequency: str = "1d") -> pd.DataFrame:
    api_key = get_fugle_api_key()
    timeframe = FUGLE_TIMEFRAME_MAP.get(frequency.lower())
    if not api_key or not timeframe:
        return pd.DataFrame()

    params: dict[str, Any] = {
        "timeframe": timeframe,
        "fields": "open,high,low,close,volume",
        "sort": "asc",
    }
    if timeframe == "D":
        params["from"] = start_date.isoformat()
        params["to"] = end_date.isoformat()

    _wait_for_fugle_slot()
    try:
        with httpx.Client(timeout=FUGLE_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = client.get(
                FUGLE_HISTORICAL_CANDLES_URL.format(symbol=_symbol_code(symbol)),
                params=params,
                headers={"X-API-KEY": api_key},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return pd.DataFrame()

    rows = []
    for item in payload.get("data", []):
        timestamp = pd.to_datetime(item.get("date"), errors="coerce")
        if pd.isna(timestamp):
            continue
        rows.append(
            {
                "datetime": timestamp,
                "open": item.get("open"),
                "high": item.get("high"),
                "low": item.get("low"),
                "close": item.get("close"),
                "volume": item.get("volume"),
            }
        )

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame

    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
    frame = frame.dropna(subset=["datetime"])
    frame = frame[
        (frame["datetime"].dt.date >= start_date)
        & (frame["datetime"].dt.date <= end_date)
    ].copy()
    return frame.sort_values("datetime").reset_index(drop=True)
