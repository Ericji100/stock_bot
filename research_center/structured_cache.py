"""Structured data cache for research results.

Stores per-stock per-date structured data as JSON files so that
/research can reuse previously fetched data instead of re-fetching.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT_DIR = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT_DIR / ".cache" / "research_structured"


def _cache_path(stock_code: str, report_date: date) -> Path:
    return CACHE_DIR / report_date.strftime("%Y%m%d") / f"{stock_code}.json"


def save_research_structured_cache(stock_code: str, report_date: date, data: dict[str, Any]) -> Path:
    """Save structured data to cache for a given stock and date."""
    path = _cache_path(stock_code, report_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "stock_code": stock_code,
        "report_date": report_date.isoformat(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": data,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def load_research_structured_cache(
    stock_code: str,
    report_date: date,
    max_age_hours: int = 24,
) -> dict[str, Any] | None:
    """Load structured data from cache for a given stock and date.

    Returns None if the cache doesn't exist or is older than max_age_hours.
    """
    path = _cache_path(stock_code, report_date)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    if not _valid_payload(payload, stock_code, report_date):
        return None
    try:
        dt = datetime.fromisoformat(payload["generated_at"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("Asia/Taipei"))
        age = datetime.now(dt.tzinfo) - dt
        if age < -timedelta(minutes=5) or age > timedelta(hours=max_age_hours):
            return None
        now = datetime.now(ZoneInfo('Asia/Taipei'))
        generated_tw = dt.astimezone(ZoneInfo('Asia/Taipei'))
        if report_date == now.date() and now.hour >= 15 and (generated_tw.date() < report_date or generated_tw.hour < 15):
            # Same calendar date is not the same market session. Keep the old
            # file as recovery evidence; request a post-close refresh instead.
            return None
    except (TypeError, ValueError, KeyError):
        return None
    return payload["data"]


def _valid_payload(payload: Any, stock_code: str, report_date: date) -> bool:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return False
    if str(payload.get("stock_code")) != str(stock_code) or payload.get("report_date") != report_date.isoformat():
        return False
    try:
        generated = datetime.fromisoformat(payload['generated_at'])
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=ZoneInfo('Asia/Taipei'))
        if generated - datetime.now(generated.tzinfo) > timedelta(minutes=5):
            return False
    except (KeyError, ValueError, TypeError):
        return False
    stock = payload["data"].get("stock") or {}
    if isinstance(stock, dict) and stock.get("code") and str(stock["code"]) != str(stock_code):
        return False
    # Data dates, not generation dates, determine point-in-time eligibility.
    from .source_date_normalizer import normalize_published_date
    def valid_dates(value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("kind") in {"forecast", "scenario", "prediction"}:
                return True
            for key in ("data_date", "as_of_date", "period_end", "price_date"):
                parsed = normalize_published_date(value.get(key))
                if parsed and parsed > report_date.isoformat():
                    return False
            return all(valid_dates(item) for item in value.values())
        return all(valid_dates(item) for item in value) if isinstance(value, list) else True
    return valid_dates(payload["data"])


def load_latest_research_structured_cache(
    stock_code: str,
    *,
    before_or_on: date | None = None,
    max_age_days: int = 7,
) -> tuple[dict[str, Any], date] | None:
    """Load the newest nearby structured cache for a stock.

    This is a resilience fallback for interactive /research runs when live data
    sources are temporarily unavailable. It deliberately searches only a short
    window so stale data is not silently used as current evidence.
    """
    if not CACHE_DIR.exists():
        return None
    target = before_or_on or datetime.now(ZoneInfo("Asia/Taipei")).date()
    candidates: list[tuple[date, Path]] = []
    for folder in CACHE_DIR.iterdir():
        if not folder.is_dir():
            continue
        try:
            folder_date = datetime.strptime(folder.name, "%Y%m%d").date()
        except ValueError:
            continue
        if folder_date > target or (target - folder_date).days > max_age_days:
            continue
        path = folder / f"{stock_code}.json"
        if path.exists():
            candidates.append((folder_date, path))
    for cache_date, path in sorted(candidates, key=lambda item: item[0], reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if _valid_payload(payload, stock_code, cache_date):
            return payload["data"], cache_date
    return None
