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
    if command == "theme_radar":
        return _append_site_queries([
            _task("熱門題材與資金輪動", "找出台股近期強勢題材與量價資金輪動。", [_group("題材輪動", [
                "上市櫃 今日 漲停 量增 題材 族群 輪動",
                "台股 題材 資金輪動 TWSE TPEx",
            ])], ["farmers market"]),
            _task("題材催化與新聞爆量", "找出新聞爆量與可驗證催化事件。", [_group("催化", [
                "台股 題材 催化 新聞 爆量 TWSE TPEx",
            ])], ["farmers market"]),
            _task("退燒題材與反證", "找出題材退燒、風險與反證。", [_group("反證", [
                "台股 題材 退燒 反證 風險 TWSE TPEx",
            ])], ["farmers market"]),
        ])
    if command == "sector_strength":
        sectors = [str(row.get("sector") or "").strip() for row in (structured_data.get("sector_rankings") or [])[:5] if isinstance(row, dict) and str(row.get("sector") or "").strip()]
        sector_text = " ".join(sectors) or "半導體業 電子零組件業"
        return [_task("類股強弱與資金流", "搜尋台股類股強弱、產業資金流、代表股與反證。", [_group("類股", [
            f"台股 {sector_text} 類股 強弱 資金流 TWSE TPEx",
            f"台股 {sector_text} 代表股 新聞 法說 TWSE TPEx",
        ])])]
    if command == "value_scan":
        pool = request.candidate_pool or request.target or structured_data.get("candidate_pool") or "台股"
        candidates = structured_data.get("ai_candidates") or structured_data.get("candidates") or []
        batches = _candidate_batches(candidates) or [str(pool)]
        return [
            _task("價值重估候選驗證", "搜尋候選股舊標籤、新標籤、產品、客戶、營收驗證與反證。", [_group("候選股", [
                f"{batch} 價值重估 產品 客戶 營收 法說 新聞" for batch in batches
            ])]),
            _task("市場重新定價線索", "搜尋法人、新聞、產業趨勢與題材推論是否開始被市場定價。", [_group("重估線索", [
                f"{batch} 法人 新聞 轉型 題材 市場重估" for batch in batches
            ])]),
        ]
    if command == "topic_maintain":
        plan_items = (
            (structured_data.get("candidate_discovery_plan") or {}).get("search_query_plan")
            if isinstance(structured_data.get("candidate_discovery_plan"), dict)
            else None
        )
        if plan_items:
            tasks: list[dict[str, Any]] = []
            for item in plan_items[:40]:
                if not isinstance(item, dict):
                    continue
                query = str(item.get("query") or "").strip()
                if not query:
                    continue
                bucket = str(item.get("bucket") or item.get("type") or "topic_market")
                tasks.append(_task(
                    bucket,
                    "搜尋台股近期題材、代表公司、供應鏈、風險與反證；題材庫維護需保持跨產業廣度，不可只集中在 AI 或半導體。",
                    [_group(bucket, [query])],
                    ["只列股票名稱但無題材原因", "無來源的社群傳聞", "過度重複的 AI 題材"],
                ))
            return _append_site_queries(tasks, max_base_queries=2, max_site_per_task=3)
        return _append_site_queries([
            _task("題材庫更新候選", "搜尋可寫入題材庫的題材、供應鏈節點、公司關聯與來源。", [_group("題材維護", [
                "台股 新題材 供應鏈 產業趨勢",
                "台股 法說會 新產品 供應鏈 客戶 產業新聞",
                "Taiwan stocks new theme supply chain product customer",
            ])])
        ])
    if command == "research":
        target = _target_label(request, structured_data)
        tasks = [
            _task("官方公告與財報", "搜尋 MOPS、公司官網、法說會、月營收、財報與股利等正式資料。", [_group("官方與財報", [
                f"{target} 公開資訊觀測站 重大訊息",
                f"{target} 月營收 財報 毛利率 EPS",
                f"{target} 法說會 簡報 投資人關係",
            ])]),
            _task("產業與競爭位置", "搜尋產業趨勢、產品線、客戶供應鏈、競爭對手與風險反證。", [_group("產業與產品", [
                f"{target} 產品 客戶 供應鏈 產業趨勢",
                f"{target} risk margin decline inventory",
            ])]),
        ]
        if request.mode in {"score", "deep"}:
            tasks.append(_task("評分與價值重估證據", "搜尋 CAGR、護城河、轉型、重估催化劑與可驗證反證。", [_group("重估與反證", [
                f"{target} CAGR 護城河 轉型效益 題材熱度 價值重估",
                f"{target} valuation rerating moat transformation catalyst",
                f"{target} 題材未驗證 營收未反映 估值過高 風險",
            ])], ["社群單一喊單"]))
        return tasks
    if command == "macro":
        scope = request.market_scope or structured_data.get("market_scope") or "台股 全球總經"
        return [
            _task("總經與利率", "搜尋利率、匯率、通膨、Fed、央行與資金環境。", [_group("總經", [
                f"{scope} 利率 匯率 通膨 Fed 央行 台股",
            ])]),
            _task("國際局勢與原物料", "搜尋關稅、油價、戰爭、原物料與區域市場影響。", [_group("國際事件", [
                f"{scope} 關稅 油價 戰爭 原物料 台股 影響",
            ])]),
        ]
    if command == "theme":
        theme = request.theme_scope or request.target or structured_data.get("theme") or "台股題材"
        companies = structured_data.get("matched_companies") or structured_data.get("matched_universe") or []
        company_text = " ".join(_candidate_labels(companies, limit=8))
        return [
            _task("題材催化與供應鏈", "搜尋題材催化、供應鏈、產品、客戶與可驗證公司關聯。", [_group("題材", [
                f"{theme} 台股 供應鏈 產品 客戶",
                f"{theme} {company_text} 供應鏈 法說會 新聞".strip(),
            ])]),
            _task("反證與退燒風險", "搜尋題材降溫、訂單風險、政策變化與市場反證。", [_group("風險", [
                f"{theme} 風險 降溫 反證 庫存 需求",
            ])]),
        ]
    if command == "theme_flow":
        theme = request.theme_scope or request.target or structured_data.get("theme_query") or "台股題材"
        return [_task("題材擴散鏈", "搜尋題材上下游擴散、下一層受惠與反證。", [_group("擴散鏈", [
            f"{theme} 上游 下游 供應鏈 台股",
        ])])]
    return [_task("一般搜尋", "搜尋公開來源並標記來源可信度。", [_group("一般搜尋", [_target_label(request, structured_data)])])]


def flatten_task_queries(tasks: list[dict[str, Any]]) -> list[str]:
    queries: list[str] = []
    for task in tasks:
        for query in _flatten_queries(task.get("queries") or []):
            if query and query not in queries:
                queries.append(query)
    return queries


def _task(label: str, objective: str, queries: list[dict[str, list[str]] | str], exclude: list[str] | None = None) -> dict[str, Any]:
    return {"label": label, "objective": objective, "exclude": exclude or [], "queries": queries}


def _group(title: str, items: list[str]) -> dict[str, list[str]]:
    return {"title": title, "items": [item for item in items if str(item).strip()]}


def _target_label(request: CommandRequest, structured_data: dict[str, Any]) -> str:
    stock = structured_data.get("stock") or {}
    parts = [request.target or request.market_scope or request.theme_scope or request.candidate_pool or "", stock.get("name") or ""]
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
    return [" ".join(labels[index:index + batch_size]) for index in range(0, len(labels), batch_size) if labels[index:index + batch_size]]


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
