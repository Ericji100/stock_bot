from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import CommandRequest, SourceItem

REQUIRED_DATA_GAP_SCHEMA_VERSION = "required_data_gap_v1"


@dataclass(frozen=True)
class RequiredEvidence:
    field: str
    label: str
    tier: str
    keywords: tuple[str, ...]
    queries: tuple[str, ...]


def build_required_data_gap_summary(
    request: CommandRequest,
    sources: list[SourceItem],
    structured_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requirements = _requirements_for_request(request, structured_data or {})
    covered: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for requirement in requirements:
        matches = _matching_sources(requirement, sources)
        row = {
            "field": requirement.field,
            "label": requirement.label,
            "tier": requirement.tier,
            "matched_source_count": len(matches),
            "matched_source_ids": [item.source_id for item in matches[:8]],
            "backfill_queries": list(requirement.queries),
        }
        if matches:
            covered.append(row)
        else:
            missing.append(row)
    high_missing = [item for item in missing if item["tier"] == "hard"]
    medium_missing = [item for item in missing if item["tier"] == "soft"]
    return {
        "schema_version": REQUIRED_DATA_GAP_SCHEMA_VERSION,
        "command": request.command,
        "mode": request.mode,
        "target": request.target or request.market_scope or request.theme_scope or request.candidate_pool,
        "requirement_count": len(requirements),
        "covered_count": len(covered),
        "missing_count": len(missing),
        "covered": covered,
        "missing": missing,
        "hard_missing": high_missing,
        "soft_missing": medium_missing,
        "backfill_recommended": bool(high_missing or medium_missing),
        "status": "complete" if not missing else "missing_required_data",
    }


def build_required_gap_fill_tasks(
    request: CommandRequest,
    gap_summary: dict[str, Any],
    *,
    max_fields: int = 10,
) -> list[dict[str, Any]]:
    missing = list(gap_summary.get("hard_missing") or []) + list(gap_summary.get("soft_missing") or [])
    if not missing:
        return []
    target = _target_label(request)
    tasks: list[dict[str, Any]] = []
    for item in missing[:max_fields]:
        field = str(item.get("field") or "")
        label = str(item.get("label") or field)
        queries = [str(q).strip() for q in (item.get("backfill_queries") or []) if str(q).strip()]
        if not queries:
            continue
        prompt = (
            "你是台股 AI 投研資料中心的缺口補搜代理。\n"
            "只針對指定缺口搜尋，不要重做完整報告，也不要產生投資結論。\n"
            f"指令：/{request.command}\n"
            f"目標：{target}\n"
            f"缺口欄位：{label}\n"
            "請優先找官方、交易所、期交所、公開資訊觀測站、公司 IR、主流財經媒體或產業研究來源。\n"
            "若找不到可驗證資料，請明確標示資料不足。"
        )
        tasks.append(
            {
                "label": f"required_gap:{field}",
                "objective": f"補齊必備資料缺口：{label}",
                "queries": queries,
                "prompt": prompt,
                "gap_field": field,
                "gap_tier": item.get("tier"),
                "found_by": ["required_data_gap_fill", f"required_field:{field}"],
                "query_intent": "required_data_gap_fill",
            }
        )
    return tasks


def _matching_sources(requirement: RequiredEvidence, sources: list[SourceItem]) -> list[SourceItem]:
    matches: list[SourceItem] = []
    for source in sources:
        text = _source_text(source)
        if any(keyword.lower() in text for keyword in requirement.keywords):
            matches.append(source)
    return matches


def _source_text(source: SourceItem) -> str:
    parts = [
        source.title,
        source.snippet,
        source.url,
        source.provider,
        source.provider_detail,
        source.source_level,
        " ".join(source.used_in_section or []),
        " ".join(source.found_by or []),
    ]
    return " ".join(str(part or "") for part in parts).lower()


def _requirements_for_request(request: CommandRequest, structured_data: dict[str, Any]) -> list[RequiredEvidence]:
    command = request.command
    if command == "macro":
        return _macro_requirements(request)
    if command == "research":
        return _research_requirements(request)
    if command in {"theme", "theme_flow"}:
        return _theme_requirements(request)
    if command == "theme_radar":
        return _theme_radar_requirements(request)
    if command == "value_scan":
        return _value_scan_requirements(request, structured_data)
    if command == "news":
        return _news_requirements(request)
    return []


def _macro_requirements(request: CommandRequest) -> list[RequiredEvidence]:
    scope = _target_label(request)
    return [
        _req("global_risk_vix", "全球風險：VIX 或等效全球風險指標", "hard",
             ("vix", "波動率", "恐慌指數", "risk temperature"),
             (f"{scope} VIX 全球風險 憂慮 指數", "VIX 美股 波動率 全球風險")),
        _req("rates_usd", "利率美元：美債殖利率、Fed 或美元", "hard",
             ("美債", "殖利率", "fed", "降息", "升息", "dxy", "美元指數", "美元兌台幣", "台幣匯率"),
             (f"{scope} 美債 殖利率 Fed 降息 美元指數", "美元兌台幣 DXY 台股 資金流")),
        _req("twse_market", "上市市場：加權指數、上市成交量、上市漲跌家數", "hard",
             ("加權指數", "twse", "上市成交量", "上市 漲跌家數", "上市市場"),
             ("加權指數 上市 成交量 漲跌家數 TWSE", "TWSE 加權指數 market breadth advance decline volume")),
        _req("tpex_market", "上櫃市場：櫃買指數、上櫃成交量、上櫃漲跌家數", "hard",
             ("櫃買指數", "tpex", "上櫃成交量", "上櫃 漲跌家數", "otc"),
             ("櫃買指數 上櫃 成交量 漲跌家數 TPEx", "TPEx OTC index market breadth volume")),
        _req("institutional_flow", "法人資金：上市/上櫃三大法人或外資資金流", "hard",
             ("三大法人", "外資", "投信", "自營商", "買賣超", "foreign investor"),
             ("上市 上櫃 三大法人 買賣超 外資 投信 自營商", "TWSE TPEx institutional investor net buy sell")),
        _req("taiwan_derivatives", "衍生品：台指期或台指選擇權情緒", "hard",
             ("台指期", "台指選擇權", "taifex", "put call", "未平倉", "外資期貨"),
             ("TAIFEX 台指期 台指選擇權 Put Call Ratio 未平倉", "台指期 外資期貨 淨部位 台股")),
        _req("commodity_geo_risk", "國際風險：原油、黃金、關稅、戰爭或地緣政治", "hard",
             ("原油", "油價", "黃金", "關稅", "戰爭", "地緣政治", "opec", "brent", "wti"),
             ("油價 黃金 關稅 戰爭 地緣政治 台股 影響", "WTI Brent gold tariff geopolitics Taiwan stocks")),
        _req("put_call_oi", "Put/Call Ratio 與未平倉", "soft",
             ("put/call", "put call", "未平倉", "open interest"),
             ("台指選擇權 Put Call Ratio 未平倉 最大未平倉",)),
        _req("fear_greed_proxy", "恐慌/貪婪 proxy", "soft",
             ("fear greed", "恐慌貪婪", "強勢股比例", "漲跌家數", "融資餘額"),
             ("台股 恐慌 貪婪 proxy VIX 融資 漲跌家數 強勢股比例",)),
        _req("regional_risk", "中國、歐洲、日本區域風險", "soft",
             ("中國", "歐洲", "日本", "日圓", "人民幣", "ecb", "boj"),
             ("中國 歐洲 日本 匯率 政策 台股 風險",)),
        _req("macro_bonus_data", "加分資料：正式 IV、大額交易人、ETF/基金流或信用壓力", "bonus",
             ("iv", "隱含波動", "大額交易人", "十大交易人", "etf flow", "信用利差", "financial stress"),
             ("台指選擇權 IV 大額交易人 十大交易人 信用利差 ETF flow",)),
    ]


def _research_requirements(request: CommandRequest) -> list[RequiredEvidence]:
    target = _target_label(request)
    return [
        _req("official_mops", "官方公告 / MOPS", "hard", ("mops", "公開資訊觀測站", "重大訊息", "公司公告"), (f"{target} 公開資訊觀測站 重大訊息 公司公告",)),
        _req("monthly_revenue", "月營收", "hard", ("月營收", "營收", "revenue"), (f"{target} 月營收 年增率",)),
        _req("financial_report", "財報", "hard", ("財報", "季報", "年報", "eps", "毛利率", "營益率"), (f"{target} 財報 EPS 毛利率 營益率",)),
        _req("ir_conference", "法說會 / IR", "soft", ("法說會", "法人說明會", "investor relations", "ir", "簡報"), (f"{target} 法說會 簡報 投資人關係 IR",)),
        _req("products_customers", "產品與客戶", "soft", ("產品", "客戶", "供應鏈", "出貨", "訂單", "customer"), (f"{target} 產品 客戶 供應鏈 訂單 出貨",)),
        _req("institutional_chip", "法人籌碼", "soft", ("外資", "投信", "自營商", "融資", "融券", "tdcc", "大戶"), (f"{target} 外資 投信 自營商 融資融券 TDCC",)),
        _req("counter_risk", "反證風險", "soft", ("風險", "下滑", "衰退", "庫存", "砍單", "毛利", "利空", "訴訟"), (f"{target} 風險 營收衰退 毛利下滑 庫存 砍單 利空",)),
    ]


def _theme_requirements(request: CommandRequest) -> list[RequiredEvidence]:
    theme = _target_label(request)
    return [
        _req("theme_definition", "題材定義", "hard", ("題材", "定義", "概念", "theme"), (f"{theme} 題材 定義 台股",)),
        _req("industry_trend", "產業趨勢", "hard", ("產業趨勢", "市場規模", "需求", "成長", "cagr"), (f"{theme} 產業趨勢 需求 成長 CAGR",)),
        _req("supply_chain_companies", "供應鏈公司", "hard", ("供應鏈", "上游", "下游", "供應商", "台股"), (f"{theme} 供應鏈 台股 上游 下游 供應商",)),
        _req("product_customer_validation", "產品或客戶驗證", "soft", ("產品", "客戶", "出貨", "訂單", "營收"), (f"{theme} 產品 客戶 出貨 訂單 營收",)),
        _req("catalyst", "催化劑", "soft", ("催化", "政策", "法說會", "新品", "訂單", "漲價"), (f"{theme} 催化劑 政策 新品 訂單 漲價",)),
        _req("cooling_counter_evidence", "退燒或反證", "soft", ("退燒", "風險", "需求放緩", "庫存", "反證"), (f"{theme} 退燒 需求放緩 庫存 風險 反證",)),
    ]


def _theme_radar_requirements(request: CommandRequest) -> list[RequiredEvidence]:
    return [
        _req("hot_themes", "熱門題材", "hard", ("熱門題材", "題材", "概念股", "強勢股"), ("台股 熱門題材 強勢股 概念股",)),
        _req("fund_rotation", "資金輪動", "hard", ("資金輪動", "族群輪動", "類股輪動", "成交量"), ("台股 資金輪動 族群輪動 成交量",)),
        _req("catalyst_news", "催化新聞", "soft", ("催化", "新聞", "政策", "訂單", "漲價", "法說會"), ("台股 題材 催化 新聞 政策 訂單 漲價",)),
        _req("theme_cooling", "題材退燒", "soft", ("退燒", "利空", "風險", "轉弱", "熄火"), ("台股 題材 退燒 利空 轉弱 風險",)),
        _req("social_auxiliary", "社群熱度輔助", "bonus", ("ptt", "dcard", "mobile01", "社群", "討論"), ("台股 題材 社群 討論 熱度 PTT Dcard",)),
    ]


def _value_scan_requirements(request: CommandRequest, structured_data: dict[str, Any]) -> list[RequiredEvidence]:
    pool = _target_label(request)
    return [
        _req("old_new_labels", "舊標籤 / 新標籤", "hard", ("舊標籤", "新標籤", "轉型", "重估", "rerating"), (f"{pool} 舊標籤 新標籤 價值重估 轉型",)),
        _req("revenue_financials", "月營收與財報", "hard", ("月營收", "財報", "eps", "毛利率", "營益率"), (f"{pool} 月營收 財報 EPS 毛利率",)),
        _req("product_customer_change", "產品客戶變化", "soft", ("新產品", "新客戶", "產品", "客戶", "供應鏈"), (f"{pool} 新產品 新客戶 供應鏈 客戶",)),
        _req("institutional_chip", "法人籌碼", "soft", ("外資", "投信", "自營商", "融資", "tdcc"), (f"{pool} 外資 投信 自營商 融資 TDCC",)),
        _req("rerating_failure_risk", "重估失敗風險", "soft", ("風險", "失敗", "下滑", "庫存", "估值過高", "利空"), (f"{pool} 重估失敗 估值過高 庫存 風險 利空",)),
    ]


def _news_requirements(request: CommandRequest) -> list[RequiredEvidence]:
    return [
        _req("market_news", "台股盤勢", "hard", ("加權", "櫃買", "成交量", "外資", "台指期"), ("台股 加權 櫃買 成交量 外資 台指期",)),
        _req("macro_risk_news", "總經與國際風險", "hard", ("vix", "美債", "美元", "fed", "油價", "戰爭", "關稅", "匯率"), ("VIX 美債 美元 Fed 油價 戰爭 關稅 匯率 台股",)),
        _req("theme_rotation_news", "題材輪動", "soft", ("題材", "輪動", "ai", "半導體", "電力", "機器人"), ("台股 題材輪動 AI 半導體 電力 機器人",)),
        _req("company_disclosure_news", "個股公告", "soft", ("mops", "重大訊息", "月營收", "法說會"), ("MOPS 重大訊息 月營收 法說會 台股",)),
        _req("chip_news", "資金與籌碼", "soft", ("三大法人", "投信", "融資", "大戶", "買賣超"), ("三大法人 投信 融資融券 大戶持股 台股",)),
        _req("counter_news", "反證新聞", "soft", ("退燒", "利空", "毛利下滑", "庫存", "需求轉弱"), ("台股 退燒 利空 毛利下滑 庫存 需求轉弱",)),
    ]


def _req(field: str, label: str, tier: str, keywords: tuple[str, ...], queries: tuple[str, ...]) -> RequiredEvidence:
    return RequiredEvidence(field=field, label=label, tier=tier, keywords=keywords, queries=queries)


def _target_label(request: CommandRequest) -> str:
    return str(
        request.target
        or request.market_scope
        or request.theme_scope
        or request.candidate_pool
        or "台股"
    ).strip()
