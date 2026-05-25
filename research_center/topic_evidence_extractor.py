"""Rule-based topic evidence candidates extractor.

Extracts structured evidence candidates from WebFetch sources using rules only.
Does NOT call any AI model.
"""
from __future__ import annotations

import re
from typing import Any


# Built-in keyword table for topic matching
_THEME_KEYWORDS = {
    "AI伺服器": ["AI伺服器", "AI伺服器需求", "AI伺服器供應鏈", "AI Server", "伺服器"],
    "AI伺服器散熱": ["散熱", "散熱方案", "液冷", "氣冷", "水冷", "散熱模組", "散熱器"],
    "先進製程": ["先進製程", "先進封裝", "CoWoS", "InFO", "SoIC", "HBM", "先進封裝技術"],
    "ASIC_GPU": ["ASIC", "GPU", "AI晶片", "H100", "GB200", "AI accelerator"],
    "電源": ["電源", "電源供應", "Power", "BBU", "資料中心電源"],
    "PCB_CCL": ["PCB", "CCL", "銅箔", "印刷電路板", "層壓板"],
    "記憶體": ["記憶體", "HBM", "DRAM", "NAND", "記憶體供應", "記憶體價格"],
    "網通": ["網通", "Switch", "路由器", "乙太網路", "光通訊", "網路設備"],
    "半導體": ["半導體", "晶片", "積體電路", "IC"],
    "伺服器代工": ["伺服器代工", "ODM", "OEM", "伺服器製造"],
}

_FIELD_PATTERNS: dict[str, list[str]] = {
    "products": ["產品", "product", "solution", "伺服器", "電源", "BBU", "散熱", "PCB", "CCL", "CoWoS", "HBM"],
    "customers": ["客戶", "customer", "CSP", "雲端", "NVIDIA", "AMD", "Microsoft", "Amazon", "Google", "Meta"],
    "revenue_exposure": ["營收", "revenue", "占比", "比重", "年增", "月增", "YoY", "財報", "法說"],
    "benefit_logic": ["受惠", "需求", "訂單", "拉貨", "擴產", "供應鏈", "滲透率", "規格升級"],
}

_COUNTER_PATTERNS = ["風險", "砍單", "庫存", "下修", "毛利率", "衰退", "延後", "競爭", "價格壓力"]


