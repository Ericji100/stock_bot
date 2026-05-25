from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .knowledge_base import (
    COMPANY_KNOWLEDGE_PATH,
    enrich_company_rows,
    load_company_knowledge,
    save_company_knowledge,
    theme_knowledge_summary,
)
from .models import CommandRequest

ProgressCallback = Callable[[str], None]

MIN_KNOWLEDGE_SOURCE_SCORE = 60
LOW_QUALITY_DOMAINS = {
    "ptt.cc",
    "dcard.tw",
    "mobile01.com",
    "facebook.com",
    "instagram.com",
    "threads.com",
    "threads.net",
    "x.com",
    "twitter.com",
    "reddit.com",
    "youtube.com",
    "youtu.be",
}

PRODUCT_KEYWORDS = (
    "AI",
    "CoWoS",
    "HBM",
    "ASIC",
    "GPU",
    "伺服器",
    "電源",
    "散熱",
    "PCB",
    "CCL",
    "矽晶圓",
    "封裝",
    "測試",
    "車用",
    "光通訊",
    "網通",
    "半導體",
    "記憶體",
    "機器人",
)


def source_quality_score(source: Any) -> dict[str, Any]:
    item = _source_dict(source)
    url = str(item.get("url") or item.get("source_url") or "").strip()
    title = str(item.get("title") or "").strip()
    level = str(item.get("source_level") or item.get("level") or "").strip()
    provider = str(item.get("provider") or item.get("source") or item.get("event_type") or "").strip()
    domain = _domain(url)

    score = 0
    reasons: list[str] = []
    if level in {"Level 1", "L1"}:
        score += 90
        reasons.append("official_level")
    elif level in {"Level 2", "L2"}:
        score += 75
        reasons.append("trusted_level")
    elif level in {"Level 3", "L3"}:
        score += 50
        reasons.append("media_level")
    elif level in {"Level 4", "L4"}:
        score += 20
        reasons.append("community_level")
    else:
        score += 35
        reasons.append("unknown_level")

    if not url:
        score -= 35
        reasons.append("missing_url")
    if not title:
        score -= 20
        reasons.append("missing_title")
    if _is_low_quality_domain(domain) or any(token in provider.lower() for token in ("forum", "ptt", "dcard", "mobile01")):
        score -= 55
        reasons.append("low_quality_domain_or_forum")
    if any(token in domain for token in ("twse.com.tw", "tpex.org.tw", "mops.twse.com.tw")):
        score = max(score, 90)
        reasons.append("official_domain")

    score = max(0, min(100, score))
    return {
        "score": score,
        "level": _quality_label(score),
        "usable_for_company_knowledge": score >= MIN_KNOWLEDGE_SOURCE_SCORE,
        "reasons": reasons,
        "domain": domain,
    }


def clean_source_events(events: list[Any] | None) -> list[dict[str, Any]]:
    cleaned = []
    for source in events or []:
        item = _source_dict(source)
        quality = source_quality_score(item)
        cleaned.append({
            **item,
            "source_quality_score": quality["score"],
            "source_quality_level": quality["level"],
            "source_quality_reasons": quality["reasons"],
            "usable_for_company_knowledge": quality["usable_for_company_knowledge"],
        })
    return cleaned


def attach_company_knowledge_autofill(
    request: CommandRequest,
    structured_data: dict[str, Any],
    progress: ProgressCallback | None = None,
    *,
    knowledge_path: Path | None = None,
) -> dict[str, Any]:
    if request.command not in {"research", "value_scan", "theme"}:
        return structured_data

    rows = _rows_for_request(request, structured_data)
    if not rows:
        structured_data["company_knowledge_update_status"] = {"status": "skipped", "reason": "no_company_rows"}
        return structured_data

    update_status = update_missing_company_knowledge(rows, knowledge_path=knowledge_path, progress=progress)
    structured_data["company_knowledge_update_status"] = update_status
    _refresh_structured_rows(request, structured_data, knowledge_path=knowledge_path)
    return structured_data


