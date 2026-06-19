"""Best-effort source published date normalization.

Only promote dates that are explicit in metadata, text, or URL. Do not invent
dates from fetch time or report time.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from html import unescape
from typing import Any


_ISO_DATE_RE = re.compile(
    r"(?<!\d)(20\d{2}|19\d{2})[-/.年](0?[1-9]|1[0-2])[-/.月](0?[1-9]|[12]\d|3[01])日?(?!\d)"
)
_COMPACT_DATE_RE = re.compile(r"(?<!\d)(20\d{2}|19\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")
_ROC_DATE_RE = re.compile(r"(?<!\d)(1\d{2})[-/.年](0?[1-9]|1[0-2])[-/.月](0?[1-9]|[12]\d|3[01])日?(?!\d)")
_EN_MONTH_DATE_RE = re.compile(
    r"\b(?:Published|Updated|Date)?\s*:?\s*"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+"
    r"([0-3]?\d),\s*(20\d{2}|19\d{2})\b",
    re.IGNORECASE,
)
_META_DATE_RE = re.compile(
    r"(?:datePublished|dateModified|article:published_time|pubdate|publishdate|published_time)"
    r"[^>]{0,160}?(20\d{2}-\d{1,2}-\d{1,2})",
    re.IGNORECASE,
)
_RELATIVE_TIME_RE = re.compile(
    r"\b(?:about\s+)?(\d{1,3})\s*(minutes?|mins?|hours?|hrs?|days?)\s+ago\b",
    re.IGNORECASE,
)
_ZH_RELATIVE_TIME_RE = re.compile(r"(\d{1,3})\s*(分鐘|分|小時|時|天|日)前")
_MONTH_MAP = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def normalize_published_date(*values: Any) -> str | None:
    """Return YYYY-MM-DD if an explicit date can be found in supplied values."""
    for value in values:
        candidate = _extract_date(value)
        if candidate:
            return candidate
    return None


def normalize_published_date_with_status(
    *,
    explicit_values: tuple[Any, ...] = (),
    inferred_values: tuple[Any, ...] = (),
) -> tuple[str | None, str]:
    """Return normalized date and status: explicit, inferred, or unknown."""
    if explicit := normalize_published_date(*explicit_values):
        return explicit, "explicit"
    if inferred := normalize_published_date(*inferred_values):
        return inferred, "inferred"
    return None, "unknown"


def _extract_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return normalize_published_date(
            value.get("published_date"),
            value.get("published_at"),
            value.get("date"),
            value.get("time"),
            value.get("datetime"),
            value.get("datePublished"),
            value.get("dateModified"),
            value.get("article:published_time"),
            value.get("displayed_link"),
            value.get("created_at") if value.get("news_origin") == "refresh" else None,
            value.get("title"),
            value.get("snippet"),
            value.get("summary"),
            value.get("content"),
            value.get("url"),
        )
    text = unescape(str(value or "").strip())
    if not text:
        return None
    if relative := _extract_relative_date(text):
        return relative
    if match := _META_DATE_RE.search(text):
        return _valid_date(match.group(1))
    if match := _ISO_DATE_RE.search(text):
        return _valid_date("-".join(match.groups()))
    if match := _ROC_DATE_RE.search(text):
        year, month, day = match.groups()
        return _valid_date(f"{int(year) + 1911}-{month}-{day}")
    if match := _EN_MONTH_DATE_RE.search(text):
        month_name, day, year = match.groups()
        month = _MONTH_MAP.get(month_name.lower().rstrip("."))
        if month:
            return _valid_date(f"{year}-{month}-{day}")
    if match := _COMPACT_DATE_RE.search(text):
        return _valid_date("-".join(match.groups()))
    return None


def _extract_relative_date(text: str) -> str | None:
    now = datetime.now()
    if match := _RELATIVE_TIME_RE.search(text):
        amount = int(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith(("minute", "min")):
            return (now - timedelta(minutes=amount)).date().isoformat()
        if unit.startswith(("hour", "hr")):
            return (now - timedelta(hours=amount)).date().isoformat()
        if unit.startswith("day"):
            return (now - timedelta(days=amount)).date().isoformat()
    if match := _ZH_RELATIVE_TIME_RE.search(text):
        amount = int(match.group(1))
        unit = match.group(2)
        if unit in {"分鐘", "分"}:
            return (now - timedelta(minutes=amount)).date().isoformat()
        if unit in {"小時", "時"}:
            return (now - timedelta(hours=amount)).date().isoformat()
        if unit in {"天", "日"}:
            return (now - timedelta(days=amount)).date().isoformat()
    return None


def _valid_date(text: str) -> str | None:
    parts = re.split(r"[-/.年月日\s]+", text.strip())
    parts = [part for part in parts if part]
    if len(parts) < 3:
        return None
    try:
        parsed = date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None
    return parsed.isoformat()
