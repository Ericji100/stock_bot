"""Shared source normalization helpers for search discovery results."""
from __future__ import annotations

from collections.abc import Iterable

from .models import CommandRequest, SourceItem
from .source_date_normalizer import normalize_published_date_with_status
from .source_text_cleaner import clean_source_text


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
        title = clean_source_text(source.title)
        snippet = clean_source_text(source.snippet) if source.snippet else source.snippet
        published_date, date_status = normalize_published_date_with_status(
            explicit_values=(source.published_date,),
            inferred_values=(title, snippet, source.url),
        )
        if date_status != "unknown":
            marker = f"source_date:{date_status}"
            if marker not in found_by:
                found_by.append(marker)

        normalized.append(
            SourceItem(
                source_id=source.source_id or f"S{index:03d}",
                title=title or source.title,
                url=source.url,
                source_level=source.source_level,
                published_date=published_date,
                snippet=snippet,
                used_in_section=used_in_section,
                provider=source.provider or provider,
                provider_detail=source.provider_detail or query_intent or source_provider,
                fetch_provider=source.fetch_provider,
                fetch_status=source.fetch_status,
                fetch_quality=source.fetch_quality,
                failure_reason=source.failure_reason,
                found_by=found_by,
            )
        )
    return normalized
