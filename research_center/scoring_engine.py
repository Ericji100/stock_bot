from __future__ import annotations

from statistics import mean
from typing import Any

from .models import CommandRequest

Score = dict[str, Any]


def build_local_scores(request: CommandRequest, structured_data: dict[str, Any]) -> list[Score]:
    if request.source_only:
        return []
    if request.command == "research":
        if request.mode not in {"score", "deep"}:
            return []
        return _research_scores(structured_data)
    if request.command == "macro":
        return _macro_scores(structured_data)
    if request.command == "theme":
        return _theme_scores(structured_data)
    if request.command == "value_scan":
        return _value_scan_scores(structured_data)
    return []


def build_buy_rating(scores: list[Score]) -> dict[str, Any]:
    if not scores:
        return {"score": 1, "max": 5, "label": "資料不足", "reason": "缺少本地評分資料。", "risk": "不得解讀為買進建議。"}
    regular = [item for item in scores if item.get("score_name") != "綜合量化評分"]
    avg = mean(_number(item.get("score_value"), 0) for item in regular) if regular else 0
    fatal = [item.get("score_name") for item in regular if _number(item.get("score_value"), 0) <= 25]
    if avg >= 82 and not fatal:
        score, label = 5, "高分候選"
    elif avg >= 68 and len(fatal) <= 1:
        score, label = 4, "偏正向觀察"
    elif avg >= 52:
        score, label = 3, "中性觀察"
    elif avg >= 35:
        score, label = 2, "偏弱觀察"
    else:
        score, label = 1, "風險偏高"
    risk = "；".join(str(name) for name in fatal[:5]) if fatal else "無重大低分項，但仍需確認來源與風險。"
    return {
        "score": score,
        "max": 5,
        "label": label,
        "reason": f"本地量化評分平均約 {avg:.1f}/100，依保守規則換算為 {score}/5。",
        "risk": risk,
        "disclaimer": "推薦買入評分是研究輔助分數，不構成投資建議或自動買入訊號。",
    }

def _research_scores(data: dict[str, Any]) -> list[Score]:
    revenue = data.get("revenue_data") or []
    financial = data.get("financial_data") or []
    technical = data.get("technical_data") or {}
    institutional = data.get("institutional_data") or []
    margin = data.get("margin_data") or []
    free_sources = data.get("free_public_sources") or {}
    tdcc = data.get("tdcc_data") or free_sources.get("tdcc") or {}
    valuation = data.get("valuation_data") or free_sources.get("valuation") or {}
    gross_margin_cache = data.get("gross_margin_cache") or free_sources.get("gross_margin_cache") or {}

    scores = [
        _score("營益率", *_operating_margin_score(financial)),
        _score("營收成長性", *_revenue_growth_score(revenue)),
        _score("獲利能力 EPS", *_eps_score(financial)),
        _score("獲利成長性", *_profit_growth_score(financial)),
        _score("自由現金流量", *_cash_flow_score(financial)),
        _score("存貨週轉率", *_inventory_score(financial)),
        _score("消息題材熱度", 50, "需由 AI 與來源文字判讀；本地只給中性基準。", "尚未有可量化新聞熱度資料源。"),
        _score("類股族群資金流向", *_institutional_flow_score(institutional)),
        _score("TDCC 籌碼集中度", *_tdcc_score(tdcc)),
        _score("估值安全邊際", *_valuation_score(valuation)),
        _score("毛利率快取驗證", *_gross_margin_cache_score(gross_margin_cache)),
        _score("市場產業成長", 50, "需由 AI 與產業資料判讀；本地只給中性基準。", "尚未有正式 CAGR 資料庫。"),
        _score("轉型效益呈現", 50, "需由 AI 依產品、客戶與營收占比判讀；本地只給中性基準。", "公司知識庫若缺產品/客戶資料，不得高分。"),
        _score("營運谷底翻轉", *_turnaround_score(revenue, financial)),
        _score("關鍵技術與護城河", 50, "需由 AI 依技術、客戶認證與供應鏈位階判讀；本地只給中性基準。", "尚未有完整技術護城河資料庫。"),
        _score("技術與籌碼", *_technical_chip_score(technical, institutional, margin)),
    ]
    average = round(mean(score["score_value"] for score in scores) / 25, 2) if scores else 0
    fatal = [score["score_name"] for score in scores if score["score_value"] <= 10]
    scores.append(
        _score(
            "綜合量化評分",
            round(min(100, average * 25), 1),
            f"{len(scores)} 項本地評分平均約 {average} / 4，依規格換算為百分制。",
            "若任一項落入 C 或資料不足，須保守解讀。" + (f" C/低分項：{', '.join(fatal)}。" if fatal else ""),
        )
    )
    return scores


