from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import yfinance as yf

from data_fetcher import StockDataFetcher, StockNotFoundError
from market_summary import MarketSummaryError, build_morning_market_report, build_noon_market_report
from portfolio_manager import list_portfolio, resolve_stock_reference
from stock_scanner import load_price_metrics, load_recent_revenue_history, load_stock_universe
from chip_strategies import get_tw_today
from curated_scan_service import CURATED_SCAN_TYPE, build_curated_scan_result, find_cached_curated_scan

from .chip_sources import build_chip_backup_events, build_chip_backup_snapshot
from .company_knowledge_update_service import attach_company_knowledge_autofill
from .date_aware_context import attach_date_aware_context
from .data_gap_service import attach_data_gap_summary
from .data_inventory_service import attach_data_inventory
from .evidence_pack_service import attach_unified_evidence_pack
from .forum_service import fetch_forum_sources
from .free_sources import build_free_macro_sources, build_free_research_sources
from .knowledge_base import enrich_company_rows, theme_knowledge_summary
from .macro_indicators import build_macro_indicators
from .mops_sources import build_mops_reference_events, financial_detail_snapshot
from .price_fallbacks import load_price_metrics_with_fallback
from .recent_scans import find_recent_scan, load_recent_scan_results, save_recent_scan_result
from .value_validation import build_value_cross_validation
from .rerating_snapshot_service import build_rerating_snapshot_for_stock
from .models import CommandRequest
from .news_context_service import attach_news_context
from .news_event_service import attach_news_events
from .sector_alias_service import (
    build_topic_maintain_sector_queries,
    rerating_label_for_industry,
    topic_search_terms_for_stock,
)
from .source_rank import make_source_items
from .stock_feature_pack_service import attach_feature_pack
from .structured_cache import load_latest_research_structured_cache, load_research_structured_cache, save_research_structured_cache
from .topic_context import build_candidates_topic_context, build_stock_topic_context, build_theme_topic_context
from .theme_report_context import load_recent_theme_report_context
from .topic_legacy_references import build_legacy_theme_references
from .topic_source_cache import load_topic_source_caches
from .theme_radar_service import (
    build_sector_strength_data,
    build_theme_flow_data,
    collect_theme_radar_data,
)

ROOT_DIR = Path(__file__).resolve().parents[1]


def collect_structured_data(request: CommandRequest, progress: Callable[[str], None] | None = None) -> tuple[dict[str, Any], list]:
    if request.command == "research":
        data = collect_research_data(request, progress=progress)
    elif request.command == "macro":
        data = collect_macro_data(request, progress=progress)
    elif request.command == "theme":
        data = collect_theme_data(request, progress=progress)
    elif request.command == "theme_radar":
        data = collect_theme_radar_data(
            request.report_date,
            lookback_days=request.lookback_days or 7,
            source=request.source or "market",
            progress=progress,
        )
    elif request.command == "theme_flow":
        data = build_theme_flow_data(
            request.theme_scope or request.target,
            request.report_date,
            lookback_days=request.lookback_days or 7,
            progress=progress,
        )
    elif request.command == "sector_strength":
        data = build_sector_strength_data(
            request.report_date,
            lookback_days=request.lookback_days or 7,
            source=request.source or "market",
            progress=progress,
        )
    elif request.command == "value_scan":
        data = collect_value_scan_data(request, progress=progress)
    elif request.command == "topic_maintain":
        data = collect_topic_maintain_data(request, progress=progress)
    else:
        return {"message": "report lookup does not collect structured market data"}, []

    attach_date_aware_context(request, data, progress=progress)
    attach_news_context(request, data, progress=progress)
    attach_news_events(request, data)
    attach_company_knowledge_autofill(request, data, progress=progress)
    attach_feature_pack(request, data)
    attach_data_gap_summary(request, data)
    attach_unified_evidence_pack(request, data)
    attach_data_inventory(request, data)

    sources = _official_sources()
    if request.command in {"theme_radar", "theme_flow", "sector_strength"}:
        data["forum_data"] = {
            "enabled": False,
            "reason": "theme radar commands use local market data and local news database; forum search is skipped by default.",
        }
        return data, sources
    forum_query = _forum_query_for_request(request, data)
    if progress:
        progress(f"論壇來源搜尋開始：{forum_query}")
    forum_result = fetch_forum_sources(forum_query, request.report_date, request.mode == "deep", progress=progress)
    if progress:
        progress(f"論壇來源搜尋完成：成功 {len(forum_result.sources)} 筆，失敗 {forum_result.failure_count} 筆")
        for note in forum_result.notes:
            progress(f"論壇來源訊息：{note}")
    data["forum_data"] = {
        "enabled": True,
        "query": forum_query,
        "source_count": len(forum_result.sources),
        "notes": forum_result.notes,
    }
    sources.extend(forum_result.sources)
    return data, sources


