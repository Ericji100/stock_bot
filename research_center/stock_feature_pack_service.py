from __future__ import annotations

from datetime import datetime
from typing import Any

from .artifact_registry import build_artifact_record, register_artifact
from .models import CommandRequest

FEATURE_PACK_SCHEMA_VERSION = "feature_pack_v2"

FEATURE_PACK_COMMANDS = {
    "research",
    "value_scan",
    "theme",
    "macro",
    "theme_radar",
    "theme_flow",
    "sector_strength",
}


def attach_feature_pack(request: CommandRequest, structured_data: dict[str, Any]) -> dict[str, Any]:
    if request.command not in FEATURE_PACK_COMMANDS:
        return structured_data
    structured_data["feature_pack"] = build_feature_pack(request, structured_data)
    structured_data["feature_pack_artifact"] = _register_feature_pack_artifact(request, structured_data)
    return structured_data


def build_feature_pack(request: CommandRequest, structured_data: dict[str, Any]) -> dict[str, Any]:
    if request.command == "research":
        pack = _research_feature_pack(structured_data)
    elif request.command == "value_scan":
        pack = _value_scan_feature_pack(structured_data)
    elif request.command == "macro":
        pack = _macro_feature_pack(structured_data)
    elif request.command in {"theme", "theme_radar", "theme_flow", "sector_strength"}:
        pack = _theme_feature_pack(request, structured_data)
    else:
        pack = {"command": request.command, "status": "unsupported"}
    return _with_metadata(request, structured_data, pack)


def _with_metadata(request: CommandRequest, data: dict[str, Any], pack: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": FEATURE_PACK_SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "command": request.command,
        "mode": request.mode,
        "target": request.target or request.market_scope or request.theme_scope or request.candidate_pool,
        "report_date": request.report_date.isoformat() if request.report_date else None,
        "resolved_entity": data.get("resolved_entity"),
        "resolved_topic": data.get("resolved_topic"),
        "event_context_summary": data.get("event_context_summary"),
        "data_gap_summary": data.get("data_gap_summary"),
        "news_event_count": len(data.get("news_events") or []),
        **pack,
    }


def _research_feature_pack(data: dict[str, Any]) -> dict[str, Any]:
    stock = data.get("stock") or {}
    chip = data.get("chip_backup_data") or {}
    return {
        "scope": "single_stock",
        "stock": stock,
        "price": data.get("price_data") or data.get("technical_data"),
        "institutional": data.get("institutional_data"),
        "margin": data.get("margin_data"),
        "revenue": data.get("revenue_data"),
        "financial": data.get("financial_data"),
        "gross_margin": data.get("gross_margin_cache"),
        "tdcc": data.get("tdcc_data"),
        "valuation": data.get("valuation_data"),
        "chip_summary": chip.get("summary") if isinstance(chip, dict) else None,
        "rerating": data.get("local_rerating_snapshot"),
        "news": _compact_news(data),
    }


def _value_scan_feature_pack(data: dict[str, Any]) -> dict[str, Any]:
    packs = data.get("ai_candidate_evidence_pack") or []
    return {
        "scope": "candidate_pool",
        "candidate_pool": data.get("candidate_pool"),
        "ai_candidate_count": len(packs),
        "ai_candidate_limit": data.get("ai_candidate_limit"),
        "candidates": packs,
        "news": _compact_news(data),
    }


def _macro_feature_pack(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": "macro",
        "market_scope": data.get("market_scope"),
        "region_scope": data.get("region_scope"),
        "theme_scope": data.get("theme_scope"),
        "quantitative_market": data.get("quantitative_market"),
        "market_score": data.get("market_score"),
        "volatility": data.get("volatility"),
        "industry_flow": data.get("industry_flow"),
        "fear_greed": data.get("fear_greed"),
        "news": _compact_news(data),
    }


def _theme_feature_pack(request: CommandRequest, data: dict[str, Any]) -> dict[str, Any]:
    companies = data.get("matched_companies") or data.get("matched_universe") or data.get("related_stocks") or []
    return {
        "scope": request.command,
        "theme": data.get("theme") or request.theme_scope or request.target,
        "matched_company_count": len(companies) if isinstance(companies, list) else 0,
        "matched_companies": companies[:60] if isinstance(companies, list) else companies,
        "supply_chain_profile": data.get("supply_chain_profile"),
        "topic_context": data.get("topic_context"),
        "theme_rankings": data.get("theme_rankings"),
        "sector_rankings": data.get("sector_rankings"),
        "news": _compact_news(data),
    }


def _compact_news(data: dict[str, Any]) -> dict[str, Any]:
    context = data.get("news_context") or data.get("saved_news_context") or {}
    return {
        "status": context.get("status"),
        "usable_count": context.get("usable_count") or len(context.get("items") or []),
        "items": (context.get("items") or [])[:12],
    }


def _register_feature_pack_artifact(request: CommandRequest, data: dict[str, Any]) -> dict[str, Any]:
    try:
        pack = data.get("feature_pack") or {}
        completeness = _feature_pack_completeness(pack)
        target = request.target or request.market_scope or request.theme_scope or request.candidate_pool or request.command
        virtual_path = f"feature_pack/{request.command}/{target or 'latest'}"
        record = build_artifact_record(
            artifact_type="feature_pack",
            path=virtual_path,
            schema_version=FEATURE_PACK_SCHEMA_VERSION,
            data_date=request.report_date,
            source=request.command,
            completeness=completeness,
            usable=completeness > 0,
            metadata={
                "command": request.command,
                "mode": request.mode,
                "target": target,
            },
        )
        registry_path = register_artifact(record)
        return {"registered": True, "path": str(registry_path), "artifact_id": record.artifact_id}
    except Exception as exc:
        return {"registered": False, "error": str(exc)[:180]}


def _feature_pack_completeness(pack: dict[str, Any]) -> float:
    if not isinstance(pack, dict) or pack.get("status") == "unsupported":
        return 0.0
    keys = [key for key, value in pack.items() if key not in {"schema_version", "generated_at"} and value not in (None, "", [], {})]
    return min(1.0, len(keys) / 8.0)
