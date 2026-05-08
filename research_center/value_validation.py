from __future__ import annotations

from typing import Any


def build_value_cross_validation(row: dict[str, Any], related_events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    knowledge = row.get("company_knowledge") or {}
    events = related_events or []
    checks = {
        "announcement": _check_events(events, ("announcement", "mops", "material_event", "mops_material_reference", "mops_announcement_reference", "mops_connectivity_check")),
        "customer_structure": _check_list(knowledge.get("customers"), "客戶結構資料庫"),
        "broker_report_summary": _check_events(events, ("broker_report", "research_report", "broker_report_reference")),
        "financial_detail": _check_financial(row),
        "product_and_supply_chain": _check_list((knowledge.get("product_lines") or []) + (knowledge.get("supply_chain_roles") or []), "產品/供應鏈資料庫"),
        "tdcc_distribution": _check_free_source(row.get("tdcc_data"), "TDCC 集保股權分散表"),
        "official_valuation": _check_free_source(row.get("valuation_data"), "TWSE/TPEx 官方估值資料"),
        "gross_margin_cache": _check_free_source(row.get("gross_margin_cache"), "毛利率本地快取"),
    }
    score = sum(item["points"] for item in checks.values())
    flags = [item["note"] for item in checks.values() if item["status"] != "verified"]
    return {
        "verification_score": min(100, score),
        "checks": checks,
        "risk_flags": flags,
        "policy": "分數代表證據覆蓋度，不代表投資評等；缺少公告、客戶、法人報告或財報細項時一律保守標示。",
    }


def _check_free_source(payload: Any, label: str) -> dict[str, Any]:
    data = payload or {}
    status = data.get("status")
    if status in ("covered", "official_public"):
        return {"status": "verified", "points": 12, "note": f"{label} 已接入", "source": data.get("source")}
    if status in ("official_reference", "reference_link", "empty"):
        return {"status": "partial", "points": 6, "note": f"{label} 只有查詢入口或沒有命中資料", "source": data.get("source")}
    return {"status": "missing", "points": 0, "note": f"缺少{label}", "source": data.get("source")}

def _check_events(events: list[dict[str, Any]], accepted_types: tuple[str, ...]) -> dict[str, Any]:
    matched = [event for event in events if str(event.get("event_type") or "") in accepted_types]
    if matched:
        hard_verified = [event for event in matched if ((event.get("payload") or {}).get("status") not in ("reference_link", "unavailable"))]
        if hard_verified:
            return {"status": "verified", "points": 20, "note": f"找到 {len(hard_verified)} 筆可用事件資料", "event_count": len(hard_verified)}
        return {"status": "partial", "points": 10, "note": "已有官方查詢入口或連線檢查，但尚未解析公告明細", "event_count": len(matched)}
    return {"status": "missing", "points": 0, "note": "缺少可用事件/公告資料", "event_count": 0}


def _check_list(values: Any, label: str) -> dict[str, Any]:
    count = len(values or [])
    if count >= 2:
        return {"status": "verified", "points": 20, "note": f"{label} 已覆蓋 {count} 項", "item_count": count}
    if count == 1:
        return {"status": "partial", "points": 10, "note": f"{label} 僅覆蓋 1 項", "item_count": count}
    return {"status": "missing", "points": 0, "note": f"缺少{label}", "item_count": 0}


def _check_financial(row: dict[str, Any]) -> dict[str, Any]:
    detail = row.get("financial_detail") or {}
    if detail.get("status") == "covered":
        return {"status": "verified", "points": 24, "note": "已接入季度財報細項 snapshot", "fields": detail.get("fields", [])}
    if detail.get("status") == "partial":
        return {"status": "partial", "points": 16, "note": "已接入部分季度財報欄位", "fields": detail.get("fields", [])}

    available = []
    if row.get("latest_monthly_revenue") is not None:
        available.append("monthly_revenue")
    if row.get("revenue_yoy") is not None:
        available.append("revenue_yoy")
    if len(available) >= 2:
        return {"status": "partial", "points": 12, "note": "目前有月營收與 YoY，尚未取得完整財報細項", "fields": available}
    return {"status": "missing", "points": 0, "note": "缺少財報細項與月營收交叉驗證", "fields": available}

