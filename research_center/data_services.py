from __future__ import annotations

import json
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
from .forum_service import fetch_forum_sources
from .free_sources import build_free_macro_sources, build_free_research_sources
from .knowledge_base import enrich_company_rows, theme_knowledge_summary
from .macro_indicators import build_macro_indicators
from .mops_sources import build_mops_reference_events, financial_detail_snapshot
from .price_fallbacks import load_price_metrics_with_fallback
from .recent_scans import find_recent_scan, save_recent_scan_result
from .value_validation import build_value_cross_validation
from .rerating_snapshot_service import build_rerating_snapshot_for_stock
from .models import CommandRequest
from .source_rank import make_source_items
from .structured_cache import load_research_structured_cache, save_research_structured_cache

ROOT_DIR = Path(__file__).resolve().parents[1]


def collect_structured_data(request: CommandRequest, progress: Callable[[str], None] | None = None) -> tuple[dict[str, Any], list]:
    if request.command == "research":
        data = collect_research_data(request, progress=progress)
    elif request.command == "macro":
        data = collect_macro_data(request, progress=progress)
    elif request.command == "theme":
        data = collect_theme_data(request, progress=progress)
    elif request.command == "value_scan":
        data = collect_value_scan_data(request, progress=progress)
    else:
        return {"message": "report lookup does not collect structured market data"}, []

    sources = _official_sources()
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


def collect_research_data(request: CommandRequest, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
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
    if request.mode in ("deep", "score"):
        if progress:
            progress(f"個股研究（{request.mode}）：建立價值重估底稿")
        result["local_rerating_snapshot"] = build_rerating_snapshot_for_stock(
            meta.code, report_date, progress=progress
        )

    try:
        save_research_structured_cache(meta.code, cache_date, result)
    except Exception:
        pass

    return result


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
    return {
        "theme": theme,
        "report_date": request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat(),
        "supply_chain_profile": profile,
        "company_knowledge_summary": theme_knowledge_summary(matched),
        "matched_universe": matched,
        "matched_companies": matched,  # alias for backward compatibility with prompt/discovery
        "notes": ["Theme 第三版加入 config/company_knowledge.json 公司產品、客戶、營收占比與供應鏈角色資料庫；未覆蓋公司會標示待驗證。"],
    }

def collect_value_scan_data(request: CommandRequest, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    # 單檔模式（/value_scan 6217）
    if request.target_type == "stock" and request.target:
        if progress:
            progress(f"價值重估：單檔模式 {request.target}")
        snapshot = build_rerating_snapshot_for_stock(request.target, request.report_date, progress=progress)
        return {
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
        rows.append(
            {
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
        )

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
    rows.sort(key=lambda row: (row["rerating_score"], row["verification_score"]), reverse=True)
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

    return {
        "candidate_pool": request.candidate_pool or "精選選股",
        "candidate_source_policy": universe_policy,
        "price_data_policy": price_policy,
        "report_date": request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat(),
        "top_n": request.top or ai_candidate_limit or 10,
        "total_candidate_count": len(rows),
        "ai_candidate_limit": ai_candidate_limit,
        "ai_candidates": ai_candidates,
        "ai_candidate_evidence_pack": ai_candidate_evidence_pack,
        "local_ranking": [{"code": r["code"], "name": r["name"], "rerating_score": r["rerating_score"]} for r in rows],
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

    return all_universe, {"source": pool, "status": "fallback_all_universe", "note": "未知候選池，暫以全市場初篩處理。"}
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
        if not row.get("company_knowledge"):
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


def _technical_snapshot(price_df: pd.DataFrame) -> dict[str, Any]:
    if price_df.empty or "Close" not in price_df.columns:
        return {"status": "no price data"}
    frame = price_df.copy()
    frame["Close"] = pd.to_numeric(frame["Close"], errors="coerce")
    latest = frame.iloc[-1]
    snapshot = {"latest_close": latest.get("Close"), "latest_date": str(latest.get("Date"))}
    for window in (5, 10, 21, 60):
        if len(frame) >= window:
            snapshot[f"ma{window}"] = float(frame["Close"].tail(window).mean())
            snapshot[f"above_ma{window}"] = bool(float(latest.get("Close")) >= snapshot[f"ma{window}"])
    if "Volume_Lots" in frame.columns and len(frame) >= 20:
        snapshot["avg_volume_20d"] = float(pd.to_numeric(frame["Volume_Lots"], errors="coerce").tail(20).mean())
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
    close = pd.to_numeric(history["Close"], errors="coerce")
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
    path = ROOT_DIR / "config" / "theme_supply_chain.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        data = {}
    if theme in data:
        return data[theme]
    for key, profile in data.items():
        if key in theme or theme in key:
            return profile
    return {"keywords": [theme], "industries": [], "supply_chain": [], "rerating_labels": ["未分類", theme]}


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
    mapping = {
        "半導體": (18, "AI/HPC/先進封裝供應鏈"),
        "電子零組件": (16, "AI 伺服器零組件/高速傳輸"),
        "電腦及週邊設備": (14, "AI 伺服器/邊緣運算設備"),
        "電機機械": (14, "重電/自動化/機器人供應鏈"),
        "電器電纜": (12, "電網升級/能源基建"),
        "通信網路": (12, "高速網通/資料中心網路"),
    }
    for key, value in mapping.items():
        if key in industry:
            return value
    return 6, f"{industry or '未分類'} / 待驗證新題材"


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


















