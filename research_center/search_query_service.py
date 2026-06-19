from __future__ import annotations

from typing import Any

from .models import CommandRequest
from .preferred_sources import build_site_queries

SEARCH_QUERY_TEMPLATE_VERSION = "search_tasks_v1"


def build_search_discovery_tasks(request: CommandRequest, structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    command = request.command
    if command == "news":
        from .news_service import build_news_discovery_queries

        return build_news_discovery_queries("latest")
    if command == "research":
        return _research_tasks(request, structured_data)
    if command == "macro":
        return _macro_tasks(request, structured_data)
    if command == "value_scan":
        return _value_scan_tasks(request, structured_data)
    if command == "theme":
        return _theme_tasks(request, structured_data)
    if command == "theme_flow":
        return _theme_flow_tasks(request, structured_data)
    if command == "theme_radar":
        return _theme_radar_tasks(request, structured_data)
    if command == "sector_strength":
        return _sector_strength_tasks(request, structured_data)
    if command == "topic_maintain":
        return _topic_maintain_tasks(request, structured_data)
    return [_task("一般公開資料", "補找與指令目標直接相關的可靠公開資料、反證與資料缺口。", [_group("核心查詢", [_target_label(request, structured_data)])])]


def flatten_task_queries(tasks: list[dict[str, Any]]) -> list[str]:
    queries: list[str] = []
    for task in tasks:
        for query in _flatten_queries(task.get("queries") or []):
            if query and query not in queries:
                queries.append(query)
    return queries


def _research_tasks(request: CommandRequest, structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    target = _target_label(request, structured_data)
    tasks = [
        _task(
            "官方公告與財報",
            "補找 MOPS、TWSE、TPEx、公司 IR、月營收、財報、年報、重大訊息、法說會，建立個股研究的 Level 1 底稿。",
            [_group("官方硬資料", [
                f"{target} 公開資訊觀測站 重大訊息 月營收 財報",
                f"{target} 法說會 簡報 investor relations 年報",
                f"{target} TWSE TPEx MOPS monthly revenue financial report",
            ])],
            ["論壇", "社群", "YouTube 短評", "股價報價頁"],
            evidence_role="official_financials",
        ),
        _task(
            "產品客戶與供應鏈",
            "補找產品線、客戶應用、供應鏈位置、競爭者與產業趨勢，區分已驗證事實與推論型線索。",
            [_group("產業與公司關聯", [
                f"{target} 產品 客戶 供應鏈 應用 產業趨勢",
                f"{target} competitor supply chain product customer Taiwan",
                f"{target} 新產品 新客戶 訂單 產能 出貨",
            ])],
            ["純股價技術分析", "未具名傳聞"],
            evidence_role="industry_product_customer",
        ),
        _task(
            "法人籌碼與市場關注",
            "補找外資、投信、自營商、券商報告摘要、目標價調整與市場重新關注，但不得單獨作為基本面強證據。",
            [_group("法人與資金", [
                f"{target} 外資 投信 自營商 買賣超 法人 報告",
                f"{target} 目標價 評等 調升 調降 法人 2026",
                f"{target} institutional investors target price rating Taiwan stock",
            ])],
            ["論壇喊單", "無日期社群貼文"],
            evidence_role="institutional_flow_sentiment",
        ),
        _task(
            "反證與風險",
            "補找毛利率下滑、庫存、需求轉弱、客戶流失、展望下修、競爭加劇、估值過高等反證。",
            [_group("風險反證", [
                f"{target} 毛利率 下滑 庫存 需求 風險 反證",
                f"{target} 展望 下修 客戶流失 訂單遞延 利空 反證",
                f"{target} margin decline inventory demand risk customer loss",
            ])],
            ["只有情緒沒有事實的討論"],
            evidence_role="risk_counter_evidence",
        ),
    ]
    if request.mode in {"deep", "score"}:
        tasks.append(_task(
            "評分與價值重估證據",
            "補找 CAGR、護城河、轉型效益、題材催化、價值重估與早期想像空間；推論型加分必須列出待驗證資料。",
            [_group("推論型潛力", [
                f"{target} CAGR 護城河 轉型效益 價值重估 題材催化",
                f"{target} rerating moat transformation catalyst high growth",
                f"{target} 新標籤 舊標籤 市場重新定價 產品升級",
            ])],
            ["只靠股價上漲或論壇熱度"],
            evidence_role="rerating_imagination_with_verification",
        ))
    return _append_site_queries(tasks)


def _macro_tasks(request: CommandRequest, structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    scope = request.market_scope or structured_data.get("market_scope") or "台股"
    exclude = ["單一個股報價頁", "無日期社群貼文", "純技術線圖頁", "廣告導購頁"]
    return [
        _task(
            "國際風險溫度",
            "補找 VIX、美債殖利率、美元指數、油價、黃金、Fed、通膨與升降息預期，判斷 risk-on / risk-off。",
            [_group("VIX / Fed / 利率", [
                f"{scope} VIX 美債殖利率 美元指數 油價 原油 黃金 Fed 通膨 台股 影響",
                f"{scope} FOMC CPI PCE rate cut hike Treasury yield DXY oil crude gold Taiwan stocks",
                f"{scope} risk on risk off volatility VIX bond yield dollar index",
            ])],
            exclude,
            evidence_role="global_risk_temperature",
        ),
        _task(
            "台指期與選擇權",
            "補找台指期、夜盤、外資期貨淨部位、台指選擇權 Put/Call、未平倉、波動率與避險需求；正式數字不足時需標示資料不足。",
            [_group("期貨選擇權", [
                f"{scope} 台指期 夜盤 外資期貨 淨多單 淨空單 未平倉",
                f"{scope} 台指選擇權 Put Call ratio 未平倉 波動率",
                f"TAIFEX TAIEX futures options put call ratio open interest volatility {scope}",
            ])],
            exclude,
            evidence_role="taiwan_derivatives_risk",
        ),
        _task(
            "台股資金流與籌碼",
            "補找三大法人、外資、投信、自營商、融資融券、成交量、類股資金流與市場寬度。",
            [_group("法人資金", [
                f"{scope} 三大法人 外資 投信 自營商 買賣超 融資融券 成交量",
                f"{scope} 台股 資金流 類股 輪動 法人 買超 賣超",
                f"{scope} institutional flow margin balance short selling sector rotation Taiwan",
            ])],
            exclude,
            evidence_role="taiwan_market_flow",
        ),
        _task(
            "恐慌貪婪與情緒 proxy",
            "補找 CNN Fear & Greed、VIX、台股情緒、融資、期貨、選擇權與成交量 proxy；不得捏造正式台股恐慌貪婪指數。",
            [_group("情緒 proxy", [
                f"{scope} 恐慌 貪婪 指數 proxy VIX 台指選擇權 融資",
                f"{scope} CNN Fear Greed Index VIX Taiwan stock sentiment proxy",
                f"{scope} 台股 過熱 恐慌 避險 貪婪 市場情緒",
            ])],
            exclude,
            evidence_role="fear_greed_proxy",
        ),
        _task(
            "地緣政治與區域市場",
            "補找中國、歐洲、日本、美國、SOX、Nasdaq、關稅、戰爭、原物料、供應鏈與匯率等對台股的宏觀影響。",
            [_group("區域與政策", [
                f"{scope} 中國 歐洲 日本 美國 SOX Nasdaq 關稅 戰爭 原物料 匯率 台股 影響",
                f"{scope} China Europe Japan SOX Nasdaq tariff geopolitics commodities FX Taiwan stocks",
                f"{scope} 地緣政治 供應鏈 政策 央行 匯率 風險",
            ])],
            exclude,
            evidence_role="geo_policy_macro_risk",
        ),
        _task(
            "反證與壓力測試",
            "補找與主流樂觀情境相反的風險：流動性收縮、信用壓力、美元走強、油價急升、台幣急貶、外資撤出。",
            [_group("壓力測試", [
                f"{scope} 流動性 風險 信用壓力 美元走強 油價 原油 急升 台幣 急貶",
                f"{scope} macro risk liquidity credit stress strong dollar oil shock foreign outflow",
                f"{scope} 台股 反證 風險 外資 賣超 期貨 淨空單",
            ])],
            exclude,
            evidence_role="macro_counter_evidence",
        ),
    ]


def _theme_tasks(request: CommandRequest, structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    theme = request.theme_scope or request.target or structured_data.get("theme") or "台股題材"
    companies = structured_data.get("matched_companies") or structured_data.get("matched_universe") or []
    company_text = " ".join(_candidate_labels(companies, limit=8))
    return _append_site_queries([
        _task("題材定義與產業趨勢", "補找題材定義、產業趨勢、需求來源、全球脈絡與台灣供應鏈位置。", [_group("題材趨勢", [
            f"{theme} 題材 定義 產業趨勢 台灣供應鏈",
            f"{theme} industry trend Taiwan supply chain demand catalyst",
            f"{theme} {company_text} 受惠 產品 應用".strip(),
        ])], ["社群喊單", "單純股價排行"], evidence_role="theme_industry_trend"),
        _task("產品客戶與營收驗證", "補找產品、客戶、訂單、出貨、營收占比與可驗證催化。", [_group("驗證資料", [
            f"{theme} 產品 客戶 訂單 出貨 營收 占比",
            f"{theme} 台廠 法說會 月營收 新產品 新客戶",
            f"{theme} product customer revenue order shipment Taiwan companies",
        ])], ["只有概念股清單"], evidence_role="theme_verified_business"),
        _task("題材反證與退燒", "補找題材退燒、估值過高、需求不如預期、毛利壓力、競爭加劇與政策風險。", [_group("反證", [
            f"{theme} 退燒 過熱 估值過高 需求不如預期",
            f"{theme} 毛利率 壓力 競爭 風險 反證",
            f"{theme} risk overheat demand slowdown margin pressure",
        ])], ["無來源日期討論"], evidence_role="theme_counter_evidence"),
    ])


def _theme_flow_tasks(request: CommandRequest, structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    theme = request.theme_scope or request.target or structured_data.get("theme_query") or "台股題材"
    return _append_site_queries([
        _task("題材擴散路徑", "補找題材從核心股擴散到上下游、次族群與補漲族群的證據。", [_group("擴散", [
            f"{theme} 上游 下游 供應鏈 擴散 次族群 補漲",
            f"{theme} 資金輪動 族群擴散 台股 概念股",
            f"{theme} supply chain rotation upstream downstream Taiwan stocks",
        ])], ["單一股票報價頁"], evidence_role="theme_flow_expansion"),
        _task("輪動失敗與退潮", "補找成交量退潮、法人轉賣、新聞熱度下降與題材輪動失敗。", [_group("退潮反證", [
            f"{theme} 輪動 失敗 退潮 法人 賣超 成交量 萎縮",
            f"{theme} 熱度 下降 題材 退燒 反證",
            f"{theme} rotation failed cooling volume decline risk",
        ])], ["社群情緒"], evidence_role="theme_flow_counter_evidence"),
    ])


def _theme_radar_tasks(request: CommandRequest, structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    return _append_site_queries([
        _task("熱門題材與資金輪動", "補找近期爆量題材、資金輪動、主流財經與產業媒體證據，降低社群與影片雜訊。", [_group("熱門題材", [
            "上市櫃 今日 漲停 量增 題材 族群 輪動 TWSE TPEx",
            "台股 近期 熱門題材 資金輪動 主流財經 產業新聞",
            "台股 題材 爆量 族群 輪動 法人 主流媒體",
            "Taiwan stocks theme radar sector rotation institutional flow",
        ])], ["YouTube 短評", "Facebook 貼文", "Threads", "論壇喊單", "farmers market"], evidence_role="theme_radar_hot_rotation"),
        _task("題材催化與新聞爆量", "補找新產品、新訂單、政策、法說會、產業催化與剛啟動題材；推論型加分需列待驗證指標。", [_group("催化", [
            "台股 新題材 催化 新產品 新訂單 法說會 供應鏈",
            "台股 概念股 早期 題材 產業趨勢 可追蹤 催化",
            "Taiwan stocks early theme catalyst product order supply chain",
        ])], ["無來源傳聞", "farmers market"], evidence_role="theme_radar_early_catalyst"),
        _task("退燒題材與反證", "補找過熱、退燒、新聞降溫、法人轉賣、成交量退潮與反證來源。", [_group("退燒反證", [
            "台股 題材 退燒 過熱 法人 賣超 成交量 反證",
            "台股 熱門族群 退潮 風險 估值 過高",
            "Taiwan theme stocks overheat cooling risk institutional selling",
        ])], ["單純排行榜"], evidence_role="theme_radar_counter_evidence"),
    ], max_base_queries=2, max_site_per_task=3)


def _sector_strength_tasks(request: CommandRequest, structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    sectors = [str(row.get("sector") or "").strip() for row in (structured_data.get("sector_rankings") or [])[:5] if isinstance(row, dict) and str(row.get("sector") or "").strip()]
    sector_text = " ".join(sectors) or "半導體 AI 電子 金融 傳產"
    return _append_site_queries([
        _task("類股資金與法人", "補找強勢類股的資金流、法人買賣、族群強弱與輪動。", [_group("類股強弱", [
            f"台股 {sector_text} 類股 強弱 資金流 法人 買賣超 TWSE TPEx",
            f"台股 {sector_text} 族群 輪動 產業新聞 主流財經",
            f"Taiwan stock sectors {sector_text} fund flow institutional buying",
        ])], ["個股報價頁"], evidence_role="sector_flow_strength"),
        _task("類股反證", "補找短線過熱、法人轉賣、輪動失敗、產業利空與需求轉弱。", [_group("類股風險", [
            f"台股 {sector_text} 過熱 法人 賣超 輪動 失敗",
            f"台股 {sector_text} 產業 風險 需求 轉弱 毛利 壓力",
            f"Taiwan sectors overheat rotation failed demand slowdown risk",
        ])], ["社群喊單"], evidence_role="sector_counter_evidence"),
    ])


def _value_scan_tasks(request: CommandRequest, structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    pool = request.candidate_pool or request.target or structured_data.get("candidate_pool") or "台股候選股"
    candidates = structured_data.get("ai_candidates") or structured_data.get("candidates") or []
    batches = _value_scan_candidate_batches(candidates, pool=str(pool), deep=request.mode == "deep")
    tasks = [
        _task("官方公告與月營收", "補找候選股公告、重大訊息、月營收、財報、毛利率、EPS、法說會與公司 IR，驗證重估是否有硬資料支撐。", [_group("官方財務", [
            f"{batch} MOPS 公告 重大訊息 月營收 YoY 財報 毛利率 EPS 法說會 公司 IR" for batch in batches
        ])], ["論壇", "純股價排行"], evidence_role="支持重估：official_financials；資料不足時標示 insufficient"),
        _task("產品客戶與供應鏈驗證", "補找產品、客戶、營收占比、供應鏈角色、同業與訂單出貨，避免只靠新聞熱度。", [_group("交叉驗證", [
            f"{batch} 產品 客戶 供應鏈 營收占比 訂單 出貨 新產品 新客戶" for batch in batches
        ])], ["只有 snippet 的客戶傳聞"], evidence_role="支持重估：business_validation；缺證據標示資料不足"),
        _task("舊標籤與新標籤重估", "補找舊業務、新產品、新客戶、新應用、轉型效益與市場重新貼標籤證據。", [_group("重估標籤", [
            f"{batch} 舊標籤 新標籤 新產品 新客戶 轉型 價值重估" for batch in batches
        ])], ["無來源題材想像"], evidence_role="支持重估；推論型加分需標示只作情緒或資料不足"),
        _task("法人籌碼與資金確認", "補找法人報告摘要、目標價調整、外資投信、自營商、TDCC 與主流媒體重貼標籤。", [_group("法人市場", [
            f"{batch} 法人 報告 目標價 評等 外資 投信 自營商 TDCC 重估" for batch in batches
        ])], ["論壇喊單"], evidence_role="只作情緒或支持重估的輔助證據"),
        _task("反證與重估失敗風險", "補找營收未驗證、毛利下滑、估值已反映、題材退燒、庫存與客戶集中風險。", [_group("反證風險", [
            f"{batch} 營收 未驗證 毛利 下滑 庫存 估值 過高 反證 毛利 下滑" for batch in batches
        ])], ["只看股價漲跌"], evidence_role="支持反證：counter_evidence；不足則標示資料不足"),
    ]
    for task in tasks:
        task["query_policy"] = {
            "candidate_count": len(candidates) if isinstance(candidates, list) else 0,
            "candidate_batches": len(batches),
                "strategy": "focus_batches_plus_pool_context",
        }
    return _append_site_queries(tasks, max_base_queries=1, max_site_per_task=2)


def _topic_maintain_tasks(request: CommandRequest, structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    plan_items = (
        (structured_data.get("candidate_discovery_plan") or {}).get("search_query_plan")
        if isinstance(structured_data.get("candidate_discovery_plan"), dict)
        else None
    )
    if plan_items:
        tasks: list[dict[str, Any]] = []
        for item in plan_items[:8]:
            if not isinstance(item, dict):
                continue
            query = str(item.get("query") or "").strip()
            if not query:
                continue
            bucket = str(item.get("bucket") or item.get("type") or "topic_market")
            tasks.append(_task(
                bucket,
                "補找題材庫維護證據：新題材、退燒題材、供應鏈新增節點、公司產品客戶、營收驗證與反證。",
                [_group(bucket, [query])],
                ["只有社群熱度", "無公司關聯的概念詞"],
                evidence_role="topic_library_evidence",
            ))
        for task in tasks:
            task["query_policy"] = {
                "strategy": "topic_maintain_representative_budget",
                "max_plan_items": 8,
                "max_site_queries_per_task": 1,
            }
        return _append_site_queries(tasks, max_base_queries=1, max_site_per_task=1)
    return _append_site_queries([
        _task("題材庫候選與反證", "補找新題材、舊題材退燒、供應鏈新增節點、產品客戶、營收驗證與資料不足。", [_group("題材維護", [
            "台股 新題材 供應鏈 新產品 客戶 營收 驗證",
            "台股 題材 退燒 反證 需求 不如預期",
            "Taiwan stock new theme supply chain product customer evidence",
        ])], ["只有社群熱度"], evidence_role="topic_library_evidence")
    ])


def _task(
    label: str,
    objective: str,
    queries: list[dict[str, list[str]] | str],
    exclude: list[str] | None = None,
    *,
    evidence_role: str | None = None,
) -> dict[str, Any]:
    task = {"label": label, "objective": objective, "exclude": exclude or [], "queries": queries}
    if evidence_role:
        task["evidence_role"] = evidence_role
    return task


def _group(title: str, items: list[str]) -> dict[str, list[str]]:
    return {"title": title, "items": [item for item in items if str(item).strip()]}


def _target_label(request: CommandRequest, structured_data: dict[str, Any]) -> str:
    stock = structured_data.get("stock") or {}
    parts = [
        request.target or request.market_scope or request.theme_scope or request.candidate_pool or "",
        stock.get("name") or "",
    ]
    return " ".join(str(part).strip() for part in parts if str(part).strip()) or "台股"


def _candidate_labels(rows: list[dict[str, Any]], *, limit: int = 32) -> list[str]:
    labels: list[str] = []
    for row in rows[:limit]:
        code = str(row.get("code") or row.get("stock_id") or "").strip()
        name = str(row.get("name") or row.get("stock_name") or "").strip()
        label = " ".join(part for part in [code, name] if part).strip()
        if label:
            labels.append(label)
    return labels


def _candidate_batches(rows: list[dict[str, Any]], *, batch_size: int = 4, max_batches: int = 8) -> list[str]:
    labels = _candidate_labels(rows, limit=batch_size * max_batches)
    return [
        " ".join(labels[index:index + batch_size])
        for index in range(0, len(labels), batch_size)
        if labels[index:index + batch_size]
    ]


def _value_scan_candidate_batches(rows: list[dict[str, Any]], *, pool: str, deep: bool = False) -> list[str]:
    max_batches = 2 if deep else 1
    batch_size = 5 if deep else 4
    batches = _candidate_batches(rows, batch_size=batch_size, max_batches=max_batches)
    pool_text = str(pool or "").strip()
    if pool_text and pool_text not in batches:
        batches.append(f"{pool_text} 候選股集合")
    return batches or [pool_text or "台股候選股"]


def _append_site_queries(tasks: list[dict[str, Any]], *, max_base_queries: int = 2, max_site_per_task: int = 4) -> list[dict[str, Any]]:
    for task in tasks:
        base_queries: list[str] = []
        for group in task.get("queries", []):
            if isinstance(group, dict):
                base_queries.extend(str(item) for item in group.get("items", []) if str(item).strip())
            elif str(group).strip():
                base_queries.append(str(group).strip())
        added = 0
        for query in base_queries[:max_base_queries]:
            for site_query in build_site_queries(query, max_domains=max_site_per_task):
                if added >= max_site_per_task:
                    break
                task.setdefault("queries", []).append(site_query)
                added += 1
            if added >= max_site_per_task:
                break
    return tasks


def _flatten_queries(queries: list[Any]) -> list[str]:
    result: list[str] = []
    for group in queries:
        if isinstance(group, dict):
            for item in group.get("items") or []:
                text = str(item).strip()
                if text:
                    result.append(text)
        else:
            text = str(group).strip()
            if text:
                result.append(text)
    return result
