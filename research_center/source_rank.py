from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlparse

from .models import SourceItem
from .preferred_sources import match_preferred_source, preferred_source_weight
from .source_date_normalizer import normalize_published_date_with_status, normalize_published_date, publication_metadata_date
from .source_text_cleaner import clean_source_text, clean_source_title, source_body_text
from .search_transmission import merge_search_text, merge_search_records

LEVEL_1_DOMAINS = ("twse.com.tw", "tpex.org.tw", "mops.twse.com.tw", "mopsov.twse.com.tw")
LEVEL_2_DOMAINS = ("moneydj.com", "cnyes.com", "ctee.com.tw", "udn.com", "digitimes.com", "trendforce.com", "cna.com.tw")
LEVEL_4_DOMAINS = (
    "ptt.cc",
    "dcard.tw",
    "mobile01.com",
    "threads.com",
    "threads.net",
    "x.com",
    "twitter.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "cmoney.tw",
    "social.cmoney.tw",
)


def rank_source(url: str, title: str = "") -> str:
    from .evidence_governance import is_official_url
    host = (urlparse(url).hostname or '').lower()
    if is_official_url(url):
        return "Level 1"
    if any(domain in host for domain in LEVEL_2_DOMAINS):
        return "Level 2"
    if any(domain in host for domain in LEVEL_4_DOMAINS):
        return "Level 4"
    if not url:
        return "Level 5"
    return "Level 3"


def make_source_items(raw_sources: list[dict[str, str]]) -> list[SourceItem]:
    items: list[SourceItem] = []
    seen: dict[str, int] = {}
    for raw in raw_sources:
        url = str(raw.get("url") or "").strip()
        original_title = clean_source_text(raw.get("title") or url or "未命名來源")
        title = clean_source_title(original_title, url)
        snippet = clean_source_text(raw.get("snippet")) if raw.get("snippet") else raw.get("snippet")
        content = source_body_text(raw) or None
        if not url:
            continue
        if url in seen:
            index = seen[url]
            existing = items[index]
            items[index] = replace(existing, snippet=merge_search_text(existing.snippet, snippet),
                                   content=merge_search_text(existing.content, content) or None,
                                   search_summary_events=merge_search_records(existing.search_summary_events, raw.get('search_summary_events')),
                                   found_by=list(dict.fromkeys([*existing.found_by, *raw.get('found_by', [])])))
            continue
        seen[url] = len(items)
        preferred = match_preferred_source(url)
        source_level = str(preferred.get("level")) if preferred else rank_source(url, title)
        provider_detail = raw.get("provider_detail") or ""
        if preferred:
            provider_detail = f"[{preferred.get('level')} {preferred.get('name')}] {provider_detail}".strip()
        published_date, date_status = normalize_published_date_with_status(
            explicit_values=(
                raw.get("published_date"),
                raw.get("published_at"),
                raw.get("datePublished"),
                raw.get("article:published_time"),
                publication_metadata_date(raw.get('snippet')),
            ),
            inferred_values=(raw.get("date"),url),
        )
        found_by = list(raw.get("found_by", []))
        if title != original_title:
            found_by.append("source_title:code_isolated")
            provider_detail = f"{provider_detail}；標題程式碼已隔離".strip("；")
        if date_status != "unknown":
            marker = f"source_date:{date_status}"
            if marker not in found_by:
                found_by.append(marker)
        items.append(
            SourceItem(
                source_id=f"S{len(items) + 1:03d}",
                title=title,
                url=url,
                source_level=source_level,
                published_date=published_date,
                snippet=snippet,
                provider=raw.get("provider"),
                provider_detail=provider_detail,
                fetch_provider=raw.get("fetch_provider"),
                fetch_status=raw.get("fetch_status"),
                fetch_quality=raw.get("fetch_quality"),
                failure_reason=raw.get("failure_reason"),
                found_by=found_by,
                date_status=raw.get('date_status') or date_status,
                event_date=raw.get('event_date') or normalize_published_date(title,snippet,raw.get('content')),
                data_date=raw.get('data_date'),period_start=raw.get('period_start'),period_end=raw.get('period_end'),
                fetched_at=raw.get('fetched_at'),updated_date=raw.get('updated_date') or normalize_published_date(raw.get('dateModified')),
                document_kind=raw.get('document_kind'),
                search_summary_events=raw.get('search_summary_events') or [],
                content=content,
            )
        )
    return items


