"""Best-effort source published date normalization.

Only promote dates that are explicit in metadata, text, or URL. Do not invent
dates from fetch time or report time.
"""
from __future__ import annotations

import re
import calendar
from datetime import date, datetime, timedelta
from html import unescape
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


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
    r"(?:datePublished|article:published_time|pubdate|publishdate|published_time)"
    r"[^>]{0,160}?(20\d{2}-\d{1,2}-\d{1,2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?)",
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


def publication_metadata_value(value: Any) -> Any:
    """Read the publisher's value, preserving an explicit clock and offset."""
    import json
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            if match := _META_DATE_RE.search(value):
                return match.group(1)
            return None
    if isinstance(value, dict):
        values = [value.get(key) for key in ('datePublished', 'article:published_time', 'published_date', 'published_at')]
        return next((item for item in values if normalize_published_date(item) and not
            (_RELATIVE_TIME_RE.search(str(item)) or _ZH_RELATIVE_TIME_RE.search(str(item)))), None)
    return None


def publication_metadata_date(value: Any) -> str | None:
    return normalize_published_date(publication_metadata_value(value))


PUBLICATION_TIME_MARKER = 'source_publication_time:'


def publication_timestamp(value: Any) -> str | None:
    if not isinstance(value, datetime):
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}[^\n]*', value.strip()):
            return None
        try:
            value = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            return None
    zone = ZoneInfo('Asia/Taipei')
    return (value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)).isoformat()


def news_publication_record(source: Any, as_of: date | datetime | None = None) -> dict[str, Any]:
    """One publication gate for news; storage/event/URL dates never prove freshness."""
    import json
    get = source.get if isinstance(source, dict) else lambda key, default=None: getattr(source, key, default)
    zone = ZoneInfo('Asia/Taipei')
    if isinstance(as_of, datetime):
        end = as_of.replace(tzinfo=zone) if as_of.tzinfo is None else as_of.astimezone(zone)
    elif isinstance(as_of, date):
        end = datetime.combine(as_of, datetime.max.time(), zone)
    else:
        end = datetime.now(zone)
    raw = get('published_at') or get('published_date') or get('datePublished') or get('article:published_time')
    day = normalize_published_date(raw)
    tags = get('tags', []) or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except ValueError:
            tags = []
    markers = get('found_by', []) or []
    status = get('date_status')
    if status == 'conflict' or 'publication_conflict' in tags or 'source_date:conflict' in markers:
        status = 'conflict'
    elif 'publication_unknown' in tags or 'source_date:unknown' in markers:
        status = 'unknown'
    elif 'publication_inferred' in tags or 'source_date:inferred' in markers:
        status = 'inferred'
    elif 'publication_explicit' in tags or 'source_date:explicit' in markers:
        status = status or 'explicit'
    metadata_raw = publication_metadata_value(get('full_text') or get('content') or get('snippet') or '')
    metadata = normalize_published_date(metadata_raw)
    conflict = bool(metadata and day and status not in {'unknown', 'inferred'} and metadata != day)
    if metadata and not conflict and status != 'conflict':
        day, status = metadata, 'explicit'
    elif raw and day and status is None:
        status = 'inferred' if _RELATIVE_TIME_RE.search(str(raw)) or _ZH_RELATIVE_TIME_RE.search(str(raw)) else 'explicit'
    if not metadata and status != 'conflict' and (_RELATIVE_TIME_RE.search(str(raw)) or _ZH_RELATIVE_TIME_RE.search(str(raw))):
        status = 'inferred'
    if not day:
        day = normalize_published_date(get('date'), get('url'))
        status = 'conflict' if status == 'conflict' else 'inferred' if day else 'unknown'
    if get('document_kind') in {'reference_link', 'unavailable'}:
        status = 'unknown'
    if conflict:
        status = 'conflict'
    status = status if status in {'explicit', 'inferred', 'unknown', 'conflict'} else 'unknown'
    stamp = None
    times = set()
    if day and status == 'explicit':
        candidates = [raw, metadata_raw, *(m.removeprefix(PUBLICATION_TIME_MARKER) for m in markers
                                          if m.startswith(PUBLICATION_TIME_MARKER))]
        times = {instant for value in candidates if (instant := publication_timestamp(value)) and instant[:10] == day}
        stamp = (datetime.fromisoformat(next(iter(times))) if len(times) == 1 else
                 datetime.combine(date.fromisoformat(day), datetime.min.time(), zone))
    usable = bool(stamp and stamp <= end)
    return {'published_date': day, 'publication_status': status,
            'published_datetime': stamp.isoformat() if stamp else None,
            'publication_timestamp': next(iter(times)) if len(times) == 1 else None,
            'publication_time_markers': sorted(PUBLICATION_TIME_MARKER + value for value in times),
            'publication_time_conflict': len(times) > 1,
            'available_by_report_date': usable, 'metadata_date': metadata,
            'reason': 'confirmed' if usable else 'future' if stamp else status,
            'analysis_date': end.date().isoformat()}


