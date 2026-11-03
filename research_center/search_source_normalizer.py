"""Shared source normalization helpers for search discovery results."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from .models import CommandRequest, SourceItem
from .source_date_normalizer import normalize_published_date_with_status, source_publication_status, normalize_published_date, publication_metadata_date
from .source_text_cleaner import clean_source_text, clean_source_title


def validate_source_identity_map(inventory, prompt_sources):
    """Check the bound prompt namespace against the report's full inventory."""
    known = {}
    errors = []
    checked = 0
    for source in inventory:
        if source.source_id in known and known[source.source_id] != source.url:
            errors.append(f'來源編號 {source.source_id} 對應多個不同網址。')
        known[source.source_id] = source.url
    for source in prompt_sources:
        source_id = source.get('source_id') if isinstance(source, dict) else source.source_id
        url = source.get('url') if isinstance(source, dict) else source.url
        if not source_id or not url:
            continue
        checked += 1
        if known.get(source_id) != url:
            errors.append(f'入模來源 {source_id} 與報告來源網址不一致，不能僅以編號存在判定引用有效。')
    return {'passed': not errors, 'checked_prompt_sources': checked, 'errors': errors,
            'scope': '只核對入模來源與報告索引的編號／網址一致性，不證明來源內容支持分析結論。'}


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
        original_title = clean_source_text(source.title)
        title = clean_source_title(original_title, source.url)
        if title != original_title and "source_title:code_isolated" not in found_by:
            found_by.append("source_title:code_isolated")
        snippet = clean_source_text(source.snippet) if source.snippet else source.snippet
        published_date, date_status = normalize_published_date_with_status(
            explicit_values=(source.published_date, publication_metadata_date(source.snippet)),
            inferred_values=(source.url,),
        )
        if source.published_date:
            date_status=source_publication_status(source)
        if date_status != "unknown":
            marker = f"source_date:{date_status}"
            if marker not in found_by:
                found_by.append(marker)

        normalized.append(
            replace(source,
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
                date_status=date_status,
                event_date=source.event_date or normalize_published_date(title, snippet),
            )
        )
    return normalized
