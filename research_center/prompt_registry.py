from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ROOT_DIR
from .models import CommandRequest, SourceItem

PROMPT_DIR = ROOT_DIR / "config" / "prompts"
SCORING_DIR = ROOT_DIR / "config" / "scoring"

TEMPLATE_MAP = {
    ("research", "normal"): "research_summary.md",
    ("research", "score"): "research_score.md",
    ("research", "deep"): "research_deep.md",
    ("macro", "normal"): "macro.md",
    ("macro", "brief"): "macro.md",
    ("macro", "deep"): "macro.md",
    ("theme", "normal"): "theme.md",
    ("theme", "deep"): "theme_deep.md",
    ("value_scan", "normal"): "value_scan.md",
    ("value_scan", "deep"): "value_scan.md",
    ("source_only", "source_only"): "source_only_summary.md",
}


def build_prompt_from_request(
    request: CommandRequest,
    structured_data: dict[str, Any],
    source_list: list[SourceItem],
) -> str:
    template = _template_for_request(request)
    base = _read_prompt("base.md")
    mode_supplement = _mode_supplement(request)
    scoring_rules = _scoring_rules_for_request(request)
    source_text = _source_text(source_list)
    report_date = request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat()

    variables = {
        "target": request.target or request.market_scope or request.candidate_pool or "latest",
        "stock_id": request.target or "",
        "stock_name": _stock_name(structured_data),
        "market_scope": request.market_scope or "全球 + 台股",
        "theme_scope": request.theme_scope or "未指定",
        "region_scope": request.region_scope or "global",
        "theme": request.theme_scope or request.target or "未指定題材",
        "candidate_pool": request.candidate_pool or request.target or "精選選股",
        "top_n": str(request.top or _default_top(request)),
        "report_date": report_date,
    }
    task_prompt = template.format(**variables)
    historical_rules = _historical_rules(request, structured_data)
    discovery_rules = _discovery_rules(request)

    return f"""{base}

---

{task_prompt}

{mode_supplement}

{historical_rules}

{discovery_rules}

---

評分與重估規則：
{scoring_rules}

---

指令解析 JSON：
```json
{_json(asdict(request))}
```

結構化資料：
```json
{_json(structured_data)[:22000]}
```

來源列表：
{source_text}

請輸出完整 Markdown 報告。所有章節標題必須使用指定章節文字，且不得省略資料來源列表。
""".strip()


def build_grounding_discovery_prompts(
    request: CommandRequest,
    structured_data: dict[str, Any],
    source_list: list[SourceItem],
) -> list[dict[str, Any]]:
    """Build multiple compact search prompts for higher quality Gemini grounding."""
    target = request.target or request.market_scope or request.theme_scope or request.candidate_pool or "latest"
    stock = structured_data.get("stock") or {}
    stock_name = stock.get("name") or ""
    report_date = datetime.now().date().isoformat()
    existing_sources = _source_text(source_list[:12])
    local_brief = _grounding_local_brief(request, structured_data)
    tasks = _grounding_discovery_tasks(request, structured_data)
    prompts: list[dict[str, str]] = []
    for index, task in enumerate(tasks, 1):
        label = str(task.get("label") or f"task_{index}")
        objective = str(task.get("objective") or _discovery_rules(request))
        queries = task.get("queries") or []
        query_text = "\n".join(f"- {query}" for query in queries)
        prompt = f"""You are the search discovery agent for a Taiwan stock AI research center.
You MUST use Google Search grounding. Do not answer from model memory only.

Discovery task {index}/{len(tasks)}: {label}
Command: /{request.command} {target}
Mode: {request.mode}
Report date: {report_date}
Target: {target} {stock_name}

Objective:
{objective}

Preferred search queries and angles:
{query_text}

Quality rules:
- Prioritize primary sources first: MOPS, TWSE, TPEx, TAIFEX, company website, investor relations, official filings.
- Then use mainstream financial or industry sources.
- Include contradictory evidence and risk sources when the task asks for risks.
- Do not fabricate sources, dates, customers, revenue exposure, CAGR, or conclusions.
- If evidence is insufficient, say "insufficient evidence".

Output format:
- At most 5 concise bullets of findings for this discovery task.
- Then list citeable sources. For each source include title, URL, date or unknown, and the claim it supports.

Local brief:
```json
{_json(local_brief)[:5000]}
```

Existing sources:
{existing_sources}
""".strip()
        prompts.append({"label": label, "prompt": prompt, "queries": list(queries), "objective": objective})
    return prompts


