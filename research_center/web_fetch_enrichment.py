"""Shared WebFetch enrichment helper — extracted to avoid circular imports."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from .models import CommandRequest, SourceItem
from .source_rank import sort_sources_by_preferred_weight
from .web_fetch_service import WebFetchService

ProgressCallback = Callable[[str], None]


def _enrich_sources_with_web_fetch(
    request: CommandRequest,
    sources: list[SourceItem],
    structured_data: dict[str, Any],
    progress: ProgressCallback | None = None,
) -> None:
    """Best-effort page-content enrichment; failures must never block AI analysis."""
    if not sources:
        return

    # topic_maintain gets higher limits
    if request.command == "topic_maintain":
        max_urls = 30 if request.mode == "deep" else 12
    else:
        max_urls = 8 if request.mode == "deep" else 4
    # Sort by preferred-source weight so high-quality sources are fetched first
    candidate_sources = sort_sources_by_preferred_weight(sources)
    selected: list[SourceItem] = []
    seen: set[str] = set()
    for source in candidate_sources:
        url = (source.url or "").strip()
        if not url or url in seen:
            continue
        lower = url.lower()
        if not lower.startswith(("http://", "https://")):
            continue
        if any(lower.endswith(ext) for ext in (".pdf", ".xls", ".xlsx", ".csv", ".zip")):
            continue
        selected.append(source)
        seen.add(url)
        if len(selected) >= max_urls:
            break

    if not selected:
        structured_data["web_fetch_diagnostics"] = {
            "enabled": True,
            "status": "skipped",
            "reason": "no_fetchable_urls",
            "total_urls": 0,
        }
        return

    try:
        if progress:
            progress(f"WebFetch：開始讀取來源正文 {len(selected)} 筆")
        service = WebFetchService(timeout=12.0, max_workers=3)
        result = service.fetch_many([item.url for item in selected], progress=progress)
        by_url = {item.url: item for item in result.results}
        enriched_sources: list[SourceItem] = []
        enriched_count = 0
        for source in sources:
            fetched = by_url.get(source.url)
            if not fetched:
                enriched_sources.append(source)
                continue
            if fetched.content:
                enriched_count += 1
                snippet = fetched.content[:2000]
            else:
                snippet = source.snippet
            enriched_sources.append(
                replace(
                    source,
                    title=fetched.title or source.title,
                    snippet=snippet,
                    fetch_provider=fetched.fetch_provider,
                    fetch_status=fetched.content_status,
                    failure_reason=fetched.failure_reason,
                )
            )
        sources[:] = enriched_sources
        structured_data["web_fetch_diagnostics"] = {
            **result.diagnostics,
            "enabled": True,
            "status": "completed",
            "selected_url_count": len(selected),
            "enriched_source_count": enriched_count,
        }
        structured_data["web_fetched_sources"] = [
            {
                "url": item.url,
                "title": item.title,
                "content_status": item.content_status,
                "fetch_provider": item.fetch_provider,
                "failure_reason": item.failure_reason,
                "content_preview": item.content[:1200],
            }
            for item in result.results
        ]
        if progress:
            progress(f"WebFetch：完成，成功補正文 {enriched_count}/{len(selected)} 筆")
    except Exception as exc:
        structured_data["web_fetch_diagnostics"] = {
            "enabled": True,
            "status": "failed",
            "error": str(exc),
            "selected_url_count": len(selected),
        }
        if progress:
            progress(f"WebFetch：失敗但不中斷 AI 分析：{exc}")