def _macro_scores(data: dict[str, Any]) -> list[Score]:
    fear_greed = data.get("fear_greed") or data.get("market_score") or {}
    score = _clamp(_number(fear_greed.get("score"), 50))
    return [_score("市場恐懼貪婪分數", score, f"本地市場模型區間：{fear_greed.get('zone', 'unknown')}。", "正式 IV、完整期貨籌碼不足時需保守解讀。")]


def _theme_scores(data: dict[str, Any]) -> list[Score]:
    summary = data.get("company_knowledge_summary") or {}
    total = _number(summary.get("total_companies"), 0)
    covered = _number(summary.get("covered_companies"), 0)
    coverage = round((covered / total) * 100, 1) if total else 0
    return [_score("供應鏈資料覆蓋度", coverage, f"候選公司 {int(total)} 家，知識庫覆蓋 {int(covered)} 家。", "未覆蓋公司不得做高信心供應鏈結論。")]


def _value_scan_scores(data: dict[str, Any]) -> list[Score]:
    candidates = data.get("candidates") or []
    scores: list[Score] = []
    for row in candidates[: int(data.get("top_n") or 10)]:
        base = _number(row.get("rerating_score"), 0)
        verification = _number(row.get("verification_score"), 0)
        tdcc_component, _, _ = _tdcc_score(row.get("tdcc_data") or {})
        valuation_component, _, _ = _valuation_score(row.get("valuation_data") or {})
        combined = round(base * 0.6 + verification * 0.25 + tdcc_component * 0.1 + valuation_component * 0.05, 1)
        name = f"{row.get('code', '')} {row.get('name', '')}".strip() or "候選股"
        scores.append(
            _score(
                f"價值重估分數：{name}",
                combined,
                f"重估分 {base}，證據覆蓋分 {verification}，TDCC {tdcc_component}，估值 {valuation_component}，依 60/25/10/5 權重合成。",
                "若公告、客戶、法人報告或財報細項不足，證據分會拉低總分。",
            )
        )
    if candidates:
        scores.insert(0, _score("價值重估候選數", min(100, len(candidates) * 10), f"本次輸出 {len(candidates)} 檔候選。", "候選數量不代表勝率。"))
    return scores


def _operating_margin_score(rows: list[dict[str, Any]]) -> tuple[float, str, str]:
    values = _series(rows, ("operating_margin", "OperatingMargin", "營益率"))[-4:]
    if not values:
        return 0, "缺少營益率資料。", "資料不足，無法評分。"
    avg = mean(values)
    latest = values[-1]
    if avg >= 15 and latest >= min(values) * 0.8:
        return 100, f"近四季平均營益率 {avg:.1f}%。", ""
    if avg >= 10:
        return 75, f"近四季平均營益率 {avg:.1f}%。", "未達 AA 門檻。"
    if avg >= 5:
        return 50, f"近四季平均營益率 {avg:.1f}%。", "僅達 BB 區間。"
    if latest < 0 or avg < 0:
        return 0, f"近四季平均營益率 {avg:.1f}%。", "最近或平均為負，落入 C。"
    return 25, f"近四季平均營益率 {avg:.1f}%。", "低於 5%，落入 B。"


