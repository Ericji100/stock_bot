from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

EVENT_CONTEXT_SCHEMA_VERSION = "event_context_v1"


def build_event_context(
    *,
    target: str | None = None,
    events: list[dict[str, Any]] | None = None,
    days: int = 30,
    event_types: list[str] | None = None,
) -> dict[str, Any]:
    rows = list(events or [])
    cutoff = datetime.now().date() - timedelta(days=days)
    selected = []
    for event in rows:
        if target and str(event.get("target") or "") not in {target, "", "unknown"}:
            continue
        if event_types and str(event.get("event_type") or "") not in event_types:
            continue
        event_date = _parse_event_date(event.get("published_date"))
        if event_date and event_date < cutoff:
            continue
        selected.append(event)
    return {
        "schema_version": EVENT_CONTEXT_SCHEMA_VERSION,
        "target": target,
        "lookback_days": days,
        "event_count": len(selected),
        "event_types": _type_counts(selected),
        "events": selected[:80],
    }


def summarize_event_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": context.get("schema_version") or EVENT_CONTEXT_SCHEMA_VERSION,
        "target": context.get("target"),
        "event_count": context.get("event_count") or 0,
        "event_types": context.get("event_types") or {},
    }


def _type_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        key = str(event.get("event_type") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _parse_event_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
