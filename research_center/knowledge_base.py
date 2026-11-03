from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
COMPANY_KNOWLEDGE_PATH = ROOT_DIR / "config" / "company_knowledge.json"


def find_theme_company_candidates(universe, query, profile, related_nodes, knowledge, progress=None):
    node_order = {str(node['company_code']): index for index, node in enumerate(related_nodes)
                  if isinstance(node, dict) and node.get('company_code')}
    companies = knowledge.get('companies') or {}
    keywords = [str(value).lower() for value in [query, *(profile.get('keywords') or [])] if value]
    industries = profile.get('industries') or []
    matched = []
    for index, entry in enumerate(universe, 1):
        if progress and (index == 1 or index == len(universe) or index % 300 == 0):
            progress(f'題材研究：比對股票宇宙 {index}/{len(universe)}，目前命中 {len(matched)} 檔')
        code = str(entry.code)
        info = companies.get(code) or {}
        # Match business descriptions, not unrelated articles stored in the evidence history.
        products = json.dumps([info.get(key) for key in ('product_lines','products','supply_chain_roles')], ensure_ascii=False).lower()
        name_text = f'{entry.name} {entry.industry}'.lower()
        linked = code in node_order
        product_hit = any(keyword in products for keyword in keywords)
        keyword_hit = any(keyword in name_text for keyword in keywords)
        industry_hit = any(industry and industry in entry.industry for industry in industries)
        if not (linked or product_hit or keyword_hit or industry_hit):
            continue
        priority = 0 if linked else 1 if product_hit or keyword_hit else 2
        reason = ('題材庫供應鏈關聯，仍須本次證據驗證' if linked else
                  '公司產品／名稱關鍵字關聯，仍須本次證據驗證' if priority == 1 else
                  f'僅產業分類命中：{entry.industry}，不等於已確認產品關係')
        row = asdict(entry) if is_dataclass(entry) else {key:getattr(entry,key,'') for key in ('code','name','symbol','industry','market')}
        matched.append({**row, 'theme_match_reason':reason, 'theme_match_priority':priority,
                        'theme_reference_order':node_order.get(code, len(node_order))})
    return sorted(matched, key=lambda row:(row['theme_match_priority'],row['theme_reference_order']))


def load_company_knowledge(path: Path | None = None) -> dict[str, Any]:
    source = path or COMPANY_KNOWLEDGE_PATH
    if not source.exists():
        return {"companies": {}, "metadata": {"status": "missing"}}
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"companies": {}, "metadata": {"status": "load_failed", "error": str(exc)}}
    companies = payload.get("companies") if isinstance(payload, dict) else {}
    return {
        "companies": companies if isinstance(companies, dict) else {},
        "metadata": payload.get("metadata", {}) if isinstance(payload, dict) else {},
    }


def save_company_knowledge(data: dict[str, Any], path: Path | None = None) -> None:
    target = path or COMPANY_KNOWLEDGE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def enrich_company_rows(rows: list[dict[str, Any]], knowledge: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = knowledge or load_company_knowledge()
    companies = data.get("companies") or {}
    enriched: list[dict[str, Any]] = []
    for row in rows:
        code = str(row.get("code") or "")
        info = companies.get(code) or {}
        enriched.append({**row, "company_knowledge": _normalize_company_knowledge(info)})
    return enriched


def theme_knowledge_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    covered = [row for row in rows if (row.get("company_knowledge") or {}).get("status") == "covered"]
    missing = len(rows) - len(covered)
    products = _top_values(covered, "product_lines")
    customers = _top_values(covered, "customers")
    revenue_tags = _top_values(covered, "revenue_exposure")
    return {
        "covered_companies": len(covered),
        "missing_companies": missing,
        "coverage_pct": round(len(covered) / len(rows) * 100, 1) if rows else 0,
        "top_product_lines": products,
        "top_customer_tags": customers,
        "top_revenue_exposure_tags": revenue_tags,
        "policy": "公司產品、客戶與營收占比以 config/company_knowledge.json 為準；缺漏時只標示待驗證。",
    }


def _normalize_company_knowledge(info: dict[str, Any]) -> dict[str, Any]:
    if not info:
        return {
            "status": "missing",
            "product_lines": [],
            "customers": [],
            "revenue_exposure": [],
            "supply_chain_roles": [],
            "evidence_sources": [],
        }
    return {
        "status": "covered",
        "product_lines": list(info.get("product_lines") or []),
        "customers": list(info.get("customers") or []),
        "revenue_exposure": list(info.get("revenue_exposure") or []),
        "supply_chain_roles": list(info.get("supply_chain_roles") or []),
        "evidence_sources": list(info.get("evidence_sources") or []),
        "missing_data": list(info.get("missing_data") or []),
        "auto_update": dict(info.get("auto_update") or {}),
        "updated_at": info.get("updated_at"),
        "confidence": info.get("confidence", "manual_review_required"),
    }


def _top_values(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        values = ((row.get("company_knowledge") or {}).get(field) or [])
        for value in values:
            key = str(value)
            counts[key] = counts.get(key, 0) + 1
    return [{"value": key, "count": count} for key, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:10]]