def _revenue_growth_score(rows: list[dict[str, Any]]) -> tuple[float, str, str]:
    values = _series(rows, ("YoY", "yoy", "revenue_yoy", "年增率"))[-6:]
    if not values:
        return 0, "缺少月營收 YoY 資料。", "資料不足，無法評分。"
    avg = mean(values)
    latest = values[-1]
    all_positive = all(value > 0 for value in values)
    stable = len(values) < 2 or latest >= values[-2] - 3
    if all_positive and avg > 25 and stable:
        return 100, f"近六月 YoY 平均 {avg:.1f}%，且皆為正。", ""
    if all_positive and avg >= 10:
        return 75, f"近六月 YoY 平均 {avg:.1f}%，且皆為正。", "未達 AA 或最近動能略弱。"
    if any(value < 0 for value in values):
        return 50, f"近六月 YoY 平均 {avg:.1f}%，但曾有負成長。", "落入 BB 或以下。"
    if latest < 0:
        return 0, f"最近月 YoY {latest:.1f}%。", "最近月為負，落入 C。"
    return 25, f"近六月 YoY 平均 {avg:.1f}%。", "最近三月可能遞減或成長不足。"


def _eps_score(rows: list[dict[str, Any]]) -> tuple[float, str, str]:
    values = _series(rows, ("EPS", "eps", "每股盈餘"))[-4:]
    if not values:
        return 0, "缺少 EPS 資料。", "資料不足，無法評分。"
    total = sum(values)
    if total > 5:
        return 100, f"近四季 EPS 合計 {total:.2f}。", ""
    if total >= 3:
        return 75, f"近四季 EPS 合計 {total:.2f}。", "未達 AA。"
    if total >= 1:
        return 50, f"近四季 EPS 合計 {total:.2f}。", "僅達 BB。"
    if total > 0 and values[-1] >= 0:
        return 25, f"近四季 EPS 合計 {total:.2f}。", "僅達 B。"
    return 0, f"近四季 EPS 合計 {total:.2f}。", "虧損或最近一季虧損，落入 C/B。"


def _profit_growth_score(rows: list[dict[str, Any]]) -> tuple[float, str, str]:
    values = _series(rows, ("net_income_yoy", "NetIncomeYoY", "稅後淨利年增率"))[-4:]
    if not values:
        return 0, "缺少稅後淨利年增率資料。", "資料不足，無法評分。"
    if len(values) >= 3 and all(value > 0 for value in values[-3:]) and values[-1] >= values[-2]:
        return 100, "近三季稅後淨利年增率為正且最近一季遞增。", ""
    if len(values) >= 2 and all(value > 0 for value in values[-2:]):
        return 75, "近兩季稅後淨利年增率為正。", "未達 AA。"
    if values[-1] < 0:
        return 25, "最近一季稅後淨利年增率為負。", "落入 B 或以下。"
    return 50, "獲利成長有部分改善。", "仍需更多季度確認。"


def _cash_flow_score(rows: list[dict[str, Any]]) -> tuple[float, str, str]:
    values = _series(rows, ("free_cash_flow", "FreeCashFlow", "自由現金流量"))[-6:]
    if not values:
        return 0, "缺少自由現金流資料。", "資料不足，無法評分。"
    six_sum = sum(values)
    four_sum = sum(values[-4:])
    if len(values) >= 6 and all(value > 0 for value in values[-6:]):
        return 100, "近六季自由現金流皆為正。", ""
    if six_sum > 0 and four_sum > 0:
        return 75, "近六季與近四季自由現金流合計皆為正。", "未達連續六季皆正。"
    if six_sum < 0 and four_sum > 0:
        return 50, "近四季合計為正，但六季合計仍為負。", "僅達 BB。"
    if six_sum > 0 and four_sum < 0:
        return 25, "近六季合計為正，但近四季轉弱。", "落入 B。"
    return 0, "近六季與近四季合計皆為負。", "落入 C。"