def news_in_publication_window(source: Any, hours: float, as_of: date | datetime | None = None) -> bool:
    zone = ZoneInfo('Asia/Taipei')
    end = (as_of.replace(tzinfo=zone) if as_of.tzinfo is None else as_of.astimezone(zone)) if isinstance(as_of, datetime) else (
        datetime.combine(as_of, datetime.max.time(), zone) if isinstance(as_of, date) else datetime.now(zone))
    record = news_publication_record(source, end)
    return bool(record['available_by_report_date'] and
                end - timedelta(hours=hours) <= datetime.fromisoformat(record['published_datetime']) <= end)


def source_publication_status(source: Any) -> str:
    return news_publication_record(source)['publication_status']


def source_temporal_record(source: Any, report_date: date) -> dict[str, Any]:
    get = source.get if isinstance(source,dict) else lambda key,default=None: getattr(source,key,default)
    published=normalize_published_date(get('published_date') or get('published_at'))
    status=source_publication_status(source)
    data_date=normalize_published_date(get('data_date') or get('as_of_date'))
    updated_date=normalize_published_date(get('updated_date'))
    body_future = future_actual_periods(str(get('content') or get('snippet') or ''), report_date)
    return {'source_id':get('source_id'), 'published_date':published,
            'publication_status':status,'updated_date':updated_date,
            'event_date':normalize_published_date(get('event_date')),'data_date':data_date,
            'period_start':get('period_start'),'period_end':get('period_end'),'fetched_at':get('fetched_at'),
            'available_by_report_date':published<=report_date.isoformat() if published and status=='explicit' else None,
            'embedded_future_actuals': body_future,
            'content_updated_after_report_date':bool((updated_date and updated_date>report_date.isoformat()) or body_future),
            'data_after_report_date':bool((data_date and data_date>report_date.isoformat()) or
                 ((end:=normalize_published_date(get('period_end'))) and end>report_date.isoformat()))}


def future_actual_periods(text: str, report_date: date) -> list[str]:
    """Detect dated realised figures in dynamic bodies, not forecast horizons.

    Ambiguous future dated numeric rows are quarantined for historical requests;
    original sources remain in the caller's archive. A publication date alone
    cannot certify that a subsequently updated table was known at that time.
    """
    result = set()
    for line in re.split(r'[\n\r]|(?<=[。；])', unescape(text)):
        forecast = bool(re.search(r'預估|預測|預期|目標|展望|假設|情境|forecast|estimate|outlook|guidance|scenario', line, re.I))
        actual = bool(re.search(r'實績|實際|已公布|公布.*營收|收盤|成交|actual|historical|\bclose\b|Schlusskurs|\bKurs\b', line, re.I))
        if forecast and not actual:
            continue
        for match in re.finditer(r'(?<!\d)(20\d{2})[-/.年]\s*(0?[1-9]|1[0-2])(?:[-/.月]\s*([0-3]?\d)(?:日)?)?(?!\d)', line):
            year, month, day = match.groups()
            try:
                period = date(int(year), int(month), int(day) if day else calendar.monthrange(int(year), int(month))[1])
            except ValueError:
                continue
            tail = line[match.end():match.end()+100]
            if period > report_date and (actual or re.search(r'\d[\d,.]*', tail)):
                result.add(period.isoformat() if day else period.strftime('%Y-%m'))
        for match in re.finditer(r'(?<!\d)([0-3]?\d)\.(0?[1-9]|1[0-2])\.(20\d{2})(?!\d)', line):
            day, month, year = match.groups()
            try:
                period = date(int(year), int(month), int(day))
            except ValueError:
                continue
            if period > report_date and (actual or re.search(r'\d', line[match.end():match.end()+100])):
                result.add(period.isoformat())
    return sorted(result)


def _extract_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        local = value.astimezone(ZoneInfo('Asia/Taipei')) if value.tzinfo is not None else value
        return local.date().isoformat()
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
            value.get("article:published_time"),
            value.get("displayed_link"),
            value.get("title"),
            value.get("snippet"),
            value.get("summary"),
            value.get("content"),
            value.get("url"),
        )
    text = unescape(str(value or "").strip())
    if not text:
        return None
    if text.startswith(('http://', 'https://')):
        try:
            parsed = urlsplit(text)
            host = parsed.hostname or ''
            pattern = None
            if host == 'ctee.com.tw' or host.endswith('.ctee.com.tw'):
                pattern = r'/news/(20\d{6})\d{6}-'
            elif host == 'cna.com.tw' or host.endswith('.cna.com.tw'):
                pattern = r'/news/[^/]+/(20\d{6})\d{4}\.aspx'
            if pattern and (match := re.search(pattern, parsed.path)):
                return datetime.strptime(match.group(1), '%Y%m%d').date().isoformat()
        except ValueError:
            pass
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}[^\n]*', text):
        try:
            timestamp = datetime.fromisoformat(text.replace('Z', '+00:00'))
            local = timestamp.astimezone(ZoneInfo('Asia/Taipei')) if timestamp.tzinfo is not None else timestamp
            return local.date().isoformat()
        except ValueError:
            pass
    if relative := _extract_relative_date(text):
        return relative
    if match := _META_DATE_RE.search(text):
        return _extract_date(match.group(1))
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
    now = datetime.now(ZoneInfo('Asia/Taipei'))
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
