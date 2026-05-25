"""Persist recent /theme report context for topic-library maintenance."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import CommandRequest, ReportArtifacts, SourceItem

ROOT_DIR = Path(__file__).resolve().parents[1]
RECENT_THEME_REPORTS_PATH = ROOT_DIR / ".cache" / "recent_theme_reports.json"
MAX_RECENT_THEME_REPORTS = 30


def save_theme_report_context(
    request: CommandRequest,
    summary: str,
    sources: list[SourceItem],
    structured_data: dict[str, Any],
    artifacts: ReportArtifacts,
) -> dict[str, Any]:
    """Save compact context from a completed /theme report."""
    if request.command != "theme":
        return {"saved": False, "reason": "not_theme_command"}
    theme = str(request.theme_scope or request.target or "").strip()
    if not theme:
        return {"saved": False, "reason": "empty_theme"}

    record = {
        "theme": theme,
        "mode": request.mode,
        "report_date": request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat(),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": str(summary or "")[:1200],
        "report_id": artifacts.report_id,
        "markdown_path": str(artifacts.markdown_path),
        "source_count": len(sources),
        "sources": [_source_to_dict(source) for source in sources[:20]],
        "suggested_search_terms": _build_search_terms(theme, structured_data, sources),
        "topic_context": _compact(structured_data.get("topic_context") or {}, max_string=500),
    }
    records = load_recent_theme_report_context(limit=MAX_RECENT_THEME_REPORTS, include_all=True)
    records = [item for item in records if item.get("report_id") != artifacts.report_id]
    records.insert(0, record)
    records = records[:MAX_RECENT_THEME_REPORTS]
    RECENT_THEME_REPORTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECENT_THEME_REPORTS_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"saved": True, "count": len(records), "path": str(RECENT_THEME_REPORTS_PATH)}


def load_recent_theme_report_context(
    focus_theme: str | None = None,
    *,
    limit: int = 5,
    include_all: bool = False,
) -> list[dict[str, Any]]:
    """Load recent /theme report context, optionally filtered by theme text."""
    if not RECENT_THEME_REPORTS_PATH.exists():
        return []
    try:
        data = json.loads(RECENT_THEME_REPORTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    records = [item for item in data if isinstance(item, dict)]
    if include_all:
        return records[: max(0, limit)]

    focus = str(focus_theme or "").strip().lower()
    if focus:
        records = [
            item for item in records
            if focus in str(item.get("theme") or "").lower()
            or any(focus in str(term).lower() for term in item.get("suggested_search_terms") or [])
        ] or records
    return records[: max(0, limit)]


def _source_to_dict(source: SourceItem) -> dict[str, Any]:
    try:
        data = asdict(source)
    except TypeError:
        data = dict(source) if isinstance(source, dict) else {}
    return {
        "source_id": data.get("source_id"),
        "title": data.get("title"),
        "url": data.get("url"),
        "source_level": data.get("source_level"),
        "published_date": data.get("published_date"),
        "snippet": data.get("snippet"),
        "provider": data.get("provider"),
    }


def _build_search_terms(theme: str, structured_data: dict[str, Any], sources: list[SourceItem]) -> list[str]:
    terms: list[str] = [theme]
    topic_context = structured_data.get("topic_context") if isinstance(structured_data, dict) else {}
    if isinstance(topic_context, dict):
        for topic in topic_context.get("matched_topics") or []:
            if not isinstance(topic, dict):
                continue
            terms.append(str(topic.get("theme_name") or ""))
            terms.append(str(topic.get("theme_id") or ""))
            terms.extend(str(k) for k in (topic.get("keywords") or [])[:6])
    for source in sources[:10]:
        terms.append(str(getattr(source, "title", "") or ""))
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        value = " ".join(str(term or "").split())
        key = value.lower()
        if value and key not in seen:
            deduped.append(value[:120])
            seen.add(key)
    return deduped[:30]


def _compact(value: Any, *, max_string: int = 800, max_list: int = 12, depth: int = 3) -> Any:
    if depth <= 0:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        return {str(k): _compact(v, max_string=max_string, max_list=max_list, depth=depth - 1) for k, v in list(value.items())[:30]}
    if isinstance(value, list):
        return [_compact(item, max_string=max_string, max_list=max_list, depth=depth - 1) for item in value[:max_list]]
    if isinstance(value, str) and len(value) > max_string:
        return value[:max_string].rstrip() + "...<truncated>"
    return value