def sort_sources_by_preferred_weight(sources: list[SourceItem]) -> list[SourceItem]:
    """Sort sources so that preferred (high-weight) ones come first.

    Tie-breaking preserves the original order (stable sort).
    Community sources are never upgraded and keep their low weight.
    """
    def _key(item: SourceItem) -> int:
        return preferred_source_weight(item.url)
    # Use sorted with stable sort (Python's Timsort is stable)
    return sorted(sources, key=_key, reverse=True)


NOISY_THEME_SOURCE_TERMS = (
    "geopolitics",
    "podcast",
    "youtube",
    "facebook",
    "threads",
    "reddit",
    "ai news today",
    "latest ai news",
)


def select_theme_sources_for_prompt(
    sources: list[SourceItem],
    *,
    theme: str,
    keywords: list[str] | None = None,
    companies: list[dict] | None = None,
    max_sources: int = 60,
) -> tuple[list[SourceItem], dict]:
    """Select a compact, theme-relevant source set for AI prompt input.

    The full source list should still be preserved in reports/sources.json.
    """
    if not sources:
        return [], {
            "enabled": True,
            "input_count": 0,
            "selected_count": 0,
            "max_sources": max_sources,
            "theme_relevant_count": 0,
        }

    terms = _theme_relevance_terms(theme, keywords or [], companies or [])
    scored: list[tuple[int, int, SourceItem]] = []
    relevant_count = 0
    for index, source in enumerate(sources):
        relevance = theme_source_relevance(source, terms)
        if relevance > 0:
            relevant_count += 1
        scored.append((_theme_source_score(source, relevance), -index, source))

    selected = [item for _score, _index, item in sorted(scored, key=lambda row: (row[0], row[1]), reverse=True)[:max_sources]]
    diagnostics = {
        "enabled": True,
        "input_count": len(sources),
        "selected_count": len(selected),
        "max_sources": max_sources,
        "theme_relevant_count": relevant_count,
        "keyword_count": len(terms),
        "selected_provider_counts": _provider_counts(selected),
        "selected_level_counts": _level_counts(selected),
    }
    # These IDs already belong to the full report inventory. Re-ranking a
    # subset must not give a different document the same citation identifier.
    return selected, diagnostics


def theme_source_relevance(source: SourceItem, terms: list[str]) -> int:
    text = f"{source.title or ''} {source.snippet or ''} {source.url or ''}".lower()
    score = 0
    for term in terms:
        value = str(term or "").strip().lower()
        if not value:
            continue
        if value in text:
            score += 3 if len(value) >= 4 else 1
    if any(term in text for term in NOISY_THEME_SOURCE_TERMS):
        score -= 4
    if "台股" in text or "taiwan" in text:
        score += 2
    return score


def _theme_source_score(source: SourceItem, relevance: int) -> int:
    level_weight = {
        "Level 1": 45,
        "L1_official": 45,
        "Level 2": 32,
        "L2_media": 32,
        "L2_industry": 32,
        "Level 3": 18,
        "Level 4": -10,
        "L3_community": -10,
        "Level 5": -20,
    }.get(source.source_level, 10)
    provider_weight = {
        "official_connector": 35,
        "tavily_extract": 28,
        "gemini_grounding": 25,
        "requests_bs4": 23,
        "html_fetch": 20,
        "minimax_mcp_search": 15,
        "tavily_search": 12,
        "forum_direct": -5,
        "forum_search": -8,
    }.get(source.provider, 0)
    fetch_bonus = 12 if source.fetch_status == "success" else 0
    preferred_bonus = preferred_source_weight(source.url) // 4
    return relevance * 8 + level_weight + provider_weight + fetch_bonus + preferred_bonus


def _theme_relevance_terms(theme: str, keywords: list[str], companies: list[dict]) -> list[str]:
    terms: list[str] = []
    terms.append(theme)
    terms.extend(keywords)
    for row in companies[:30]:
        if not isinstance(row, dict):
            continue
        terms.append(str(row.get("code") or ""))
        terms.append(str(row.get("name") or ""))
        terms.append(str(row.get("company_code") or ""))
        terms.append(str(row.get("company_name") or ""))
    if "電源" in theme or "power" in theme.lower():
        terms.extend(["電源", "PSU", "伺服器電源", "BBU", "HVDC", "800VDC", "power supply"])
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        value = " ".join(str(term or "").split())
        key = value.lower()
        if value and key not in seen:
            deduped.append(value)
            seen.add(key)
    return deduped


def _renumber_sources(sources: list[SourceItem]) -> list[SourceItem]:
    return list(sources)


def _provider_counts(sources: list[SourceItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source in sources:
        key = source.provider or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _level_counts(sources: list[SourceItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source in sources:
        key = source.source_level or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts
