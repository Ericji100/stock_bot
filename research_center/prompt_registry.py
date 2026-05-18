from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ROOT_DIR
from .models import CommandRequest, SourceItem

PROMPT_ROOT = ROOT_DIR / "prompt"
PROMPT_BASE_DIR = PROMPT_ROOT / "base"
PROMPT_REPORT_DIR = PROMPT_ROOT / "report"
PROMPT_DISCOVERY_DIR = PROMPT_ROOT / "discovery"
PROMPT_SCORING_DIR = PROMPT_ROOT / "scoring"
PROMPT_RULES_DIR = PROMPT_ROOT / "rules"

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


def _prompt_structured_data(request: CommandRequest, structured_data: dict[str, Any]) -> str:
    """依指令類型使用專用 structured prompt pack，避免 [:22000] 截斷導致重要資料遺漏。

    - value_scan: 使用 ai_candidate_evidence_pack，完整不打折扣
    - research: 使用 research 專用 pack，保留 local_rerating_snapshot / local_scoring
    - macro: 使用 macro 專用 pack，保留 quantitative_market / fear_greed 等
    - theme: 使用 theme 專用 pack，保留 matched_universe / matched_companies
    """
    if request.command == "value_scan":
        if "ai_candidate_evidence_pack" in structured_data:
            pack = structured_data["ai_candidate_evidence_pack"]
            return _json({
                "ai_candidate_evidence_pack": pack,
                "candidate_pool": structured_data.get("candidate_pool"),
                "report_date": structured_data.get("report_date"),
                "total_candidate_count": structured_data.get("total_candidate_count"),
                "ai_candidate_limit": structured_data.get("ai_candidate_limit"),
                "scoring_rules": structured_data.get("scoring_rules"),
            })
        return _json(structured_data)

    if request.command == "research":
        return _json(_research_structured_prompt_data(structured_data))

    if request.command == "macro":
        return _json(_macro_structured_prompt_data(structured_data))

    if request.command == "theme":
        return _json(_theme_structured_prompt_data(structured_data))

    return _json(structured_data)


def _research_structured_prompt_data(structured_data: dict[str, Any]) -> dict[str, Any]:
    """Research 指令專用 structured prompt pack，完整保留評分與重估資料。"""
    chip_data = structured_data.get("chip_backup_data") or {}
    chip_summary = chip_data.get("summary") if isinstance(chip_data, dict) else None
    return {
        "stock": structured_data.get("stock"),
        "report_date": structured_data.get("report_date"),
        "technical_data": structured_data.get("technical_data"),
        "price_data": structured_data.get("price_data"),
        "institutional_data": structured_data.get("institutional_data"),
        "margin_data": structured_data.get("margin_data"),
        "revenue_data": structured_data.get("revenue_data"),
        "financial_data": structured_data.get("financial_data"),
        "strategy_summary": structured_data.get("strategy_summary"),
        "valuation_data": structured_data.get("valuation_data"),
        "tdcc_data": structured_data.get("tdcc_data"),
        "gross_margin_cache": structured_data.get("gross_margin_cache"),
        "chip_backup_summary": chip_summary,
        "mops_documents": structured_data.get("mops_documents"),
        "source_events": structured_data.get("source_events"),
        "local_rerating_snapshot": structured_data.get("local_rerating_snapshot"),
        "local_scoring": structured_data.get("local_scoring"),
        "historical_data_policy": structured_data.get("historical_data_policy"),
        "historical_snapshots": structured_data.get("historical_snapshots"),
    }


def _macro_structured_prompt_data(structured_data: dict[str, Any]) -> dict[str, Any]:
    """Macro 指令專用 structured prompt pack，完整保留總經指標與情緒資料。"""
    return {
        "market_scope": structured_data.get("market_scope"),
        "theme_scope": structured_data.get("theme_scope"),
        "region_scope": structured_data.get("region_scope"),
        "report_date": structured_data.get("report_date"),
        "noon_market_report": structured_data.get("noon_market_report"),
        "morning_market_report": structured_data.get("morning_market_report"),
        "quantitative_market": structured_data.get("quantitative_market"),
        "volatility": structured_data.get("volatility"),
        "industry_flow": structured_data.get("industry_flow"),
        "fear_greed": structured_data.get("fear_greed"),
        "market_score": structured_data.get("market_score"),
        "industry_index_data": structured_data.get("industry_index_data"),
        "free_public_sources": structured_data.get("free_public_sources"),
        "local_scoring": structured_data.get("local_scoring"),
        "historical_data_policy": structured_data.get("historical_data_policy"),
    }


def _theme_structured_prompt_data(structured_data: dict[str, Any]) -> dict[str, Any]:
    """Theme 指令專用 structured prompt pack，完整保留題材命中公司資料。"""
    return {
        "theme": structured_data.get("theme"),
        "report_date": structured_data.get("report_date"),
        "supply_chain_profile": structured_data.get("supply_chain_profile"),
        "company_knowledge_summary": structured_data.get("company_knowledge_summary"),
        "matched_universe": structured_data.get("matched_universe"),
        "matched_companies": structured_data.get("matched_companies") or structured_data.get("matched_universe"),
        "local_scoring": structured_data.get("local_scoring"),
        "historical_data_policy": structured_data.get("historical_data_policy"),
    }


def build_prompt_from_request(
    request: CommandRequest,
    structured_data: dict[str, Any],
    source_list: list[SourceItem],
) -> str:
    template = _template_for_request(request)
    base = _read_base_prompt("base.md")
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
        # value_scan 使用實際 AI 候選股數量，不要只看 request.top（deep 模式 request.top=None）
        "top_n": str(len(structured_data.get("ai_candidates", [])) or request.top or _default_top(request)),
        "report_date": report_date,
    }
    task_prompt = template.format(**variables)
    historical_rules = _historical_rules(request, structured_data)
    discovery_rules = _discovery_rules(request)

    # report context template (from prompt/rules/report_context.md)
    # 依指令使用專用 structured prompt pack，不做 [:22000] 截斷
    structured_data_json = _prompt_structured_data(request, structured_data)
    report_context = _read_rule_prompt("report_context.md").format(
        request_json=_json(asdict(request)),
        structured_data_json=structured_data_json,
        source_text=source_text,
    )

    # load rules based on command/mode
    rules_blocks = _rules_for_request(request)
    local_scoring_rules = _read_rule_prompt("local_scoring_and_ai_final_scoring.md")

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

{report_context}

{local_scoring_rules}

{rules_blocks}