def update_missing_company_knowledge(
    rows: list[dict[str, Any]],
    *,
    knowledge_path: Path | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    path = knowledge_path or COMPANY_KNOWLEDGE_PATH
    knowledge = load_company_knowledge(path)
    companies = knowledge.setdefault("companies", {})
    if not isinstance(companies, dict):
        companies = {}
        knowledge["companies"] = companies

    updated_codes: list[str] = []
    skipped: list[dict[str, Any]] = []
    rejected_low_quality = 0

    for row in rows:
        code = str(row.get("code") or (row.get("stock") or {}).get("code") or "").strip()
        if not code:
            continue
        existing = companies.get(code)
        if isinstance(existing, dict) and _has_core_knowledge(existing):
            skipped.append({"code": code, "reason": "existing_core_knowledge"})
            continue

        clean_events = clean_source_events(_row_sources(row))
        usable_sources = [item for item in clean_events if item.get("usable_for_company_knowledge")]
        rejected_low_quality += len(clean_events) - len(usable_sources)
        draft = _build_company_knowledge_entry(row, usable_sources)
        if not draft:
            skipped.append({"code": code, "reason": "insufficient_high_quality_evidence"})
            continue
        companies[code] = _merge_company_entry(existing if isinstance(existing, dict) else {}, draft)
        updated_codes.append(code)

    if updated_codes:
        metadata = knowledge.setdefault("metadata", {})
        metadata["auto_company_knowledge_updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        metadata["auto_company_knowledge_policy"] = "Only high-quality public sources are allowed; existing core fields are not overwritten."
        save_company_knowledge(knowledge, path)
        if progress:
            progress(f"公司知識庫自動補全：新增/補齊 {len(updated_codes)} 檔")

    return {
        "status": "updated" if updated_codes else "no_update",
        "updated_count": len(updated_codes),
        "updated_codes": updated_codes,
        "skipped": skipped[:30],
        "low_quality_rejected_count": rejected_low_quality,
        "policy": "existing core fields are preserved; low-quality/community sources are rejected",
    }


def _rows_for_request(request: CommandRequest, data: dict[str, Any]) -> list[dict[str, Any]]:
    if request.command == "research":
        stock = data.get("stock") or {}
        if not stock:
            return []
        return [{
            "code": stock.get("code"),
            "name": stock.get("name"),
            "industry": stock.get("industry") or stock.get("market"),
            "source_events": data.get("source_events") or [],
            "mops_documents": data.get("mops_documents"),
            "free_public_sources": data.get("free_public_sources"),
            "news_context": data.get("news_context"),
            "saved_news_context": data.get("saved_news_context"),
            "company_knowledge": data.get("company_knowledge"),
        }]
    if request.command == "value_scan":
        return list(data.get("ai_candidates") or data.get("candidates") or [])
    if request.command == "theme":
        return list(data.get("matched_companies") or data.get("matched_universe") or [])
    return []


def _refresh_structured_rows(request: CommandRequest, data: dict[str, Any], *, knowledge_path: Path | None = None) -> None:
    knowledge = load_company_knowledge(knowledge_path)
    if request.command == "research":
        stock = data.get("stock") or {}
        code = str(stock.get("code") or "")
        enriched = enrich_company_rows([{"code": code}], knowledge) if code else []
        if enriched:
            data["company_knowledge"] = enriched[0].get("company_knowledge")
        return

    if request.command == "theme":
        rows = data.get("matched_companies") or data.get("matched_universe") or []
        enriched = enrich_company_rows(rows, knowledge)
        data["matched_universe"] = enriched
        data["matched_companies"] = enriched
        data["company_knowledge_summary"] = theme_knowledge_summary(enriched)
        return

    if request.command == "value_scan":
        for key in ("ai_candidates", "candidates"):
            rows = data.get(key)
            if isinstance(rows, list):
                data[key] = enrich_company_rows(rows, knowledge)
        pack = data.get("ai_candidate_evidence_pack")
        if isinstance(pack, list):
            data["ai_candidate_evidence_pack"] = [
                _refresh_pack_missing_status(item)
                for item in enrich_company_rows(pack, knowledge)
            ]


def _build_company_knowledge_entry(row: dict[str, Any], usable_sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not usable_sources:
        return None
    name = str(row.get("name") or (row.get("stock") or {}).get("name") or "").strip()
    industry = str(row.get("industry") or "").strip()
    text = _row_text(row, usable_sources)
    product_lines = _extract_product_lines(text, industry)
    supply_roles = _extract_supply_chain_roles(text, industry)
    if not product_lines and not supply_roles:
        return None
    missing = []
    if not product_lines:
        missing.append("product_lines")
    if not supply_roles:
        missing.append("supply_chain_roles")
    missing.extend(["customers", "revenue_exposure"])
    best_score = max(int(src.get("source_quality_score") or 0) for src in usable_sources)
    return {
        "company_name": name,
        "product_lines": product_lines,
        "customers": [],
        "revenue_exposure": [],
        "supply_chain_roles": supply_roles,
        "evidence_sources": [_compact_source(src) for src in usable_sources[:6]],
        "missing_data": sorted(set(missing)),
        "confidence": "auto_high" if best_score >= 85 else "auto_medium",
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "auto_update": {
            "method": "deterministic_source_quality_rules",
            "source_count": len(usable_sources),
            "best_source_quality_score": best_score,
        },
    }


def _merge_company_entry(existing: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing or {})
    for key in ("company_name", "updated_at", "confidence", "auto_update"):
        if not merged.get(key) and draft.get(key):
            merged[key] = draft[key]
    for key in ("product_lines", "customers", "revenue_exposure", "supply_chain_roles", "evidence_sources", "missing_data"):
        merged[key] = _merge_list(merged.get(key), draft.get(key))
    if draft.get("updated_at"):
        merged["updated_at"] = draft["updated_at"]
    if draft.get("auto_update"):
        merged["auto_update"] = draft["auto_update"]
    if not merged.get("confidence"):
        merged["confidence"] = draft.get("confidence", "auto_medium")
    return merged


def _refresh_pack_missing_status(item: dict[str, Any]) -> dict[str, Any]:
    missing = list(item.get("missing_data_status") or [])
    knowledge = item.get("company_knowledge") or {}
    if knowledge.get("status") == "covered":
        missing = [field for field in missing if field != "company_knowledge"]
    return {**item, "missing_data_status": missing if missing else None}


def _row_sources(row: dict[str, Any]) -> list[Any]:
    sources: list[Any] = []
    for key in ("source_events", "mops_documents", "free_public_sources"):
        value = row.get(key)
        sources.extend(_flatten_sources(value))
    for key in ("news_context", "saved_news_context"):
        context = row.get(key) or {}
        if isinstance(context, dict):
            sources.extend(context.get("items") or [])
    return sources


def _flatten_sources(value: Any) -> list[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        items: list[Any] = []
        if any(k in value for k in ("title", "url", "source_url", "source_level")):
            items.append(value)
        for item in value.values():
            if isinstance(item, (dict, list)):
                items.extend(_flatten_sources(item))
        return items
    return []


def _source_dict(source: Any) -> dict[str, Any]:
    if isinstance(source, dict):
        item = dict(source)
    else:
        item = {key: getattr(source, key) for key in ("title", "url", "source_level", "published_date", "snippet", "provider") if hasattr(source, key)}
    if "url" not in item and "source_url" in item:
        item["url"] = item.get("source_url")
    if "source_url" not in item and "url" in item:
        item["source_url"] = item.get("url")
    return item


def _row_text(row: dict[str, Any], sources: list[dict[str, Any]]) -> str:
    chunks = [
        str(row.get("name") or ""),
        str(row.get("industry") or ""),
        str(row.get("old_market_label") or ""),
        str(row.get("new_market_label") or ""),
        " ".join(str(x) for x in (row.get("rerating_evidence") or [])),
    ]
    for source in sources:
        chunks.append(str(source.get("title") or ""))
        chunks.append(str(source.get("snippet") or ""))
        payload = source.get("payload")
        if isinstance(payload, dict):
            chunks.append(str(payload.get("kind") or ""))
            chunks.append(str(payload.get("status") or ""))
    return " ".join(chunks)


def _extract_product_lines(text: str, industry: str) -> list[str]:
    found = []
    normalized = text.lower()
    for keyword in PRODUCT_KEYWORDS:
        if keyword.lower() in normalized or keyword in text:
            found.append(keyword)
    if industry and len(industry) <= 20:
        found.append(industry)
    return _unique(found)[:8]


def _extract_supply_chain_roles(text: str, industry: str) -> list[str]:
    roles = []
    if any(token in text for token in ("供應鏈", "上游", "材料", "矽晶圓", "CCL")):
        roles.append("supply_chain_upstream_or_material")
    if any(token in text for token in ("製造", "封裝", "測試", "組裝")):
        roles.append("manufacturing_packaging_testing")
    if any(token in text for token in ("伺服器", "電源", "散熱", "網通", "PCB")):
        roles.append("ai_server_component_or_system")
    if industry and not roles:
        roles.append(f"industry:{industry}")
    return _unique(roles)[:8]


def _compact_source(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": source.get("title"),
        "url": source.get("url") or source.get("source_url"),
        "source_level": source.get("source_level"),
        "published_date": source.get("published_date"),
        "source_quality_score": source.get("source_quality_score"),
        "source_quality_level": source.get("source_quality_level"),
    }


def _has_core_knowledge(entry: dict[str, Any]) -> bool:
    return bool(entry.get("product_lines") or entry.get("customers") or entry.get("revenue_exposure") or entry.get("supply_chain_roles"))


def _merge_list(a: Any, b: Any) -> list[Any]:
    values = []
    for item in list(a or []) + list(b or []):
        if item in (None, ""):
            continue
        if item not in values:
            values.append(item)
    return values


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def _is_low_quality_domain(domain: str) -> bool:
    return any(domain == item or domain.endswith("." + item) for item in LOW_QUALITY_DOMAINS)


def _quality_label(score: int) -> str:
    if score >= 85:
        return "high"
    if score >= 60:
        return "medium"
    if score >= 35:
        return "low"
    return "rejected"


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = re.sub(r"\s+", " ", str(value)).strip()
        if text and text not in result:
            result.append(text)
    return result
