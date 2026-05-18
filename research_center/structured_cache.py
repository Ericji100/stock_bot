"""Structured data cache for research results.

Stores per-stock per-date structured data as JSON files so that
/research can reuse previously fetched data instead of re-fetching.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

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

    generated_at = payload.get("generated_at")
    if generated_at:
        try:
            dt = datetime.fromisoformat(generated_at)
            if datetime.now(dt.tzinfo) - dt > timedelta(hours=max_age_hours):
                return None
        except Exception:
            pass

    return payload.get("data") if isinstance(payload.get("data"), dict) else None