from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from .config import ROOT_DIR

SECTOR_ALIAS_PATH = ROOT_DIR / "config" / "sector_alias_map.json"


@lru_cache(maxsize=1)
def load_sector_alias_map() -> dict[str, Any]:
    if not SECTOR_ALIAS_PATH.exists():
        return {"schema_version": "missing", "sectors": {}, "alias_redirects": {}}
    try:
        data = json.loads(SECTOR_ALIAS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": "invalid", "sectors": {}, "alias_redirects": {}}
    if not isinstance(data, dict):
        return {"schema_version": "invalid", "sectors": {}, "alias_redirects": {}}
    data.setdefault("sectors", {})
    data.setdefault("alias_redirects", {})
    return data


def sector_profile_for(industry: str | None) -> dict[str, Any]:
    data = load_sector_alias_map()
    sectors = data.get("sectors") or {}
    key = canonical_sector_name(industry)
    profile = sectors.get(key)
    if isinstance(profile, dict):
        return {"sector": key, **profile}
    return {
        "sector": key or str(industry or "未知"),
        "display_name": str(industry or "未知"),
        "aliases": [str(industry or "未知")],
        "subsectors": [],
        "rerating_bonus": 6,
        "rerating_label": f"{industry or '未知'} / 市場輪動候選",
    }


def canonical_sector_name(industry: str | None) -> str:
    text = str(industry or "").strip()
    if not text:
        return "未知"
    data = load_sector_alias_map()
    sectors = data.get("sectors") or {}
    if text in sectors:
        return text
    redirects = data.get("alias_redirects") or {}
    if text in redirects:
        return str(redirects[text])
    for sector, profile in sectors.items():
        aliases = [sector, *(profile.get("aliases") or [])]
        if any(alias and (alias == text or alias in text or text in alias) for alias in aliases):
            return str(sector)
    return text


def sector_display_name(industry: str | None) -> str:
    profile = sector_profile_for(industry)
    return str(profile.get("display_name") or profile.get("sector") or industry or "未知")


def annotate_rows_with_subsectors(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        profile = sector_profile_for(str(row.get("industry") or ""))
        row["sector"] = profile.get("sector") or row.get("industry")
        row["sector_display_name"] = profile.get("display_name") or row.get("industry")
        matches = subsector_matches_for_stock(row, profile=profile)
        row["subsector_matches"] = matches[:3]
        row["primary_subsector"] = matches[0]["subsector"] if matches else ""
    return rows


def subsector_matches_for_stock(row: dict[str, Any], profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    profile = profile or sector_profile_for(str(row.get("industry") or ""))
    code = str(row.get("code") or "").strip()
    text = _row_text(row)
    matches: list[dict[str, Any]] = []
    for sub in profile.get("subsectors") or []:
        if not isinstance(sub, dict):
            continue
        score = 0
        reasons: list[str] = []
        hints = {str(item) for item in (sub.get("stock_hints") or [])}
        if code and code in hints:
            score += 45
            reasons.append("stock_hint")
        aliases = [str(item) for item in (sub.get("aliases") or []) if item]
        alias_hits = _keyword_hits(aliases, text)
        if alias_hits:
            score += 22 + min(12, len(alias_hits) * 4)
            reasons.append("alias:" + ",".join(alias_hits[:4]))
        theme_terms = [str(item) for item in (sub.get("theme_keywords") or []) if item]
        theme_hits = _keyword_hits(theme_terms, text)
        if theme_hits:
            score += 8 + min(8, len(theme_hits) * 2)
            reasons.append("theme:" + ",".join(theme_hits[:4]))
        search_terms = [str(item) for item in (sub.get("search_terms") or []) if item]
        search_hits = _keyword_hits(search_terms, text)
        if search_hits:
            score += 8
            reasons.append("term:" + ",".join(search_hits[:4]))
        if score <= 0:
            continue
        matches.append({
            "sector": profile.get("sector"),
            "sector_display_name": profile.get("display_name") or profile.get("sector"),
            "subsector": sub.get("name"),
            "match_score": min(100, score),
            "match_reasons": reasons,
            "search_terms": search_terms[:6],
        })
    matches.sort(key=lambda item: item.get("match_score") or 0, reverse=True)
    return matches


def build_subsector_rankings(rows: list[dict[str, Any]], *, limit: int = 30) -> list[dict[str, Any]]:
    annotated = annotate_rows_with_subsectors(rows)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in annotated:
        subsector = str(row.get("primary_subsector") or "").strip()
        if not subsector:
            continue
        sector = str(row.get("sector") or row.get("industry") or "未知")
        grouped.setdefault((sector, subsector), []).append(row)

    rankings: list[dict[str, Any]] = []
    for (sector, subsector), items in grouped.items():
        avg_change = _avg(_num(row.get("change_pct")) for row in items if row.get("change_pct") is not None)
        avg_volume = _avg(_num(row.get("avg_volume_20d")) for row in items)
        volume_surge_count = sum(1 for row in items if _num(row.get("volume_ratio")) >= 1.5)
        new_high_count = sum(1 for row in items if _num(row.get("new_high_days")) > 0)
        trend_pullback_count = sum(1 for row in items if row.get("trend_state") == "trend_pullback")
        active_breakout_count = sum(1 for row in items if row.get("trend_state") == "active_breakout")
        avg_trend_score = _avg(_num(row.get("trend_score")) for row in items)
        limit_up_count = sum(1 for row in items if row.get("limit_up"))
        theme_hit_count = sum(1 for row in items if row.get("theme_matches"))
        score = min(
            100.0,
            len(items) * 10
            + max(0.0, avg_change or 0.0) * 5
            + volume_surge_count * 5
            + new_high_count * 6
            + trend_pullback_count * 3
            + min(12, (avg_trend_score or 0.0) / 8)
            + limit_up_count * 8
            + min(16, avg_volume / 800)
            + theme_hit_count * 2,
        )
        samples = sorted(items, key=lambda row: (_num(row.get("trend_score")), _num(row.get("change_pct")), _num(row.get("avg_volume_20d"))), reverse=True)[:8]
        rankings.append({
            "sector": sector,
            "sector_display_name": sector_display_name(sector),
            "subsector": subsector,
            "subsector_score": round(score, 2),
            "strong_stock_count": len(items),
            "avg_change_pct": round(avg_change, 2) if avg_change is not None else None,
            "volume_surge_count": volume_surge_count,
            "new_high_count": new_high_count,
            "active_breakout_count": active_breakout_count,
            "trend_pullback_count": trend_pullback_count,
            "avg_trend_score": round(avg_trend_score, 2) if avg_trend_score is not None else None,
            "subsector_state": _group_trend_state(items),
            "limit_up_count": limit_up_count,
            "avg_volume_20d": round(avg_volume, 2),
            "theme_hit_count": theme_hit_count,
            "strong_samples": samples,
            "interpretation_hint": "Subsector is derived from sector_alias_map plus stock/product/theme clues; it is a market grouping signal, not verified company-theme evidence.",
        })
    rankings.sort(key=lambda item: (item["subsector_score"], item["strong_stock_count"]), reverse=True)
    return rankings[:limit]


def top_subsectors_for_sector(subsector_rankings: list[dict[str, Any]], sector: str, *, limit: int = 5) -> list[dict[str, Any]]:
    canonical = canonical_sector_name(sector)
    return [
        row for row in subsector_rankings
        if canonical_sector_name(str(row.get("sector") or "")) == canonical
    ][:limit]


def topic_search_terms_for_stock(stock: dict[str, Any], *, max_terms: int = 8) -> list[str]:
    profile = sector_profile_for(str(stock.get("industry") or ""))
    row = dict(stock)
    row.setdefault("sector", profile.get("sector"))
    matches = subsector_matches_for_stock(row, profile=profile)
    terms: list[str] = []
    for match in matches[:2]:
        terms.append(str(match.get("subsector") or ""))
        terms.extend(str(item) for item in (match.get("search_terms") or [])[:4])
    if not terms:
        terms.extend(str(item) for item in (profile.get("aliases") or [])[:4])
        for sub in (profile.get("subsectors") or [])[:2]:
            terms.append(str(sub.get("name") or ""))
            terms.extend(str(item) for item in (sub.get("search_terms") or [])[:2])
    return _dedupe([term for term in terms if term])[:max_terms]


def build_topic_maintain_sector_queries(stocks: list[dict[str, Any]], *, limit: int = 12) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for stock in stocks[:60]:
        profile = sector_profile_for(str(stock.get("industry") or ""))
        matches = subsector_matches_for_stock(stock, profile=profile)
        if matches:
            selected = matches[0]
            key = (str(selected.get("sector") or profile.get("sector")), str(selected.get("subsector") or ""))
            terms = [str(selected.get("subsector") or ""), *[str(t) for t in (selected.get("search_terms") or [])]]
        else:
            key = (str(profile.get("sector") or stock.get("industry") or "未知"), "")
            terms = [str(profile.get("display_name") or key[0]), *[str(t) for t in (profile.get("aliases") or [])[:3]]]
        item = buckets.setdefault(key, {"sector": key[0], "subsector": key[1], "terms": [], "stocks": []})
        item["terms"].extend(terms)
        if stock.get("code") or stock.get("name"):
            item["stocks"].append(" ".join(str(part) for part in (stock.get("code"), stock.get("name")) if part))

    queries: list[dict[str, Any]] = []
    ranked = sorted(buckets.values(), key=lambda item: len(item.get("stocks") or []), reverse=True)
    for item in ranked[:limit]:
        terms = _dedupe(item.get("terms") or [])[:8]
        stocks_text = " ".join(_dedupe(item.get("stocks") or [])[:5])
        label = " ".join([str(item.get("sector") or ""), str(item.get("subsector") or ""), *terms]).strip()
        if not label:
            continue
        queries.append({"type": "sector_subsector_discovery", "query": f"台股 {label} 代表股 供應鏈 受惠 公司"})
        queries.append({"type": "sector_subsector_evidence", "query": f"{label} {stocks_text} 產品 客戶 營收占比 法說會"})
    return queries[:limit]


def rerating_label_for_industry(industry: str | None) -> tuple[float, str]:
    profile = sector_profile_for(industry)
    try:
        bonus = float(profile.get("rerating_bonus", 6) or 6)
    except (TypeError, ValueError):
        bonus = 6.0
    label = str(profile.get("rerating_label") or f"{industry or '未知'} / 市場輪動候選")
    return bonus, label


def _row_text(row: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("code", "name", "industry", "sector", "sector_display_name", "primary_theme_name", "primary_subsector"):
        value = row.get(key)
        if value:
            parts.append(str(value))
    for match in row.get("theme_matches") or []:
        if not isinstance(match, dict):
            continue
        for key in ("theme_name", "supply_chain_role", "benefit_logic"):
            if match.get(key):
                parts.append(str(match.get(key)))
        for key in ("product_keywords", "customers"):
            value = match.get(key)
            if isinstance(value, list):
                parts.extend(str(item) for item in value)
            elif value:
                parts.append(str(value))
    return _normalize_text(" ".join(parts))


def _keyword_hits(keywords: list[str], text: str) -> list[str]:
    hits: list[str] = []
    for keyword in keywords:
        key = _normalize_text(keyword)
        if not key:
            continue
        if key in text:
            hits.append(keyword)
    return _dedupe(hits)


def _normalize_text(value: str) -> str:
    text = str(value or "").lower()
    return re.sub(r"\s+", "", text)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _group_trend_state(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "unknown"
    counts: dict[str, int] = {}
    for row in rows:
        state = str(row.get("trend_state") or "neutral")
        counts[state] = counts.get(state, 0) + 1
    if counts.get("active_breakout", 0) >= max(2, counts.get("weak", 0) + counts.get("trend_pullback", 0)):
        return "active_breakout"
    if counts.get("trend_pullback", 0) >= 1 and counts.get("active_breakout", 0) + counts.get("improving", 0) + counts.get("trend_pullback", 0) >= counts.get("weak", 0):
        return "trend_pullback"
    if counts.get("weak", 0) > counts.get("active_breakout", 0) + counts.get("improving", 0) + counts.get("trend_pullback", 0):
        return "weak"
    if counts.get("cooling", 0) or counts.get("trend_pullback", 0):
        return "cooling"
    return "neutral"


def _avg(values: Any) -> float:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else 0.0
