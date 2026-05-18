"""價值重估底稿服務 - 供 /research --deep 與 /value_scan 共用。"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from stock_scanner import load_price_metrics, load_recent_revenue_history, load_stock_universe

from .chip_sources import build_chip_backup_events, build_chip_backup_snapshot
from .free_sources import build_free_research_sources
from .knowledge_base import enrich_company_rows
from .mops_sources import build_mops_reference_events, financial_detail_snapshot
from .price_fallbacks import load_price_metrics_with_fallback
from .value_validation import build_value_cross_validation


def build_rerating_snapshot_for_stock(
    stock_id: str,
    as_of_date: date | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    為單一股票建立價值重估底稿。

    回傳欄位：
    - stock_id, stock_name, symbol, industry
    - as_of_date
    - rerating_score, verification_score, tdcc_score, valuation_score
    - local_rerating_composite_score
    - old_market_label, new_market_label
    - rerating_evidence, counter_evidence
    - data_gaps
    - theme_freshness, source_coverage

    此服務不呼叫 AI，純本地計算。
    """
    data_gaps: list[str] = []
    evidence: list[str] = []
    counter_evidence: list[str] = []

    all_universe = load_stock_universe(False)
    by_code = {entry.code: entry for entry in all_universe}
    entry = by_code.get(stock_id)

    if not entry:
        return _empty_snapshot(stock_id, as_of_date, f"股票代號 {stock_id} 不在股票宇宙中")

    revenue_history = load_recent_revenue_history([entry])
    price_metrics, price_policy = load_price_metrics_with_fallback([entry], progress=progress)

    metric = price_metrics.get(entry.symbol) or {}
    revenue_points = revenue_history.get(entry.code) or []
    latest_revenue = revenue_points[0] if revenue_points else None

    price = metric.get("price")
    avg_volume = metric.get("avg_volume_20d")

    if price is None:
        data_gaps.append("價量資料缺漏")
    if latest_revenue is None:
        data_gaps.append("最近月營收資料缺漏")
    if latest_revenue and latest_revenue.yoy is None:
        data_gaps.append("月營收 YoY 資料缺漏")

    score_detail = _rerating_score(entry.industry, price, avg_volume, latest_revenue.yoy if latest_revenue else None)
    rerating_score = score_detail["score"]

    rerating_evidence = list(score_detail.get("evidence", []))
    if price is None:
        rerating_evidence.append("價量資料缺漏，保守處理")
    if latest_revenue is None:
        rerating_evidence.append("最近月營收資料缺漏，保守處理")

    tdcc_score = 0.0
    valuation_score = 0.0
    valuation_data: dict[str, Any] = {}
    tdcc_data: dict[str, Any] = {}
    gross_margin_cache: dict[str, Any] = {}
    mops_documents: dict[str, Any] = {}
    financial_detail: dict[str, Any] = {}

    try:
        free_sources = build_free_research_sources(entry.code, entry.symbol, as_of_date)
        valuation_data = free_sources.get("valuation", {})
        tdcc_data = free_sources.get("tdcc", {})
        gross_margin_cache = free_sources.get("gross_margin_cache", {})
        mops_documents = free_sources.get("mops_documents", {})
        tdcc_score = float(valuation_data.get("tdcc_score", 0) or 0)
        valuation_score = float(valuation_data.get("valuation_score", 0) or 0)
    except Exception:
        data_gaps.append("公開來源（TWSE/TPEx/TDCC/MOPS）取得失敗")

    try:
        chip_backup = build_chip_backup_snapshot(entry.code, as_of_date)
        events = [*build_mops_reference_events(entry.code, as_of_date), *build_chip_backup_events(entry.code, as_of_date)]
    except Exception:
        chip_backup = {}
        events = []
        data_gaps.append("法人/籌碼備用資料取得失敗")

    # 延遲導入避免循環
    from data_fetcher import StockDataFetcher

    try:
        with StockDataFetcher() as fetcher:
            meta = fetcher.resolve_stock(entry.code)
            financial_df = fetcher.fetch_quarterly_financials(meta)
            if as_of_date is not None:
                financial_df = _filter_quarter_frame(financial_df, "Quarter", as_of_date)
            financial_detail = financial_detail_snapshot(_tail_records(financial_df, 4))
    except Exception as exc:
        financial_detail = {"status": "unavailable", "error": str(exc), "score_points": 0}
        data_gaps.append("季度財報資料取得失敗")

    # 交叉驗證（用於 verification_score）
    temp_row = {
        "code": entry.code,
        "name": entry.name,
        "symbol": entry.symbol,
        "industry": entry.industry,
        "price": price,
        "avg_volume_20d": avg_volume,
        "latest_monthly_revenue": latest_revenue.revenue if latest_revenue else None,
        "revenue_yoy": latest_revenue.yoy if latest_revenue else None,
        "old_market_label": score_detail["old_market_label"],
        "new_market_label": score_detail["new_market_label"],
        "rerating_evidence": rerating_evidence,
        "score_components": score_detail["components"],
        "rerating_score": rerating_score,
        "source_events": events,
    }
    cross_validation = build_value_cross_validation(temp_row, events)
    verification_score = cross_validation.get("verification_score", 0.0)

    # 反證：verification 分數低時視為反證
    if verification_score < 40:
        counter_evidence.append(f"交叉驗證分數偏低（{verification_score}），需要更多官方資料確認")
    if not events:
        counter_evidence.append("官方/公開來源事件空白，支撐證據不足")
    if financial_detail.get("status") == "unavailable":
        counter_evidence.append("季度財報資料無法取得，財務體質無法完全確認")

    # 主題與來源覆蓋度
    theme_freshness = "unknown"
    source_coverage = {
        "price": price is not None,
        "revenue": latest_revenue is not None,
        "tdcc": bool(tdcc_data),
        "valuation": bool(valuation_data),
        "mops_documents": bool(mops_documents),
        "chip_backup": bool(chip_backup),
        "financial_detail": financial_detail.get("status") != "unavailable",
    }

    # 本地重估複合分數（60/25/10/5 公式，在 verification_score 算出之後）
    local_rerating_composite = round(
        max(
            0,
            min(
                100,
                rerating_score * 0.6
                + verification_score * 0.25
                + tdcc_score * 0.1
                + valuation_score * 0.05,
            ),
        ),
        2,
    )

    return {
        "stock_id": entry.code,
        "stock_name": entry.name,
        "symbol": entry.symbol,
        "industry": entry.industry,
        "as_of_date": as_of_date.isoformat() if as_of_date else None,
        "rerating_score": rerating_score,
        "verification_score": round(verification_score, 2),
        "tdcc_score": round(tdcc_score, 2),
        "valuation_score": round(valuation_score, 2),
        "local_rerating_composite_score": round(local_rerating_composite, 2),
        "old_market_label": score_detail["old_market_label"],
        "new_market_label": score_detail["new_market_label"],
        "rerating_evidence": rerating_evidence,
        "counter_evidence": counter_evidence,
        "data_gaps": data_gaps,
        "theme_freshness": theme_freshness,
        "source_coverage": source_coverage,
        "local_rerating_components": score_detail["components"],
        "cross_validation": cross_validation,
        "financial_detail": financial_detail,
    }


