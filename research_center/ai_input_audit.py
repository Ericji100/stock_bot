from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
from urllib.parse import urlsplit
from typing import Any

from .models import CommandRequest


AUDIT_SCHEMA_VERSION = "ai_input_audit_v1"


def build_ai_input_audit(
    request: CommandRequest,
    *,
    source_selection: dict[str, Any],
    structured_data: dict[str, Any],
    ai_prompt_context: dict[str, Any],
) -> dict[str, Any]:
    selected_sources = source_selection.get("selected_sources") or []
    omitted_sources = source_selection.get("omitted_sources") or []
    structured_summary = _structured_coverage(request, structured_data)
    context_summary = _context_size_summary(ai_prompt_context)
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "command": request.command,
        "mode": request.mode,
        "target": request.target or request.market_scope or request.theme_scope or request.candidate_pool,
        "ai_received": {
            "selected_source_count": len(selected_sources),
            "structured_sections": structured_summary["available_sections"],
            "prompt_context_chars": context_summary["prompt_context_chars"],
        },
        "ai_not_received_directly": {
            "omitted_source_count": len(omitted_sources),
            "omitted_reason_counts": _reason_counts(omitted_sources),
            "note": "未直接入模的資料仍保存在完整報告 JSON 或來源檔。",
        },
        "source_coverage": source_selection.get("coverage") or {},
        "all_source_coverage": source_selection.get("all_source_coverage") or {},
        "structured_coverage": structured_summary,
        "context_size": context_summary,
        "quality_checks": _quality_checks(request, structured_data, selected_sources),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _quality_checks(request, data, selected):
    bodies, domains, future = set(), set(), 0
    target = str(request.report_date or data.get('report_date') or datetime.now().date())[:10]
    for entry in selected:
        row = entry.get('source') or {}
        text = str(row.get('content') or row.get('snippet') or '').strip()
        try:
            url = urlsplit(str(row.get('url') or ''))
        except ValueError:
            continue
        day = str(row.get('published_date') or '')[:10]
        if day and day > target:
            future += 1
            continue
        domains.add(url.hostname or '')
        fetched = (row.get('fetch_status') in {'ok', 'success'} or
                   row.get('provider') in {'html_fetch', 'requests_bs4', 'tavily_extract', 'deepseek_fetch'} or
                   'source_text:provided_content' in (row.get('found_by') or []))
        if fetched and len(text) >= 300 and url.path.strip('/'):
            bodies.add(hashlib.sha256(''.join(text.split()).encode()).hexdigest())
    warnings = []
    caps = []
    if not bodies:
        warnings.append('尚未確認取得可追溯正文，來源數量不能代表證據完整。')
        caps.append(69)
    elif len(bodies) == 1 and len(selected) > 1:
        warnings.append('可確認的正文僅一份，轉載副本不能當作多份原始證據。')
        caps.append(79)
    if len(domains - {''}) < 2:
        warnings.append('獨立來源網域不足，轉載不能視為交叉驗證。')
        caps.append(69)
    if future:
        warnings.append('來源包含指定日期之後的資料，不得作為當時已知證據。')
        caps.append(49)

    def inspect(value, path='', depth=0):
        if not isinstance(value, dict) or depth > 5:
            return
        audit = value.get('price_date_audit') or {}
        if audit.get('mixed_dates') or audit.get('undated_stock_count'):
            warnings.append('行情日期混合或缺漏，跨股比較需降低確信。')
            caps.append(69)
        day = audit.get('dominant_price_date')
        if day and day < target:
            try:
                start, end = date.fromisoformat(day), date.fromisoformat(target)
                weekday_gap = sum((start + timedelta(days=offset)).weekday() < 5 for offset in range(1, (end-start).days+1))
            except ValueError:
                weekday_gap = 99
            if weekday_gap > 1:
                warnings.append(f'主要行情日期為 {day}，與指定日期 {target} 有多個平日差距；需確認休市及發布延遲。')
                caps.append(79)
        if value.get('status') == 'invalid' or (value.get('valid') is False and 'tdcc' in path):
            warnings.append('集保或結構化數值檢核未通過，異常值不得作為支持證據。')
            caps.append(49)
        if request.command == 'theme_flow' and 'quantitative_stock_count' in value:
            if value.get('flow_evidence_basis') == 'price_volume_proxy':
                warnings.append('題材流向只有量價代理，未直接驗證法人與資金流；來源多不代表資金方向已證實。')
                caps.append(69)
            count = value['quantitative_stock_count']
            if count < 2:
                warnings.append('題材流向量價不足兩檔，無法代表完整族群。')
                caps.append(49)
            elif value.get('quantitative_missing_codes'):
                warnings.append('部分題材關聯公司缺少量價，族群廣度仍不完整。')
                caps.append(69)
        for key in ('data_quality', 'market_movers_data_quality', 'market_movers', 'tdcc_data', 'tdcc', 'free_public_sources'):
            inspect(value.get(key), f'{path}/{key}', depth + 1)
    inspect(data)
    financial = data.get('financial_period_audit') or {}
    if request.command in {'research', 'value_scan'} and financial.get('warnings'):
        warnings.extend(financial['warnings'])
        caps.append(69)
    if request.command in {'research', 'value_scan'} and financial and not financial.get('official_body_urls'):
        warnings.append('尚未取得最新官方財報正文；搜尋摘要與入口連結不足以證明期間完整。')
        caps.append(69)
    return {'body_evidence_count': len(bodies), 'independent_domain_count': len(domains - {''}),
            'future_source_count': future, 'warnings': list(dict.fromkeys(warnings)),
            'confidence_cap': min(caps) if caps else 100,
            'scope': '資料品質檢核，不是投資評分、主張真實性認證或預測成功率。'}