請輸出完整 Markdown 報告。所有章節標題必須使用指定章節文字，且不得省略資料來源列表。
""".strip()


def _safe_task_id(value: str) -> str:
    import re
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", str(value)).strip("_")[:40] or "target"


def _flatten_queries(queries: list) -> list[str]:
    flat: list[str] = []
    for item in queries or []:
        if isinstance(item, dict):
            flat.extend(str(q) for q in item.get("items", []) if str(q).strip())
        elif str(item).strip():
            flat.append(str(item))
    return flat


def build_grounding_discovery_prompts(
    request: CommandRequest,
    structured_data: dict[str, Any],
    source_list: list[SourceItem],
) -> list[dict[str, Any]]:
    """Build multiple compact search prompts for higher quality Gemini grounding."""
    target = request.target or request.market_scope or request.theme_scope or request.candidate_pool or "latest"
    stock = structured_data.get("stock") or {}
    stock_name = stock.get("name") or ""
    report_date = request.report_date.isoformat() if request.report_date else datetime.now().date().isoformat()
    existing_sources = _source_text(source_list[:12])
    local_brief = _grounding_local_brief(request, structured_data)
    tasks = _grounding_discovery_tasks(request, structured_data)
    prompts: list[dict[str, str]] = []
    for index, task in enumerate(tasks, 1):
        label = str(task.get("label") or f"task_{index}")
        objective = str(task.get("objective") or _discovery_rules(request))

        exclude_items = task.get("exclude") or ["無"]
        exclude_text = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(exclude_items))

        queries = task.get("queries") or []
        if queries and isinstance(queries[0], dict):
            query_text = "\n".join(
                f"{group.get('title', '')}：\n" + "\n".join(f"- {q}" for q in group.get("items", []))
                for group in queries
            )
        else:
            query_text = "\n".join(f"- {query}" for query in queries)

        prompt = _format_discovery_prompt(
            _read_discovery_prompt("discovery_task.md"),
            index=index,
            total=len(tasks),
            label=label,
            command=request.command,
            target=target,
            mode=request.mode,
            report_date=report_date,
            stock_name=stock_name,
            objective=objective,
            exclude_text=exclude_text,
            query_text=query_text,
            local_brief_json=_json(local_brief)[:5000],
            existing_sources=existing_sources,
        ).strip()
        flat_queries = _flatten_queries(queries)
        prompts.append({"label": label, "prompt": prompt, "queries": flat_queries, "objective": objective})
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
                "label": "官方公告與財報",
                "objective": "請尋找公開資訊觀測站重大訊息、月營收公告、季報與年報、法說會簡報、股利政策、股東會資料、公司官網與投資人關係資料。",
                "exclude": ["技術面走勢", "法人買賣超", "論壇討論", "題材熱度", "產業競爭者比較"],
                "queries": [
                    {"title": "官方公告", "items": [
                        f"{target_label} 公開資訊觀測站 重大訊息",
                        f"{target_label} MOPS material information"
                    ]},
                    {"title": "月營收與財報", "items": [
                        f"{target_label} 月營收 財報 2026",
                        f"{target_label} Q1 財報 毛利率 EPS",
                        f"{target_label} monthly revenue financial report"
                    ]},
                    {"title": "法說會與投資人資料", "items": [
                        f"{target_label} 法說會 簡報 2026",
                        f"{target_label} 投資人關係 investor relations",
                        f"{target_label} investor conference presentation"
                    ]},
                    {"title": "股利與股東會", "items": [
                        f"{target_label} 股利政策 除息 股東會",
                        f"{target_label} dividend policy"
                    ]},
                    {"title": "風險與負面資訊", "items": [
                        f"{target_label} 營收 衰退 毛利率 下滑",
                        f"{target_label} 客戶 庫存 需求 風險",
                        f"{target_label} risk margin decline inventory"
                    ]}
                ],
            },
            {
                "label": "近期新聞與公司事件",
                "objective": "請尋找近期公司新聞、訂單、產品、管理層、營運展望、產能、客戶、併購、訴訟或市場事件。",
                "exclude": ["技術線型", "論壇情緒", "未具名爆料", "沒有日期的轉貼文"],
                "queries": [
                    {"title": "近期新聞", "items": [
                        f"{target_label} 近期新聞 營收 訂單 產品",
                        f"{target_label} MoneyDJ 鉅亨 工商 經濟日報 中央社",
                        f"{target_label} recent news revenue earnings product order"
                    ]},
                    {"title": "公司事件", "items": [
                        f"{target_label} 新產品 客戶 產能 展望",
                        f"{target_label} 管理層 併購 訴訟 風險"
                    ]}
                ],
            },
            {
                "label": "產業與題材",
                "objective": "請尋找產業趨勢、產品線、需求驅動、CAGR、市場規模、技術護城河、供應鏈位置、轉型效益與題材連結證據。",
                "exclude": ["短線技術面", "法人買賣超", "論壇喊單", "沒有營收連結的純題材文章"],
                "queries": [
                    {"title": "產業成長", "items": [
                        f"{target_label} 產業 趨勢 市場規模 CAGR",
                        f"{target_label} market size CAGR demand driver"
                    ]},
                    {"title": "產品與技術", "items": [
                        f"{target_label} 產品線 技術優勢 護城河",
                        f"{target_label} product line technology moat"
                    ]},
                    {"title": "供應鏈與轉型", "items": [
                        f"{target_label} 供應鏈 客戶 營收占比",
                        f"{target_label} 轉型 新產品 新應用"
                    ]}
                ],
            },
            {
                "label": "籌碼與法人",
                "objective": "請尋找公開可驗證的法人關注、外資投信、自營商、融資融券、股權結構、集保股權分散、董監持股或大戶籌碼資料。",
                "exclude": ["未具名主力傳聞", "論壇猜測", "技術型態解讀", "沒有來源的籌碼截圖"],
                "queries": [
                    {"title": "法人與籌碼", "items": [
                        f"{target_label} 外資 投信 自營商 買賣超",
                        f"{target_label} institutional investors foreign buying investment trust"
                    ]},
                    {"title": "股權結構", "items": [
                        f"{target_label} 集保 股權分散 大戶 董監持股",
                        f"{target_label} shareholder structure TDCC margin trading"
                    ]}
                ],
            },
            {
                "label": "風險與反證",
                "objective": "請尋找負面證據、風險、利空、需求放緩、毛利率壓力、庫存、客戶集中、價格競爭、景氣循環與看法矛盾之處。",
                "exclude": ["無來源的看空留言", "純技術面回檔", "沒有日期的舊新聞"],
                "queries": [
                    {"title": "營運風險", "items": [
                        f"{target_label} 風險 毛利率 下滑 庫存 需求放緩",
                        f"{target_label} risk margin pressure inventory demand slowdown"
                    ]},
                    {"title": "反證與矛盾", "items": [
                        f"{target_label} 利空 下修 競爭 客戶集中",
                        f"{target_label} bearish risk customer concentration competition"
                    ]}
                ],
            },
        ]
        if request.mode in {"score", "deep"}:
            tasks.append(
                {
                    "label": "評分證據",
                    "objective": "請尋找可用於量化評分與價值重估的證據，包括 CAGR、護城河、轉型效益、題材熱度、估值重估、反證與扣分依據。不得只因新聞熱門就給高分。",
                    "exclude": ["最終買賣建議", "目標價", "無來源評分", "純論壇情緒"],
                    "queries": [
                        {"title": "評分資料", "items": [
                            f"{target_label} CAGR 護城河 轉型效益 題材熱度",
                            f"{target_label} valuation rerating moat transformation evidence"
                        ]},
                        {"title": "扣分資料", "items": [
                            f"{target_label} 估值過高 營收未跟上 題材水分",
                            f"{target_label} weak revenue link valuation risk hype"
                        ]}
                    ],
                }
            )
        return tasks
    if request.command == "macro":
        market = request.market_scope or target
        macro_exclude = ["個股買賣建議", "未具名市場傳言", "無來源社群情緒", "過期資料"]
        return [
            {
                "label": "官方總經與市場資料",
                "objective": "請尋找台灣與全球的官方公開總經與市場資料，包括指數、利率、匯率、資金流與官方風險指標。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "官方資料", "items": [
                        f"{market} 官方 總經 數據 指數 匯率 利率 資金",
                        f"{market} official market data index FX rates liquidity"
                    ]}
                ],
            },
            {
                "label": "台股市場新聞",
                "objective": "請尋找近期台股市場新聞、指數驅動因素、類股輪動、法人資金流與風險事件。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "台股盤勢", "items": [
                        f"{market} 台股 盤勢 類股輪動 法人資金 風險",
                        f"{market} Taiwan stock market news sector rotation institutional flow"
                    ]}
                ],
            },
            {
                "label": "全球跨資產",
                "objective": "請尋找全球跨資產環境：美股四大指數、SOX、美債殖利率、美元/台幣、原油、黃金、VIX 與主要風險事件。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "跨資產", "items": [
                        "美債殖利率 美元 台幣 SOX Nasdaq VIX 油價 金價",
                        "US10Y USD TWD SOX Nasdaq VIX oil gold risk"
                    ]}
                ],
            },
            {
                "label": "地緣政治與貿易",
                "objective": "請尋找當前地緣政治、戰爭、制裁、關稅、出口管制、貿易政策發展，可能影響全球市場與台股。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "國際局勢", "items": [
                        f"{market} 國際局勢 戰爭 制裁 關稅 出口管制 貿易政策",
                        f"{market} geopolitics war sanctions tariffs export controls trade policy"
                    ]}
                ],
            },
            {
                "label": "央行政策與利率",
                "objective": "請尋找 Fed、歐洲央行、日本央行、中國人行、台灣央行政策、升降息預期、債券殖利率、美元指數、匯率。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "央行政策", "items": [
                        f"{market} Fed 歐洲央行 日本央行 中國人行 升息 降息 匯率",
                        f"{market} Fed ECB BOJ PBOC rate cut hike bond yield DXY FX"
                    ]}
                ],
            },
            {
                "label": "原物料與能源",
                "objective": "請尋找原油、天然氣、銅、鋁、黃金、鋼鐵、塑化、農產品原物料趨勢與成本壓力對市場和台灣產業的影響。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "原物料", "items": [
                        f"{market} 油價 天然氣 銅 鋁 黃金 鋼鐵 原物料 通膨",
                        f"{market} oil natural gas copper aluminum gold steel commodities"
                    ]}
                ],
            },
            {
                "label": "房地產與信用風險",
                "objective": "請尋找美國、中國、台灣、歐洲房地產、房貸、信用風險、銀行壓力與房市政策風險。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "房市信用", "items": [
                        f"{market} 房地產 房貸 信用風險 銀行壓力 中國房市 美國房市",
                        f"{market} real estate mortgage credit risk banking stress"
                    ]}
                ],
            },
            {
                "label": "期貨與波動率",
                "objective": "請尋找台指期、選擇權、波動率、期貨法人未平倉與台灣 VIX 資料。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "期貨波動率", "items": [
                        "台指期 選擇權 三大法人 未平倉 波動率 台灣 VIX TAIFEX",
                        "TAIFEX futures institutional open interest volatility Taiwan VIX"
                    ]}
                ],
            },
            {
                "label": "總經風險",
                "objective": "請尋找可能推翻樂觀看法的總經風險、政策風險、地緣政治風險、流動性風險、信用風險、原物料與匯率衝擊。",
                "exclude": macro_exclude,
                "queries": [
                    {"title": "總經風險", "items": [
                        f"{market} 總經 風險 流動性 信用風險 匯率 原物料",
                        f"{market} macro risk liquidity credit commodity FX shock"
                    ]}
                ],
            },
        ]
    if request.command == "theme":
        theme = request.theme_scope or request.target or target
        theme_exclude = ["個股買賣建議", "無營收連結的題材文章", "論壇喊單", "沒有來源的供應鏈名單"]
        return [
            {
                "label": "題材定義與市場規模",
                "objective": "請尋找題材的明確定義、需求驅動力、市場規模、CAGR 與關鍵產業證據。",
                "exclude": theme_exclude,
                "queries": [
                    {"title": "題材定義", "items": [
                        f"{theme} 題材 定義 市場規模 CAGR 需求驅動",
                        f"{theme} market size CAGR demand driver Taiwan stocks"
                    ]}
                ],
            },
            {
                "label": "台股供應鏈",
                "objective": "請尋找台股相關供應鏈公司、產品角色、上下游關係與每個角色的證據。",
                "exclude": theme_exclude,
                "queries": [
                    {"title": "供應鏈", "items": [
                        f"{theme} 台股 供應鏈 公司 產品 角色",
                        f"{theme} Taiwan supply chain companies product role"
                    ]}
                ],
            },
            {
                "label": "公司產品客戶與營收占比",
                "objective": "請尋找可能受惠公司的產品、客戶分類、營收占比、投資人資料與官方證據。",
                "exclude": theme_exclude,
                "queries": [
                    {"title": "公司證據", "items": [
                        f"{theme} 公司 產品 客戶 營收占比 法說會",
                        f"{theme} company revenue exposure customer product investor"
                    ]}
                ],
            },
            {
                "label": "近期催化因素",
                "objective": "請尋找近期新聞、訂單、政策催化、資本支出、需求轉折與短期事件。",
                "exclude": theme_exclude,
                "queries": [
                    {"title": "催化因素", "items": [
                        f"{theme} 近期新聞 訂單 政策 催化 資本支出",
                        f"{theme} recent news orders capex catalyst demand inflection"
                    ]}
                ],
            },
            {
                "label": "題材水分與反證",
                "objective": "請尋找題材水分、估值風險、營收連結薄弱的證據、矛盾資訊與只沾邊的股票。",
                "exclude": theme_exclude,
                "queries": [
                    {"title": "風險反證", "items": [
                        f"{theme} 風險 估值過高 題材水分 營收連結不足",
                        f"{theme} risk valuation hype weak revenue link contradiction"
                    ]}
                ],
            },
        ]
    if request.command == "value_scan":
        pool = request.candidate_pool or request.target or target
        candidates = structured_data.get("ai_candidates") or structured_data.get("candidates") or []
        top_codes = " ".join(str(row.get("code") or "") for row in candidates[:10]).strip()
        focus = f"{pool} {top_codes}".strip()
        vs_exclude = ["最終買賣建議", "無來源評分", "純論壇情緒", "沒有日期的舊資料"]
        return [
            {
                "label": "候選股近期新聞",
                "objective": "請尋找價值重估候選股近期新聞，特別是產品、訂單、客戶、營收與管理層變化。",
                "exclude": vs_exclude,
                "queries": [
                    {"title": "近期新聞", "items": [
                        f"{focus} 近期新聞 營收 產品 訂單 客戶",
                        f"{focus} recent news earnings product orders"
                    ]}
                ],
            },
            {
                "label": "官方公告與法說",
                "objective": "請尋找候選股的 MOPS 公告、財報、月營收、法說會與官方公司資料。",
                "exclude": vs_exclude,
                "queries": [
                    {"title": "官方資料", "items": [
                        f"{focus} 公開資訊觀測站 月營收 財報 法說會",
                        f"{focus} MOPS monthly revenue financial report investor conference"
                    ]}
                ],
            },
            {
                "label": "新舊標籤重估證據",
                "objective": "請尋找舊市場標籤與新市場標籤的證據，包括轉型、新產品線、新需求與客戶分類變化。",
                "exclude": vs_exclude,
                "queries": [
                    {"title": "重估證據", "items": [
                        f"{focus} 轉型 新產品 新應用 價值重估",
                        f"{focus} transformation new product rerating customer revenue exposure"
                    ]}
                ],
            },
            {
                "label": "估值與財務品質",
                "objective": "請尋找本益比、EPS、毛利率、營收成長、庫存、現金流等與重估品質相關的估值與財務證據。",
                "exclude": vs_exclude,
                "queries": [
                    {"title": "估值財務", "items": [
                        f"{focus} 本益比 EPS 毛利率 庫存 現金流",
                        f"{focus} valuation EPS margin revenue inventory cash flow"
                    ]}
                ],
            },
            {
                "label": "法人與籌碼",
                "objective": "請尋找法人關注、籌碼變化、外資/投信動態、股權集中度與流動性證據。",
                "exclude": vs_exclude,
                "queries": [
                    {"title": "法人籌碼", "items": [
                        f"{focus} 外資 投信 大戶 集保 融資融券",
                        f"{focus} institutional investor foreign buying shareholder concentration"
                    ]}
                ],
            },
            {
                "label": "重估失敗風險與反證",
                "objective": "請尋找重估失敗的下行風險、無營收的題材、客戶集中、景氣循環下行與矛盾看法。",
                "exclude": vs_exclude,
                "queries": [
                    {"title": "風險反證", "items": [
                        f"{focus} 重估失敗 風險 題材水分 營收未跟上",
                        f"{focus} rerating risk hype revenue weak customer concentration downturn"
                    ]}
                ],
            },
        ]
    return [{"label": "一般搜尋", "objective": _discovery_rules(request), "exclude": [], "queries": [{"title": "一般搜尋", "items": [str(target)]}]}]


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
        matched = structured_data.get("matched_companies") or structured_data.get("matched_universe") or []
        return {
            "theme": request.theme_scope or request.target,
            "matched_count": len(matched),
            "company_knowledge_summary": structured_data.get("company_knowledge_summary"),
            "top_companies": matched[:20],
        }
    if request.command == "value_scan":
        candidates = structured_data.get("ai_candidates") or structured_data.get("candidates") or []
        return {
            "candidate_pool": request.candidate_pool or request.target,
            "top_n": structured_data.get("top_n"),
            "candidate_count": len(candidates),
            "top_candidates": candidates[:20],
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
    return _read_report_prompt(_template_name_for_request(request))


def _template_name_for_request(request: CommandRequest) -> str:
    if request.source_only:
        return TEMPLATE_MAP[("source_only", "source_only")]
    return TEMPLATE_MAP.get((request.command, request.mode)) or TEMPLATE_MAP.get((request.command, "normal"), "research_summary.md")


def _mode_supplement(request: CommandRequest) -> str:
    supplements: list[str] = []
    if request.command == "macro" and request.mode == "deep":
        supplements.append(_read_report_prompt("macro_deep.md"))
    if request.command == "macro" and request.mode == "brief":
        supplements.append("Brief 模式補充要求：仍可產出本地報告檔，但 Telegram 摘要必須更短，只保留市場總結、風險等級、持股水位與 3 個觀察重點。")
    if request.command == "value_scan" and request.mode == "deep":
        supplements.append(_read_report_prompt("value_scan_deep.md"))
    return "\n\n".join(supplements)


def _historical_rules(request: CommandRequest, structured_data: dict[str, Any]) -> str:
    if request.report_date is None:
        return ""
    snapshots = structured_data.get("historical_snapshots") or {}
    return _read_rule_prompt("historical_rules.md").format(
        report_date=request.report_date.isoformat(),
        snapshot_status=snapshots.get("status", "unknown"),
        snapshot_count=snapshots.get("snapshot_count", 0),
    )


def _discovery_rules(request: CommandRequest) -> str:
    if request.report_date is not None:
        return ""
    if request.command == "research":
        if request.mode in {"score", "deep"}:
            return _read_rule_prompt("discovery_research.md")
        return _read_rule_prompt("discovery_research.md")
    if request.command == "theme":
        return _read_rule_prompt("discovery_theme.md")
    if request.command == "value_scan":
        return _read_rule_prompt("discovery_value_scan.md")
    if request.command == "macro":
        return _read_rule_prompt("discovery_macro.md")
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


def _rules_for_request(request: CommandRequest) -> str:
    """Load prompt rules files based on command and mode."""
    blocks: list[str] = []
    rules_map = {
        "research": {
            "normal": [
                "source_quality_rules.md",
                "risk_and_counter_evidence_rules.md",
            ],
            "deep": [
                "local_scoring_and_ai_final_scoring.md",
                "quantitative_score_rules.md",
                "rerating_snapshot_rules.md",
                "chip_score_rules.md",
                "technical_score_rules.md",
                "source_quality_rules.md",
                "risk_and_counter_evidence_rules.md",
            ],
            "score": [
                "local_scoring_and_ai_final_scoring.md",
                "quantitative_score_rules.md",
                "rerating_snapshot_rules.md",
                "source_quality_rules.md",
            ],
        },
        "value_scan": {
            "normal": [
                "local_scoring_and_ai_final_scoring.md",
                "rerating_snapshot_rules.md",
                "source_quality_rules.md",
                "risk_and_counter_evidence_rules.md",
            ],
            "deep": [
                "local_scoring_and_ai_final_scoring.md",
                "quantitative_score_rules.md",
                "rerating_snapshot_rules.md",
                "chip_score_rules.md",
                "technical_score_rules.md",
                "source_quality_rules.md",
                "risk_and_counter_evidence_rules.md",
            ],
            "source_only": [
                "source_quality_rules.md",
            ],
        },
        "macro": {
            "normal": ["source_quality_rules.md"],
            "deep": ["source_quality_rules.md"],
            "brief": ["source_quality_rules.md"],
        },
        "theme": {
            "normal": ["source_quality_rules.md"],
            "deep": ["source_quality_rules.md"],
            "source_only": ["source_quality_rules.md"],
        },
    }
    rule_files = rules_map.get(request.command, {}).get(request.mode, [])
    for fname in rule_files:
        content = _read_rule_prompt(fname)
        if content:
            blocks.append(f"## {fname}\n{content}")
    # --date mode loads historical_rules.md in addition
    if request.report_date is not None:
        hist = _read_rule_prompt("historical_date_rules.md")
        if hist:
            blocks.append(f"## historical_date_rules.md\n{hist}")
    return "\n\n".join(blocks)


def _read_base_prompt(name: str) -> str:
    path = PROMPT_BASE_DIR / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8-sig")


def _read_report_prompt(name: str) -> str:
    path = PROMPT_REPORT_DIR / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8-sig")


def _read_rule_prompt(name: str) -> str:
    path = PROMPT_RULES_DIR / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8-sig")


def _format_discovery_prompt(template: str, **kwargs) -> str:
    """Format discovery prompt template with safe variable replacement.

    Uses regex-based replacement that only matches simple {var_name} patterns
    (alphanumeric + underscore, NOT containing whitespace or quotes).
    This prevents JSON example content like {level} or {finding} from being
    incorrectly treated as format placeholders.
    """
    import re
    # Match {word_chars} only - prevents matching JSON-like content with spaces/quotes
    def replacer(m):
        key = m.group(1)
        if key in kwargs:
            return str(kwargs[key])
        # Leave unrecognized placeholders as-is (original text)
        return m.group(0)
    return re.sub(r'\{([A-Za-z0-9_]+)\}', replacer, template)


def _read_discovery_prompt(name: str) -> str:
    path = PROMPT_DISCOVERY_DIR / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8-sig")


def _read_prompt(name: str) -> str:
    """Read from legacy config/prompts/ for backward compatibility; new code uses _read_base_prompt / _read_report_prompt."""
    # Try new location first
    if name == "base.md":
        new_path = PROMPT_BASE_DIR / name
        if new_path.exists():
            return new_path.read_text(encoding="utf-8-sig")
    new_path = PROMPT_REPORT_DIR / name
    if new_path.exists():
        return new_path.read_text(encoding="utf-8-sig")
    # Fallback to legacy config/prompts/
    legacy_path = ROOT_DIR / "config" / "prompts" / name
    if legacy_path.exists():
        return legacy_path.read_text(encoding="utf-8-sig")
    return ""


def _read_scoring(name: str) -> str:
    # Try new location first, then fallback to legacy config/scoring/
    new_path = PROMPT_SCORING_DIR / name
    if new_path.exists():
        return new_path.read_text(encoding="utf-8-sig")
    legacy_path = ROOT_DIR / "config" / "scoring" / name
    if legacy_path.exists():
        return legacy_path.read_text(encoding="utf-8-sig")
    return "評分原稿檔案不存在，該項不得高分。"


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






