"""Shared source normalization helpers for search discovery results."""
from __future__ import annotations

from collections.abc import Iterable

from .models import CommandRequest, SourceItem


def normalize_source_items(
    sources: Iterable[SourceItem],
    request: CommandRequest,
    *,
    provider: str | None = None,
    query_intent: str | None = None,
) -> list[SourceItem]:
    """Add consistent provider, found_by and used_in_section metadata."""
    normalized: list[SourceItem] = []
    for index, source in enumerate(sources, 1):
        source_provider = provider or source.provider or "unknown"
        found_by = list(source.found_by or [])
        if source_provider and source_provider not in found_by:
            found_by.append(source_provider)
        if query_intent and query_intent not in found_by:
            found_by.append(query_intent)

        used_in_section = list(source.used_in_section or [])
        if request.command and request.command not in used_in_section:
            used_in_section.append(request.command)

        normalized.append(
            SourceItem(
                source_id=source.source_id or f"S{index:03d}",
                title=source.title,
                url=source.url,
                source_level=source.source_level,
                published_date=source.published_date,
                snippet=source.snippet,
                used_in_section=used_in_section,
                provider=source.provider or provider,
                provider_detail=source.provider_detail or query_intent or source_provider,
                fetch_provider=source.fetch_provider,
                fetch_status=source.fetch_status,
                failure_reason=source.failure_reason,
                found_by=found_by,
            )
        )
    return normalized