def _structured_coverage(request: CommandRequest, data: dict[str, Any]) -> dict[str, Any]:
    fields = _fields_for_command(request.command)
    available = []
    missing = []
    counts: dict[str, int] = {}
    for field in fields:
        value = data.get(field)
        if _has_value(value):
            available.append(field)
            counts[field] = _value_count(value)
        else:
            missing.append(field)
            counts[field] = 0
    return {
        "required_sections": fields,
        "available_sections": available,
        "missing_sections": missing,
        "section_counts": counts,
        "coverage_ratio": round(len(available) / len(fields), 4) if fields else 1,
    }


def _context_size_summary(value: Any) -> dict[str, int]:
    import json

    text = json.dumps(value, ensure_ascii=False, default=str)
    return {
        "prompt_context_chars": len(text),
        "prompt_context_bytes": len(text.encode("utf-8")),
    }


def _reason_counts(omitted_sources: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in omitted_sources:
        reason = str(item.get("status") or item.get("reason") or "未入模")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _fields_for_command(command: str) -> list[str]:
    if command == "research":
        return [
            "stock",
            "price_data",
            "institutional_data",
            "margin_data",
            "revenue_data",
            "financial_data",
            "local_rerating_snapshot",
            "local_scoring",
            "topic_context",
            "unified_evidence_pack",
        ]
    if command == "value_scan":
        return [
            "ai_candidates",
            "ai_candidate_evidence_pack",
            "local_ranking",
            "local_scoring",
            "topic_context",
            "unified_evidence_pack",
        ]
    if command == "macro":
        return [
            "quantitative_market",
            "volatility",
            "industry_flow",
            "fear_greed",
            "market_score",
            "unified_evidence_pack",
        ]
    if command in {"theme", "theme_radar", "theme_flow", "sector_strength"}:
        return [
            "theme",
            "matched_companies",
            "topic_context",
            "supply_chain_profile",
            "theme_rankings",
            "sector_rankings",
            "data_quality",
            "unified_evidence_pack",
        ]
    if command == "radar":
        return ["candidates", "evidence_pack", "data_coverage"]
    if command == "news":
        return ["news_batch", "news_context", "sources"]
    if command == "topic_maintain":
        return ["existing_profiles", "source_candidates", "topic_context"]
    return ["unified_evidence_pack", "feature_pack", "data_coverage"]


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, (list, tuple, set, str)):
        return bool(value)
    return True


def _value_count(value: Any) -> int:
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, (list, tuple, set)):
        return len(value)
    if value is None:
        return 0
    return 1
