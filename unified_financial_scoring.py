"""Pure v3 revenue and financial scoring for the shared 100-point Radar core.

The module intentionally has no network or cache side effects.  Data fetching
stays in the existing services; this file only normalises structured facts and
applies the approved merged rules.
"""

from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
from statistics import median
from typing import Any


FINANCIAL_INDUSTRIES = {"金融保險", "金融業", "保險業", "證券期貨業", "金控業"}
RULE_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "financial_course_rules.json"


def _load_parameters() -> dict[str, Any]:
    try:
        payload = json.loads(RULE_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    value = payload.get("parameters") if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else {}


PARAMETERS = _load_parameters()
REVENUE_PARAMETERS = PARAMETERS.get("revenue") or {}
PROFITABILITY_PARAMETERS = PARAMETERS.get("profitability") or {}
CONTRACT_PARAMETERS = PARAMETERS.get("contract_liabilities") or {}
LIQUIDITY_PARAMETERS = PARAMETERS.get("liquidity") or {}
PRICE_PARAMETERS = PARAMETERS.get("price") or {}
UNIFIED_PARAMETERS = PARAMETERS.get("unified_scoring") or {}
UNIFIED_REVENUE = UNIFIED_PARAMETERS.get("revenue") or {}
UNIFIED_FINANCIAL = UNIFIED_PARAMETERS.get("financial") or {}

REVENUE_RULE_CODES = {
    "最新有效營收 YoY 大於 50%": "REV_LATEST_YOY_TOP",
    "最新有效營收 YoY 30% 以上": "REV_LATEST_YOY_HIGH",
    "最新有效營收 YoY 10% 以上": "REV_LATEST_YOY_POSITIVE",
    "G1：最近四個有效營收 YoY 皆達 1%": "REV_PERSISTENCE_G1",
    "最近五個有效營收 YoY 至少四個為正": "REV_PERSISTENCE_4_OF_5",
    "G2：四期至少兩期達 1%，且全數高於 -15%": "REV_PERSISTENCE_G2",
    "最近三個有效營收 YoY 至少兩個為正": "REV_PERSISTENCE_2_OF_3",
    "營收 YoY 連續三個有效觀察值加速": "REV_ACCELERATION_3",
    "營收 YoY 由負轉正": "REV_TURN_POSITIVE",
    "營收 YoY 連續兩個有效觀察值改善": "REV_ACCELERATION_2",
    "最新營收創 12 個有效月份新高": "REV_NEW_HIGH_12M",
    "最新營收創 6 個有效月份新高": "REV_NEW_HIGH_6M",
    "最新營收高於公司自身同月份季節基準": "REV_SEASONAL_SURPRISE",
    "經季節調整 MoM 為正": "REV_SEASONAL_ADJUSTED_MOM",
    "歷史不足 24 月，僅採原始 MoM 正成長 1 分": "REV_RAW_MOM_FALLBACK",
    "正式產業同業營收正成長廣度達 60%": "REV_PEER_BREADTH",
    "營收公布前 20 日超額報酬未逾 10%": "REV_PREANNOUNCEMENT_PRICE_FULL",
    "營收公布前 20 日超額報酬介於 10% 至 20%": "REV_PREANNOUNCEMENT_PRICE_PARTIAL",
    "最新營收 YoY 較前三期平均減速至少 10 個百分點": "REV_DECELERATION_PENALTY",
    "高 YoY 前多期非正成長，可能為單月跳升": "REV_ONE_MONTH_SPIKE_PENALTY",
    "高 YoY 但經季節調整 MoM 低於 -30%": "REV_MOM_COLLAPSE_PENALTY",
}

FINANCIAL_RULE_CODES = {
    "最近三季毛利率連續上升": "FIN_GROSS_MARGIN_RISING",
    "最新毛利率改善或由負轉正": "FIN_GROSS_MARGIN_IMPROVING",
    "最新毛利率為正但未改善": "FIN_GROSS_MARGIN_POSITIVE",
    "營益率由負轉正或連續三季改善": "FIN_OPERATING_MARGIN_TURN",
    "最近兩季營益率改善或虧損收斂": "FIN_OPERATING_MARGIN_IMPROVING",
    "最新營益率為正但未改善": "FIN_OPERATING_MARGIN_POSITIVE",
    "近四季平均營益率至少 15%": "FIN_OPERATING_MARGIN_LEVEL_HIGH",
    "近四季平均營益率至少 10%": "FIN_OPERATING_MARGIN_LEVEL_MID",
    "EPS 由負轉正或連續三季改善": "FIN_EPS_TURN",
    "EPS 連續兩季改善或虧損收斂至少 50%": "FIN_EPS_IMPROVING",
    "淨利改善且業外占比低於 30%": "FIN_NET_INCOME_QUALITY",
    "TTM 營業現金流為正": "FIN_OCF_POSITIVE",
    "最近六季自由現金流皆為正": "FIN_FCF_6Q_POSITIVE",
    "TTM 自由現金流為正": "FIN_FCF_TTM_POSITIVE",
    "合約負債占股本至少 5%": "FIN_CONTRACT_LIABILITY_SIGNIFICANT",
    "合約負債連續三季上升": "FIN_CONTRACT_LIABILITY_RISING",
    "歷史合約負債上升事件至少 60% 在一至三季內轉為營收年增": "FIN_CONTRACT_LIABILITY_LEAD",
    "存貨下降、周轉改善且營收與毛利未惡化": "FIN_INVENTORY_HEALTHY",
    "存貨增加但周轉、合約負債與營收同步健康": "FIN_INVENTORY_SUPPORTED",
    "存貨周轉未惡化且存貨占 TTM 營收比未上升": "FIN_INVENTORY_EFFICIENT",
    "流動與速動比率皆至少 200%": "FIN_LIQUIDITY_STRONG",
    "流動與速動比率皆至少 100%": "FIN_LIQUIDITY_ACCEPTABLE",
    "負債比率未高於去年同期": "FIN_DEBT_RATIO_STABLE",
    "估值位於公司自身歷史第 60 百分位以下": "FIN_VALUATION_OWN_HISTORY",
    "估值不高於正式同業比較門檻": "FIN_VALUATION_PEERS",
    "最新毛利率季減至少 5 個百分點": "FIN_GROSS_MARGIN_DROP_PENALTY",
    "業外占稅前淨利至少 30%": "FIN_NON_OPERATING_PENALTY",
    "TTM EPS 為正但 TTM 營業現金流為負": "FIN_EARNINGS_CASH_DIVERGENCE",
    "單季營收年增但單季毛利未年增": "FIN_REVENUE_GROSS_PROFIT_DIVERGENCE",
    "存貨增加、周轉下降、合約負債與毛利同步轉弱": "FIN_INVENTORY_BACKLOG_PENALTY",
}


def _configured(mapping: dict[str, Any], key: str, default: float) -> float:
    value = mapping.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def score_unified_revenue(item: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    raw_rows = _normalise_revenue_rows((snapshot.get("revenue") or {}).get("history") or getattr(item, "revenue_history", []))
    if not raw_rows:
        return _detail(0, 20, [], ["營收資料缺漏"], {"component": "revenue", "row_count": 0, "scoring_version": "v3", "as_of_timestamp": snapshot.get("analysis_date"), "data_status": "unknown"})

    rows = effective_revenue_rows(raw_rows)
    if not rows:
        return _detail(0, 20, [], ["營收有效觀察值不足"], {"component": "revenue", "row_count": len(raw_rows), "scoring_version": "v3", "as_of_timestamp": snapshot.get("analysis_date"), "data_status": "insufficient"})

    reasons: list[str] = []
    risks: list[str] = []
    latest = rows[-1]
    yoy_values = [_value(row, "yoy", "YoY", "YoY%", "revenue_yoy") for row in rows]
    revenues = [_value(row, "revenue", "Monthly_Revenue", "monthly_revenue") for row in rows]
    latest_yoy = yoy_values[-1]
    score = 0.0
    latest_top = _configured(UNIFIED_REVENUE, "latest_yoy_top_pct", 50)
    latest_high = _configured(UNIFIED_REVENUE, "latest_yoy_high_pct", 30)
    latest_positive = _configured(UNIFIED_REVENUE, "latest_yoy_positive_pct", 10)
    seasonal_full_history = int(_configured(UNIFIED_REVENUE, "seasonal_full_history_months", 24))
    long_high_window = int(_configured(UNIFIED_REVENUE, "new_high_long_months", 12))
    short_high_window = int(_configured(UNIFIED_REVENUE, "new_high_short_months", 6))

    # Latest adjusted YoY: 4 points.
    if latest_yoy is not None:
        if latest_yoy > latest_top:
            score += 4
            reasons.append("最新有效營收 YoY 大於 50%")
        elif latest_yoy >= latest_high:
            score += 3
            reasons.append("最新有效營收 YoY 30% 以上")
        elif latest_yoy >= latest_positive:
            score += 2
            reasons.append("最新有效營收 YoY 10% 以上")

    # Persistence: mutually exclusive, 4 points.
    latest4 = [value for value in yoy_values[-4:] if value is not None]
    latest5 = [value for value in yoy_values[-5:] if value is not None]
    latest3 = [value for value in yoy_values[-3:] if value is not None]
    group = _revenue_group(latest4)
    persistence = 0.0
    if group == "group_1":
        persistence = 4
        reasons.append("G1：最近四個有效營收 YoY 皆達 1%")
    elif len(latest5) == 5 and sum(value > 0 for value in latest5) >= 4:
        persistence = 3
        reasons.append("最近五個有效營收 YoY 至少四個為正")
    elif group == "group_2":
        persistence = 2
        reasons.append("G2：四期至少兩期達 1%，且全數高於 -15%")
    elif len(latest3) == 3 and sum(value > 0 for value in latest3) >= 2:
        persistence = 1
        reasons.append("最近三個有效營收 YoY 至少兩個為正")
    score += persistence

    # Turnaround / acceleration: mutually exclusive, 3 points.
    acceleration = _consecutive_increases(yoy_values)
    if acceleration >= 3:
        score += 3
        reasons.append("營收 YoY 連續三個有效觀察值加速")
    elif len(yoy_values) >= 2 and yoy_values[-2] is not None and latest_yoy is not None and yoy_values[-2] < 0 <= latest_yoy:
        score += 2
        reasons.append("營收 YoY 由負轉正")
    elif acceleration >= 2:
        score += 2
        reasons.append("營收 YoY 連續兩個有效觀察值改善")

    # New highs: 2 points.
    present_revenues = [(index, value) for index, value in enumerate(revenues) if value is not None]
    if present_revenues and revenues[-1] is not None:
        if len([value for value in revenues[-long_high_window:] if value is not None]) == long_high_window and revenues[-1] >= max(value for value in revenues[-long_high_window:] if value is not None):
            score += 2
            reasons.append("最新營收創 12 個有效月份新高")
        elif len([value for value in revenues[-short_high_window:] if value is not None]) == short_high_window and revenues[-1] >= max(value for value in revenues[-short_high_window:] if value is not None):
            score += 1
            reasons.append("最新營收創 6 個有效月份新高")

    # Seasonality / MoM: 3 points.
    seasonal_meta = _seasonal_revenue_context(raw_rows, latest)
    if len(raw_rows) >= seasonal_full_history:
        if seasonal_meta.get("positive_surprise"):
            score += 2
            reasons.append("最新營收高於公司自身同月份季節基準")
        if (seasonal_meta.get("adjusted_mom_pct") or 0) > 0:
            score += 1
            reasons.append("經季節調整 MoM 為正")
    elif (seasonal_meta.get("raw_mom_pct") or 0) > 0:
        score += 1
        reasons.append("歷史不足 24 月，僅採原始 MoM 正成長 1 分")

    # Official-industry peer breadth: 2 points.
    peer = (snapshot.get("revenue") or {}).get("peer_context") or {}
    peer_min_count = int(REVENUE_PARAMETERS.get("peer_min_count") or 3)
    peer_breadth_ratio = float(REVENUE_PARAMETERS.get("peer_breadth_ratio") or 0.6)
    if int(peer.get("valid_peer_count") or 0) >= peer_min_count and _num(peer.get("positive_ratio")) is not None and float(peer["positive_ratio"]) >= peer_breadth_ratio:
        score += 2
        reasons.append("正式產業同業營收正成長廣度達 60%")

    # Event price reflection: only exact/trusted event timestamps are eligible.
    price_context = (snapshot.get("revenue") or {}).get("preannouncement_price") or {}
    excess_return = _num(price_context.get("excess_return_20d_pct"))
    price_signal_eligible = bool(
        latest_yoy is not None
        and (
            latest_yoy >= latest_positive
            or (len(yoy_values) >= 2 and yoy_values[-2] is not None and yoy_values[-2] < 0 <= latest_yoy)
            or (latest_yoy > 0 and acceleration >= 2)
        )
    )
    if price_signal_eligible and price_context.get("status") == "ok" and excess_return is not None:
        if excess_return <= _configured(UNIFIED_REVENUE, "price_full_point_max_pct", 10):
            score += 2
            reasons.append("營收公布前 20 日超額報酬未逾 10%")
        elif excess_return <= _configured(UNIFIED_REVENUE, "price_partial_point_max_pct", 20):
            score += 1
            reasons.append("營收公布前 20 日超額報酬介於 10% 至 20%")
        if excess_return > float(PRICE_PARAMETERS.get("preannouncement_runup_warning_pct") or 30):
            risks.append("營收利多公布前股價可能已大幅反映")
    elif price_signal_eligible:
        risks.append("缺少可信的逐公司營收公布時間，公告前股價不加分")

    # Revenue deductions, capped at four points.
    deductions = 0.0
    prior3 = [value for value in yoy_values[-4:-1] if value is not None]
    deceleration_warning = float(REVENUE_PARAMETERS.get("deceleration_warning_pp") or 10)
    if latest_yoy is not None and len(prior3) == 3 and latest_yoy <= sum(prior3) / 3 - deceleration_warning:
        deductions += 3
        risks.append("最新營收 YoY 較前三期平均減速至少 10 個百分點")
    previous4 = [value for value in yoy_values[-5:-1] if value is not None]
    if latest_yoy is not None and latest_yoy > _configured(UNIFIED_REVENUE, "high_yoy_one_off_pct", 50) and len(previous4) == 4 and sum(value <= 0 for value in previous4) >= 3:
        deductions += 2
        risks.append("高 YoY 前多期非正成長，可能為單月跳升")
    adjusted_mom = _num(seasonal_meta.get("adjusted_mom_pct"))
    if latest_yoy is not None and latest_yoy > _configured(UNIFIED_REVENUE, "high_yoy_mom_warning_pct", 30) and adjusted_mom is not None and adjusted_mom < _configured(UNIFIED_REVENUE, "adjusted_mom_warning_pct", -30):
        deductions += 2
        risks.append("高 YoY 但經季節調整 MoM 低於 -30%")
    deductions = min(_configured(UNIFIED_REVENUE, "deduction_cap", 4), deductions)
    score -= deductions

    history_cap = (
        _configured(UNIFIED_REVENUE, "history_cap_full", 20)
        if len(raw_rows) >= seasonal_full_history
        else _configured(UNIFIED_REVENUE, "history_cap_12_to_23", 18)
        if len(raw_rows) >= 12
        else _configured(UNIFIED_REVENUE, "history_cap_4_to_11", 14)
        if len(raw_rows) >= 4
        else 0
    )
    score = min(score, history_cap)
    return _detail(
        score,
        20,
        reasons,
        risks,
        {
            "component": "revenue",
            "scoring_version": "v3",
            "as_of_timestamp": snapshot.get("analysis_date"),
            "data_status": "covered",
            "raw_row_count": len(raw_rows),
            "effective_row_count": len(rows),
            "latest": latest,
            "latest_yoy": latest_yoy,
            "revenue_group": group,
            "seasonality": seasonal_meta,
            "peer_context": peer,
            "preannouncement_price": price_context,
            "deductions": deductions,
            "data_cap": history_cap,
        },
    )


def score_unified_financial(item: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    financial = snapshot.get("financial") or {}
    rows = _normalise_financial_rows(financial.get("financial_data"))
    industry = str(getattr(item, "industry", "") or (snapshot.get("stock") or {}).get("industry") or "")
    if industry in FINANCIAL_INDUSTRIES:
        return _detail(
            0,
            15,
            [],
            ["金融業不適用一般企業財報整併規則"],
            {"component": "financial", "scoring_version": "v3", "as_of_timestamp": snapshot.get("analysis_date"), "applicability": "rule_scope_not_applicable", "industry": industry, "data_status": "not_applicable"},
        )
    if not rows:
        return _detail(0, 15, [], ["財報資料缺漏，不跨構面補暫定分"], {"component": "financial", "scoring_version": "v3", "as_of_timestamp": snapshot.get("analysis_date"), "row_count": 0, "data_status": "unknown"})

    reasons: list[str] = []
    risks: list[str] = []
    latest = rows[-1]
    previous = rows[-2] if len(rows) >= 2 else {}
    gross_margins = [_value(row, "Gross_Margin", "gross_margin") for row in rows]
    operating_margins = [_value(row, "Operating_Margin", "operating_margin") for row in rows]
    eps_values = [_value(row, "EPS", "eps") for row in rows]
    net_income_values = [_value(row, "Net_Income", "net_income") for row in rows]
    revenue_values = [_value(row, "Revenue", "revenue") for row in rows]
    gross_profit_values = [_value(row, "Gross_Profit", "gross_profit") for row in rows]
    ocf_values = [_value(row, "Operating_Cash_Flow", "operating_cash_flow") for row in rows]
    fcf_values = [_value(row, "Free_Cash_Flow", "free_cash_flow") for row in rows]

    profitability_score = 0.0
    profitability_deductions = 0.0

    # Gross margin: 2 points.
    gm_score = 0.0
    gm_present = [value for value in gross_margins if value is not None]
    gm_latest = gross_margins[-1]
    gm_previous = gross_margins[-2] if len(gross_margins) >= 2 else None
    if len(gm_present) >= 3 and gm_present[-3] < gm_present[-2] < gm_present[-1]:
        gm_score = 2
        reasons.append("最近三季毛利率連續上升")
    elif gm_latest is not None and gm_previous is not None and (gm_latest > gm_previous or gm_previous < 0 <= gm_latest):
        gm_score = 1
        reasons.append("最新毛利率改善或由負轉正")
    elif gm_latest is not None and gm_latest > 0:
        gm_score = 0.5
        reasons.append("最新毛利率為正但未改善")
    if gm_latest is not None and gm_previous is not None and gm_latest <= gm_previous - _configured(UNIFIED_FINANCIAL, "gross_margin_drop_warning_pp", 5):
        profitability_deductions += 1
        risks.append("最新毛利率季減至少 5 個百分點")
    profitability_score += min(2.0, gm_score)

    # Operating margin: 2 points including absolute-level bonus.
    om_score = 0.0
    om_present = [value for value in operating_margins if value is not None]
    om_latest = operating_margins[-1]
    om_previous = operating_margins[-2] if len(operating_margins) >= 2 else None
    if om_latest is not None and om_previous is not None and (om_previous < 0 <= om_latest or (len(om_present) >= 3 and om_present[-3] < om_present[-2] < om_present[-1] and om_latest > 0)):
        om_score = 2
        reasons.append("營益率由負轉正或連續三季改善")
    elif om_latest is not None and om_previous is not None and om_latest > om_previous:
        om_score = 1
        reasons.append("最近兩季營益率改善或虧損收斂")
    elif om_latest is not None and om_latest > 0:
        om_score = 0.5
        reasons.append("最新營益率為正但未改善")
    recent_om = [value for value in operating_margins[-4:] if value is not None]
    if len(recent_om) == 4:
        avg_om = sum(recent_om) / 4
        if avg_om >= _configured(UNIFIED_FINANCIAL, "operating_margin_high_pct", 15):
            om_score += 0.5
            reasons.append("近四季平均營益率至少 15%")
        elif avg_om >= _configured(UNIFIED_FINANCIAL, "operating_margin_mid_pct", 10):
            om_score += 0.25
            reasons.append("近四季平均營益率至少 10%")
    profitability_score += min(2.0, om_score)

    # EPS: 2 points.
    ttm_eps = _sum_last(eps_values, 4)
    eps_score = 0.0
    if ttm_eps is not None:
        if ttm_eps > _configured(UNIFIED_FINANCIAL, "ttm_eps_top", 5):
            eps_score += 1
        elif ttm_eps >= _configured(UNIFIED_FINANCIAL, "ttm_eps_high", 3):
            eps_score += 0.75
        elif ttm_eps >= _configured(UNIFIED_FINANCIAL, "ttm_eps_mid", 1):
            eps_score += 0.5
        elif ttm_eps > 0:
            eps_score += 0.25
    eps_increase = _consecutive_increases(eps_values)
    eps_latest = eps_values[-1]
    eps_previous = eps_values[-2] if len(eps_values) >= 2 else None
    if eps_latest is not None and eps_previous is not None and (eps_previous < 0 <= eps_latest or eps_increase >= 3):
        eps_score += 1
        reasons.append("EPS 由負轉正或連續三季改善")
    elif eps_increase >= 2 or _loss_shrunk_by_half(eps_previous, eps_latest):
        eps_score += 0.5
        reasons.append("EPS 連續兩季改善或虧損收斂至少 50%")
    if ttm_eps is not None:
        reasons.append(f"TTM EPS {ttm_eps:.2f}")
    profitability_score += min(2.0, eps_score)

    # Net income and non-operating quality: 1 point.
    net_latest = net_income_values[-1]
    net_previous = net_income_values[-2] if len(net_income_values) >= 2 else None
    net_improving = net_latest is not None and net_previous is not None and net_latest > net_previous
    nonop = _value(latest, "Non_Operating_Income", "non_operating_income")
    pretax = _value(latest, "Pre_Tax_Income", "pre_tax_income")
    nonop_share = abs(nonop) / abs(pretax) if nonop is not None and pretax not in (None, 0) else None
    nonop_warning = float(PROFITABILITY_PARAMETERS.get("non_operating_share_warning") or 0.3)
    nonop_healthy = nonop_share is not None and nonop_share < nonop_warning
    if net_improving and nonop_healthy:
        profitability_score += 1
        reasons.append("淨利改善且業外占比低於 30%")
    elif net_improving or nonop_healthy:
        profitability_score += 0.5
    if nonop_share is not None and nonop_share >= nonop_warning:
        profitability_deductions += 1
        risks.append("業外占稅前淨利至少 30%")

    # Cash flow: 2 points.
    ttm_ocf = _sum_last(ocf_values, 4)
    ttm_fcf = _sum_last(fcf_values, 4)
    cash_score = 0.0
    if ttm_ocf is not None and ttm_ocf > 0:
        cash_score += 1
        reasons.append("TTM 營業現金流為正")
    last6_fcf = [value for value in fcf_values[-6:] if value is not None]
    if len(last6_fcf) == 6 and all(value > 0 for value in last6_fcf):
        cash_score += 1
        reasons.append("最近六季自由現金流皆為正")
    elif ttm_fcf is not None and ttm_fcf > 0:
        cash_score += 0.5
        reasons.append("TTM 自由現金流為正")
    profitability_score += min(2.0, cash_score)

    if ttm_eps is not None and ttm_eps > 0 and ttm_ocf is not None and ttm_ocf < 0:
        profitability_deductions += 2
        risks.append("TTM EPS 為正但 TTM 營業現金流為負")
    quarterly_revenue_yoy = _yoy(revenue_values)
    quarterly_gp_yoy = _yoy(gross_profit_values)
    if quarterly_revenue_yoy is not None and quarterly_revenue_yoy > 0 and quarterly_gp_yoy is not None and quarterly_gp_yoy <= 0:
        profitability_deductions += 1
        risks.append("單季營收年增但單季毛利未年增")
    profitability_deductions = min(_configured(UNIFIED_FINANCIAL, "profitability_deduction_cap", 3), profitability_deductions)
    profitability_cap = 9 if len(rows) >= 8 else 8 if len(rows) >= 4 else 6 if len(rows) == 3 else 3
    profitability_net = max(0.0, min(profitability_cap, profitability_score - profitability_deductions))

    # Contract liabilities: 2 points.
    contract_score, contract_meta, contract_reasons, contract_risks = _contract_liability_score(rows)
    reasons.extend(contract_reasons)
    risks.extend(contract_risks)

    # Inventory and liquidity: 2 points plus at most one inventory deduction.
    inventory_score, inventory_deduction, inventory_meta, inventory_reasons, inventory_risks = _inventory_score(rows)
    reasons.extend(inventory_reasons)
    risks.extend(inventory_risks)
    liquidity_score, liquidity_meta, liquidity_reasons, liquidity_risks = _liquidity_score(rows)
    reasons.extend(liquidity_reasons)
    risks.extend(liquidity_risks)

    # Relative valuation: 2 points.
    valuation_score, valuation_meta, valuation_reasons, valuation_risks = _valuation_score(
        financial.get("valuation_data") or {},
        ttm_eps=ttm_eps,
        nonop_share=nonop_share,
    )
    reasons.extend(valuation_reasons)
    risks.extend(valuation_risks)

    score = profitability_net + contract_score + inventory_score + liquidity_score + valuation_score - inventory_deduction
    score = max(0.0, min(15.0, score))
    if om_latest is not None and om_latest < 0 and ttm_eps is not None and ttm_eps <= 0:
        score = min(score, _configured(UNIFIED_FINANCIAL, "negative_profitability_financial_cap", 5))
        risks.append("最新營益率為負且 TTM EPS 非正，財報分數上限 5")

    return _detail(
        score,
        15,
        reasons,
        risks,
        {
            "component": "financial",
            "scoring_version": "v3",
            "as_of_timestamp": snapshot.get("analysis_date"),
            "data_status": "covered",
            "row_count": len(rows),
            "latest": latest,
            "profitability": {
                "raw_score": profitability_score,
                "deductions": profitability_deductions,
                "data_cap": profitability_cap,
                "net_score": profitability_net,
                "ttm_eps": ttm_eps,
                "ttm_ocf": ttm_ocf,
                "ttm_fcf": ttm_fcf,
                "non_operating_share": nonop_share,
            },
            "contract_liabilities": contract_meta,
            "inventory": inventory_meta,
            "liquidity": liquidity_meta,
            "valuation": valuation_meta,
            "inventory_deduction": inventory_deduction,
        },
    )


def _contract_liability_score(rows: list[dict[str, Any]]) -> tuple[float, dict[str, Any], list[str], list[str]]:
    reasons: list[str] = []
    risks: list[str] = []
    contract_values = [_value(row, "Contract_Liabilities", "contract_liabilities") for row in rows]
    capital_values = [_value(row, "Paid_In_Capital", "paid_in_capital") for row in rows]
    reported_flags = [_contract_liability_is_reported(row, value) for row, value in zip(rows, contract_values)]
    reported_quarters = sum(reported_flags)
    nonzero_quarters = sum(
        reported and value is not None and value > 0
        for reported, value in zip(reported_flags, contract_values)
    )
    reported_min = int(_configured(UNIFIED_FINANCIAL, "contract_reported_quarters_min", 4))
    nonzero_min = int(_configured(UNIFIED_FINANCIAL, "contract_nonzero_quarters_min", 3))
    applicable = reported_quarters >= reported_min and nonzero_quarters >= nonzero_min
    latest_contract = contract_values[-1]
    latest_capital = capital_values[-1]
    ratio = latest_contract / latest_capital if latest_contract is not None and latest_capital not in (None, 0) else None
    score = 0.0
    contract_capital_min = float(CONTRACT_PARAMETERS.get("capital_ratio_min") or 0.05)
    if applicable and ratio is not None and ratio >= contract_capital_min:
        score += 0.5
        reasons.append("合約負債占股本至少 5%")
        last3_pairs = list(zip(reported_flags[-3:], contract_values[-3:]))
        last3 = [value for reported, value in last3_pairs if reported and value is not None]
        if len(last3) == 3 and last3[0] < last3[1] < last3[2]:
            score += 0.5
            reasons.append("合約負債連續三季上升")
    elif not applicable:
        if reported_quarters == 0:
            risks.append("公司未申報合約負債或欄位無資料，該分項不加分")
        else:
            risks.append("合約負債申報或非零歷史不足，該分項不加分")

    completed = 0
    successes = 0
    revenue_values = [_value(row, "Revenue", "revenue") for row in rows]
    for index in range(1, max(1, len(rows) - 3)):
        current = contract_values[index]
        previous = contract_values[index - 1]
        capital = capital_values[index]
        if not applicable or not reported_flags[index] or not reported_flags[index - 1]:
            continue
        if current is None or previous is None or capital in (None, 0):
            continue
        if current <= previous or current / capital < contract_capital_min or index + 3 >= len(rows):
            continue
        completed += 1
        if any((_yoy_at(revenue_values, future) or -math.inf) > 0 for future in range(index + 1, index + 4)):
            successes += 1
    conversion_rate = successes / completed if completed else None
    if completed >= int(_configured(UNIFIED_FINANCIAL, "contract_conversion_min_events", 3)) and conversion_rate is not None and conversion_rate >= _configured(UNIFIED_FINANCIAL, "contract_conversion_success_ratio", 0.6):
        score += 1
        reasons.append("歷史合約負債上升事件至少 60% 在一至三季內轉為營收年增")

    contract_full_history = int(_configured(UNIFIED_FINANCIAL, "contract_full_history_quarters", 8))
    cap = 2.0 if applicable and len(rows) >= contract_full_history and latest_capital is not None else 1.0 if applicable else 0.0
    score = min(score, cap)
    return score, {
        "score": score,
        "data_cap": cap,
        "applicable": applicable,
        "applicability_status": "applicable" if applicable else "insufficient_reported_history",
        "reported_quarters": reported_quarters,
        "nonzero_quarters": nonzero_quarters,
        "reported_quarters_min": reported_min,
        "nonzero_quarters_min": nonzero_min,
        "capital_ratio": ratio,
        "completed_events": completed,
        "successful_events": successes,
        "conversion_rate": conversion_rate,
    }, reasons, risks


def _contract_liability_is_reported(row: dict[str, Any], value: float | None) -> bool:
    status = str(row.get("Contract_Liabilities_Status") or row.get("contract_liabilities_status") or "").strip().lower()
    if status in {"not_reported", "not reported", "unavailable", "not_applicable", "n/a"}:
        return False
    if status in {"reported", "available", "official"}:
        return True
    return value is not None


def _inventory_score(rows: list[dict[str, Any]]) -> tuple[float, float, dict[str, Any], list[str], list[str]]:
    reasons: list[str] = []
    risks: list[str] = []
    inventory = [_value(row, "Inventory", "inventory") for row in rows]
    turnover = [_value(row, "Inventory_Turnover", "inventory_turnover") for row in rows]
    revenue = [_value(row, "Revenue", "revenue") for row in rows]
    margins = [_value(row, "Gross_Margin", "gross_margin") for row in rows]
    contracts = [_value(row, "Contract_Liabilities", "contract_liabilities") for row in rows]
    inventory_revenue = [_value(row, "Inventory_To_TTM_Revenue", "inventory_to_ttm_revenue") for row in rows]

    inv_yoy = _yoy(inventory)
    turnover_yoy = _yoy(turnover)
    revenue_yoy = _yoy(revenue)
    margin_yoy = _difference_yoy(margins)
    contract_yoy = _yoy(contracts)
    ratio_yoy = _difference_yoy(inventory_revenue)
    inventory_partial_history = int(_configured(UNIFIED_FINANCIAL, "inventory_partial_history_quarters", 4))
    inventory_full_history = int(_configured(UNIFIED_FINANCIAL, "inventory_full_history_quarters", 8))
    score = 0.0
    if None not in (inv_yoy, turnover_yoy, revenue_yoy, margin_yoy) and inv_yoy <= 0 and turnover_yoy > 0 and revenue_yoy >= 0 and margin_yoy >= 0:
        score = 1
        reasons.append("存貨下降、周轉改善且營收與毛利未惡化")
    elif None not in (inv_yoy, turnover_yoy, contract_yoy, revenue_yoy) and inv_yoy > 0 and turnover_yoy >= 0 and contract_yoy > 0 and revenue_yoy > 0:
        score = 0.5
        reasons.append("存貨增加但周轉、合約負債與營收同步健康")
    elif turnover_yoy is not None and ratio_yoy is not None and turnover_yoy >= 0 and ratio_yoy <= 0:
        score = 0.5
        reasons.append("存貨周轉未惡化且存貨占 TTM 營收比未上升")
    elif inventory_partial_history <= len(rows) < inventory_full_history:
        latest_turnover = turnover[-1]
        previous_turnover = turnover[-2] if len(turnover) >= 2 else None
        latest_ratio = inventory_revenue[-1]
        previous_ratio = inventory_revenue[-2] if len(inventory_revenue) >= 2 else None
        if None not in (latest_turnover, previous_turnover, latest_ratio, previous_ratio) and latest_turnover >= previous_turnover and latest_ratio <= previous_ratio:
            score = 0.5
            reasons.append("資料未滿八季，存貨效率短期改善")

    deduction = 0.0
    if None not in (inv_yoy, turnover_yoy, contract_yoy, margin_yoy) and inv_yoy > 0 and turnover_yoy < 0 and contract_yoy <= 0 and margin_yoy < 0:
        deduction = 1
        risks.append("存貨增加、周轉下降、合約負債與毛利同步轉弱")
    cap = 1.0 if len(rows) >= inventory_full_history else 0.5 if len(rows) >= inventory_partial_history else 0.0
    score = min(score, cap)
    return score, deduction, {
        "score": score,
        "deduction": deduction,
        "data_cap": cap,
        "inventory_yoy": inv_yoy,
        "turnover_yoy": turnover_yoy,
        "revenue_yoy": revenue_yoy,
        "gross_margin_yoy_pp": margin_yoy,
        "contract_liability_yoy": contract_yoy,
    }, reasons, risks


def _liquidity_score(rows: list[dict[str, Any]]) -> tuple[float, dict[str, Any], list[str], list[str]]:
    reasons: list[str] = []
    risks: list[str] = []
    latest = rows[-1]
    current_ratio = _value(latest, "Current_Ratio", "current_ratio")
    quick_ratio = _value(latest, "Quick_Ratio", "quick_ratio")
    debt_ratios = [_value(row, "Debt_Ratio", "debt_ratio") for row in rows]
    score = 0.0
    preferred_ratio = float(LIQUIDITY_PARAMETERS.get("preferred_ratio") or 2)
    concern_ratio = float(LIQUIDITY_PARAMETERS.get("concern_ratio") or 1)
    gap_warning = float(LIQUIDITY_PARAMETERS.get("current_quick_gap_warning") or 0.5)
    if current_ratio is not None and quick_ratio is not None:
        if current_ratio >= preferred_ratio and quick_ratio >= preferred_ratio:
            score += 0.75
            reasons.append("流動與速動比率皆至少 200%")
        elif current_ratio >= concern_ratio and quick_ratio >= concern_ratio:
            score += 0.5
            reasons.append("流動與速動比率皆至少 100%")
        else:
            risks.append("流動或速動比率低於 100%")
        if current_ratio - quick_ratio > gap_warning:
            risks.append("流動與速動比率差距超過 50 個百分點")
    else:
        risks.append("流動或速動資產資料不足，流動能力不加分")
    debt_latest = debt_ratios[-1]
    debt_prior_year = debt_ratios[-5] if len(debt_ratios) >= 5 else None
    if debt_latest is not None and debt_prior_year is not None and debt_latest <= debt_prior_year:
        score += 0.25
        reasons.append("負債比率未高於去年同期")
    return min(1.0, score), {
        "score": min(1.0, score),
        "current_ratio": current_ratio,
        "quick_ratio": quick_ratio,
        "debt_ratio": debt_latest,
        "prior_year_debt_ratio": debt_prior_year,
    }, reasons, risks


def _valuation_score(
    valuation: dict[str, Any],
    *,
    ttm_eps: float | None,
    nonop_share: float | None,
) -> tuple[float, dict[str, Any], list[str], list[str]]:
    reasons: list[str] = []
    risks: list[str] = []
    latest = valuation.get("latest") if isinstance(valuation, dict) else None
    history = list(valuation.get("history") or []) if isinstance(valuation, dict) else []
    peers = list(valuation.get("peers") or []) if isinstance(valuation, dict) else []
    if not isinstance(latest, dict):
        return 0.0, {"score": 0.0, "status": "missing"}, [], ["缺少官方相對估值資料"]

    nonop_warning = float(PROFITABILITY_PARAMETERS.get("non_operating_share_warning") or 0.3)
    metric = "pe_ratio" if ttm_eps is not None and ttm_eps > 0 and nonop_share is not None and nonop_share < nonop_warning else "pb_ratio"
    fallback_pb = metric == "pb_ratio"
    current = _num(latest.get(metric))
    if current is None or current <= 0:
        return 0.0, {"score": 0.0, "metric": metric, "status": "invalid"}, [], ["有效 PE／PB 資料不足"]

    history_values = [value for value in (_num(row.get(metric)) for row in history) if value is not None and value > 0]
    peer_values = [value for value in (_num(row.get(metric)) for row in peers) if value is not None and value > 0]
    history_min = int(_configured(UNIFIED_FINANCIAL, "valuation_history_min_months", 36))
    history_full_percentile = _configured(UNIFIED_FINANCIAL, "valuation_history_full_percentile", 40)
    history_partial_percentile = _configured(UNIFIED_FINANCIAL, "valuation_history_partial_percentile", 60)
    peer_full_count = int(_configured(UNIFIED_FINANCIAL, "valuation_peer_full_count", 5))
    peer_partial_count = int(_configured(UNIFIED_FINANCIAL, "valuation_peer_partial_count", 3))
    peer_partial_percentile = _configured(UNIFIED_FINANCIAL, "valuation_peer_partial_percentile", 75)
    own_score = 0.0
    own_percentile = None
    if len(history_values) >= history_min:
        own_percentile = 100 * sum(value <= current for value in history_values) / len(history_values)
        if own_percentile <= history_full_percentile:
            own_score = 1
        elif own_percentile <= history_partial_percentile:
            own_score = 0.5
        if own_score:
            reasons.append("估值位於公司自身歷史第 60 百分位以下")
    else:
        risks.append("估值歷史少於 36 個有效月")

    peer_score = 0.0
    peer_median = median(peer_values) if peer_values else None
    peer_p75 = _percentile(peer_values, peer_partial_percentile) if peer_values else None
    if len(peer_values) >= peer_full_count and peer_median is not None and peer_p75 is not None:
        if current <= peer_median:
            peer_score = 1
        elif current <= peer_p75:
            peer_score = 0.5
    elif len(peer_values) >= peer_partial_count and peer_median is not None and current <= peer_median:
        peer_score = 0.5
    if peer_score:
        reasons.append("估值不高於正式同業比較門檻")
    elif len(peer_values) < peer_partial_count:
        risks.append("有效估值同業少於三家")

    score = own_score + peer_score
    if fallback_pb:
        score = min(_configured(UNIFIED_FINANCIAL, "pb_fallback_cap", 1), score)
        reasons.append("PE 不適用，改以 PB 保守評分且上限 1 分")
    return min(2.0, score), {
        "score": min(2.0, score),
        "metric": metric,
        "current": current,
        "history_count": len(history_values),
        "history_percentile": own_percentile,
        "peer_count": len(peer_values),
        "peer_median": peer_median,
        "peer_p75": peer_p75,
        "pb_fallback_cap": fallback_pb,
    }, reasons, risks


def _detail(score: float, max_score: int, reasons: list[str], risks: list[str], details: dict[str, Any]) -> dict[str, Any]:
    bounded = max(0.0, min(float(max_score), score))
    rounded = int(math.floor(bounded + 0.5))
    component = str(details.get("component") or "")
    code_map = REVENUE_RULE_CODES if component == "revenue" else FINANCIAL_RULE_CODES if component == "financial" else {}
    rule_results = []
    for text in _unique(reasons):
        rule_id = code_map.get(text)
        if rule_id:
            rule_results.append({"rule_id": rule_id, "status": "hit", "effect": "positive", "reason": text})
    for text in _unique(risks):
        rule_id = code_map.get(text)
        if rule_id:
            rule_results.append({"rule_id": rule_id, "status": "hit", "effect": "penalty_or_risk", "reason": text})
    if details.get("data_status") in {"unknown", "insufficient", "not_applicable"}:
        rule_results.append(
            {
                "rule_id": f"{component.upper()}_DATA_STATUS" if component else "DATA_STATUS",
                "status": details.get("data_status"),
                "effect": "data_gate",
            }
        )
    details["rule_results"] = rule_results
    return {
        "score": rounded,
        "raw_score": round(bounded, 4),
        "reasons": _unique(reasons),
        "risks": _unique(risks),
        "details": details,
    }


def _normalise_revenue_rows(value: Any) -> list[dict[str, Any]]:
    rows = [dict(row) for row in value if isinstance(row, dict)] if isinstance(value, list) else []
    return sorted(rows, key=lambda row: str(row.get("month") or row.get("Month") or row.get("date") or ""))


def _normalise_financial_rows(value: Any) -> list[dict[str, Any]]:
    rows = [dict(row) for row in value if isinstance(row, dict)] if isinstance(value, list) else []
    return sorted(rows, key=lambda row: str(row.get("Quarter") or row.get("quarter") or ""))


def effective_revenue_rows(value: Any) -> list[dict[str, Any]]:
    """Return chronological revenue observations with the Jan/Feb rule applied."""

    rows = _normalise_revenue_rows(value)
    by_month: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        parsed = _parse_month(row.get("month") or row.get("Month") or row.get("date"))
        if parsed:
            by_month[(parsed.year, parsed.month)] = row
    result: list[dict[str, Any]] = []
    for row in rows:
        parsed = _parse_month(row.get("month") or row.get("Month") or row.get("date"))
        if not parsed:
            result.append(row)
            continue
        if parsed.month == 1:
            continue
        if parsed.month == 2:
            jan = by_month.get((parsed.year, 1))
            prior_jan = by_month.get((parsed.year - 1, 1))
            prior_feb = by_month.get((parsed.year - 1, 2))
            current_sum = _sum_values([jan, row], ("revenue", "Monthly_Revenue", "monthly_revenue"))
            prior_sum = _sum_values([prior_jan, prior_feb], ("revenue", "Monthly_Revenue", "monthly_revenue"))
            combined = dict(row)
            combined["jan_feb_combined"] = True
            combined["revenue"] = current_sum
            combined["yoy"] = ((current_sum / prior_sum) - 1) * 100 if current_sum is not None and prior_sum not in (None, 0) else None
            result.append(combined)
            continue
        result.append(row)
    return result


def _seasonal_revenue_context(raw_rows: list[dict[str, Any]], latest_effective: dict[str, Any]) -> dict[str, Any]:
    latest_date = _parse_month(latest_effective.get("month") or latest_effective.get("Month"))
    latest_revenue = _value(latest_effective, "revenue", "Monthly_Revenue", "monthly_revenue")
    if latest_date is None or latest_revenue is None:
        return {}
    ordered = [(row, _parse_month(row.get("month") or row.get("Month"))) for row in raw_rows]
    if latest_effective.get("jan_feb_combined"):
        by_month = {
            (parsed.year, parsed.month): _value(row, "revenue", "Monthly_Revenue", "monthly_revenue")
            for row, parsed in ordered
            if parsed
        }
        prior_combined = []
        for year in sorted({parsed.year for _row, parsed in ordered if parsed and parsed.year < latest_date.year}):
            jan = by_month.get((year, 1))
            feb = by_month.get((year, 2))
            if jan is not None and feb is not None:
                prior_combined.append(jan + feb)
        baseline = median(prior_combined) if prior_combined else None
        return {
            "same_month_baseline": baseline,
            "positive_surprise": baseline is not None and latest_revenue > baseline,
            "raw_mom_pct": None,
            "typical_mom_pct": None,
            "adjusted_mom_pct": None,
            "jan_feb_combined": True,
        }
    same_month = [
        _value(row, "revenue", "Monthly_Revenue", "monthly_revenue")
        for row, parsed in ordered
        if parsed and parsed.month == latest_date.month and parsed.year < latest_date.year
    ]
    same_month = [value for value in same_month if value is not None]
    baseline = median(same_month) if same_month else None
    index = next((idx for idx, (row, parsed) in enumerate(ordered) if row is latest_effective or parsed == latest_date), None)
    raw_mom = None
    if index is not None and index > 0:
        previous_revenue = _value(ordered[index - 1][0], "revenue", "Monthly_Revenue", "monthly_revenue")
        raw_mom = _pct_change(latest_revenue, previous_revenue)
    historical_transitions: list[float] = []
    for idx in range(1, len(ordered)):
        parsed = ordered[idx][1]
        if not parsed or parsed.month != latest_date.month or parsed.year >= latest_date.year:
            continue
        current = _value(ordered[idx][0], "revenue", "Monthly_Revenue", "monthly_revenue")
        previous = _value(ordered[idx - 1][0], "revenue", "Monthly_Revenue", "monthly_revenue")
        change = _pct_change(current, previous)
        if change is not None:
            historical_transitions.append(change)
    typical_mom = median(historical_transitions) if historical_transitions else 0.0
    adjusted_mom = raw_mom - typical_mom if raw_mom is not None else None
    return {
        "same_month_baseline": baseline,
        "positive_surprise": baseline is not None and latest_revenue > baseline,
        "raw_mom_pct": raw_mom,
        "typical_mom_pct": typical_mom,
        "adjusted_mom_pct": adjusted_mom,
    }


def _revenue_group(values: list[float]) -> str | None:
    if len(values) != 4:
        return None
    positive_threshold = _configured(UNIFIED_REVENUE, "group_positive_pct", 1)
    group_2_floor = _configured(UNIFIED_REVENUE, "group_2_floor_pct", -15)
    if all(value >= positive_threshold for value in values):
        return "group_1"
    if sum(value >= positive_threshold for value in values) >= 2 and all(value > group_2_floor for value in values):
        return "group_2"
    return None


def _value(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in row:
            value = _num(row.get(key))
            if value is not None:
                return value
    return None


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_month(value: Any) -> datetime | None:
    text = str(value or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y/%m/%d", "%Y/%m"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _consecutive_increases(values: list[float | None]) -> int:
    clean = [value for value in values if value is not None]
    count = 0
    for index in range(len(clean) - 1, 0, -1):
        if clean[index] > clean[index - 1]:
            count += 1
        else:
            break
    return count


def _sum_last(values: list[float | None], count: int) -> float | None:
    selected = values[-count:]
    return sum(value for value in selected if value is not None) if len(selected) == count and all(value is not None for value in selected) else None


def _loss_shrunk_by_half(previous: float | None, latest: float | None) -> bool:
    ratio = _configured(UNIFIED_FINANCIAL, "loss_shrink_ratio", 0.5)
    return bool(previous is not None and latest is not None and previous < 0 and latest < 0 and abs(latest) <= abs(previous) * ratio)


def _pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return (current - previous) / abs(previous) * 100


def _yoy(values: list[float | None]) -> float | None:
    return _yoy_at(values, len(values) - 1)


def _yoy_at(values: list[float | None], index: int) -> float | None:
    if index < 4 or index >= len(values):
        return None
    return _pct_change(values[index], values[index - 4])


def _difference_yoy(values: list[float | None]) -> float | None:
    if len(values) < 5 or values[-1] is None or values[-5] is None:
        return None
    return values[-1] - values[-5]


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _sum_values(rows: list[dict[str, Any] | None], keys: tuple[str, ...]) -> float | None:
    values: list[float] = []
    for row in rows:
        if row is None:
            return None
        value = _value(row, *keys)
        if value is None:
            return None
        values.append(value)
    return sum(values)


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