def build_grounding_discovery_prompt(
    request: CommandRequest,
    structured_data: dict[str, Any],
    source_list: list[SourceItem],
) -> str:
    """Backward compatible single discovery prompt; new code should use build_grounding_discovery_prompts."""
    prompts = build_grounding_discovery_prompts(request, structured_data, source_list)
    return prompts[0]["prompt"] if prompts else ""


def _grounding_discovery_tasks(request: CommandRequest, structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    target = request.target or request.market_scope or request.theme_scope or request.candidate_pool or "latest"
    stock = structured_data.get("stock") or {}
    stock_name = stock.get("name") or ""
    target_label = " ".join(part for part in [str(target), str(stock_name or "")] if part).strip()
    if request.command == "research":
        tasks = [
            {
                "label": "official_filings",
                "objective": "Find official disclosures, MOPS material information, monthly revenue, financial reports, dividends, investor conference materials, and company website evidence.",
                "queries": [
                    f"{target_label} MOPS material information monthly revenue financial report investor conference",
                    f"{target_label} company investor relations annual report dividend",
                    f"{target_label} ??????? ??? ?? ???",
                ],
            },
            {
                "label": "recent_news",
                "objective": "Find recent company news and mainstream financial news that explain recent price, revenue, order, product, or management changes.",
                "queries": [
                    f"{target_label} recent news revenue earnings product order",
                    f"{target_label} ?? MoneyDJ ?? ???? ??? ?? ??",
                ],
            },
            {
                "label": "industry_and_theme",
                "objective": "Find industry context, product lines, demand drivers, CAGR, technology moat, transformation effect, and theme heat evidence.",
                "queries": [
                    f"{target_label} industry product line market growth CAGR moat",
                    f"{target_label} ??? ?? ??? ??? ???? ??",
                ],
            },
            {
                "label": "chips_and_institutions",
                "objective": "Find public evidence about institutional attention, chips, foreign investors, margin trading, and shareholder structure if available.",
                "queries": [
                    f"{target_label} institutional investors foreign buying margin trading shareholder structure",
                    f"{target_label} ?? ?? ?? ?? ???? ??",
                ],
            },
            {
                "label": "risks_and_contradictions",
                "objective": "Find negative evidence, risks, bearish views, demand slowdown, margin pressure, inventory, customer concentration, and contradictory facts.",
                "queries": [
                    f"{target_label} risk margin pressure inventory demand slowdown",
                    f"{target_label} ?? ??? ?? ?? ?? ?? ??",
                ],
            },
        ]
        if request.mode in {"score", "deep"}:
            tasks.append(
                {
                    "label": "scoring_evidence",
                    "objective": "Find evidence specifically usable for scoring: CAGR, moat, transformation benefit, theme heat, valuation rerating, and counter-evidence. Never give high scores without sources.",
                    "queries": [
                        f"{target_label} CAGR moat transformation valuation rerating evidence",
                        f"{target_label} CAGR ??? ?? ?? ?? ??",
                    ],
                }
            )
        return tasks
    if request.command == "macro":
        market = request.market_scope or target
        return [
            {
                "label": "official_macro_data",
                "objective": "Find official/public macro and market data for Taiwan and global markets, including indices, rates, FX, and official risk indicators.",
                "queries": [f"{market} official market data index FX rates Taiwan stock", f"{market} 官方 市場 數據 指數 匯率 利率 資金"],
            },
            {
                "label": "taiwan_market_news",
                "objective": "Find recent Taiwan market news, index drivers, sector rotation, institutional flow context, and risk events.",
                "queries": [f"{market} Taiwan stock market news sector rotation institutional flow", f"{market} 台股 盤勢 類股輪動 法人資金 風險"],
            },
            {
                "label": "global_cross_asset",
                "objective": "Find global cross-asset context: US indices, SOX, US10Y, USD/TWD, oil, gold, VIX, and major risk events.",
                "queries": ["US10Y USD TWD SOX Nasdaq VIX oil gold market risk", "美債殖利率 美元 台幣 SOX Nasdaq VIX 油價 金價 風險"],
            },
            {
                "label": "geopolitics_trade_tariffs",
                "objective": "Find current geopolitical, war, sanctions, tariff, export control, and trade policy developments that could affect global markets and Taiwan stocks.",
                "queries": [f"{market} geopolitics war sanctions tariffs export controls trade policy market impact", f"{market} 國際局勢 戰爭 制裁 關稅 出口管制 貿易政策 股市 影響"],
            },
            {
                "label": "central_banks_rates_fx",
                "objective": "Find current Fed, ECB, BOJ, PBOC, Taiwan central bank policy, rate cut/hike expectations, bond yields, DXY, USD/TWD, CNY, JPY, and EUR FX context.",
                "queries": [f"{market} Fed ECB BOJ PBOC rate cut hike bond yield DXY USD TWD CNY JPY EUR", f"{market} Fed 歐洲央行 日本央行 中國人行 升息 降息 匯率 美元 台幣 人民幣 日圓 歐元"],
            },
            {
                "label": "commodities_energy",
                "objective": "Find current oil, natural gas, electricity, copper, aluminum, gold, steel, plastic, agriculture commodity trends and cost pressure for markets and Taiwan industries.",
                "queries": [f"{market} oil natural gas copper aluminum gold steel commodities inflation cost pressure", f"{market} 油價 天然氣 銅 鋁 黃金 鋼鐵 原物料 通膨 成本壓力"],
            },
            {
                "label": "real_estate_credit",
                "objective": "Find current US, China, Taiwan, and Europe real estate, mortgage, credit, bank stress, and property policy risks.",
                "queries": [f"{market} real estate mortgage credit risk banking stress property policy", f"{market} 房地產 房貸 信用風險 銀行壓力 房市政策 中國房市 美國房市"],
            },
            {
                "label": "futures_options_chips",
                "objective": "Find TAIFEX futures/options, VIX/volatility, futures institutional positioning, and official caveats where free data is limited.",
                "queries": ["TAIFEX futures institutional open interest option volatility Taiwan VIX", "台指期 選擇權 三大法人 未平倉 波動率 台灣 VIX"],
            },
            {
                "label": "macro_risks",
                "objective": "Find bearish macro risks, policy risks, geopolitical risks, earnings risks, liquidity risks, credit risks, and commodity/FX shocks that could invalidate bullish views.",
                "queries": [f"{market} macro risk policy geopolitical liquidity credit earnings commodity FX risk", f"{market} 總經 風險 政策 地緣政治 流動性 信用風險 匯率 原物料"],
            },
        ]
    if request.command == "theme":
        theme = request.theme_scope or request.target or target
        return [
            {
                "label": "theme_definition",
                "objective": "Find a clear definition of the theme, demand drivers, market size, CAGR, and key industry evidence.",
                "queries": [f"{theme} market size CAGR demand driver Taiwan stocks", f"{theme} ???? ??? ?? ?? ??"],
            },
            {
                "label": "supply_chain",
                "objective": "Find Taiwan supply chain companies, product roles, upstream/downstream relationships, and evidence for each role.",
                "queries": [f"{theme} Taiwan supply chain companies product role", f"{theme} ?? ??? ?? ?? ??"],
            },
            {
                "label": "company_evidence",
                "objective": "Find company-level product, customer category, revenue exposure, investor materials, and official evidence for likely beneficiaries.",
                "queries": [f"{theme} company revenue exposure customer product investor presentation", f"{theme} ???? ?? ?? ?? ??"],
            },
            {
                "label": "news_and_catalysts",
                "objective": "Find recent news, orders, policy catalysts, capex, demand inflection, and near-term events for the theme.",
                "queries": [f"{theme} recent news orders capex catalyst", f"{theme} ?? ?? ???? ?? ??"],
            },
            {
                "label": "risks_and_hype",
                "objective": "Find hype risks, valuation risks, weak revenue linkage, contradictory evidence, and companies that are only loosely related.",
                "queries": [f"{theme} risk valuation hype weak revenue link", f"{theme} ?? ?? ???? ?? ?? ??"],
            },
        ]
    if request.command == "value_scan":
        pool = request.candidate_pool or request.target or target
        candidates = structured_data.get("candidates") or []
        top_codes = " ".join(str(row.get("code") or "") for row in candidates[:10]).strip()
        focus = f"{pool} {top_codes}".strip()
        return [
            {
                "label": "candidate_news",
                "objective": "Find recent news for top rerating candidates, especially product, order, customer, earnings, and management changes.",
                "queries": [f"{focus} recent news earnings product orders", f"{focus} ?? ?? ?? ?? ?? ??"],
            },
            {
                "label": "official_announcements",
                "objective": "Find MOPS announcements, financial reports, monthly revenue, investor conferences, and official company filings for top candidates.",
                "queries": [f"{focus} MOPS monthly revenue financial report investor conference", f"{focus} ??????? ??? ?? ???"],
            },
            {
                "label": "old_new_label_evidence",
                "objective": "Find evidence for old market label versus new market label, including transformation, new product lines, new demand, and changing customer categories.",
                "queries": [f"{focus} transformation new product rerating customer revenue exposure", f"{focus} ?? ??? ?? ?? ????"],
            },
            {
                "label": "valuation_and_financials",
                "objective": "Find valuation, margins, EPS, revenue growth, inventory, cash flow, and balance-sheet evidence relevant to rerating quality.",
                "queries": [f"{focus} valuation EPS margin revenue inventory cash flow", f"{focus} ?? EPS ??? ?? ?? ???"],
            },
            {
                "label": "institutional_and_chips",
                "objective": "Find institutional attention, chip changes, foreign/investment trust activity, shareholder concentration, and liquidity evidence.",
                "queries": [f"{focus} institutional investor foreign buying shareholder concentration liquidity", f"{focus} ?? ?? ?? ?? ???"],
            },
            {
                "label": "rerating_risks",
                "objective": "Find downside risks, failed rerating evidence, hype without revenue, customer concentration, cyclical downturn, and contradictory views.",
                "queries": [f"{focus} rerating risk hype revenue weak customer concentration downturn", f"{focus} ?? ?? ???? ?? ?? ?? ??"],
            },
        ]
    return [{"label": "general_discovery", "objective": _discovery_rules(request), "queries": [str(target)]}]


def _grounding_local_brief(request: CommandRequest, structured_data: dict[str, Any]) -> dict[str, Any]:
    if request.command == "research":
        return {
            "stock": structured_data.get("stock"),
            "technical_data": structured_data.get("technical_data"),
            "latest_revenue": (structured_data.get("revenue_data") or [])[-3:],
            "latest_financial": (structured_data.get("financial_data") or [])[-4:],
            "valuation_data": structured_data.get("valuation_data"),
            "chip_backup_summary": (structured_data.get("chip_backup_data") or {}).get("summary"),
        }
    if request.command == "macro":
        return {
            "market_scope": request.market_scope,
            "market_score": structured_data.get("market_score"),
            "fear_greed": structured_data.get("fear_greed"),
            "global_public_macro": structured_data.get("global_public_macro"),
        }
    if request.command == "theme":
        return {
            "theme": request.theme_scope or request.target,
            "matched_count": len(structured_data.get("matched_companies") or []),
            "company_knowledge_summary": structured_data.get("company_knowledge_summary"),
            "top_companies": (structured_data.get("matched_companies") or [])[:20],
        }
    if request.command == "value_scan":
        return {
            "candidate_pool": request.candidate_pool or request.target,
            "top_n": structured_data.get("top_n"),
            "candidate_count": len(structured_data.get("candidates") or []),
            "top_candidates": (structured_data.get("candidates") or [])[:20],
        }
    return {"command": request.command}


def prompt_metadata(request: CommandRequest) -> dict[str, Any]:
    template_name = _template_name_for_request(request)
    scoring = []
    if request.command == "research" and request.mode in {"score", "deep"}:
        scoring.append("股票量化評分標準.md")
        scoring.append("股票標籤重估模型.md")
    if request.command == "value_scan":
        scoring.append("股票標籤重估模型.md")
        scoring.append("股票量化評分標準.md")
    return {
        "template": template_name,
        "base_prompt": "base.md",
        "scoring_files": scoring,
        "strict_sections": True,
        "source_rules": True,
    }


def _template_for_request(request: CommandRequest) -> str:
    return _read_prompt(_template_name_for_request(request))


def _template_name_for_request(request: CommandRequest) -> str:
    if request.source_only:
        return TEMPLATE_MAP[("source_only", "source_only")]
    return TEMPLATE_MAP.get((request.command, request.mode)) or TEMPLATE_MAP.get((request.command, "normal"), "research_summary.md")


def _mode_supplement(request: CommandRequest) -> str:
    supplements: list[str] = []
    if request.command == "macro" and request.mode == "deep":
        supplements.append(_read_prompt("macro_deep.md"))
    if request.command == "macro" and request.mode == "brief":
        supplements.append("Brief 模式補充要求：仍可產出本地報告檔，但 Telegram 摘要必須更短，只保留市場總結、風險等級、持股水位與 3 個觀察重點。")
    if request.command == "value_scan" and request.mode == "deep":
        supplements.append(_read_prompt("value_scan_deep.md"))
    return "\n\n".join(supplements)


def _historical_rules(request: CommandRequest, structured_data: dict[str, Any]) -> str:
    if request.report_date is None:
        return ""
    snapshots = structured_data.get("historical_snapshots") or {}
    return (
        "歷史日期模式強制規則：\n"
        f"- 報告日期為 {request.report_date.isoformat()}，不得使用該日期之後才發布或才可見的資訊。\n"
        "- Gemini Search / 現在網路搜尋已由程式停用；你只能整理結構化資料與 historical_snapshots。\n"
        f"- historical_snapshots 狀態：{snapshots.get('status')}，筆數：{snapshots.get('snapshot_count')}。\n"
        "- 若 snapshot 不足，必須明確寫資料不足，不得用現在已知結果補推。"
    )


def _discovery_rules(request: CommandRequest) -> str:
    if request.report_date is not None:
        return ""
    if request.command == "research":
        if request.mode in {"score", "deep"}:
            return "Gemini Search 任務：補找 CAGR、護城河、轉型效益、題材熱度、MOPS/年報/法說會與新聞來源；每個高分判斷都必須附來源，不足則保守給分。"
        return "Gemini Search 任務：補找公司近期重大消息、MOPS公告、月營收/財報新聞、法說會、產業新聞與市場討論；一般研究也必須嘗試搜尋並在資料來源列表列出 grounding citations。"
    if request.command == "theme":
        return "Gemini Search 任務：補找產品線、客戶分類、供應鏈角色、營收占比與證據來源；請區分已證實、推論、資料不足。"
    if request.command == "value_scan":
        return "Gemini Search 任務：補找公告、法說、年報、產品、客戶、產業題材、新聞與反證；不得只因新聞熱門提高分數。"
    if request.command == "macro":
        return "Gemini Search 任務：補找免費公開宏觀資料與新聞脈絡，但正式 IV/籌碼數字必須以結構化資料或官方來源為準。"
    return ""

def _scoring_rules_for_request(request: CommandRequest) -> str:
    blocks: list[str] = []
    if request.command == "research" and request.mode in {"score", "deep"}:
        blocks.append("## 股票量化評分標準原稿\n" + _read_scoring("股票量化評分標準.md"))
        blocks.append("## 股票標籤重估模型原稿\n" + _read_scoring("股票標籤重估模型.md"))
    elif request.command == "value_scan":
        blocks.append("## 股票標籤重估模型原稿\n" + _read_scoring("股票標籤重估模型.md"))
        blocks.append("## 股票量化評分標準中與重估相關的原稿\n" + _read_scoring("股票量化評分標準.md"))
    else:
        blocks.append("本模式不要求完整量化評分；若資料不足，不得自行給分。")
    return "\n\n".join(blocks)[:18000]


def _read_prompt(name: str) -> str:
    path = PROMPT_DIR / name
    return path.read_text(encoding="utf-8-sig")


def _read_scoring(name: str) -> str:
    path = SCORING_DIR / name
    if not path.exists():
        return "評分原稿檔案不存在，該項不得高分。"
    return path.read_text(encoding="utf-8-sig")


def _source_text(source_list: list[SourceItem]) -> str:
    if not source_list:
        return "目前沒有外部來源。"
    return "\n".join(
        f"[{item.source_id}] {item.source_level} {item.title} {item.url} published_date={item.published_date or 'unknown'}"
        for item in source_list
    )


def _stock_name(structured_data: dict[str, Any]) -> str:
    stock = structured_data.get("stock") or {}
    return str(stock.get("name") or "")


def _default_top(request: CommandRequest) -> int:
    if request.command == "theme":
        return 10
    if request.command == "value_scan":
        return 10
    return 0


def _json(value: Any) -> str:
    return json_dumps(value)


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2, default=str)