def collect_topic_maintain_data(request: CommandRequest, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Collect structured data for /topic_maintain prompt injection.

    Gathers:
    - Stock universe summary
    - Industry distribution
    - Recent scan results
    - Existing formal topic library (theme_profiles.json)
    - Company-topic map
    - Supply chain nodes
    - Company knowledge summary (from knowledge_base)
    - Recent AI candidate evidence if available
    """
    focus_theme = (request.target or request.theme_scope or "").strip()
    if progress:
        progress("題材知識庫維護：載入股票宇宙")
    universe = load_stock_universe(False)

    # Industry summary from universe
    industry_count: dict[str, int] = {}
    for entry in universe:
        ind = str(entry.industry or "未知")
        industry_count[ind] = industry_count.get(ind, 0) + 1

    if progress:
        progress("題材知識庫維護：讀取既有題材知識庫")
    # Load existing formal library (reuses theme_profiles.json path)
    existing_profiles: list[dict[str, Any]] = []
    profiles_path = ROOT_DIR / "config" / "theme_profiles.json"
    if profiles_path.exists():
        try:
            raw = json.loads(profiles_path.read_text(encoding="utf-8"))
            existing_profiles = list(raw) if isinstance(raw, list) else []
        except Exception:
            pass

    if progress:
        progress("題材知識庫維護：讀取公司-題材對應")
    company_topic_map: dict[str, Any] = {}
    ctm_path = ROOT_DIR / "config" / "company_theme_map.json"
    if ctm_path.exists():
        try:
            company_topic_map = json.loads(ctm_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    if progress:
        progress("題材知識庫維護：讀取供應鏈節點")
    supply_chain_nodes: list[dict[str, Any]] = []
    sc_path = ROOT_DIR / "config" / "supply_chain_nodes.json"
    if sc_path.exists():
        try:
            supply_chain_nodes = json.loads(sc_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    if progress:
        progress("題材知識庫維護：讀取近期 /theme 題材研究紀錄")
    recent_theme_reports = load_recent_theme_report_context(focus_theme, limit=5)

    if progress:
        progress("題材知識庫維護：讀取外部產業來源快取")
    external_topic_source_caches = load_topic_source_caches()

    # Recent scan detail - collect top 30 candidates per scan from cached data
    if progress:
        progress("題材知識庫維護：讀取近期掃描明細")
    recent_scan_candidates: list[dict[str, Any]] = []
    recent_scans: list[dict[str, Any]] = []
    try:
        scan_records = load_recent_scan_results(limit=5)  # Returns list
        if scan_records:
            for scan in scan_records[:5]:
                codes = scan.get("codes", [])[:30]  # top 30 codes per scan
                preview = []
                code_to_name = {str(e.code): str(e.name) for e in universe}
                code_to_industry = {str(e.code): str(e.industry or "") for e in universe}
                for code in codes:
                    preview.append({
                        "code": code,
                        "name": code_to_name.get(code, ""),
                        "scan_id": scan.get("scan_id"),
                        "scan_date": scan.get("scan_date"),
                        "scan_type": scan.get("scan_type"),
                        "industry": code_to_industry.get(code, ""),
                        "score": None,
                        "matched_strategies": [],
                        "theme_keywords": [],
                        "reason": None,
                    })
                recent_scan_candidates.append({
                    "scan_id": scan.get("scan_id"),
                    "scan_date": scan.get("scan_date"),
                    "scan_type": scan.get("scan_type"),
                    "candidate_count": len(codes),
                    "candidates": preview,
                })
                recent_scans.append({
                    "scan_id": scan.get("scan_id"),
                    "scan_date": scan.get("scan_date"),
                    "candidate_count": len(scan.get("candidates", scan.get("codes", []))),
                })
    except Exception:
        pass

    # Build legacy-style references from the formal topic library.
    legacy_themes = build_legacy_theme_references(existing_profiles, supply_chain_nodes)

    # Candidate companies for topic linking (top 50 by volume)
    if progress:
        progress("題材知識庫維護：整理候選公司清單")
    price_metrics = load_price_metrics(universe)
    candidates = []
    for entry in universe:
        code = str(entry.code)
        if code in price_metrics:
            pm = price_metrics[code]
            vol = float(pm.get("avg_volume_20d") or 0)
            candidates.append({
                "code": code,
                "name": str(entry.name),
                "industry": str(entry.industry or ""),
                "avg_volume_20d": vol,
            })
    candidates.sort(key=lambda x: x["avg_volume_20d"], reverse=True)
    candidate_companies = candidates[:50]

    # Build market signals from local caches only (no external API calls)
    if progress:
        progress("題材知識庫維護：整理市場訊號")
    market_signals: dict[str, Any] = {
        "high_volume_companies": [],
        "industry_distribution": dict(sorted(industry_count.items(), key=lambda x: x[1], reverse=True)[:20]),
        "recent_scan_top_industries": [],
        "curated_scan_summary": [],
        "revenue_growth_candidates": [],
        "chip_hot_candidates": [],
        "rerating_candidates": [],
        "backfill_health": {"status": "unknown", "missing_reason": "backfill_health marker not found"},
    }
    # High volume companies from universe
    try:
        market_signals["high_volume_companies"] = [
            {"code": c["code"], "name": c["name"], "industry": c["industry"], "avg_volume_20d": c["avg_volume_20d"]}
            for c in candidates[:30]
        ]
    except Exception:
        pass
    # Recent scan top industries (aggregate industries from recent scan candidates)
    try:
        scan_industry_map: dict[str, int] = {}
        for scan_detail in recent_scan_candidates:
            for cand in scan_detail.get("candidates", []):
                ind = cand.get("industry") or "未知"
                scan_industry_map[ind] = scan_industry_map.get(ind, 0) + 1
        market_signals["recent_scan_top_industries"] = [
            {"industry": k, "count": v}
            for k, v in sorted(scan_industry_map.items(), key=lambda x: x[1], reverse=True)[:10]
        ]
    except Exception:
        pass
    # Try loading curated scan summary from structured cache
    try:
        curated_path = ROOT_DIR / ".cache" / "curated_scan_summary.json"
        if curated_path.exists():
            market_signals["curated_scan_summary"] = json.loads(curated_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    # Try loading revenue growth candidates
    try:
        rev_path = ROOT_DIR / ".cache" / "revenue_growth_candidates.json"
        if rev_path.exists():
            market_signals["revenue_growth_candidates"] = json.loads(rev_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    # Try loading chip hot candidates
    try:
        chip_path = ROOT_DIR / ".cache" / "chip_hot_candidates.json"
        if chip_path.exists():
            market_signals["chip_hot_candidates"] = json.loads(chip_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    # Try loading rerating candidates
    try:
        rerating_path = ROOT_DIR / ".cache" / "rerating_candidates.json"
        if rerating_path.exists():
            market_signals["rerating_candidates"] = json.loads(rerating_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    # Check backfill health
    try:
        backfill_marker = ROOT_DIR / ".cache" / "backfill_healthy.marker"
        if backfill_marker.exists():
            market_signals["backfill_health"] = {"status": "healthy"}
        else:
            market_signals["backfill_health"] = {"status": "unknown", "missing_reason": "backfill_healthy.marker not found"}
    except Exception:
        pass

    if progress:
        progress("題材知識庫維護：結構化資料收集完成")

    maintenance_mode = _topic_maintain_mode(request, focus_theme, existing_profiles)
    topic_gaps = _topic_library_gap_analysis(existing_profiles, company_topic_map, supply_chain_nodes)
    discovery_plan = _topic_candidate_discovery_plan(
        focus_theme,
        maintenance_mode,
        existing_profiles,
        recent_scan_candidates,
        market_signals,
        topic_gaps,
    )

    return {
        "topic_maintain_mode_hint": "initial" if not existing_profiles else "update",
        "maintenance_mode": maintenance_mode,
        "focus_theme": focus_theme,
        "focus_policy": {
            "enabled": bool(focus_theme),
            "instruction": "若 focus_theme 不為空，優先補該題材代表公司、產品、客戶、營收曝險、供應鏈角色與證據。",
        },
        "topic_library_gap_analysis": topic_gaps,
        "candidate_discovery_plan": discovery_plan,
        "source_policy": _topic_source_policy(),
        "ai_candidate_policy": {
            "ai_knowledge_allowed": True,
            "rule": "AI 可提出題材、候選股、供應鏈推論，但沒有可追溯 evidence/source_url/source_level 的項目只能標 candidate 或 missing；只有 verified/inferred 會在 /topic_confirm 自動寫入正式題材庫。",
            "apply_statuses": ["verified", "inferred"],
            "non_apply_statuses": ["candidate", "missing"],
        },
        "stock_universe_summary": {
            "total_stocks": len(universe),
            "industry_count": len(industry_count),
            "top_industries": sorted(industry_count.items(), key=lambda x: x[1], reverse=True)[:10],
        },
        "industry_summary": dict(sorted(industry_count.items(), key=lambda x: x[1], reverse=True)[:20]),
        "existing_topic_profiles": existing_profiles,
        "company_topic_map": company_topic_map,
        "supply_chain_nodes": supply_chain_nodes,
        "external_topic_source_caches": external_topic_source_caches,
        "recent_theme_reports": recent_theme_reports,
        "recent_scans": recent_scans,
        "recent_scan_candidates": recent_scan_candidates,
        "market_signals": market_signals,
        "legacy_theme_references": legacy_themes,
        "candidate_companies": candidate_companies,
        "report_date": request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat(),
        "notes": [
            "topic_maintain 先用本地快取與既有設定檔建立候選題材、候選股、缺口與搜尋計畫，再交由 discovery/WebFetch 取得最新公開來源。",
            "正式寫入規則：verified 與 inferred 可套用；candidate 不寫入正式題材庫；missing 只記錄缺口。",
        ],
    }


def _topic_maintain_mode(request: CommandRequest, focus_theme: str, existing_profiles: list[dict[str, Any]]) -> str:
    if focus_theme.startswith("__from_radar__:"):
        return "from_theme_radar"
    if focus_theme:
        return "focused_theme"
    return "full_market_maintenance" if existing_profiles else "full_market_initial"


def _topic_library_gap_analysis(
    profiles: list[dict[str, Any]],
    company_map: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    company_gaps: list[dict[str, Any]] = []
    for code, value in company_map.items():
        if not isinstance(value, dict):
            company_gaps.append({"company_code": code, "missing": ["structured relation object"], "priority": "high"})
            continue
        missing = [
            field for field in ("role", "relation_type", "products", "customers", "revenue_exposure", "benefit_logic", "evidence")
            if not value.get(field)
        ]
        if missing:
            company_gaps.append({
                "company_code": code,
                "company_name": value.get("company_name", ""),
                "themes": value.get("themes", []),
                "missing": missing,
                "priority": "high" if "evidence" in missing or "products" in missing else "medium",
            })

    node_gaps: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        missing = [
            field for field in ("layer", "role", "product_keywords", "customers", "revenue_exposure", "benefit_logic", "evidence")
            if not node.get(field)
        ]
        if missing:
            node_gaps.append({
                "node_id": node.get("node_id"),
                "theme_id": node.get("theme_id"),
                "company_code": node.get("company_code"),
                "company_name": node.get("company_name"),
                "missing": missing,
                "priority": "high" if "evidence" in missing else "medium",
            })

    profile_gaps = []
    for profile in profiles:
        missing = [field for field in ("keywords", "industries", "risk_notes", "missing_data") if not profile.get(field)]
        if missing:
            profile_gaps.append({"theme_id": profile.get("theme_id"), "theme_name": profile.get("theme_name"), "missing": missing})

    return {
        "profile_gap_count": len(profile_gaps),
        "company_gap_count": len(company_gaps),
        "supply_chain_node_gap_count": len(node_gaps),
        "priority_company_gaps": company_gaps[:40],
        "priority_supply_chain_node_gaps": node_gaps[:40],
        "profile_gaps": profile_gaps[:40],
        "recommended_mode": "full_market_maintenance",
    }


def _topic_candidate_discovery_plan(
    focus_theme: str,
    maintenance_mode: str,
    profiles: list[dict[str, Any]],
    recent_scan_candidates: list[dict[str, Any]],
    market_signals: dict[str, Any],
    gaps: dict[str, Any],
) -> dict[str, Any]:
    candidate_themes: list[dict[str, Any]] = []
    if focus_theme and not focus_theme.startswith("__from_radar__:"):
        candidate_themes.append({"theme": focus_theme, "source": "user_focus", "priority": "high"})
    if focus_theme.startswith("__from_radar__:"):
        candidate_themes.append({"theme": focus_theme.replace("__from_radar__:", "", 1), "source": "theme_radar", "priority": "high"})
    for profile in profiles[:30]:
        missing = [field for field in ("keywords", "industries", "risk_notes", "missing_data") if not profile.get(field)]
        if missing:
            candidate_themes.append({
                "theme": profile.get("theme_name") or profile.get("theme_id"),
                "theme_id": profile.get("theme_id"),
                "source": "topic_library_gap",
                "priority": "medium",
                "missing": missing,
            })
    for item in market_signals.get("recent_scan_top_industries", [])[:10]:
        candidate_themes.append({"theme": item.get("industry"), "source": "recent_scan_top_industries", "priority": "medium", "count": item.get("count")})

    candidate_stocks: list[dict[str, Any]] = []
    for scan in recent_scan_candidates[:5]:
        for item in scan.get("candidates", [])[:12]:
            candidate_stocks.append({
                "code": item.get("code"),
                "name": item.get("name"),
                "industry": item.get("industry"),
                "source": "recent_scan_candidate",
                "scan_type": scan.get("scan_type"),
                "scan_date": scan.get("scan_date"),
            })
    for item in market_signals.get("high_volume_companies", [])[:20]:
        candidate_stocks.append({**item, "source": "high_volume_company"})

    query_plan = _topic_search_query_plan(candidate_themes[:20], candidate_stocks[:40], gaps, maintenance_mode)
    return {
        "maintenance_mode": maintenance_mode,
        "candidate_themes": candidate_themes[:40],
        "candidate_stocks": candidate_stocks[:80],
        "evidence_targets": [
            "products",
            "customers",
            "revenue_exposure",
            "benefit_logic",
            "supply_chain_role",
            "counter_evidence",
        ],
        "search_query_plan": query_plan,
    }


def _topic_search_query_plan(
    themes: list[dict[str, Any]],
    stocks: list[dict[str, Any]],
    gaps: dict[str, Any],
    maintenance_mode: str,
) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    queries.extend(_topic_full_market_query_plan())
    queries.extend(build_topic_maintain_sector_queries(stocks[:40], limit=20))
    for theme in themes[:12]:
        name = str(theme.get("theme") or "").strip()
        if not name:
            continue
        queries.extend([
            {"type": "theme_representative_stocks", "query": f"台股 {name} 代表股 供應鏈 受惠 公司"},
            {"type": "theme_products_customers", "query": f"{name} 台股 產品 客戶 營收占比 法說會"},
            {"type": "theme_counter_evidence", "query": f"{name} 台股 風險 庫存 砍單 毛利 財報"},
            {"type": "topic_supply_chain_layers", "query": f"{name} 上游 中游 下游 關鍵零組件 供應鏈層級"},
            {"type": "theme_catalysts", "query": f"{name} 訂單 政策 國際大廠 資本支出 近期新聞"},
            {"type": "theme_alternative_risks", "query": f"{name} 替代技術 競爭者 題材退燒 營收連結不足"},
        ])
    for stock in stocks[:20]:
        code = str(stock.get("code") or "")
        name = str(stock.get("name") or "")
        label = " ".join(part for part in (code, name) if part).strip()
        if not label:
            continue
        context_terms = " ".join(topic_search_terms_for_stock(stock, max_terms=8))
        queries.extend([
            {"type": "company_product_evidence", "query": f"{label} 產品 客戶 法說會 營收占比"},
            {"type": "company_theme_evidence", "query": f"{label} 題材 受惠 供應鏈 {context_terms}".strip()},
            {"type": "company_official_evidence", "query": f"{label} 公開資訊觀測站 月營收 年報 法說會 投資人關係"},
            {"type": "company_counter_evidence", "query": f"{label} 風險 庫存 毛利率 下滑 砍單 客戶集中"},
        ])
    for gap in gaps.get("priority_company_gaps", [])[:12]:
        label = " ".join(part for part in (str(gap.get("company_code") or ""), str(gap.get("company_name") or "")) if part).strip()
        if label:
            queries.append({"type": "backfill_company_gap", "query": f"{label} {' '.join(gap.get('themes') or [])} {' '.join(gap.get('missing') or [])}"})
            queries.append({"type": "backfill_official_gap", "query": f"{label} {' '.join(gap.get('missing') or [])} 官方 法說會 年報 月營收"})
    return [{"mode": maintenance_mode, **item} for item in queries[:120]]


def _topic_full_market_query_plan() -> list[dict[str, Any]]:
    """Broad default topic-maintenance searches across major Taiwan market buckets."""
    buckets = [
        ("ai_semiconductor", [
            "台股 AI 半導體 伺服器 ASIC HBM CoWoS 先進封裝 最新題材",
            "台股 AI 供應鏈 散熱 電源 PCB CCL 伺服器代工 近期新聞",
        ]),
        ("power_energy_grid", [
            "台股 重電 電力設備 電網 儲能 變壓器 AI資料中心 用電 近期新聞",
            "台股 能源 綠電 儲能 電線電纜 電力基建 政策 受惠股",
        ]),
        ("pcb_components", [
            "台股 PCB CCL IC載板 被動元件 連接器 高速傳輸 近期題材",
            "台股 電子零組件 MLCC 散熱 模組 材料 報價 訂單 新聞",
        ]),
        ("financial_dividend", [
            "台股 金融股 壽險 金控 高股息 ETF 資金輪動 近期新聞",
            "台股 高股息 價值股 金融 補漲 低波動 題材",
        ]),
        ("biotech_healthcare", [
            "台股 生技 醫療 新藥 CDMO 醫材 長照 政策 近期新聞",
            "台股 生技股 臨床 授權 營收 醫療科技 題材",
        ]),
        ("shipping_commodities_cycle", [
            "台股 航運 散裝 原物料 鋼鐵 水泥 塑化 景氣循環 近期題材",
            "BDI 運價 原物料 台股 航運 鋼鐵 塑化 受惠股",
        ]),
        ("defense_security", [
            "台股 軍工 無人機 安控 資安 國防預算 政策 近期新聞",
            "台股 國防 無人機 通訊 安控 供應鏈 受惠公司",
        ]),
        ("domestic_consumption", [
            "台股 內需 觀光 餐飲 食品 通路 百貨 消費復甦 近期新聞",
            "台股 觀光 旅遊 食品 通路 內需 題材 受惠股",
        ]),
        ("ev_automotive", [
            "台股 車用電子 電動車 充電樁 車聯網 ADAS 近期題材",
            "台股 EV 車用 充電樁 電池 車用零組件 供應鏈 新聞",
        ]),
        ("robotics_automation", [
            "台股 機器人 自動化 工具機 工業電腦 物理AI 近期新聞",
            "台股 自動化 機器人 工具機 感測器 工控 受惠股",
        ]),
        ("telecom_satellite", [
            "台股 低軌衛星 網通 光通訊 CPO 交換器 通訊 近期題材",
            "台股 衛星 網通 光通訊 高速傳輸 供應鏈 受惠公司",
        ]),
        ("policy_macro_rotation", [
            "台股 政策受惠 產業輪動 法人看好 近期熱門族群",
            "台股 近一個月 熱門題材 族群輪動 法人 媒體 產業新聞",
        ]),
    ]
    result: list[dict[str, Any]] = []
    for bucket, bucket_queries in buckets:
        for query in bucket_queries:
            result.append({"type": "full_market_bucket", "bucket": bucket, "query": query})
    return result


def _topic_source_policy() -> dict[str, Any]:
    return {
        "L1_official": ["MOPS/公開資訊觀測站", "年報", "法說會", "公司新聞稿", "月營收公告", "財報"],
        "L2_media": ["可信財經媒體", "券商報告摘要", "交易所/產業新聞轉載"],
        "L3_community": ["社群討論", "論壇", "未具名市場傳聞"],
        "rules": [
            "products/customers/revenue_exposure 若有 L1 證據可標 verified。",
            "benefit_logic、供應鏈延伸若有 L1/L2 來源或多項證據可標 inferred。",
            "只有 AI 記憶、社群、關鍵字命中或產業分類命中時只能標 candidate。",
            "找不到資料時標 missing，寫入 missing_data，不要補成事實。",
        ],
    }


def collect_research_data(request: CommandRequest, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    try:
        return _collect_research_data_live(request, progress=progress)
    except Exception as exc:
        target = request.target or ""
        resolved = resolve_stock_reference(target)
        code = resolved.code if resolved else str(target).upper().split(".", 1)[0]
        cache_date = request.report_date or datetime.now().date()
        fallback = load_latest_research_structured_cache(code, before_or_on=cache_date)
        if fallback is None:
            raise
        cached_data, fallback_date = fallback
        data = deepcopy(cached_data)
        notes = list(data.get("notes") or [])
        notes.append(
            f"即時投研結構化資料收集失敗，改用 {fallback_date.isoformat()} 最近快取；"
            f"原始錯誤：{type(exc).__name__}: {exc}"
        )
        data["notes"] = notes
        data["structured_cache_fallback"] = {
            "enabled": True,
            "fallback_date": fallback_date.isoformat(),
            "target_date": cache_date.isoformat(),
            "reason": f"{type(exc).__name__}: {exc}",
        }
        if progress:
            progress(f"個股研究：即時資料失敗，改用最近投研結構化快取 {code} {fallback_date.isoformat()}")
        return data


def _collect_research_data_live(request: CommandRequest, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    target = request.target or ""
    if progress:
        progress(f"個股研究：解析股票代號/名稱 {target}")
    resolved = resolve_stock_reference(target)
    code = resolved.code if resolved else target
    report_date = request.report_date
    cache_date = report_date or datetime.now().date()

    # Try loading from structured cache (24-hour TTL)
    cached = load_research_structured_cache(code, cache_date)
    if cached is not None:
        if progress:
            progress(f"個股研究：使用投研結構化快取 {code} {cache_date.isoformat()}")
        _ensure_research_rerating_snapshot(cached, request, code, progress=progress)
        return cached

    with StockDataFetcher() as fetcher:
        try:
            meta = fetcher.resolve_stock(code)
        except StockNotFoundError:
            raise StockNotFoundError("查無此股票，請確認股票代號或名稱。")

        if progress:
            progress(f"個股研究：取得股價資料 {meta.code} {meta.name}")
        price_df = fetcher.fetch_price_history(meta)
        if report_date is not None:
            price_df = _filter_date_frame(price_df, "Date", report_date)
        trading_dates = price_df["Date"].tolist() if not price_df.empty else []

        if progress:
            progress(f"個股研究：取得法人買賣資料，交易日 {len(trading_dates)} 筆")
        institutional_df = fetcher.fetch_institutional_daily(meta, trading_dates) if trading_dates else pd.DataFrame()

        if progress:
            progress("個股研究：取得融資融券資料")
        margin_df = fetcher.fetch_margin_daily(meta, trading_dates) if trading_dates else pd.DataFrame()

        if progress:
            progress("個股研究：取得月營收資料")
        revenue_df = fetcher.fetch_monthly_revenue(meta, start_year=max(2023, (report_date.year - 2 if report_date else 2023)))

        if progress:
            progress("個股研究：取得季度財報資料")
        financial_df = fetcher.fetch_quarterly_financials(meta)

        if report_date is not None:
            revenue_df = _filter_month_frame(revenue_df, "Month", report_date)
            financial_df = _filter_quarter_frame(financial_df, "Quarter", report_date)

        if progress:
            progress("個股研究：合併策略摘要")
        daily_df = fetcher.merge_daily_frames(price_df, institutional_df, margin_df) if not price_df.empty else pd.DataFrame()
        summary_df = fetcher.build_strategy_summary(meta, daily_df, revenue_df, financial_df)

    if progress:
        progress("個股研究：取得免費公開來源 TWSE/TPEx/TDCC/MOPS")
    free_sources = build_free_research_sources(meta.code, meta.symbol, report_date)
    if progress:
        progress("個股研究：讀取選股程式法人/籌碼/大戶備用資料")
    chip_backup = build_chip_backup_snapshot(meta.code, report_date)

    cache_date = report_date or datetime.now().date()

    result = {
        "stock": {"code": meta.code, "name": meta.name, "symbol": meta.symbol, "market": meta.market},
        "report_date": cache_date.isoformat(),
        "price_data": _tail_records(price_df, 30),
        "technical_data": _technical_snapshot(price_df),
        "institutional_data": _tail_records(institutional_df, 30),
        "margin_data": _tail_records(margin_df, 30),
        "revenue_data": _tail_records(revenue_df, 18),
        "financial_data": _tail_records(financial_df, 12),
        "strategy_summary": _tail_records(summary_df, 50),
        "free_public_sources": free_sources,
        "valuation_data": free_sources.get("valuation", {}),
        "tdcc_data": free_sources.get("tdcc", {}),
        "gross_margin_cache": free_sources.get("gross_margin_cache", {}),
        "mops_documents": free_sources.get("mops_documents", {}),
        "chip_backup_data": chip_backup,
        "source_events": build_chip_backup_events(meta.code, report_date),
        "notes": [
            "--date 模式目前對本地結構化資料採日期切片；若原始抓取函式只取近期資料，較久日期可能資料不足。"
        ] if report_date else [],
    }

    # /research --deep 或 --score 時寫入價值重估底稿
    _ensure_research_rerating_snapshot(result, request, meta.code, progress=progress)

    # Inject topic context as background reference
    try:
        result["topic_context"] = build_stock_topic_context(meta.code, meta.name)
    except Exception as exc:
        result["topic_context_error"] = str(exc)

    try:
        save_research_structured_cache(meta.code, cache_date, result)
    except Exception:
        pass

    return result


def _ensure_research_rerating_snapshot(
    data: dict[str, Any],
    request: CommandRequest,
    stock_code: str,
    progress: Callable[[str], None] | None = None,
) -> None:
    """Ensure deep/score research always carries the shared rerating draft."""
    if request.mode not in ("deep", "score"):
        return
    if data.get("local_rerating_snapshot"):
        return
    if progress:
        progress(f"個股研究（{request.mode}）：建立價值重估底稿")
    try:
        data["local_rerating_snapshot"] = build_rerating_snapshot_for_stock(
            stock_code,
            request.report_date,
            progress=progress,
        )
    except Exception as exc:
        notes = list(data.get("notes") or [])
        notes.append(f"價值重估底稿建立失敗，AI 需保守解讀：{type(exc).__name__}: {exc}")
        data["notes"] = notes
        data["local_rerating_snapshot_error"] = f"{type(exc).__name__}: {exc}"


def collect_macro_data(request: CommandRequest, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "market_scope": request.market_scope,
        "theme_scope": request.theme_scope,
        "region_scope": request.region_scope,
        "report_date": request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat(),
    }
    if progress:
        progress("宏觀研究：取得台股收盤/盤中摘要")
    try:
        data["noon_market_report"] = build_noon_market_report()
    except MarketSummaryError as exc:
        data["noon_market_report_error"] = str(exc)
        if progress:
            progress(f"宏觀研究：台股摘要失敗：{exc}")
    if progress:
        progress("宏觀研究：取得美股與台指期夜盤摘要")
    try:
        data["morning_market_report"] = build_morning_market_report()
    except MarketSummaryError as exc:
        data["morning_market_report_error"] = str(exc)
        if progress:
            progress(f"宏觀研究：晨間摘要失敗：{exc}")
    if progress:
        progress("宏觀研究：取得量化市場資料、VIX、期貨籌碼、法人資金流")
    data["quantitative_market"] = build_macro_indicators(request.report_date, progress=progress)
    data["volatility"] = data["quantitative_market"].get("volatility", {})
    data["industry_flow"] = data["quantitative_market"].get("industry_flow", {})
    data["fear_greed"] = data["quantitative_market"].get("fear_greed", {})
    data["market_score"] = data["fear_greed"]
    if progress:
        progress("宏觀研究：取得 TWSE 類股公開資料")
    data["free_public_sources"] = build_free_macro_sources(request.report_date)
    data["industry_index_data"] = data["free_public_sources"].get("twse_industry_index", {})
    data["notes"] = ["Macro 第三版加入 VIX proxy、台指選擇權 IV 接入狀態、類股流動性 proxy 與 fear/greed 系統分數。"]
    return data

def collect_theme_data(request: CommandRequest, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    if progress:
        progress("題材研究：載入股票宇宙")
    universe = load_stock_universe(False)
    theme = request.theme_scope or request.target or ""
    if progress:
        progress(f"題材研究：讀取題材知識庫 {theme}")
    profile = _theme_profile(theme)
    keywords = [theme, *profile.get("keywords", [])]
    industries = set(profile.get("industries", []))
    matched = []
    total = len(universe)
    for index, entry in enumerate(universe, 1):
        if progress and (index == 1 or index == total or index % 300 == 0):
            progress(f"題材研究：比對股票宇宙 {index}/{total}，目前命中 {len(matched)} 檔")
        text = f"{entry.name} {entry.industry}".lower()
        keyword_hit = any(str(keyword).lower() in text for keyword in keywords if keyword)
        industry_hit = any(industry in entry.industry for industry in industries)
        if keyword_hit or industry_hit:
            matched.append({**asdict(entry), "theme_match_reason": _theme_match_reason(entry.industry, keyword_hit, industry_hit)})
    matched = matched[: request.top or 10] if matched else [asdict(entry) for entry in universe[: min(request.top or 10, 50)]]
    if progress:
        progress(f"題材研究：命中整理完成，候選 {len(matched)} 檔，補公司知識庫")
    matched = enrich_company_rows(matched)
    result = {
        "theme": theme,
        "report_date": request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat(),
        "supply_chain_profile": profile,
        "company_knowledge_summary": theme_knowledge_summary(matched),
        "matched_universe": matched,
        "matched_companies": matched,  # alias for backward compatibility with prompt/discovery
        "notes": ["Theme 第三版加入 config/company_knowledge.json 公司產品、客戶、營收占比與供應鏈角色資料庫；未覆蓋公司會標示待驗證。"],
    }
    try:
        result["topic_context"] = build_theme_topic_context(theme)
    except Exception as exc:
        result["topic_context_error"] = str(exc)
    result["theme_quality_context"] = _build_theme_quality_context(result)
    return result


def _build_theme_quality_context(data: dict[str, Any]) -> dict[str, Any]:
    matched = data.get("matched_companies") or data.get("matched_universe") or []
    summary = data.get("company_knowledge_summary") or {}
    topic_context = data.get("topic_context") if isinstance(data.get("topic_context"), dict) else {}
    related_nodes = topic_context.get("related_supply_chain_nodes") or []
    matched_topics = topic_context.get("matched_topics") or []

    matched_count = len(matched) if isinstance(matched, list) else 0
    knowledge_total = int(summary.get("total_companies") or matched_count or 0)
    knowledge_covered = int(summary.get("covered_companies") or 0)
    node_company_codes = {
        str(node.get("company_code") or "").strip()
        for node in related_nodes
        if isinstance(node, dict) and str(node.get("company_code") or "").strip()
    }
    effective_total = max(knowledge_total, matched_count, len(node_company_codes))
    effective_covered = max(knowledge_covered, len(node_company_codes))
    coverage_pct = round((effective_covered / effective_total) * 100, 1) if effective_total else 0
    return {
        "matched_company_count": matched_count,
        "knowledge_total_companies": knowledge_total,
        "knowledge_covered_companies": knowledge_covered,
        "topic_matched_count": len(matched_topics) if isinstance(matched_topics, list) else 0,
        "related_supply_chain_node_count": len(related_nodes) if isinstance(related_nodes, list) else 0,
        "related_supply_chain_company_count": len(node_company_codes),
        "effective_total_companies": effective_total,
        "effective_covered_companies": effective_covered,
        "coverage_pct": coverage_pct,
        "coverage_source": "topic_context" if len(node_company_codes) > knowledge_covered else "company_knowledge_summary",
    }

def collect_value_scan_data(request: CommandRequest, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    # 單檔模式（/value_scan 6217）
    if request.target_type == "stock" and request.target:
        if progress:
            progress(f"價值重估：單檔模式 {request.target}")
        snapshot = build_rerating_snapshot_for_stock(request.target, request.report_date, progress=progress)
        result = {
            "candidate_pool": request.target,
            "candidate_source_policy": {"source": "單一股票", "status": "single_stock"},
            "report_date": request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat(),
            "top_n": 1,
            "candidates": [
                {
                    "code": snapshot.get("stock_id"),
                    "name": snapshot.get("stock_name"),
                    "symbol": snapshot.get("symbol"),
                    "industry": snapshot.get("industry"),
                    "rerating_score": snapshot.get("rerating_score"),
                    "verification_score": snapshot.get("verification_score"),
                    "tdcc_score": snapshot.get("tdcc_score"),
                    "valuation_score": snapshot.get("valuation_score"),
                    "old_market_label": snapshot.get("old_market_label"),
                    "new_market_label": snapshot.get("new_market_label"),
                    "rerating_evidence": snapshot.get("rerating_evidence", []),
                    "counter_evidence": snapshot.get("counter_evidence", []),
                    "data_gaps": snapshot.get("data_gaps", []),
                    "source_coverage": snapshot.get("source_coverage", {}),
                    "financial_detail": snapshot.get("financial_detail", {}),
                    "local_rerating_snapshot": snapshot,
                }
            ],
            "local_rerating_snapshot": snapshot,
            "source_events": [],
            "scoring_rules": _value_scan_rules(),
            "verification_policy": "單一股票價值重估模式",
            "notes": ["單檔模式直接使用價值重估底稿服務，不走候選池掃描。"],
        }
        try:
            result["topic_context"] = build_stock_topic_context(
                str(snapshot.get("stock_id", "")), str(snapshot.get("stock_name", ""))
            )
        except Exception as exc:
            result["topic_context_error"] = str(exc)
        return result

    if progress:
        progress("價值重估：載入候選股票池")
    universe, universe_policy = _value_scan_universe(request, progress=progress)
    top_n = request.top or 10
    if progress:
        progress(f"價值重估：候選池={universe_policy.get('source')}，狀態={universe_policy.get('status')}，候選 {len(universe)} 檔，top={top_n}")

    if not universe:
        return {
            "candidate_pool": request.candidate_pool or "精選選股",
            "candidate_source_policy": universe_policy,
            "report_date": request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat(),
            "top_n": top_n,
            "candidates": [],
            "source_events": [],
            "scoring_rules": _value_scan_rules(),
            "verification_policy": "候選池為空，未執行重估。",
            "notes": [universe_policy.get("note") or "候選池沒有可分析股票。"],
        }

    if progress:
        progress(f"價值重估：讀取最近營收資料（{len(universe)} 檔）")
    revenue_history = load_recent_revenue_history(universe)
    if progress:
        progress(f"價值重估：營收資料完成，涵蓋 {len(revenue_history)} 檔")

    if progress:
        progress(f"價值重估：讀取價量資料（{len(universe)} 檔）")
    price_metrics, price_policy = load_price_metrics_with_fallback(universe, progress=progress)
    if progress:
        progress(f"價值重估：價量資料完成，涵蓋 {len(price_metrics)} 檔")

    rows: list[dict[str, Any]] = []
    radar_candidates_by_code = universe_policy.get("radar_candidates_by_code") if isinstance(universe_policy, dict) else None
    total = len(universe)
    for index, entry in enumerate(universe, 1):
        metric = price_metrics.get(entry.symbol) or {}
        revenue_points = revenue_history.get(entry.code) or []
        latest_revenue = revenue_points[0] if revenue_points else None
        price = metric.get("price")
        avg_volume = metric.get("avg_volume_20d")
        if progress and (index == 1 or index == total or index % 25 == 0):
            progress(f"價值重估：本地初評 {index}/{total}，目前可評 {len(rows)} 檔")
        revenue_value = latest_revenue.revenue if latest_revenue else None
        revenue_yoy = latest_revenue.yoy if latest_revenue else None
        score_detail = _value_rerating_score(entry.industry, price, avg_volume, revenue_yoy)
        row = {
                "code": entry.code,
                "name": entry.name,
                "symbol": entry.symbol,
                "industry": entry.industry,
                "price": price,
                "avg_volume_20d": avg_volume,
                "latest_monthly_revenue": revenue_value,
                "revenue_yoy": revenue_yoy,
                "old_market_label": score_detail["old_market_label"],
                "new_market_label": score_detail["new_market_label"],
                "rerating_evidence": [*score_detail["evidence"], *([] if price is not None else ["價量資料缺漏，保守處理"]), *([] if latest_revenue is not None else ["最近月營收資料缺漏，保守處理"])],
                "score_components": score_detail["components"],
                "rerating_score": score_detail["score"],
            }
        if isinstance(radar_candidates_by_code, dict):
            radar_payload = radar_candidates_by_code.get(entry.code)
            if isinstance(radar_payload, dict):
                row["radar_score"] = radar_payload.get("total_score")
                row["radar_score_components"] = radar_payload.get("score_components")
                row["radar_strategy_codes"] = radar_payload.get("strategy_codes")
                row["radar_data_coverage"] = radar_payload.get("data_coverage")
                row["radar_evidence_pack"] = radar_payload.get("evidence_pack")
                row["radar_ai_sources"] = radar_payload.get("ai_sources")
        rows.append(row)

    rows.sort(key=lambda row: row["rerating_score"], reverse=True)
    rows = enrich_company_rows(rows)
    # 保留足夠 rows（至少 ai_candidate_limit），確保 deep 模式可取到 30 檔
    rows = rows[: max(request.top or 30, 30)]

    # AI candidates 上限：source-only 不送 AI，一般 mode 上限 10，deep 上限 30
    if request.source_only:
        ai_candidate_limit = 0
    elif request.mode == "deep":
        ai_candidate_limit = 30
    else:
        ai_candidate_limit = 10

    # 官方/公開資料蒐證：對預篩候選股執行（數量至少涵蓋 ai_candidate_limit）
    evidence_scope = min(len(rows), ai_candidate_limit if ai_candidate_limit > 0 else 10)
    if progress:
        progress(f"價值重估：本地排序完成，可評 {len(rows)} 檔；開始逐檔官方蒐證 {evidence_scope} 檔")
    _attach_value_scan_evidence(rows[:evidence_scope], request.report_date, progress=progress)
    for row in rows:
        events = row.get("source_events") or []
        row["cross_validation"] = build_value_cross_validation(row, events)
        row["verification_score"] = row["cross_validation"]["verification_score"]
        row["early_signal_priority"] = _value_scan_early_signal_priority(row, universe_policy)
    if _value_scan_should_preserve_early_candidates(universe_policy):
        rows.sort(key=lambda row: (row["early_signal_priority"], row["rerating_score"], row["verification_score"]), reverse=True)
        sort_policy = "early_signal_priority_then_rerating_for_curated_or_recent_pool"
    else:
        rows.sort(key=lambda row: (row["rerating_score"], row["verification_score"]), reverse=True)
        sort_policy = "rerating_score_then_verification_score"
    if progress:
        progress(f"價值重估：交叉驗證完成，準備輸出 AI 候選股 {min(ai_candidate_limit, len(rows))} 檔")

    # ai_candidates 必須在交叉驗證後建立，確保是最終排序結果
    if request.source_only:
        ai_candidates: list[dict[str, Any]] = []
    else:
        ai_candidates = rows[:ai_candidate_limit]

    # source_events 從最終 ai_candidates 彙整
    source_events: list[dict[str, Any]] = []
    for row in ai_candidates if ai_candidates else rows[:evidence_scope]:
        source_events.extend(row.get("source_events") or [])

    # 建立 AI 候選股完整證據包（每檔固定欄位，避免 JSON 截斷导致資料漏送）
    ai_candidate_evidence_pack = _build_ai_candidate_evidence_pack(ai_candidates if ai_candidates else rows[:evidence_scope])

    result = {
        "candidate_pool": request.candidate_pool or "精選選股",
        "candidate_source_policy": universe_policy,
        "price_data_policy": price_policy,
        "report_date": request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat(),
        "top_n": request.top or ai_candidate_limit or 10,
        "total_candidate_count": len(rows),
        "ai_candidate_limit": ai_candidate_limit,
        "value_scan_sort_policy": sort_policy,
        "ai_candidates": ai_candidates,
        "ai_candidate_evidence_pack": ai_candidate_evidence_pack,
        "local_ranking": [
            {
                "code": r["code"],
                "name": r["name"],
                "rerating_score": r["rerating_score"],
                "verification_score": r.get("verification_score"),
                "early_signal_priority": r.get("early_signal_priority", 0),
            }
            for r in rows
        ],
        "local_ranking_truncated": len(rows) > ai_candidate_limit if ai_candidate_limit else False,
        "candidates": rows[: min(request.top or 10, 10)],
        "source_events": source_events,
        "scoring_rules": _value_scan_rules(),
        "verification_policy": "第三版後續強化：前段候選股會接 MOPS 官方查詢入口/連線檢查、季度財報 snapshot、公司知識庫與既有事件資料，形成 evidence coverage。",
        "notes": [
            "Value scan 已加入官方 MOPS 參考事件與財報細項 snapshot；MOPS 明細解析若遇反爬或欄位變動會保守降為 reference_link。",
            "法人報告摘要仍需使用者提供合法來源或付費資料源，系統目前只保留 broker_report_reference 事件型別。",
        ],
    }

    # Inject topic context for candidate pool
    try:
        result["topic_context"] = build_candidates_topic_context(ai_candidates)
    except Exception as exc:
        result["topic_context_error"] = str(exc)

    return result

def _value_scan_universe(request: CommandRequest, progress: Callable[[str], None] | None = None) -> tuple[list[Any], dict[str, Any]]:
    all_universe = load_stock_universe(False)
    pool = str(request.candidate_pool or request.target or "精選選股").strip()
    by_code = {entry.code: entry for entry in all_universe}

    if pool in {"精選選股", "精選選股名單", "curated"}:
        target_date = request.report_date or get_tw_today()
        cached = find_cached_curated_scan(target_date)
        if cached:
            codes = [str(code) for code in cached.get("codes") or []]
            selected = [by_code[code] for code in codes if code in by_code]
            missing = [code for code in codes if code not in by_code]
            if progress:
                progress(f"價值重估：找到資料日期 {target_date.isoformat()} 的精選選股快取，使用候選名單 {len(selected)} 檔")
            return selected, {
                "source": "精選選股交叉命中快取",
                "status": "cached",
                "candidate_count": len(selected),
                "report_date": target_date.isoformat(),
                "scan_id": cached.get("scan_id"),
                "requested_codes": codes,
                "missing_codes": missing,
                "note": "已使用相同資料日期的 /scan 精選選股交叉命中快取，不重新執行選股程式。",
            }
        try:
            if progress:
                progress(f"價值重估：沒有資料日期 {target_date.isoformat()} 的精選選股快取，開始執行精選選股交叉命中")
            curated_result = build_curated_scan_result(report_date=target_date)
            codes = curated_result.selected_codes
            selected = [by_code[code] for code in codes if code in by_code]
            missing = [code for code in codes if code not in by_code]
            save_recent_scan_result(CURATED_SCAN_TYPE, target_date, curated_result.report_text, curated_result.selected_codes)
            status = "covered" if selected else "empty"
            note = "已調用主程式精選選股交叉命中邏輯取得候選名單後再做本地重估排序。"
            if not selected:
                note = "精選選股交叉命中結果為 0 檔，未自動改用全市場初篩。若要掃全市場，請使用 /value_scan 全市場初篩。"
            return selected, {
                "source": "精選選股交叉命中",
                "status": status,
                "candidate_count": len(selected),
                "report_date": target_date.isoformat(),
                "requested_codes": codes,
                "missing_codes": missing,
                "note": note,
            }
        except Exception as exc:
            if progress:
                progress(f"價值重估：精選選股交叉命中失敗，不改用全市場初篩：{exc}")
            return [], {
                "source": "精選選股交叉命中",
                "status": "failed",
                "candidate_count": 0,
                "report_date": target_date.isoformat(),
                "error": str(exc),
                "note": "精選選股交叉命中失敗，未自動改用全市場初篩，避免候選池語意被放寬。",
            }

    if pool in {"我的持股", "持股", "portfolio"}:
        portfolio = list_portfolio()
        codes = [item.code for item in portfolio]
        selected = [by_code[code] for code in codes if code in by_code]
        missing = [code for code in codes if code not in by_code]
        return selected, {"source": "我的持股", "status": "covered", "candidate_count": len(selected), "requested_codes": codes, "missing_codes": missing}

    if pool in {"監控清單", "monitor", "監控"}:
        codes = _load_monitor_codes()
        selected = [by_code[code] for code in codes if code in by_code]
        missing = [code for code in codes if code not in by_code]
        return selected, {"source": "監控清單", "status": "covered", "candidate_count": len(selected), "requested_codes": codes, "missing_codes": missing}

    if pool in {"選股雷達", "雷達選股", "radar"}:
        try:
            from radar_service import load_radar_result
            radar_result = load_radar_result(request.report_date)
        except Exception as exc:
            return [], {"source": "選股雷達", "status": "failed", "candidate_count": 0, "error": str(exc), "note": "讀取 Radar 快取失敗，請先執行 /radar。"}
        if not radar_result or not radar_result.candidates:
            return [], {"source": "選股雷達", "status": "missing", "candidate_count": 0, "note": "目前尚未找到已保存的 Radar 結果，請先執行 /radar。"}
        codes = [str(item.code) for item in radar_result.candidates if str(item.code or "").strip()]
        selected = [by_code[code] for code in codes if code in by_code]
        missing = [code for code in codes if code not in by_code]
        radar_candidates_by_code = {
            str(item.code): {
                "code": str(item.code),
                "name": str(getattr(item, "name", "") or ""),
                "total_score": int(getattr(item, "total_score", 0) or 0),
                "score_components": dict(getattr(item, "score_components", {}) or {}),
                "strategy_codes": sorted(getattr(item, "strategy_codes", set()) or []),
                "data_coverage": dict(getattr(item, "data_coverage", {}) or {}),
                "evidence_pack": dict(getattr(item, "evidence_pack", {}) or {}),
                "ai_sources": list(getattr(item, "ai_sources", []) or []),
                "web_sources": list(getattr(item, "web_sources", []) or []),
            }
            for item in radar_result.candidates
            if str(item.code or "").strip()
        }
        return selected, {
            "source": "選股雷達",
            "status": "covered",
            "candidate_count": len(selected),
            "report_date": radar_result.report_date.isoformat(),
            "radar_source": radar_result.request.source,
            "requested_codes": codes,
            "missing_codes": missing,
            "radar_candidate_count": len(radar_candidates_by_code),
            "radar_candidates_by_code": radar_candidates_by_code,
            "note": "使用最近一次或指定日期的 /radar 快取候選名單，不重新執行 Radar。",
        }

    if pool.startswith("自訂:") or "," in pool:
        raw = pool.replace("自訂:", "")
        codes = [part.strip().split()[0] for part in raw.replace("，", ",").split(",") if part.strip()]
        selected = [by_code[code] for code in codes if code in by_code]
        missing = [code for code in codes if code not in by_code]
        return selected, {"source": "自訂股票清單", "status": "covered", "candidate_count": len(selected), "requested_codes": codes, "missing_codes": missing}

    if pool in {"全市場初篩", "全市場", "all"}:
        return all_universe, {"source": "全市場初篩", "status": "covered", "candidate_count": len(all_universe)}

    if pool.startswith("最近掃描"):
        scan_id = None
        parts = pool.split(maxsplit=1)
        if len(parts) > 1:
            scan_id = parts[1].strip()
        record = find_recent_scan(scan_id)
        if not record:
            return [], {"source": "最近掃描結果", "status": "missing", "note": "目前尚未找到已保存的最近掃描結果，請先執行 /scan 或改用精選選股名單。"}
        codes = [str(code) for code in record.get("codes") or []]
        selected = [by_code[code] for code in codes if code in by_code]
        return selected, {"source": "最近掃描結果", "status": "covered", "candidate_count": len(selected), "scan_id": record.get("scan_id"), "scan_type": record.get("scan_type"), "requested_codes": codes}

    resolved = resolve_stock_reference(pool)
    if resolved:
        selected = [by_code[resolved.code]] if resolved.code in by_code else []
        return selected, {
            "source": "單一股票",
            "status": "covered" if selected else "missing",
            "candidate_count": len(selected),
            "requested_codes": [resolved.code],
            "missing_codes": [] if selected else [resolved.code],
            "note": "依股票代號或名稱解析為單一股票後進行價值重估。",
        }

    return all_universe, {"source": pool, "status": "fallback_all_universe", "note": "未知候選池，暫以全市場初篩處理。"}


def _load_monitor_codes() -> list[str]:
    path = Path(__file__).resolve().parents[1] / "config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    codes: list[str] = []
    seen: set[str] = set()
    for item in config.get("monitor_stocks", []) or []:
        raw = item.get("symbol") if isinstance(item, dict) else item
        code = str(raw or "").strip().split(".", 1)[0]
        if len(code) == 4 and code.isdigit() and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def _value_scan_should_preserve_early_candidates(universe_policy: dict[str, Any]) -> bool:
    source = str(universe_policy.get("source") or "")
    scan_type = str(universe_policy.get("scan_type") or "")
    return any(token in source for token in ("精選選股", "最近掃描", "選股雷達", "雷達")) or any(
        token in scan_type for token in ("精選", "curated", "Radar", "雷達")
    )


def _value_scan_early_signal_priority(row: dict[str, Any], universe_policy: dict[str, Any]) -> float:
    if not _value_scan_should_preserve_early_candidates(universe_policy):
        return 0.0
    score = 0.0
    components = row.get("score_components") or {}
    yoy = row.get("revenue_yoy")
    volume = row.get("avg_volume_20d")

    if isinstance(yoy, (int, float)):
        if yoy >= 30:
            score += 30
        elif yoy >= 10:
            score += 22
        elif yoy > 0:
            score += 14
    if isinstance(volume, (int, float)):
        if 300 <= volume <= 8000:
            score += 20
        elif volume > 0:
            score += 10
    if float(components.get("theme_label_shift") or 0) > 0:
        score += 20
    if row.get("new_market_label") and row.get("new_market_label") != row.get("old_market_label"):
        score += 10
    if row.get("price") is not None:
        score += 5

    evidence_text = " ".join(str(item) for item in (row.get("rerating_evidence") or []))
    if any(token in evidence_text for token in ("營收", "價量", "重估", "新市場")):
        score += 10
    return round(max(0.0, min(100.0, score)), 2)


def _attach_value_scan_evidence(rows: list[dict[str, Any]], report_date: date | None, progress: Callable[[str], None] | None = None) -> None:
    total = len(rows)
    with StockDataFetcher() as fetcher:
        for index, row in enumerate(rows, 1):
            code = str(row.get("code") or "")
            name = str(row.get("name") or "")
            if progress:
                progress(f"價值重估：官方/公開資料蒐證 {index}/{total} {code} {name}".strip())
            free_sources = build_free_research_sources(code, row.get("symbol"), report_date)
            row["free_public_sources"] = free_sources
            row["valuation_data"] = free_sources.get("valuation", {})
            row["tdcc_data"] = free_sources.get("tdcc", {})
            row["gross_margin_cache"] = free_sources.get("gross_margin_cache", {})
            row["mops_documents"] = free_sources.get("mops_documents", {})
            row["chip_backup_data"] = build_chip_backup_snapshot(code, report_date)
            events = [*build_mops_reference_events(code, report_date), *build_chip_backup_events(code, report_date)]
            row["source_events"] = events
            try:
                meta = fetcher.resolve_stock(code)
                financial_df = fetcher.fetch_quarterly_financials(meta)
                if report_date is not None:
                    financial_df = _filter_quarter_frame(financial_df, "Quarter", report_date)
                row["financial_detail"] = financial_detail_snapshot(_tail_records(financial_df, 4))
            except Exception as exc:
                row["financial_detail"] = {"status": "unavailable", "error": str(exc), "score_points": 0}


def _build_ai_candidate_evidence_pack(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """為每檔 AI 候選股建立結構化證據包，避免 JSON 截斷導致資料漏送。

    每檔候選股包含完整欄位：
    code, name, symbol, industry, price, avg_volume_20d,
    latest_monthly_revenue, revenue_yoy,
    old_market_label, new_market_label,
    rerating_score, verification_score,
    local_rerating_composite_score, tdcc_score, valuation_score,
    rerating_evidence, counter_evidence, score_components, cross_validation,
    financial_detail, gross_margin_cache, chip_backup_summary,
    valuation_data, tdcc_data, mops_documents,
    source_events, company_knowledge, source_coverage,
    missing_data_status
    """
    pack: list[dict[str, Any]] = []
    for row in candidates:
        code = str(row.get("code") or "")
        name = str(row.get("name") or "")

        # 確認哪些關鍵資料缺失（固定欄位，缺資料也要保留 key）
        missing: list[str] = []
        if not row.get("financial_detail") or row["financial_detail"].get("status") == "unavailable":
            missing.append("financial_detail")
        if not row.get("gross_margin_cache"):
            missing.append("gross_margin_cache")
        if not row.get("chip_backup_data"):
            missing.append("chip_backup_data")
        if not row.get("latest_monthly_revenue"):
            missing.append("revenue")
        if not row.get("mops_documents"):
            missing.append("mops_documents")
        if not row.get("source_events"):
            missing.append("source_events")
        company_knowledge = row.get("company_knowledge") or {}
        if not company_knowledge or company_knowledge.get("status") == "missing":
            missing.append("company_knowledge")

        # 籌碼摘要（取最重要欄位，避免完整晶片資料過大）
        chip = row.get("chip_backup_data") or {}
        chip_summary: dict[str, Any] = {}
        if chip:
            chip_summary = {
                "top3_holders": chip.get("top3_holders"),
                "holding_ratio": chip.get("holding_ratio"),
                "total_shares": chip.get("total_shares"),
            }
            if len(str(chip)) > 2000:
                chip_summary["_note"] = "chip_backup_data too large, showing summary only"
                chip_summary["holder_count"] = chip.get("holder_count")
        else:
            chip_summary = {"status": "no data"}

        cross_val = row.get("cross_validation") or {}
        tdcc_score = cross_val.get("tdcc_score") if cross_val.get("tdcc_score") is not None else 0.0
        valuation_score = cross_val.get("valuation_score") if cross_val.get("valuation_score") is not None else 0.0
        rerating_s = row.get("rerating_score") or 0.0
        verification_s = row.get("verification_score") or 0.0
        composite = round(rerating_s * 0.6 + verification_s * 0.25 + tdcc_score * 0.1 + valuation_score * 0.05, 2)
        composite = max(0.0, min(100.0, composite))
        pack.append({
            # 基本識別
            "code": code,
            "name": name,
            "symbol": row.get("symbol"),
            "industry": row.get("industry"),
            # 價量
            "price": row.get("price"),
            "avg_volume_20d": row.get("avg_volume_20d"),
            # 營收
            "latest_monthly_revenue": row.get("latest_monthly_revenue"),
            "revenue_yoy": row.get("revenue_yoy"),
            # 標籤
            "old_market_label": row.get("old_market_label"),
            "new_market_label": row.get("new_market_label"),
            # 分數
            "rerating_score": rerating_s,
            "verification_score": verification_s,
            "early_signal_priority": row.get("early_signal_priority", 0),
            "local_rerating_composite_score": composite,
            "tdcc_score": tdcc_score,
            "valuation_score": valuation_score,
            # 證據
            "rerating_evidence": row.get("rerating_evidence") or [],
            "counter_evidence": row.get("counter_evidence") or [],
            "score_components": row.get("score_components"),
            "cross_validation": cross_val,
            # 財務
            "financial_detail": row.get("financial_detail") or {"status": "unavailable"},
            "gross_margin_cache": row.get("gross_margin_cache") or {},
            # 籌碼
            "chip_backup_summary": chip_summary,
            "valuation_data": row.get("valuation_data") or {},
            "tdcc_data": row.get("tdcc_data") or {},
            "mops_documents": row.get("mops_documents") or {},
            # 事件與知識
            "source_events": row.get("source_events") or [],
            "company_knowledge": row.get("company_knowledge") or {},
            "source_coverage": cross_val.get("source_coverage"),
            # 缺失狀態
            "missing_data_status": missing if missing else None,
        })
    return pack


def _filter_date_frame(frame: pd.DataFrame, column: str, report_date: date) -> pd.DataFrame:
    if frame.empty or column not in frame.columns:
        return frame
    dates = pd.to_datetime(frame[column]).dt.date
    return frame[dates <= report_date].reset_index(drop=True)


def _filter_month_frame(frame: pd.DataFrame, column: str, report_date: date) -> pd.DataFrame:
    if frame.empty or column not in frame.columns:
        return frame
    months = pd.to_datetime(frame[column]).dt.date
    return frame[months <= report_date].reset_index(drop=True)


def _filter_quarter_frame(frame: pd.DataFrame, column: str, report_date: date) -> pd.DataFrame:
    if frame.empty or column not in frame.columns:
        return frame

    def quarter_end(value: object) -> date:
        text = str(value)
        year = int(text[:4])
        q = int(text[-1])
        month = q * 3
        return date(year, month, 28)

    mask = frame[column].map(lambda value: quarter_end(value) <= report_date)
    return frame[mask].reset_index(drop=True)


def _tail_records(frame: pd.DataFrame, count: int) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    clean = frame.tail(count).where(pd.notnull(frame), None)
    return clean.to_dict(orient="records")


def _column_as_series(frame: pd.DataFrame, column: str) -> pd.Series:
    value = frame[column]
    if isinstance(value, pd.DataFrame):
        if value.empty:
            return pd.Series(pd.NA, index=frame.index)
        value = value.iloc[:, 0]
    if isinstance(value, pd.Series):
        return value
    return pd.Series(value, index=frame.index)


def _technical_snapshot(price_df: pd.DataFrame) -> dict[str, Any]:
    if price_df.empty or "Close" not in price_df.columns:
        return {"status": "no price data"}
    source_frame = price_df.copy()
    frame = pd.DataFrame(index=source_frame.index)
    if "Date" in source_frame.columns:
        frame["Date"] = _column_as_series(source_frame, "Date")
    frame["Close"] = pd.to_numeric(_column_as_series(source_frame, "Close"), errors="coerce")
    if "Volume_Lots" in source_frame.columns:
        frame["Volume_Lots"] = _column_as_series(source_frame, "Volume_Lots")
    latest = frame.iloc[-1]
    snapshot = {"latest_close": latest.get("Close"), "latest_date": str(latest.get("Date"))}
    for window in (5, 10, 21, 60):
        if len(frame) >= window:
            snapshot[f"ma{window}"] = float(frame["Close"].tail(window).mean())
            snapshot[f"above_ma{window}"] = bool(float(latest.get("Close")) >= snapshot[f"ma{window}"])
    if "Volume_Lots" in frame.columns and len(frame) >= 20:
        snapshot["avg_volume_20d"] = float(pd.to_numeric(_column_as_series(frame, "Volume_Lots"), errors="coerce").tail(20).mean())
    return snapshot


def _official_sources() -> list:
    return make_source_items(
        [
            {"title": "台灣證券交易所", "url": "https://www.twse.com.tw/"},
            {"title": "櫃買中心", "url": "https://www.tpex.org.tw/"},
            {"title": "公開資訊觀測站", "url": "https://mops.twse.com.tw/"},
        ]
    )


def _forum_query_for_request(request: CommandRequest, data: dict[str, Any]) -> str:
    if request.command == "research":
        stock = data.get("stock") or {}
        return " ".join(part for part in [stock.get("code"), stock.get("name")] if part)
    return str(request.target or request.theme_scope or request.candidate_pool or request.market_scope or "台股")


def _macro_quantitative_context() -> dict[str, Any]:
    context: dict[str, Any] = {"indices": {}, "breadth": {}, "volatility": {"status": "資料不足"}}
    for symbol, label in (("^TWII", "加權指數"), ("^TWOII", "櫃買指數")):
        try:
            history = yf.Ticker(symbol).history(period="90d", interval="1d", auto_adjust=False).dropna(subset=["Close"])
            context["indices"][label] = _index_metrics(history)
        except Exception as exc:
            context["indices"][label] = {"status": f"取得失敗：{exc}"}
    try:
        universe = load_stock_universe(False)
        price_metrics = load_price_metrics(universe)
        prices = [float(metric.get("price")) for metric in price_metrics.values() if metric.get("price") is not None]
        volumes = [float(metric.get("avg_volume_20d")) for metric in price_metrics.values() if metric.get("avg_volume_20d") is not None]
        context["breadth"] = {
            "priced_symbols": len(prices),
            "avg_price": round(sum(prices) / len(prices), 2) if prices else None,
            "avg_volume_20d": round(sum(volumes) / len(volumes), 2) if volumes else None,
        }
    except Exception as exc:
        context["breadth"] = {"status": f"取得失敗：{exc}"}
    return context


def _index_metrics(history: pd.DataFrame) -> dict[str, Any]:
    if history.empty:
        return {"status": "no data"}
    close = pd.to_numeric(_column_as_series(history, "Close"), errors="coerce")
    latest = float(close.iloc[-1])
    result: dict[str, Any] = {"latest_close": latest, "latest_date": str(history.index[-1].date())}
    for window in (5, 10, 21, 60):
        if len(close) >= window:
            ma = float(close.tail(window).mean())
            result[f"ma{window}"] = round(ma, 2)
            result[f"above_ma{window}"] = latest >= ma
    if len(close) >= 21:
        result["twenty_day_return_pct"] = round((latest / float(close.iloc[-21]) - 1) * 100, 2)
    return result


def _market_score(context: dict[str, Any]) -> dict[str, Any]:
    score = 50
    reasons: list[str] = []
    for label, metrics in (context.get("indices") or {}).items():
        for key, points in (("above_ma5", 4), ("above_ma10", 4), ("above_ma21", 6), ("above_ma60", 8)):
            if metrics.get(key) is True:
                score += points
                reasons.append(f"{label} 站上 {key.replace('above_', '').upper()} +{points}")
            elif metrics.get(key) is False:
                score -= max(2, points // 2)
    score = max(0, min(100, score))
    if score >= 80:
        exposure = "70%~90%"
    elif score >= 60:
        exposure = "50%~70%"
    elif score >= 40:
        exposure = "30%~50%"
    elif score >= 20:
        exposure = "10%~30%"
    else:
        exposure = "0%~10%"
    return {"score": score, "suggested_exposure": exposure, "reasons": reasons[:12], "disclaimer": "持股水位為系統風險控管建議，不是絕對投資指令。"}


def _theme_profile(theme: str) -> dict[str, Any]:
    # Build the old profile shape from the formal topic library.
    new_path = ROOT_DIR / "config" / "theme_profiles.json"

    try:
        if new_path.exists():
            profiles = json.loads(new_path.read_text(encoding="utf-8"))
            for p in profiles:
                if p.get("theme_name") == theme or p.get("theme_id") == theme:
                    return {
                        "keywords": p.get("keywords", [theme]),
                        "industries": p.get("industries", []),
                        "supply_chain": [p.get("supply_chain_role", "")],
                        "rerating_labels": ["已分類", theme],
                    }
            for p in profiles:
                name = str(p.get("theme_name") or "")
                theme_id = str(p.get("theme_id") or "")
                if (name and (name in theme or theme in name)) or (theme_id and (theme_id in theme or theme in theme_id)):
                    return {
                        "keywords": p.get("keywords", [theme]),
                        "industries": p.get("industries", []),
                        "supply_chain": [p.get("supply_chain_role", "")],
                        "rerating_labels": ["已分類", theme],
                    }
    except Exception:
        pass

    return {"keywords": [theme], "industries": [], "supply_chain": [], "rerating_labels": ["未分類", theme]}


def load_company_theme_map_data() -> list[dict[str, Any]]:
    """Load company-theme mappings from config/company_theme_map.json."""
    path = ROOT_DIR / "config" / "company_theme_map.json"
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return [{"company_code": code, **mapping} for code, mapping in raw.items()]
    except Exception:
        pass
    return []


def _theme_match_reason(industry: str, keyword_hit: bool, industry_hit: bool) -> str:
    reasons = []
    if keyword_hit:
        reasons.append("名稱/產業關鍵字命中")
    if industry_hit:
        reasons.append(f"產業分類命中：{industry}")
    return "；".join(reasons) or "候選補充"


def _value_rerating_score(industry: str, price: Any, avg_volume: Any, yoy: Any) -> dict[str, Any]:
    components: dict[str, float] = {}
    try:
        yoy_value = float(yoy or 0)
    except (TypeError, ValueError):
        yoy_value = 0.0
    components["revenue_turnaround"] = max(0, min(30, (yoy_value + 20) * 0.3))
    try:
        volume_value = float(avg_volume or 0)
    except (TypeError, ValueError):
        volume_value = 0.0
    components["liquidity_attention"] = 20 if volume_value >= 3000 else 14 if volume_value >= 1000 else 7 if volume_value >= 300 else 0
    try:
        price_value = float(price or 0)
    except (TypeError, ValueError):
        price_value = 0.0
    components["price_zone"] = 12 if 10 <= price_value <= 120 else 5
    theme_bonus, new_label = _industry_rerating_label(industry)
    components["theme_label_shift"] = theme_bonus
    score = round(max(0, min(100, 25 + sum(components.values()))), 2)
    evidence = []
    if yoy_value > 20:
        evidence.append(f"月營收 YoY {yoy_value:.1f}% 顯示成長動能")
    if volume_value >= 1000:
        evidence.append(f"20 日均量 {volume_value:.0f} 張，具市場關注度")
    if theme_bonus:
        evidence.append(f"產業分類 {industry} 具題材重估可能")
    return {
        "score": score,
        "components": components,
        "old_market_label": f"{industry or '未分類'} / 傳統分類",
        "new_market_label": new_label,
        "evidence": evidence or ["目前主要依本地價量與營收資料做保守評估"],
    }


def _industry_rerating_label(industry: str) -> tuple[float, str]:
    return rerating_label_for_industry(industry)


def _value_scan_rules() -> dict[str, Any]:
    return {
        "score_max": 100,
        "components": {
            "base": 25,
            "revenue_turnaround": "YoY 改善與成長，最高 30",
            "liquidity_attention": "20 日均量代表市場關注，最高 20",
            "price_zone": "可交易價格區間，最高 12",
            "theme_label_shift": "產業題材可能帶來標籤重估，最高 18",
        },
        "risk_control": "若缺少產品、客戶、公告、法人報告或財報細項證據，AI 報告不得只因分數高就給強結論。",
    }