def _rerating_score(industry: str, price: Any, avg_volume: Any, yoy: Any) -> dict[str, Any]:
    """復用 data_services.py 的本地重估評分邏輯。"""
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
    """復用 data_services.py 的產業標籤重估映射。"""
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


def _filter_quarter_frame(frame, column: str, report_date: date):
    """復用 data_services.py 的季度篩選邏輯。"""
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


def _tail_records(frame, count: int) -> list[dict[str, Any]]:
    """復用 data_services.py 的取末筆記錄。"""
    if frame.empty:
        return []
    clean = frame.tail(count).where(frame.notnull(), None)
    return clean.to_dict(orient="records")


def _empty_snapshot(stock_id: str, as_of_date: date | None, note: str) -> dict[str, Any]:
    """資料不足時的回傳格式。"""
    return {
        "stock_id": stock_id,
        "stock_name": None,
        "symbol": None,
        "industry": None,
        "as_of_date": as_of_date.isoformat() if as_of_date else None,
        "rerating_score": 0.0,
        "verification_score": 0.0,
        "tdcc_score": 0.0,
        "valuation_score": 0.0,
        "local_rerating_composite_score": 0.0,
        "old_market_label": "未知",
        "new_market_label": "未知",
        "rerating_evidence": [],
        "counter_evidence": [note],
        "data_gaps": [note],
        "theme_freshness": "unknown",
        "source_coverage": {},
        "local_rerating_components": {},
        "cross_validation": {},
        "financial_detail": {},
    }