def _inventory_score(rows: list[dict[str, Any]]) -> tuple[float, str, str]:
    values = _series(rows, ("inventory_turnover", "InventoryTurnover", "存貨週轉率"))[-4:]
    if not values:
        return 50, "缺少存貨週轉率資料，暫以不評分/中性處理。", "若產業非低庫存，仍需補資料。"
    avg = mean(values)
    stable = all(values[index] >= values[index - 1] * 0.8 for index in range(1, len(values)))
    if stable and avg > 1.5:
        return 100, f"近四季存貨週轉率平均 {avg:.2f} 且穩定。", ""
    if stable:
        return 75, f"近四季存貨週轉率平均 {avg:.2f} 且穩定。", "平均低於 1.5。"
    if len(values) >= 2 and values[-1] < values[-2] * 0.8:
        return 0, "最近一季存貨週轉率下跌超過 20%。", "落入 C。"
    return 50, "存貨週轉率有下滑但非最近大幅惡化。", "僅達 BB。"


def _institutional_flow_score(rows: list[dict[str, Any]]) -> tuple[float, str, str]:
    values = _series(rows, ("NetBuy", "net_buy", "外資投信合計", "TotalNetBuy"))[-5:]
    if not values:
        return 50, "缺少近五日法人買賣超資料，暫以中性處理。", "需要法人資料確認。"
    total = sum(values)
    if total > 0:
        return min(100, 60 + total / max(abs(total), 1) * 25), f"近五日法人合計偏買超：{total:.0f}。", "需確認是否集中在少數交易日。"
    if total < 0:
        return 25, f"近五日法人合計偏賣超：{total:.0f}。", "資金流向偏弱。"
    return 50, "近五日法人買賣超約為中性。", "動能不足。"


def _turnaround_score(revenue: list[dict[str, Any]], financial: list[dict[str, Any]]) -> tuple[float, str, str]:
    yoy = _series(revenue, ("YoY", "yoy", "revenue_yoy", "年增率"))[-3:]
    eps = _series(financial, ("EPS", "eps", "每股盈餘"))[-2:]
    if yoy and all(value > 0 for value in yoy) and eps and eps[-1] > 0:
        return 100, "近三月營收 YoY 為正且最近 EPS 為正。", ""
    if yoy and yoy[-1] > 0:
        return 75, "最近月營收 YoY 轉正。", "仍需 EPS 或毛利驗證。"
    if yoy and yoy[-1] > (yoy[0] if len(yoy) > 1 else -999):
        return 50, "營收年增率有收斂或改善跡象。", "尚未確認轉盈。"
    return 25, "尚未看到明確谷底翻轉。", "營收與獲利改善證據不足。"


def _technical_chip_score(technical: dict[str, Any], institutional: list[dict[str, Any]], margin: list[dict[str, Any]]) -> tuple[float, str, str]:
    score = 40.0
    reasons = []
    deductions = []
    if technical.get("above_ma21"):
        score += 20
        reasons.append("股價站上 21MA")
    else:
        deductions.append("未確認站上 21MA")
    if technical.get("avg_volume_20d"):
        score += 10
        reasons.append("有 20 日均量資料")
    inst_score, inst_reason, inst_deduction = _institutional_flow_score(institutional)
    score += (inst_score - 50) * 0.3
    reasons.append(inst_reason)
    if inst_deduction:
        deductions.append(inst_deduction)
    margin_values = _series(margin, ("MarginBalance", "margin_balance", "融資餘額"))[-2:]
    if len(margin_values) == 2 and margin_values[-1] > margin_values[-2] * 1.1:
        score -= 10
        deductions.append("融資餘額短期增加超過 10%")
    return _clamp(score), "；".join(reasons) or "技術籌碼資料不足。", "；".join(deductions) or ""


