from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from .company_knowledge_update_service import source_quality_score
from .models import CommandRequest, SourceItem

REPORT_QUALITY_SCHEMA_VERSION = "report_quality_v1"
QUALITY_COMMANDS = (
    "research",
    "value_scan",
    "macro",
    "theme",
    "theme_radar",
    "theme_flow",
    "sector_strength",
    "radar",
    "news",
    "topic_maintain",
)


def build_report_quality_layer(
    request: CommandRequest,
    structured_data: dict[str, Any] | None,
    sources: list[SourceItem],
) -> dict[str, Any]:
    data = structured_data or {}
    evidence_pack = build_report_evidence_pack(request, data)
    completeness = build_data_completeness_matrix(request, data, evidence_pack)
    source_quality = build_source_quality_metadata(sources)
    source_coverage = build_source_coverage_summary(sources, source_quality)
    missing = [row["field"] for row in completeness if not row.get("available")]
    coverage_score = _coverage_score(completeness)
    warnings = build_report_quality_warnings(
        coverage_score=coverage_score,
        missing_fields=missing,
        source_quality=source_quality,
    )
    return {
        "schema_version": REPORT_QUALITY_SCHEMA_VERSION,
        "command": request.command,
        "mode": request.mode,
        "target": request.target or request.market_scope or request.theme_scope or request.candidate_pool,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "evidence_pack": evidence_pack,
        "data_completeness_matrix": completeness,
        "data_coverage_score": coverage_score,
        "source_coverage_summary": source_coverage,
        "source_quality": source_quality,
        "data_gap_summary": data.get("data_gap_summary"),
        "unified_evidence_pack": data.get("unified_evidence_pack"),
        "news_event_summary": data.get("news_event_summary"),
        "missing_data_policy": missing_data_policy_summary(),
        "qa_warnings": warnings,
    }


def build_report_evidence_pack(request: CommandRequest, data: dict[str, Any]) -> dict[str, Any]:
    if request.command == "value_scan":
        return {
            "scope": "candidate_pool",
            "candidate_pool": data.get("candidate_pool") or request.candidate_pool or request.target,
            "ai_candidate_count": len(data.get("ai_candidates") or []),
            "ai_candidate_limit": data.get("ai_candidate_limit"),
            "ai_candidate_evidence_pack": _compact(data.get("ai_candidate_evidence_pack") or [], depth=7, max_list=40),
            "local_ranking": _compact(data.get("local_ranking") or [], depth=4, max_list=80),
            "company_knowledge_update_status": data.get("company_knowledge_update_status"),
        }
    if request.command == "research":
        chip = data.get("chip_backup_data") or {}
        return {
            "scope": "single_stock",
            "stock": data.get("stock"),
            "price_data": _compact(data.get("price_data") or data.get("technical_data")),
            "institutional_data": _compact(data.get("institutional_data"), max_list=20),
            "margin_data": _compact(data.get("margin_data"), max_list=20),
            "revenue_data": _compact(data.get("revenue_data"), max_list=18),
            "financial_data": _compact(data.get("financial_data"), max_list=12),
            "gross_margin_cache": _compact(data.get("gross_margin_cache")),
            "tdcc_data": _compact(data.get("tdcc_data")),
            "valuation_data": _compact(data.get("valuation_data")),
            "chip_summary": _compact(chip.get("summary") if isinstance(chip, dict) else None),
            "local_rerating_snapshot": _compact(data.get("local_rerating_snapshot"), depth=6),
            "company_knowledge": _compact(data.get("company_knowledge"), depth=5),
            "company_knowledge_update_status": data.get("company_knowledge_update_status"),
        }
    if request.command == "macro":
        return {
            "scope": "macro",
            "market_scope": data.get("market_scope") or request.market_scope,
            "region_scope": data.get("region_scope") or request.region_scope,
            "quantitative_market": _compact(data.get("quantitative_market")),
            "market_score": _compact(data.get("market_score")),
            "volatility": _compact(data.get("volatility")),
            "industry_flow": _compact(data.get("industry_flow")),
            "fear_greed": _compact(data.get("fear_greed")),
            "global_public_macro": _compact(data.get("global_public_macro")),
        }
    if request.command in {"theme", "theme_radar", "theme_flow", "sector_strength"}:
        companies = data.get("matched_companies") or data.get("matched_universe") or data.get("related_stocks")
        return {
            "scope": request.command,
            "theme": data.get("theme") or request.theme_scope or request.target,
            "matched_companies": _compact(companies or [], max_list=80),
            "company_knowledge_summary": _compact(data.get("company_knowledge_summary"), depth=5),
            "company_knowledge_update_status": data.get("company_knowledge_update_status"),
            "supply_chain_profile": _compact(data.get("supply_chain_profile"), depth=5),
            "topic_context": _compact(data.get("topic_context"), depth=5),
            "theme_rankings": _compact(data.get("theme_rankings"), max_list=40),
            "sector_rankings": _compact(data.get("sector_rankings"), max_list=40),
        }
    return {
        "scope": request.command,
        "feature_pack": _compact(data.get("feature_pack")),
        "news_context": _compact(data.get("news_context")),
    }