def build_topic_evidence_candidates(
    webfetch_sources: list[dict],
    existing_topic_profiles: list[dict] | None = None,
    company_universe: list[dict] | None = None,
    max_items: int = 80,
) -> dict[str, Any]:
    """Build rule-based evidence candidates from WebFetch sources.

    Does NOT call any AI. Pure rule-based extraction.

    Args:
        webfetch_sources: List of WebFetch source dicts (each must have title/url/snippet).
        existing_topic_profiles: Optional existing topic profiles for keyword enrichment.
        company_universe: Optional list of company dicts with name/code for mention detection.
        max_items: Maximum number of evidence candidates to return.

    Returns:
        dict with keys:
            - mode: "rule_based"
            - items: list of evidence candidate dicts
            - warnings: list of warning strings
    """
    items: list[dict[str, Any]] = []
    warnings: list[str] = []

    if not webfetch_sources:
        return {"mode": "rule_based", "items": [], "warnings": ["No webfetch sources provided"]}

    # Build keyword set from built-in table + existing topics
    keyword_set: set[str] = set()
    for keywords in _THEME_KEYWORDS.values():
        for kw in keywords:
            keyword_set.add(kw)

    if existing_topic_profiles:
        for profile in existing_topic_profiles:
            for kw in profile.get("keywords", []):
                keyword_set.add(kw)
            theme_name = profile.get("theme_name", "")
            if theme_name:
                keyword_set.add(theme_name)

    # Build company name set
    company_names: set[str] = set()
    company_codes: set[str] = set()
    if company_universe:
        for c in company_universe:
            name = c.get("name", "")
            code = c.get("code", "")
            if name:
                company_names.add(name)
            if code:
                company_codes.add(code)

    for idx, source in enumerate(webfetch_sources):
        try:
            title = _get_str(source, "title", "")
            url = _get_str(source, "url", "")
            snippet = _get_str(source, "snippet", "")
            # Also handle content/text/body
            content = (
                _get_str(source, "content", "")
                or _get_str(source, "text", "")
                or _get_str(source, "body", "")
            )

            if not title and not snippet:
                continue

            # Determine source_level
            raw_level = _get_str(source, "source_level", "L2_media")
            source_level = _normalize_level(raw_level)

            # Extract matched keywords
            text_for_kw = f"{title} {snippet} {content}"
            matched_keywords: list[str] = []
            for kw in sorted(keyword_set, key=len, reverse=True):
                if kw in text_for_kw:
                    matched_keywords.append(kw)

            # Extract mentioned companies
            mentioned_companies: list[str] = []
            for name in company_names:
                if name and name in text_for_kw:
                    mentioned_companies.append(name)
            for code in company_codes:
                if code and code in text_for_kw:
                    mentioned_companies.append(code)

            # Possible topics
            possible_topics: list[str] = list(dict.fromkeys(matched_keywords[:5]))
            field_candidates = _field_candidates(text_for_kw, source_level, idx)
            counter_evidence_candidates = _counter_candidates(text_for_kw, source_level, idx)
            candidate_status = _candidate_status(source_level, field_candidates or matched_keywords)

            # Snippet truncation
            final_snippet = snippet[:300] if snippet else ""

            item = {
                "title": title[:200] if title else "",
                "url": url,
                "source": _get_str(source, "provider", "") or _get_str(source, "source", ""),
                "published_date": _get_str(source, "published_date", ""),
                "source_level": source_level,
                "snippet": final_snippet,
                "matched_keywords": matched_keywords[:10],
                "mentioned_companies": list(dict.fromkeys(mentioned_companies))[:20],
                "possible_topics": possible_topics,
                "candidate_status": candidate_status,
                "field_candidates": field_candidates,
                "counter_evidence_candidates": counter_evidence_candidates,
                "raw_index": idx,
            }
            items.append(item)
        except Exception as exc:
            warnings.append(f"Source {idx} processing error: {exc}")
            continue

    # Truncate to max_items
    if len(items) > max_items:
        items = items[:max_items]

    return {
        "mode": "rule_based",
        "items": items,
        "warnings": warnings if warnings else [],
    }


def _get_str(d: dict[str, Any], key: str, default: str = "") -> str:
    val = d.get(key)
    if val is None:
        return default
    return str(val).strip()


def _normalize_level(raw: str) -> str:
    raw_lower = raw.strip().lower()
    if raw_lower in ("l1_official", "l1", "official"):
        return "L1_official"
    if raw_lower in ("l3_community", "l3", "community"):
        return "L3_community"
    return "L2_media"


def _candidate_status(source_level: str, evidence_markers: Any) -> str:
    if "L1" in source_level and evidence_markers:
        return "verified"
    if "L2" in source_level and evidence_markers:
        return "inferred"
    if evidence_markers:
        return "candidate"
    return "missing"


def _field_candidates(text: str, source_level: str, source_index: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for field, patterns in _FIELD_PATTERNS.items():
        hits = [pattern for pattern in patterns if pattern and pattern.lower() in text.lower()]
        if not hits:
            continue
        candidates.append({
            "field": field,
            "matched_terms": hits[:8],
            "status": _candidate_status(source_level, hits),
            "source_level": source_level,
            "source_index": source_index,
            "confidence": "high" if "L1" in source_level else "medium" if "L2" in source_level else "low",
        })
    return candidates


def _counter_candidates(text: str, source_level: str, source_index: int) -> list[dict[str, Any]]:
    hits = [pattern for pattern in _COUNTER_PATTERNS if pattern and pattern.lower() in text.lower()]
    if not hits:
        return []
    return [{
        "field": "counter_evidence",
        "matched_terms": hits[:8],
        "status": _candidate_status(source_level, hits),
        "source_level": source_level,
        "source_index": source_index,
        "confidence": "high" if "L1" in source_level else "medium" if "L2" in source_level else "low",
    }]
