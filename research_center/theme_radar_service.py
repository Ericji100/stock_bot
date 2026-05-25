from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from stock_scanner import load_price_metrics, load_stock_universe

from .config import ROOT_DIR
from .market_movers_service import build_market_movers, rows_from_market_movers
from .news_repository import NewsRepository
from .recent_scans import load_recent_scan_results
from .topic_legacy_references import build_legacy_theme_references
from .topic_quality import infer_status, normalize_status

ProgressCallback = Callable[[str], None]

STRICT_PRODUCT_THEME_IDS = {"memory_recovery", "hbm_memory_supply_chain"}

STRICT_PRODUCT_KEYWORDS = {
    "memory_recovery": {
        "dram", "nand", "nor", "flash", "hbm", "ddr", "lpddr", "ssd", "emmc", "ufs",
        "記憶體", "記憶體模組", "記憶體控制", "記憶體控制晶片", "nand控制", "nand 控制",
        "儲存控制", "儲存控制晶片",
    },
    "hbm_memory_supply_chain": {
        "hbm", "hbm3e", "hbm4", "dram", "記憶體", "ai 記憶體", "ai記憶體",
        "先進封裝", "封裝測試", "記憶體測試", "記憶體材料",
    },
}


def collect_theme_radar_data(
    report_date: date | None = None,
    *,
    lookback_days: int = 7,
    source: str = "radar",
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Build full-market theme radar structured data from local caches.

    This layer intentionally stays local and deterministic. AI receives this
    structured pack for interpretation, lifecycle judgment, and inference.
    """
    target_date = report_date or datetime.now().date()
    lookback_days = _clean_days(lookback_days)
    _emit(progress, f"題材雷達：載入股票宇宙與價量資料，date={target_date.isoformat()} days={lookback_days}")
    universe = load_stock_universe(False)
    by_code = {str(item.code): item for item in universe}
    price_metrics = _safe_price_metrics(universe)
    market_movers = build_market_movers(
        target_date,
        universe=universe,
        price_metrics=price_metrics,
        progress=progress,
    )
    strong_rows = rows_from_market_movers(market_movers, "active_movers")
    strong_policy = {
        "source": source or "market",
        "status": "market_movers",
        "input_record_count": market_movers.get("mover_universe_count"),
        "candidate_count": len(strong_rows),
        "report_date": target_date.isoformat(),
        "note": "題材雷達強勢股來源為全市場 market_movers，不套用 /scan 硬篩；Radar/scan 只作輔助參考。",
    }
    if not strong_rows:
        strong_rows = _fallback_active_rows(universe, price_metrics, limit=80)
        strong_policy = {
            **strong_policy,
            "status": "fallback_active_universe",
            "note": "market_movers 沒有可用價量異動，暫用高流動性股票建立題材雷達底稿。",
        }

    _emit(progress, f"題材雷達：強勢股票底稿 {len(strong_rows)} 檔，建立題材映射")
    topic_library = _load_topic_library()
    mapped_rows = _attach_theme_matches(strong_rows, topic_library)
    news_stats = _build_news_theme_stats(topic_library, lookback_days)
    sector_strength = build_sector_strength_data(
        report_date,
        lookback_days=lookback_days,
        source=source,
        universe=universe,
        price_metrics=price_metrics,
        strong_rows=mapped_rows,
        market_movers=market_movers,
    )
    theme_rankings = _build_theme_rankings(mapped_rows, topic_library, news_stats)
    flow_summaries = [
        build_theme_flow_data(
            theme.get("theme_name") or theme.get("theme_id"),
            report_date,
            lookback_days=lookback_days,
            preloaded={
                "topic_library": topic_library,
                "stock_rows": mapped_rows,
                "news_stats": news_stats,
                "market_movers": market_movers,
            },
            market_movers=market_movers,
        )
        for theme in theme_rankings[:3]
    ]

    return {
        "command_role": "market_theme_radar",
        "report_date": target_date.isoformat(),
        "market_data_date": market_movers.get("market_data_date") or target_date.isoformat(),
        "report_generated_at": datetime.now().isoformat(timespec="seconds"),
        "lookback_days": lookback_days,
        "source": source,
        "market_movers": market_movers,
        "strong_stock_policy": strong_policy,
        "strong_stocks": mapped_rows[:120],
        "theme_rankings": theme_rankings,
        "theme_flow_summaries": flow_summaries,
        "sector_strength": sector_strength,
        "news_theme_stats": news_stats,
        "topic_library_summary": _topic_library_summary(topic_library),
        "data_quality": _data_quality(topic_library, mapped_rows, news_stats),
        "analysis_policy": _analysis_policy(),
    }


def build_theme_flow_data(
    theme_query: str | None,
    report_date: date | None = None,
    *,
    lookback_days: int = 7,
    preloaded: dict[str, Any] | None = None,
    market_movers: dict[str, Any] | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    target_date = report_date or datetime.now().date()
    lookback_days = _clean_days(lookback_days)
    topic_library = (preloaded or {}).get("topic_library") or _load_topic_library()
    query = (theme_query or "").strip()
    theme = _find_theme(query, topic_library)
    if progress:
        _emit(progress, f"題材流向：分析 {query or '未指定題材'}")

    stock_rows = (preloaded or {}).get("stock_rows")
    if stock_rows is None:
        universe = load_stock_universe(False)
        price_metrics = _safe_price_metrics(universe[:200])
        market_movers = market_movers or build_market_movers(target_date, universe=universe, price_metrics=price_metrics, progress=progress)
        stock_rows = _attach_theme_matches(
            rows_from_market_movers(market_movers, "active_movers")
            or _fallback_active_rows(universe[:200], price_metrics, 160),
            topic_library,
        )
    else:
        market_movers = market_movers or (preloaded or {}).get("market_movers")

    theme_id = theme.get("theme_id") if theme else ""
    theme_name = theme.get("theme_name") if theme else query
    related = [
        row for row in stock_rows
        if any(m.get("theme_id") == theme_id for m in row.get("theme_matches", []))
        or (query and query in json.dumps(row.get("theme_matches", []), ensure_ascii=False))
    ]
    if preloaded is not None and "news_stats" in preloaded:
        news_stats = preloaded.get("news_stats") or []
    else:
        news_stats = _build_news_theme_stats(topic_library, lookback_days)
    nodes = _nodes_for_theme(theme_id, topic_library)
    layers = _build_layers(theme, nodes, related)
    layer_market_validation = _layer_market_validation(layers)
    return {
        "command_role": "theme_flow",
        "report_date": target_date.isoformat(),
        "market_data_date": (market_movers or {}).get("market_data_date") or target_date.isoformat(),
        "report_generated_at": datetime.now().isoformat(timespec="seconds"),
        "lookback_days": lookback_days,
        "theme_query": query,
        "theme": theme or {"theme_id": "", "theme_name": query, "status": "not_found"},
        "related_stock_count": len(related),
        "related_stocks": related[:80],
        "layers": layers,
        "layer_market_validation": layer_market_validation,
        "next_layer_candidates": _next_layer_candidates(layers),
        "news_stats": _news_for_theme(theme_id, theme_name, news_stats),
        "data_quality": {
            "theme_found": bool(theme),
            "has_supply_chain_nodes": bool(nodes),
            "related_stock_count": len(related),
        },
        "analysis_policy": _analysis_policy(),
    }


def build_sector_strength_data(
    report_date: date | None = None,
    *,
    lookback_days: int = 7,
    source: str = "radar",
    universe: list[Any] | None = None,
    price_metrics: dict[str, Any] | None = None,
    strong_rows: list[dict[str, Any]] | None = None,
    market_movers: dict[str, Any] | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    target_date = report_date or datetime.now().date()
    lookback_days = _clean_days(lookback_days)
    if universe is None:
        _emit(progress, "類股強弱：載入股票宇宙")
        universe = load_stock_universe(False)
    if price_metrics is None:
        price_metrics = _safe_price_metrics(universe)
    if strong_rows is None:
        market_movers = market_movers or build_market_movers(
            target_date,
            universe=universe,
            price_metrics=price_metrics,
            progress=progress,
        )
        strong_rows = rows_from_market_movers(market_movers, "active_movers") or _fallback_active_rows(universe, price_metrics, 120)
    elif market_movers is None:
        market_movers = {"status": "provided_strong_rows", "active_movers": strong_rows, "data_quality": {}}

    by_sector: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in strong_rows:
        sector = str(row.get("industry") or "未分類")
        by_sector[sector].append(row)

    rankings = []
    for sector, rows in by_sector.items():
        avg_volume = _avg(_num(row.get("avg_volume_20d")) for row in rows)
        avg_change = _avg(_num(row.get("change_pct")) for row in rows if row.get("change_pct") is not None)
        breadth = len(rows)
        theme_hits = sum(1 for row in rows if row.get("theme_matches"))
        volume_surge_count = sum(1 for row in rows if _num(row.get("volume_ratio")) >= 1.5)
        new_high_count = sum(1 for row in rows if _num(row.get("new_high_days")) > 0)
        limit_up_count = sum(1 for row in rows if row.get("limit_up"))
        stock_groups = _split_sector_stock_groups(rows)
        score = min(
            100.0,
            breadth * 5
            + max(0.0, avg_change or 0.0) * 4
            + volume_surge_count * 4
            + new_high_count * 5
            + limit_up_count * 6
            + min(20, avg_volume / 500)
            + theme_hits * 2,
        )
        rankings.append({
            "sector": sector,
            "sector_score": round(score, 2),
            "strong_stock_count": breadth,
            "avg_change_pct": round(avg_change, 2) if avg_change is not None else None,
            "volume_surge_count": volume_surge_count,
            "new_high_count": new_high_count,
            "limit_up_count": limit_up_count,
            "avg_volume_20d": round(avg_volume, 2),
            "theme_hit_count": theme_hits,
            "sector_strong_samples": stock_groups["sector_strong_samples"],
            "representative_stocks": stock_groups["representative_stocks"],
            "candidate_stocks": stock_groups["candidate_stocks"],
            "display_stock_groups": stock_groups["display_stock_groups"],
            "theme_relation_status_counts": stock_groups["theme_relation_status_counts"],
            "representative_policy": (
                "sector_strong_samples are price/volume strong stocks in the sector; "
                "representative_stocks only include verified or inferred topic relations; "
                "candidate_stocks must not be called representatives."
            ),
            "interpretation_hint": "全市場 market_movers 類股強弱統計；強勢樣本不等於題材代表股，也不是買賣建議。",
        })
    rankings.sort(key=lambda row: (row["sector_score"], row["strong_stock_count"]), reverse=True)
    return {
        "command_role": "sector_strength",
        "report_date": target_date.isoformat(),
        "market_data_date": (market_movers or {}).get("market_data_date") or target_date.isoformat(),
        "report_generated_at": datetime.now().isoformat(timespec="seconds"),
        "lookback_days": lookback_days,
        "source": source,
        "market_movers": {
            "market_data_date": (market_movers or {}).get("market_data_date"),
            "report_generated_at": (market_movers or {}).get("report_generated_at") or (market_movers or {}).get("generated_at"),
            "source_mode": (market_movers or {}).get("source_mode"),
            "hard_filter_policy": (market_movers or {}).get("hard_filter_policy"),
            "top_gainers": (market_movers or {}).get("top_gainers", [])[:20],
            "top_losers": (market_movers or {}).get("top_losers", [])[:20],
            "top_volume_surge": (market_movers or {}).get("top_volume_surge", [])[:20],
            "top_turnover": (market_movers or {}).get("top_turnover", [])[:20],
            "new_highs": (market_movers or {}).get("new_highs", [])[:20],
            "new_lows": (market_movers or {}).get("new_lows", [])[:20],
            "sector_mover_rankings": (market_movers or {}).get("sector_mover_rankings", [])[:20],
            "data_quality": (market_movers or {}).get("data_quality", {}),
        },
        "sector_rankings": rankings[:20],
        "data_quality": {
            "input_stock_count": len(strong_rows),
            "sector_count": len(rankings),
            "market_movers_data_quality": (market_movers or {}).get("data_quality", {}),
        },
        "analysis_policy": _analysis_policy(),
    }


def _split_sector_stock_groups(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sector_samples = sorted(rows, key=lambda row: (_num(row.get("avg_volume_20d")), _num(row.get("price"))), reverse=True)[:8]
    verified: list[dict[str, Any]] = []
    inferred: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    missing = 0
    status_counts: Counter[str] = Counter()

    for row in rows:
        statuses = [
            normalize_status(match.get("verification_status"), default="candidate") or "candidate"
            for match in row.get("theme_matches") or []
            if isinstance(match, dict)
        ]
        if not statuses:
            missing += 1
            continue
        unique_statuses = set(statuses)
        status_counts.update(unique_statuses)
        if "verified" in unique_statuses:
            verified.append(row)
        elif "inferred" in unique_statuses:
            inferred.append(row)
        elif "candidate" in unique_statuses:
            candidates.append(row)

    verified.sort(key=lambda row: (_num(row.get("avg_volume_20d")), _num(row.get("price"))), reverse=True)
    inferred.sort(key=lambda row: (_num(row.get("avg_volume_20d")), _num(row.get("price"))), reverse=True)
    candidates.sort(key=lambda row: (_num(row.get("avg_volume_20d")), _num(row.get("price"))), reverse=True)
    status_counts["missing"] = missing
    representative = (verified + inferred)[:8]
    return {
        "sector_strong_samples": sector_samples,
        "representative_stocks": representative,
        "candidate_stocks": candidates[:8],
        "display_stock_groups": {
            "sector_strong_samples": sector_samples,
            "sector_sample_label": "類股強勢樣本，只代表價格/量能轉強，不等於題材代表股",
            "verified_representatives": verified[:8],
            "inferred_representatives": inferred[:8],
            "candidate_watchlist": candidates[:8],
            "candidate_label": "待驗證候選股，不得稱為代表股或核心受惠股",
            "missing_topic_relation_label": "無題材命中，只能稱為類股強勢樣本",
            "required_terms": {
                "sector_sample": "類股強勢樣本",
                "verified": "已驗證代表股",
                "inferred": "推論型代表股",
                "candidate": "待驗證候選股",
                "missing": "題材關聯未命中",
            },
        },
        "theme_relation_status_counts": dict(status_counts),
    }


def _load_topic_library() -> dict[str, Any]:
    profiles = _load_json(ROOT_DIR / "config" / "theme_profiles.json", [])
    company_map = _load_json(ROOT_DIR / "config" / "company_theme_map.json", {})
    nodes = _load_json(ROOT_DIR / "config" / "supply_chain_nodes.json", [])
    formal_profiles = profiles if isinstance(profiles, list) else []
    formal_nodes = nodes if isinstance(nodes, list) else []
    legacy = build_legacy_theme_references(formal_profiles, formal_nodes)
    profile_by_id = {str(p.get("theme_id")): p for p in profiles if p.get("theme_id")}
    return {
        "profiles": formal_profiles,
        "profile_by_id": profile_by_id,
        "company_theme_map": company_map if isinstance(company_map, dict) else {},
        "supply_chain_nodes": formal_nodes,
        "legacy_theme_references": legacy,
    }


def _attach_theme_matches(rows: list[dict[str, Any]], topic_library: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = topic_library["profiles"]
    profile_by_id = topic_library["profile_by_id"]
    company_map = topic_library["company_theme_map"]
    nodes = topic_library["supply_chain_nodes"]
    for row in rows:
        code = str(row.get("code") or "")
        direct = company_map.get(code, [])
        direct_theme_ids: list[str] = []
        if isinstance(direct, dict):
            direct_theme_ids = [str(t) for t in direct.get("themes", []) if t]
            if direct.get("primary_theme") and direct.get("primary_theme") not in direct_theme_ids:
                direct_theme_ids.insert(0, str(direct.get("primary_theme")))
        elif isinstance(direct, list):
            direct_theme_ids = [str(t.get("theme_id") if isinstance(t, dict) else t) for t in direct if t]
        elif isinstance(direct, str):
            direct_theme_ids = [direct]

        matches: list[dict[str, Any]] = []
        for theme_id in direct_theme_ids:
            profile = profile_by_id.get(theme_id, {})
            relation = direct if isinstance(direct, dict) else {}
            matches.append(_theme_match(theme_id, profile, "direct_map", code, nodes, relation_score=90, relation=relation))

        if not matches:
            text = f"{row.get('name', '')} {row.get('industry', '')}".lower()
            for profile in profiles:
                theme_id = str(profile.get("theme_id") or "")
                keywords = [str(k).lower() for k in profile.get("keywords", []) or [] if k]
                industries = [str(i) for i in profile.get("industries", []) or [] if i]
                keyword_hit = any(k and k in text for k in keywords)
                industry_hit = any(i and i in str(row.get("industry") or "") for i in industries)
                if theme_id in STRICT_PRODUCT_THEME_IDS and not _has_strict_product_evidence(theme_id, row, None):
                    continue
                if keyword_hit or industry_hit:
                    matches.append(_theme_match(theme_id, profile, "keyword_or_industry", code, nodes, relation_score=45))
                if len(matches) >= 3:
                    break

        row["theme_matches"] = matches[:5]
        row["primary_theme_id"] = matches[0]["theme_id"] if matches else ""
        row["primary_theme_name"] = matches[0]["theme_name"] if matches else ""
    return rows


def _theme_match(
    theme_id: str,
    profile: dict[str, Any],
    method: str,
    code: str,
    nodes: list[dict[str, Any]],
    relation_score: int,
    relation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    node = next((n for n in nodes if str(n.get("company_code") or "") == code and str(n.get("node_id") or "").startswith(theme_id)), None)
    relation = relation or {}
    evidence = (relation.get("evidence") or (node or {}).get("evidence") or [])[:3]
    verification_status = "candidate" if method != "direct_map" else infer_status(relation, evidence)
    return {
        "theme_id": theme_id,
        "theme_name": profile.get("theme_name") or theme_id,
        "match_method": method,
        "relation_score": relation_score,
        "confidence": relation.get("relation_strength") or profile.get("confidence") or ("high" if method == "direct_map" else "watch_only"),
        "source_level": relation.get("source_level") or profile.get("source_level") or "topic_library",
        "relation_type": relation.get("relation_type") or ("direct" if method == "direct_map" else "candidate"),
        "verification_status": verification_status,
        "supply_chain_role": (node or {}).get("role") or profile.get("supply_chain_role") or "",
        "layer": (node or {}).get("layer"),
        "product_keywords": _merge_text_list((node or {}).get("product_keywords") or [], relation.get("products") or []),
        "customers": _merge_text_list((node or {}).get("customers") or [], relation.get("customers") or []),
        "revenue_exposure": relation.get("revenue_exposure") or (node or {}).get("revenue_exposure") or {},
        "benefit_logic": relation.get("benefit_logic") or (node or {}).get("benefit_logic") or "",
        "evidence": evidence,
        "counter_evidence": (relation.get("counter_evidence") or [])[:3],
        "risk_notes": (profile.get("risk_notes") or [])[:3],
        "missing_data": _merge_text_list(profile.get("missing_data") or [], relation.get("missing_data") or [])[:5],
    }


def _has_strict_product_evidence(theme_id: str, row: dict[str, Any], relation: dict[str, Any] | None) -> bool:
    """Return True only when a strict product theme has product-level evidence.

    Broad TWSE industries such as "半導體業" are intentionally ignored for memory
    themes because they include many non-memory IC designers.
    """
    keywords = STRICT_PRODUCT_KEYWORDS.get(theme_id, set())
    if not keywords:
        return True
    relation = relation or {}
    text_parts: list[str] = [
        str(row.get("name") or ""),
        str(relation.get("role") or ""),
        str(relation.get("benefit_logic") or ""),
    ]
    for key in ("products", "product_keywords", "customers"):
        value = relation.get(key)
        if isinstance(value, list):
            text_parts.extend(str(item) for item in value)
        elif value:
            text_parts.append(str(value))
    for evidence in relation.get("evidence") or []:
        if isinstance(evidence, dict):
            text_parts.extend(str(evidence.get(k) or "") for k in ("content", "source", "title"))
    text = " ".join(text_parts).lower()
    return any(keyword and keyword.lower() in text for keyword in keywords)


def _merge_text_list(*values: Any) -> list[Any]:
    merged = []
    seen = set()
    for value in values:
        items = value if isinstance(value, list) else [value] if value else []
        for item in items:
            marker = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if marker not in seen:
                merged.append(item)
                seen.add(marker)
    return merged


def _build_theme_rankings(rows: list[dict[str, Any]], topic_library: dict[str, Any], news_stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_theme: dict[str, list[dict[str, Any]]] = defaultdict(list)
    theme_name: dict[str, str] = {}
    for row in rows:
        for match in row.get("theme_matches", []):
            tid = match.get("theme_id")
            if not tid:
                continue
            by_theme[tid].append(row)
            theme_name[tid] = match.get("theme_name") or tid
    news_by_id = {item.get("theme_id"): item for item in news_stats}
    rankings = []
    for tid, theme_rows in by_theme.items():
        breadth = len({r.get("code") for r in theme_rows})
        weighted_breadth = sum(_theme_row_weight(row, tid) for row in theme_rows)
        avg_volume = _avg(_num(row.get("avg_volume_20d")) for row in theme_rows)
        direct_count = sum(1 for row in theme_rows for m in row.get("theme_matches", []) if m.get("theme_id") == tid and m.get("match_method") == "direct_map")
        inferred_count = sum(1 for row in theme_rows if _match_status_for_theme(row, tid) == "inferred")
        candidate_count = sum(1 for row in theme_rows if _match_status_for_theme(row, tid) == "candidate")
        news = news_by_id.get(tid, {})
        news_score = min(15.0, _num(news.get("news_count_7d")) * 1.5 + _num(news.get("news_count_24h")) * 2)
        price_score = min(25.0, weighted_breadth * 3 + min(10.0, avg_volume / 1000))
        breadth_score = min(20.0, weighted_breadth * 4 + direct_count)
        volume_score = min(15.0, avg_volume / 700)
        chip_score = 0.0
        diffusion_score = min(10.0, len(_roles_for_theme(theme_rows, tid)) * 2.5 + (3 if news.get("trend_direction") == "rising" else 0))
        total = round(price_score + breadth_score + volume_score + chip_score + news_score + diffusion_score, 2)
        lifecycle = _lifecycle(total, breadth, news, diffusion_score)
        representative_stocks, candidate_stocks = _split_representative_stocks(theme_rows, tid)
        rankings.append({
            "theme_id": tid,
            "theme_name": theme_name.get(tid, tid),
            "theme_strength_score": total,
            "lifecycle": lifecycle,
            "score_breakdown": {
                "price_strength": round(price_score, 2),
                "breadth": round(breadth_score, 2),
                "volume_strength": round(volume_score, 2),
                "chip_strength": round(chip_score, 2),
                "news_heat": round(news_score, 2),
                "diffusion_potential": round(diffusion_score, 2),
            },
            "strong_stock_count": breadth,
            "weighted_strong_stock_count": round(weighted_breadth, 2),
            "direct_relation_count": direct_count,
            "inferred_relation_count": inferred_count,
            "candidate_relation_count": candidate_count,
            "strong_nodes": _roles_for_theme(theme_rows, tid)[:8],
            "representative_stocks": representative_stocks,
            "candidate_stocks": candidate_stocks,
            "display_stock_groups": _display_stock_groups(representative_stocks, candidate_stocks, tid),
            "representative_policy": "representative_stocks excludes candidate-only keyword/industry matches; candidate_stocks are watch-list only.",
            "news_stats": news,
            "main_risks": _theme_risks(tid, topic_library),
        })
    rankings.sort(key=lambda item: (item["theme_strength_score"], item["strong_stock_count"]), reverse=True)
    return rankings[:20]


def _match_for_theme(row: dict[str, Any], theme_id: str) -> dict[str, Any]:
    for match in row.get("theme_matches", []):
        if match.get("theme_id") == theme_id:
            return match
    return {}


def _match_status_for_theme(row: dict[str, Any], theme_id: str) -> str:
    match = _match_for_theme(row, theme_id)
    return normalize_status(match.get("verification_status"), default="candidate") or "candidate"


def _theme_row_weight(row: dict[str, Any], theme_id: str) -> float:
    match = _match_for_theme(row, theme_id)
    status = normalize_status(match.get("verification_status"), default="candidate") or "candidate"
    method = match.get("match_method")
    if method == "direct_map" and status == "verified":
        return 1.0
    if method == "direct_map" or status == "inferred":
        return 0.75
    return 0.2


def _split_representative_stocks(rows: list[dict[str, Any]], theme_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    representative: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for row in rows:
        status = _match_status_for_theme(row, theme_id)
        if status == "candidate":
            candidates.append(row)
        else:
            representative.append(row)
    representative.sort(key=lambda row: (_theme_row_weight(row, theme_id), _num(row.get("avg_volume_20d"))), reverse=True)
    candidates.sort(key=lambda row: (_num(row.get("avg_volume_20d")), _num(row.get("price"))), reverse=True)
    return representative[:10], candidates[:10]


def _display_stock_groups(representative: list[dict[str, Any]], candidates: list[dict[str, Any]], theme_id: str) -> dict[str, Any]:
    verified = [row for row in representative if _match_status_for_theme(row, theme_id) == "verified"]
    inferred = [row for row in representative if _match_status_for_theme(row, theme_id) == "inferred"]
    return {
        "verified_representatives": verified,
        "inferred_representatives": inferred,
        "candidate_watchlist": candidates,
        "candidate_label": "待驗證候選股，不得稱為代表股或核心受惠股",
        "required_terms": {
            "verified": "已驗證代表股",
            "inferred": "推論型代表股",
            "candidate": "待驗證候選股",
        },
    }


def _build_news_theme_stats(topic_library: dict[str, Any], lookback_days: int) -> list[dict[str, Any]]:
    try:
        repo = NewsRepository()
        items_7d = repo.query_all_recent(hours=lookback_days * 24)
        items_24h = repo.query_all_recent(hours=24)
    except Exception as exc:
        return [{"status": "unavailable", "error": str(exc)}]
    result = []
    for profile in topic_library["profiles"]:
        tid = str(profile.get("theme_id") or "")
        name = str(profile.get("theme_name") or tid)
        keywords = [name, tid, *[str(k) for k in profile.get("keywords", []) or []]]
        matches_7d = [_news_match(item, keywords) for item in items_7d]
        matches_24h = [_news_match(item, keywords) for item in items_24h]
        matches_7d = [m for m in matches_7d if m]
        matches_24h = [m for m in matches_24h if m]
        if not matches_7d and not matches_24h:
            continue
        keyword_counter: Counter[str] = Counter()
        stock_counter: Counter[str] = Counter()
        for row in matches_7d:
            keyword_counter.update(row.get("keywords", []))
            stock_counter.update(row.get("symbols", []))
        result.append({
            "theme_id": tid,
            "theme_name": name,
            "news_count_24h": len(matches_24h),
            "news_count_7d": len(matches_7d),
            "top_keywords": [k for k, _ in keyword_counter.most_common(10)],
            "mentioned_stocks": [s for s, _ in stock_counter.most_common(10)],
            "source_quality_score": min(100, 40 + len(matches_7d) * 4),
            "trend_direction": "rising" if len(matches_24h) >= max(1, len(matches_7d) / max(lookback_days, 1)) else "flat",
            "evidence_level": "L2_or_news_db",
        })
    result.sort(key=lambda row: (row["news_count_24h"], row["news_count_7d"]), reverse=True)
    return result[:30]


def _news_match(item: Any, keywords: list[str]) -> dict[str, Any] | None:
    text = f"{item.title} {item.summary} {item.full_text} {' '.join(item.related_topics or [])}".lower()
    hits = [kw for kw in keywords if kw and kw.lower() in text]
    if not hits:
        return None
    return {"keywords": hits, "symbols": list(item.related_symbols or [])}


def _strong_stock_codes(source: str, target_date: date, by_code: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    records = load_recent_scan_results(limit=30)
    codes: list[str] = []
    for record in records:
        if source not in {"all", "recent"}:
            scan_type = str(record.get("scan_type") or "")
            if source == "radar" and "Radar" not in scan_type and "技術" not in scan_type and "精選" not in scan_type:
                pass
        for code in record.get("codes") or record.get("selected_codes") or []:
            code_s = str(code).strip()
            if code_s in by_code and code_s not in codes:
                codes.append(code_s)
        if len(codes) >= 120:
            break
    return codes, {"source": source, "status": "recent_scan_cache", "input_record_count": len(records), "candidate_count": len(codes), "report_date": target_date.isoformat()}


def _stock_rows(codes: list[str], by_code: dict[str, Any], price_metrics: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for code in codes:
        entry = by_code.get(code)
        if not entry:
            continue
        rows.append(_stock_row(entry, price_metrics))
    return rows


def _fallback_active_rows(universe: list[Any], price_metrics: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    rows = [_stock_row(entry, price_metrics) for entry in universe]
    rows.sort(key=lambda row: (_num(row.get("avg_volume_20d")), _num(row.get("price"))), reverse=True)
    return rows[:limit]


def _stock_row(entry: Any, price_metrics: dict[str, Any]) -> dict[str, Any]:
    metric = price_metrics.get(getattr(entry, "symbol", "")) or price_metrics.get(getattr(entry, "code", "")) or {}
    return {
        "code": str(getattr(entry, "code", "")),
        "name": str(getattr(entry, "name", "")),
        "symbol": str(getattr(entry, "symbol", "")),
        "industry": str(getattr(entry, "industry", "")),
        "price": metric.get("price") or metric.get("close"),
        "previous_close": metric.get("previous_close") or metric.get("prev_close"),
        "change_pct": metric.get("change_pct") or metric.get("pct_change") or metric.get("return_1d_pct"),
        "volume": metric.get("volume"),
        "avg_volume_20d": metric.get("avg_volume_20d"),
        "volume_ratio": metric.get("volume_ratio") or metric.get("rvol"),
        "turnover": metric.get("turnover") or metric.get("amount"),
        "new_high_days": metric.get("new_high_days"),
        "new_low_days": metric.get("new_low_days"),
        "limit_up": bool(metric.get("limit_up") or False),
        "limit_down": bool(metric.get("limit_down") or False),
    }


def _safe_price_metrics(universe: list[Any]) -> dict[str, Any]:
    try:
        return load_price_metrics(universe)
    except Exception:
        return {}


def _build_layers(theme: dict[str, Any] | None, nodes: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    roles = Counter()
    role_stocks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    role_layer: dict[str, int] = {}
    for node in nodes:
        role = str(node.get("role") or "待補供應鏈節點")
        if node.get("layer") is not None:
            try:
                role_layer[role] = int(node.get("layer"))
            except Exception:
                pass
        roles[role] += 1
    for row in rows:
        for match in row.get("theme_matches", []):
            role = match.get("supply_chain_role") or "待確認關聯"
            if match.get("layer") is not None:
                try:
                    role_layer[role] = int(match.get("layer"))
                except Exception:
                    pass
            role_stocks[role].append(row)
            roles[role] += 1
    ordered = [role for role, _ in sorted(roles.items(), key=lambda item: (role_layer.get(item[0], 99), -item[1]))]
    if not ordered and theme:
        ordered = [str(theme.get("supply_chain_role") or "待補供應鏈節點")]
    layers = []
    for index in range(4):
        role = ordered[index] if index < len(ordered) else _default_layer_name(index + 1)
        reps, candidate_stocks = _split_layer_stocks(role_stocks.get(role, []), role)
        visible_rows = reps or candidate_stocks
        layers.append({
            "layer": index + 1,
            "name": _default_layer_name(index + 1),
            "nodes": [role],
            "current_strength": _layer_strength(reps, candidate_stocks),
            "stage": _layer_stage(index + 1, reps, candidate_stocks),
            "representative_stocks": reps,
            "candidate_stocks": candidate_stocks,
            "display_stock_groups": _display_layer_stock_groups(reps, candidate_stocks, role),
            "candidate_label": "待驗證候選股，不得稱為代表股" if candidate_stocks else "",
            "inference": "資料不足，需補供應鏈節點與公司證據。" if not visible_rows else "依本地強勢股與題材映射推估此層已有市場關注。",
            "verification_needed": ["公告/法說會", "月營收", "公司產品與客戶證據", "法人或可信產業新聞"],
        })
    return layers


def _split_layer_stocks(rows: list[dict[str, Any]], role: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    representative: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for row in rows:
        status = _layer_relation_status(row, role)
        if status == "candidate":
            candidates.append(row)
        else:
            representative.append(row)
    representative.sort(key=lambda row: (_num(row.get("avg_volume_20d")), _num(row.get("price"))), reverse=True)
    candidates.sort(key=lambda row: (_num(row.get("avg_volume_20d")), _num(row.get("price"))), reverse=True)
    return representative[:8], candidates[:8]


def _layer_relation_status(row: dict[str, Any], role: str) -> str:
    statuses = []
    for match in row.get("theme_matches", []):
        if (match.get("supply_chain_role") or "未分類角色") == role:
            statuses.append(normalize_status(match.get("verification_status"), default="candidate") or "candidate")
    if not statuses:
        return "candidate"
    if "verified" in statuses:
        return "verified"
    if "inferred" in statuses:
        return "inferred"
    return "candidate"


def _display_layer_stock_groups(representative: list[dict[str, Any]], candidates: list[dict[str, Any]], role: str) -> dict[str, Any]:
    verified = [row for row in representative if _layer_relation_status(row, role) == "verified"]
    inferred = [row for row in representative if _layer_relation_status(row, role) == "inferred"]
    return {
        "verified_representatives": verified,
        "inferred_representatives": inferred,
        "candidate_watchlist": candidates,
        "candidate_label": "待驗證候選股，不得稱為代表股或核心受惠股",
        "required_terms": {
            "verified": "已驗證代表股",
            "inferred": "推論型代表股",
            "candidate": "待驗證候選股",
        },
    }


def _next_layer_candidates(layers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for layer in layers:
        if layer.get("current_strength") in {"弱", "資料不足"} or layer.get("stage") in {"剛起漲", "待驗證"}:
            candidates.append({
                "layer": layer.get("layer"),
                "nodes": layer.get("nodes"),
                "reason": "前層題材若延續，這一層可能被市場尋找補漲或延伸受惠。",
                "status": "推論，尚待驗證",
            })
    return candidates[:3]


def _find_theme(query: str, topic_library: dict[str, Any]) -> dict[str, Any] | None:
    q = (query or "").lower()
    for profile in topic_library["profiles"]:
        text = " ".join([str(profile.get("theme_id") or ""), str(profile.get("theme_name") or ""), *[str(k) for k in profile.get("keywords", []) or []]]).lower()
        if q and q in text:
            return profile
    return None


def _nodes_for_theme(theme_id: str, topic_library: dict[str, Any]) -> list[dict[str, Any]]:
    if not theme_id:
        return []
    return [n for n in topic_library["supply_chain_nodes"] if str(n.get("node_id") or "").startswith(theme_id) or n.get("theme_id") == theme_id]


def _news_for_theme(theme_id: str, theme_name: str, news_stats: list[dict[str, Any]]) -> dict[str, Any]:
    for item in news_stats:
        if item.get("theme_id") == theme_id or item.get("theme_name") == theme_name:
            return item
    return {"theme_id": theme_id, "theme_name": theme_name, "news_count_24h": 0, "news_count_7d": 0}


def _roles_for_theme(rows: list[dict[str, Any]], theme_id: str) -> list[str]:
    counter: Counter[str] = Counter()
    for row in rows:
        for match in row.get("theme_matches", []):
            if match.get("theme_id") == theme_id:
                role = match.get("supply_chain_role") or "待確認關聯"
                counter[role] += 1
    return [role for role, _ in counter.most_common()]


def _theme_risks(theme_id: str, topic_library: dict[str, Any]) -> list[str]:
    profile = topic_library["profile_by_id"].get(theme_id, {})
    return (profile.get("risk_notes") or [])[:5]


def _lifecycle(score: float, breadth: int, news: dict[str, Any], diffusion_score: float) -> str:
    if score >= 75 and diffusion_score >= 6:
        return "擴散段"
    if score >= 70:
        return "主升段"
    if score >= 45 and breadth >= 2:
        return "初升段"
    if score >= 45 and news.get("news_count_7d", 0) > breadth:
        return "末升段"
    return "待觀察"


def _layer_strength(rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]] | None = None) -> str:
    candidate_rows = candidate_rows or []
    if not rows and candidate_rows:
        return "價格強但關聯待驗證"
    if len(rows) >= 5:
        return "強"
    if len(rows) >= 2:
        return "中"
    if len(rows) == 1:
        return "弱"
    return "資料不足"


def _layer_stage(layer: int, rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]] | None = None) -> str:
    candidate_rows = candidate_rows or []
    if not rows and candidate_rows:
        return "待驗證候選"
    if not rows:
        return "待驗證"
    if layer <= 2 and len(rows) >= 3:
        return "主升或高位階"
    if layer >= 3 and len(rows) >= 1:
        return "擴散或剛起漲"
    return "剛起漲"


def _layer_market_validation(layers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validation = []
    for layer in layers:
        rows = [
            *(layer.get("representative_stocks") or []),
            *(layer.get("candidate_stocks") or []),
        ]
        gainers = [row for row in rows if _num(row.get("change_pct")) > 0]
        volume_surge = [row for row in rows if _num(row.get("volume_ratio")) >= 1.5]
        new_highs = [row for row in rows if _num(row.get("new_high_days")) > 0]
        limit_up = [row for row in rows if row.get("limit_up")]
        validation.append({
            "layer": layer.get("layer"),
            "layer_name": layer.get("layer_name"),
            "market_validated": bool(gainers or volume_surge or new_highs or limit_up),
            "status": (
                "盤面已驗證"
                if gainers or volume_surge or new_highs or limit_up
                else "尚未從盤面驗證"
            ),
            "positive_mover_count": len(gainers),
            "volume_surge_count": len(volume_surge),
            "new_high_count": len(new_highs),
            "limit_up_count": len(limit_up),
            "evidence_stocks": [
                {
                    "code": row.get("code"),
                    "name": row.get("name"),
                    "change_pct": row.get("change_pct"),
                    "volume_ratio": row.get("volume_ratio"),
                    "new_high_days": row.get("new_high_days"),
                }
                for row in rows
                if _num(row.get("change_pct")) > 0 or _num(row.get("volume_ratio")) >= 1.5 or _num(row.get("new_high_days")) > 0 or row.get("limit_up")
            ][:8],
        })
    return validation


def _default_layer_name(layer: int) -> str:
    return {
        1: "核心受惠層",
        2: "主系統 / 主零組件層",
        3: "周邊零組件 / 材料 / 設備層",
        4: "延伸受惠 / 測試 / 服務層",
    }.get(layer, f"Layer {layer}")


def _topic_library_summary(topic_library: dict[str, Any]) -> dict[str, Any]:
    return {
        "theme_profile_count": len(topic_library["profiles"]),
        "company_theme_map_count": len(topic_library["company_theme_map"]),
        "supply_chain_node_count": len(topic_library["supply_chain_nodes"]),
        "usage": "題材庫為背景映射與初篩依據，最終結論仍需本地市場資料與來源證據驗證。",
    }


def _data_quality(topic_library: dict[str, Any], rows: list[dict[str, Any]], news_stats: list[dict[str, Any]]) -> dict[str, Any]:
    matched = sum(1 for row in rows if row.get("theme_matches"))
    evidence_empty = sum(1 for value in topic_library["company_theme_map"].values() if isinstance(value, dict) and not value.get("evidence"))
    evidence_ready = sum(1 for value in topic_library["company_theme_map"].values() if isinstance(value, dict) and value.get("evidence"))
    company_map_count = len(topic_library["company_theme_map"])
    matches = [match for row in rows for match in row.get("theme_matches", [])]
    status_counts = Counter(normalize_status(match.get("verification_status"), default="candidate") or "candidate" for match in matches)
    relation_rows = [value for value in topic_library["company_theme_map"].values() if isinstance(value, dict)]
    revenue_ready = sum(1 for value in relation_rows if isinstance(value.get("revenue_exposure"), dict) and value.get("revenue_exposure"))
    customer_ready = sum(1 for value in relation_rows if value.get("customers"))
    node_count = len(topic_library["supply_chain_nodes"])
    node_layer_ready = sum(1 for node in topic_library["supply_chain_nodes"] if isinstance(node, dict) and node.get("layer") is not None)
    return {
        "stock_rows": len(rows),
        "theme_mapped_stock_rows": matched,
        "theme_mapping_coverage_pct": round(matched / len(rows) * 100, 2) if rows else 0,
        "company_relation_evidence_coverage_pct": round(evidence_ready / company_map_count * 100, 2) if company_map_count else 0,
        "company_relations_without_evidence_count": evidence_empty,
        "relation_status_counts": dict(status_counts),
        "direct_relation_count": status_counts.get("verified", 0),
        "inferred_relation_count": status_counts.get("inferred", 0),
        "candidate_relation_count": status_counts.get("candidate", 0),
        "missing_relation_count": status_counts.get("missing", 0),
        "revenue_exposure_coverage_pct": round(revenue_ready / len(relation_rows) * 100, 2) if relation_rows else 0,
        "customer_coverage_pct": round(customer_ready / len(relation_rows) * 100, 2) if relation_rows else 0,
        "supply_chain_layer_coverage_pct": round(node_layer_ready / node_count * 100, 2) if node_count else 0,
        "financial_validation_coverage_pct": 0,
        "chip_validation_coverage_pct": 0,
        "news_theme_stat_count": len(news_stats),
        "data_quality_policy": "verified requires evidence; keyword/industry matches stay candidate; revenue/financial/chip coverage must be treated as insufficient when 0.",
        "known_gaps": [
            "部分 company_theme_map 缺少公司層級 evidence。",
            "供應鏈節點未必都有 layer 欄位，因此先以 role 聚合並交給 AI 標示待驗證。",
            "本地價量資料若缺少漲跌幅欄位，價格強度採保守 proxy。",
        ],
    }


def _analysis_policy() -> dict[str, Any]:
    return {
        "no_trading_advice": True,
        "facts_vs_inference_required": True,
        "community_is_sentiment_only": True,
        "topic_library_is_background_only": True,
        "inference_must_include_verification": True,
        "keyword_or_industry_match_is_candidate_only": True,
        "candidate_must_not_be_called_representative": True,
        "candidate_allowed_terms": ["待驗證候選股", "價格強勢候選", "疑似蹭題材"],
        "representative_allowed_terms": ["已驗證代表股", "推論型代表股"],
        "verified_requires_l1_or_explicit_evidence": True,
        "taiwan_stock_sources_preferred": ["TWSE", "TPEx", "MOPS", "company IR", "credible Taiwan financial media"],
    }


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default
    return default


def _clean_days(value: int) -> int:
    try:
        return max(1, min(60, int(value)))
    except Exception:
        return 7


def _num(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _avg(values: Any) -> float:
    nums = [float(v) for v in values if v is not None]
    return sum(nums) / len(nums) if nums else 0.0


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress:
        progress(message)