def build_data_completeness_matrix(
    request: CommandRequest,
    data: dict[str, Any],
    evidence_pack: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    fields = _required_fields_for_command(request.command)
    rows = []
    for field in fields:
        value = _field_value(field, data, evidence_pack or {})
        rows.append({
            "field": field,
            "available": _has_value(value),
            "count": _value_count(value),
            "status": "covered" if _has_value(value) else "missing",
        })
    return rows


def build_source_quality_metadata(sources: list[SourceItem]) -> dict[str, Any]:
    items = []
    buckets = {"high": 0, "medium": 0, "low": 0, "rejected": 0}
    for source in sources:
        quality = source_quality_score(source)
        buckets[quality["level"]] = buckets.get(quality["level"], 0) + 1
        items.append({
            "source_id": source.source_id,
            "title": source.title,
            "url": source.url,
            "source_level": source.source_level,
            "provider": source.provider,
            "fetch_provider": source.fetch_provider,
            "fetch_status": source.fetch_status,
            "fetch_quality": source.fetch_quality,
            "failure_reason": source.failure_reason,
            "source_quality_score": quality["score"],
            "source_quality_level": quality["level"],
            "usable_for_company_knowledge": quality["usable_for_company_knowledge"],
            "source_quality_reasons": quality["reasons"],
        })
    return {"summary": buckets, "items": items}


def build_source_coverage_summary(sources: list[SourceItem], source_quality: dict[str, Any]) -> dict[str, Any]:
    by_provider: dict[str, int] = {}
    by_level: dict[str, int] = {}
    dated = 0
    explicit_dated = 0
    inferred_dated = 0
    for source in sources:
        provider = source.provider or source.fetch_provider or "unknown"
        level = source.source_level or "unknown"
        by_provider[provider] = by_provider.get(provider, 0) + 1
        by_level[level] = by_level.get(level, 0) + 1
        if source.published_date:
            dated += 1
            found_by = set(source.found_by or [])
            if "source_date:explicit" in found_by:
                explicit_dated += 1
            elif "source_date:inferred" in found_by:
                inferred_dated += 1
    return {
        "total_sources": len(sources),
        "dated_sources": dated,
        "explicit_dated_sources": explicit_dated,
        "inferred_dated_sources": inferred_dated,
        "undated_sources": max(0, len(sources) - dated),
        "by_provider": by_provider,
        "by_source_level": by_level,
        "quality_summary": source_quality.get("summary") or {},
    }


def build_report_quality_warnings(
    *,
    coverage_score: int,
    missing_fields: list[str],
    source_quality: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    if coverage_score < 60:
        warnings.append("data_coverage_below_60")
    if missing_fields:
        warnings.append("missing_fields:" + ",".join(missing_fields[:12]))
    summary = source_quality.get("summary") or {}
    high = int(summary.get("high") or 0)
    rejected = int(summary.get("rejected") or 0)
    if high == 0:
        warnings.append("no_high_quality_source")
    if rejected:
        warnings.append(f"rejected_sources:{rejected}")
    return warnings


def missing_data_policy_summary() -> dict[str, str]:
    return {
        "financial": "缺財報或營收時，不得硬評財務硬指標。",
        "company_knowledge": "缺公司知識庫時，不得硬講客戶、產品營收占比或供應鏈關係。",
        "source_quality": "低品質來源只能作為情緒或線索，不得單獨支撐高分結論。",
        "inference": "推論型加分必須標示待驗證資料與反證風險。",
    }


def supported_quality_commands() -> list[str]:
    return list(QUALITY_COMMANDS)


def required_fields_for_command(command: str) -> list[str]:
    return list(_required_fields_for_command(command))


def build_quality_coverage_snapshot(commands: list[str] | None = None) -> dict[str, Any]:
    selected = commands or list(QUALITY_COMMANDS)
    rows = [
        {
            "command": command,
            "required_fields": required_fields_for_command(command),
            "required_field_count": len(required_fields_for_command(command)),
        }
        for command in selected
    ]
    return {
        "schema_version": REPORT_QUALITY_SCHEMA_VERSION,
        "command_count": len(rows),
        "commands": rows,
    }


def _required_fields_for_command(command: str) -> list[str]:
    common = ["news_context", "feature_pack", "data_coverage"]
    if command == "research":
        return [
            "stock",
            "price_data",
            "institutional_data",
            "revenue_data",
            "financial_data",
            "gross_margin_cache",
            "chip_summary",
            "local_rerating_snapshot",
            "company_knowledge",
            *common,
        ]
    if command == "value_scan":
        return [
            "ai_candidates",
            "ai_candidate_evidence_pack",
            "local_ranking",
            "company_knowledge_update_status",
            *common,
        ]
    if command == "macro":
        return [
            "quantitative_market",
            "market_score",
            "volatility",
            "industry_flow",
            "fear_greed",
            *common,
        ]
    if command == "theme":
        return [
            "matched_companies",
            "topic_context",
            "supply_chain_profile",
            "company_knowledge_summary",
            *common,
        ]
    if command == "theme_radar":
        return [
            "market_movers",
            "theme_rankings",
            "sector_strength",
            "news_context",
            "feature_pack",
            "data_coverage",
        ]
    if command == "theme_flow":
        return [
            "theme",
            "layers",
            "layer_market_validation",
            "related_stocks",
            "supply_chain_profile",
            "company_knowledge_summary",
            "news_context",
            "feature_pack",
            "data_coverage",
        ]
    if command == "sector_strength":
        return [
            "market_movers",
            "sector_rankings",
            "news_context",
            "feature_pack",
            "data_coverage",
        ]
    if command == "radar":
        return [
            "candidates",
            "evidence_pack",
            "news_context",
            "feature_pack",
            "data_coverage",
        ]
    if command == "news":
        return [
            "news_context",
            "news_events",
            "source_coverage",
            "data_coverage",
        ]
    if command == "topic_maintain":
        return [
            "topic_context",
            "discovery_sources",
            "change_pack",
            "news_context",
            "feature_pack",
            "data_coverage",
        ]
    return common


def _field_value(field: str, data: dict[str, Any], evidence_pack: dict[str, Any]) -> Any:
    if field == "matched_companies":
        return data.get("matched_companies") or data.get("matched_universe") or data.get("related_stocks")
    if field == "source_coverage":
        return data.get("source_coverage") or data.get("source_coverage_summary")
    if field == "chip_summary":
        chip = data.get("chip_backup_data") or {}
        return chip.get("summary") if isinstance(chip, dict) else None
    if field in evidence_pack:
        return evidence_pack.get(field)
    return data.get(field)


def _coverage_score(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    covered = sum(1 for row in rows if row.get("available"))
    return int(round(covered / len(rows) * 100))


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        status = str(value.get("status") or "").lower()
        if status in {"missing", "unavailable", "no data", "insufficient"}:
            return False
        return bool(value)
    if isinstance(value, (list, tuple, set, str)):
        return bool(value)
    return True


def _value_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, (list, tuple, set, str)):
        return len(value)
    return 1


def _compact(value: Any, *, depth: int = 4, max_list: int = 30, max_keys: int = 80, max_string: int = 1200) -> Any:
    if depth <= 0:
        if isinstance(value, (dict, list, tuple)):
            return f"<{type(value).__name__} truncated>"
        return value
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_keys:
                compact["_truncated_keys"] = len(value) - max_keys
                break
            compact[str(key)] = _compact(item, depth=depth - 1, max_list=max_list, max_keys=max_keys, max_string=max_string)
        return compact
    if isinstance(value, (list, tuple)):
        items = [
            _compact(item, depth=depth - 1, max_list=max_list, max_keys=max_keys, max_string=max_string)
            for item in list(value)[:max_list]
        ]
        if len(value) > max_list:
            items.append({"_truncated_items": len(value) - max_list})
        return items
    if isinstance(value, str) and len(value) > max_string:
        return value[:max_string].rstrip() + "...<truncated>"
    return value