def _tdcc_score(tdcc: dict[str, Any]) -> tuple[float, str, str]:
    status = tdcc.get("status")
    if status != "covered":
        return 50, "缺少 TDCC 集保分布快取，暫以中性處理。", "籌碼集中度需補 TDCC 資料。"
    signal = tdcc.get("concentration_signal")
    large = _number(tdcc.get("large_holder_pct"), 0)
    retail = _number(tdcc.get("retail_holder_pct"), 0)
    if signal == "high_concentration":
        score = 82
    elif signal == "moderate_concentration":
        score = 68
    elif signal == "retail_heavy":
        score = 35
    else:
        score = 55
    return score, f"大戶級距約 {large:.1f}%，散戶級距約 {retail:.1f}%，訊號 {signal or 'neutral'}。", "TDCC 是庫存分布，不等同主力買賣超。"


def _valuation_score(valuation: dict[str, Any]) -> tuple[float, str, str]:
    if valuation.get("status") != "official_public":
        return 50, "未取得 TWSE/TPEx 估值公開資料，暫以中性處理。", "缺少本益比/股淨比/殖利率官方資料。"
    latest = valuation.get("latest") or {}
    pe = _number(latest.get("pe_ratio"), None)
    pb = _number(latest.get("pb_ratio"), None)
    dividend = _number(latest.get("dividend_yield_pct"), 0) or 0
    score = 55.0
    reasons = []
    deductions = []
    if pe is not None:
        reasons.append(f"本益比 {pe:.1f}")
        if 0 < pe <= 15:
            score += 20
        elif pe >= 40:
            score -= 18
            deductions.append("本益比偏高")
    else:
        deductions.append("缺少本益比")
    if pb is not None:
        reasons.append(f"股淨比 {pb:.1f}")
        if 0 < pb <= 2:
            score += 12
        elif pb >= 5:
            score -= 12
            deductions.append("股淨比偏高")
    else:
        deductions.append("缺少股淨比")
    if dividend >= 3:
        score += 8
        reasons.append(f"殖利率 {dividend:.1f}%")
    return _clamp(score), "；".join(reasons) or "官方估值資料欄位不足。", "；".join(deductions) or "估值未見重大扣分。"


def _gross_margin_cache_score(snapshot: dict[str, Any]) -> tuple[float, str, str]:
    if snapshot.get("status") != "covered":
        return 50, "缺少毛利率快取資料，暫以中性處理。", "需補財報毛利率序列。"
    series = snapshot.get("series") or []
    values = _series(series, ("gross_margin", "GrossMargin", "毛利率"))[:4]
    if not values:
        return 50, "毛利率快取存在，但欄位無法解析。", "需確認 gross_margin 欄位。"
    latest = values[0]
    previous = values[1] if len(values) > 1 else latest
    score = 60
    if latest >= 30:
        score += 20
    elif latest < 10:
        score -= 20
    if latest >= previous:
        score += 10
    else:
        score -= 8
    return _clamp(score), f"最近毛利率約 {latest:.1f}%，前期約 {previous:.1f}%。", "毛利率下滑需確認產品組合與價格壓力。" if latest < previous else "無重大扣分。"

def _score(name: str, value: float, reason: str, deduction: str) -> Score:
    return {
        "score_name": name,
        "score_value": round(_clamp(value), 1),
        "score_max": 100,
        "score_reason": reason,
        "deduction_reason": deduction or "無重大扣分。",
    }


def _series(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[float]:
    values = []
    for row in rows:
        for key in keys:
            if key in row:
                value = _number(row.get(key), None)
                if value is not None:
                    values.append(value)
                break
    return values


def _number(value: Any, default: float | None = 0) -> float | None:
    if value is None:
        return default
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return default


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))




