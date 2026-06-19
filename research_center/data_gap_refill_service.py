from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import pandas as pd

from data_fetcher import StockDataFetcher
from stock_scanner import StockUniverseEntry, load_stock_universe

from .chip_sources import build_chip_backup_events, build_chip_backup_snapshot
from .data_gap_service import build_data_gap_summary
from .free_sources import build_free_macro_sources, build_free_research_sources
from .mops_sources import build_mops_reference_events, financial_detail_snapshot
from .models import CommandRequest
from .news_context_service import attach_news_context
from .price_fallbacks import load_price_metrics_with_fallback
from .topic_context import build_candidates_topic_context, build_stock_topic_context, build_theme_topic_context

DATA_GAP_REFILL_SCHEMA_VERSION = "data_gap_refill_v1"


@dataclass(frozen=True)
class DataGapRefillLimits:
    value_scan_candidates: int = 30
    theme_companies: int = 30
    research_targets: int = 1


ProgressCallback = Callable[[str], None]


def refill_data_gaps(
    request: CommandRequest,
    structured_data: dict[str, Any],
    *,
    progress: ProgressCallback | None = None,
    limits: DataGapRefillLimits | None = None,
) -> dict[str, Any]:
    """Best-effort data gap refill coordinator.

    The coordinator only calls existing data services and records outcomes. It
    does not change prompt text, scoring weights, source priority, report
    sections, or candidate ordering.
    """

    limits = limits or DataGapRefillLimits()
    if request.command not in {"research", "value_scan", "theme", "theme_radar", "macro"}:
        return structured_data

    before = build_data_gap_summary(request, structured_data)
    attempts: list[dict[str, Any]] = []

    if request.command == "research":
        _refill_research(request, structured_data, attempts, progress=progress, limits=limits)
    elif request.command == "value_scan":
        _refill_value_scan(request, structured_data, attempts, progress=progress, limits=limits)
    elif request.command in {"theme", "theme_radar"}:
        _refill_theme(request, structured_data, attempts, progress=progress, limits=limits)
    elif request.command == "macro":
        _refill_macro(request, structured_data, attempts, progress=progress)

    _refill_news_context(request, structured_data, attempts, progress=progress)

    after = build_data_gap_summary(request, structured_data)
    structured_data["data_gap_refill"] = {
        "schema_version": DATA_GAP_REFILL_SCHEMA_VERSION,
        "command": request.command,
        "mode": request.mode,
        "target": request.target or request.market_scope or request.theme_scope or request.candidate_pool,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "limits": {
            "value_scan_candidates": limits.value_scan_candidates,
            "theme_companies": limits.theme_companies,
            "research_targets": limits.research_targets,
        },
        "before_gap_count": before.get("gap_count"),
        "after_gap_count": after.get("gap_count"),
        "attempt_count": len(attempts),
        "success_count": sum(1 for item in attempts if item.get("status") == "filled"),
        "attempts": attempts,
        "policy": "best_effort_existing_services_only_no_prompt_scoring_sort_report_change",
    }
    return structured_data


def _refill_research(
    request: CommandRequest,
    data: dict[str, Any],
    attempts: list[dict[str, Any]],
    *,
    progress: ProgressCallback | None,
    limits: DataGapRefillLimits,
) -> None:
    stock = data.get("stock") if isinstance(data.get("stock"), dict) else {}
    code = str(stock.get("code") or request.target or "").strip()
    symbol = str(stock.get("symbol") or code).strip()
    name = str(stock.get("name") or "").strip()
    if not code or limits.research_targets <= 0:
        _record(attempts, "research_target", "skipped", reason="no_target_code")
        return

    if progress:
        progress(f"資料缺口補抓：research 目標 {code} {name}".strip())

    _refill_price_metrics_for_codes([(code, symbol, name, stock.get("market"), stock.get("industry"))], data, attempts, progress=progress)
    _merge_free_sources(code, symbol, request.report_date, data, attempts)
    _merge_chip_sources(code, request.report_date, data, attempts)
    _refill_research_financial_detail(code, data, attempts)
    if not _has_value(data.get("topic_context")):
        try:
            data["topic_context"] = build_stock_topic_context(code, name)
            _record(attempts, "topic_context", "filled", target=code)
        except Exception as exc:
            status, reason = _status_for_exception(exc)
            _record(attempts, "topic_context", status, target=code, reason=reason, error=exc)


def _refill_value_scan(
    request: CommandRequest,
    data: dict[str, Any],
    attempts: list[dict[str, Any]],
    *,
    progress: ProgressCallback | None,
    limits: DataGapRefillLimits,
) -> None:
    candidates = list(data.get("ai_candidates") or [])
    if not candidates and str(request.target or request.candidate_pool or "").isdigit():
        candidates = [{"code": str(request.target or request.candidate_pool)}]
    candidates = candidates[: max(0, limits.value_scan_candidates)]
    if progress:
        progress(f"資料缺口補抓：value_scan AI candidates {len(candidates)} 檔")
    _record(
        attempts,
        "value_scan_scope",
        "filled" if candidates else "skipped",
        target_count=len(candidates),
        reason=None if candidates else "no_ai_candidates",
    )

    pack_by_code = _pack_by_code(data.get("ai_candidate_evidence_pack") or [])
    for row in candidates:
        code = str(row.get("code") or row.get("stock_id") or "").strip()
        if not code:
            continue
        symbol = str(row.get("symbol") or code).strip()
        target_pack = pack_by_code.setdefault(code, {"code": code, "name": row.get("name")})
        free_sources = _safe_free_sources(code, symbol, request.report_date, attempts, field_prefix=f"value_scan:{code}")
        if free_sources:
            _merge_pack_free_sources(target_pack, free_sources)
        chip = _safe_chip_snapshot(code, request.report_date, attempts, field=f"value_scan:{code}:chip_backup_data")
        if chip:
            target_pack["chip_backup_summary"] = _chip_summary(chip)
        if not _has_value(target_pack.get("source_events")):
            try:
                events = [*build_mops_reference_events(code, request.report_date), *build_chip_backup_events(code, request.report_date)]
                target_pack["source_events"] = events
                _record(attempts, f"value_scan:{code}:source_events", "filled" if events else "skipped", target=code)
            except Exception as exc:
                status, reason = _status_for_exception(exc)
                _record(attempts, f"value_scan:{code}:source_events", status, target=code, reason=reason, error=exc)
        if not _has_value(target_pack.get("financial_detail")):
            target_pack["financial_detail"] = _financial_detail_for_code(code, attempts, field=f"value_scan:{code}:financial_detail")
        target_pack["missing_data_status"] = _value_pack_missing_status(target_pack)

    if pack_by_code:
        ordered_codes = [str(row.get("code") or row.get("stock_id") or "") for row in candidates]
        ordered_pack = [pack_by_code[code] for code in ordered_codes if code in pack_by_code]
        data["ai_candidate_evidence_pack"] = ordered_pack
    if candidates and not _has_value(data.get("topic_context")):
        try:
            data["topic_context"] = build_candidates_topic_context(candidates)
            _record(attempts, "topic_context", "filled", target_count=len(candidates))
        except Exception as exc:
            status, reason = _status_for_exception(exc)
            _record(attempts, "topic_context", status, reason=reason, error=exc)


def _refill_theme(
    request: CommandRequest,
    data: dict[str, Any],
    attempts: list[dict[str, Any]],
    *,
    progress: ProgressCallback | None,
    limits: DataGapRefillLimits,
) -> None:
    theme = str(data.get("theme") or request.theme_scope or request.target or "").strip()
    if not _has_value(data.get("topic_context")) and theme:
        try:
            data["topic_context"] = build_theme_topic_context(theme)
            _record(attempts, "topic_context", "filled", target=theme)
        except Exception as exc:
            status, reason = _status_for_exception(exc)
            _record(attempts, "topic_context", status, target=theme, reason=reason, error=exc)
    companies = data.get("matched_companies") or data.get("matched_universe") or data.get("related_stocks") or []
    if isinstance(companies, list):
        _record(attempts, "theme_company_scope", "filled", target_count=min(len(companies), limits.theme_companies))
    if progress and theme:
        progress(f"資料缺口補抓：theme/topic context {theme}")


def _refill_macro(
    request: CommandRequest,
    data: dict[str, Any],
    attempts: list[dict[str, Any]],
    *,
    progress: ProgressCallback | None,
) -> None:
    if not _has_value(data.get("global_public_macro")):
        try:
            data["global_public_macro"] = build_free_macro_sources(request.report_date)
            _record(attempts, "global_public_macro", "filled", target=request.market_scope or request.target)
        except Exception as exc:
            status, reason = _status_for_exception(exc)
            _record(attempts, "global_public_macro", status, reason=reason, error=exc)
    _record(attempts, "macro_stock_specific_refill", "skipped", reason="macro_command_does_not_refill_single_stock_fields")
    if progress:
        progress("資料缺口補抓：macro 僅補市場/新聞上下文，不補個股毛利率或籌碼")


def _refill_news_context(
    request: CommandRequest,
    data: dict[str, Any],
    attempts: list[dict[str, Any]],
    *,
    progress: ProgressCallback | None,
) -> None:
    before = data.get("news_context") if isinstance(data.get("news_context"), dict) else {}
    before_count = int(before.get("usable_count") or 0)
    try:
        attach_news_context(request, data, progress=None)
        after = data.get("news_context") if isinstance(data.get("news_context"), dict) else {}
        after_count = int(after.get("usable_count") or 0)
        status = "filled" if after_count > before_count else "skipped"
        _record(
            attempts,
            "news_context",
            status,
            before_count=before_count,
            after_count=after_count,
            reason="local_news_repository_rechecked" if status == "skipped" else None,
        )
        if after.get("search_recommended"):
            _record(attempts, "news_external_search", "skipped", reason="handled_later_by_existing_discovery_and_news_persistence")
    except Exception as exc:
        status, reason = _status_for_exception(exc)
        _record(attempts, "news_context", status, reason=reason, error=exc)
        if progress:
            progress(f"資料缺口補抓：news context 失敗 {exc}")


def _refill_price_metrics_for_codes(
    rows: list[tuple[str, str, str, Any, Any]],
    data: dict[str, Any],
    attempts: list[dict[str, Any]],
    *,
    progress: ProgressCallback | None,
) -> None:
    entries = [
        StockUniverseEntry(
            code=code,
            symbol=symbol or _symbol_for_code(code),
            market=str(market or ""),
            name=name or code,
            industry=str(industry or ""),
        )
        for code, symbol, name, market, industry in rows
        if code
    ]
    if not entries:
        return
    try:
        metrics, policy = load_price_metrics_with_fallback(entries, progress=progress)
        data["price_refill_metrics"] = metrics
        data["price_refill_policy"] = policy
        _record(attempts, "price_metrics", "filled" if metrics else "skipped", target_count=len(entries), covered_count=len(metrics))
    except Exception as exc:
        status, reason = _status_for_exception(exc)
        _record(attempts, "price_metrics", status, target_count=len(entries), reason=reason, error=exc)


def _merge_free_sources(code: str, symbol: str, report_date: Any, data: dict[str, Any], attempts: list[dict[str, Any]]) -> None:
    free_sources = _safe_free_sources(code, symbol, report_date, attempts, field_prefix="research")
    if not free_sources:
        return
    data["free_public_sources"] = free_sources
    for key, target in (
        ("valuation", "valuation_data"),
        ("tdcc", "tdcc_data"),
        ("gross_margin_cache", "gross_margin_cache"),
        ("mops_documents", "mops_documents"),
    ):
        if not _has_value(data.get(target)) and key in free_sources:
            data[target] = free_sources.get(key)
            _record(attempts, target, "filled", target=code)


def _merge_chip_sources(code: str, report_date: Any, data: dict[str, Any], attempts: list[dict[str, Any]]) -> None:
    if not _has_value(data.get("chip_backup_data")):
        chip = _safe_chip_snapshot(code, report_date, attempts, field="chip_backup_data")
        if chip:
            data["chip_backup_data"] = chip
    if not _has_value(data.get("source_events")):
        try:
            data["source_events"] = build_chip_backup_events(code, report_date)
            _record(attempts, "source_events", "filled" if data["source_events"] else "skipped", target=code)
        except Exception as exc:
            status, reason = _status_for_exception(exc)
            _record(attempts, "source_events", status, target=code, reason=reason, error=exc)


def _refill_research_financial_detail(code: str, data: dict[str, Any], attempts: list[dict[str, Any]]) -> None:
    if _has_value(data.get("financial_data")):
        return
    detail = _financial_detail_for_code(code, attempts, field="financial_data")
    if _has_value(detail):
        data["financial_data"] = detail


def _financial_detail_for_code(code: str, attempts: list[dict[str, Any]], *, field: str) -> dict[str, Any]:
    try:
        with StockDataFetcher() as fetcher:
            meta = fetcher.resolve_stock(code)
            frame = fetcher.fetch_quarterly_financials(meta)
        rows = _tail_records(frame, 4)
        detail = financial_detail_snapshot(rows)
        _record(attempts, field, "filled" if _has_value(detail) else "skipped", target=code)
        return detail
    except Exception as exc:
        status, reason = _status_for_exception(exc)
        _record(attempts, field, status, target=code, reason=reason, error=exc)
        return {"status": "unavailable", "error": str(exc), "score_points": 0}


def _safe_free_sources(code: str, symbol: str, report_date: Any, attempts: list[dict[str, Any]], *, field_prefix: str) -> dict[str, Any]:
    try:
        free_sources = build_free_research_sources(code, symbol, report_date)
        _record(
            attempts,
            f"{field_prefix}:free_public_sources",
            "filled" if _free_sources_have_usable_data(free_sources) else "skipped",
            target=code,
            reason=None if _free_sources_have_usable_data(free_sources) else "sources_returned_unavailable_or_empty",
        )
        return free_sources
    except Exception as exc:
        status, reason = _status_for_exception(exc)
        _record(attempts, f"{field_prefix}:free_public_sources", status, target=code, reason=reason, error=exc)
        return {}


def _safe_chip_snapshot(code: str, report_date: Any, attempts: list[dict[str, Any]], *, field: str) -> dict[str, Any]:
    try:
        chip = build_chip_backup_snapshot(code, report_date)
        _record(attempts, field, "filled" if _has_value(chip) else "skipped", target=code)
        return chip
    except Exception as exc:
        status, reason = _status_for_exception(exc)
        _record(attempts, field, status, target=code, reason=reason, error=exc)
        return {}


def _merge_pack_free_sources(pack: dict[str, Any], free_sources: dict[str, Any]) -> None:
    mapping = {
        "gross_margin_cache": "gross_margin_cache",
        "tdcc": "tdcc_data",
        "valuation": "valuation_data",
        "mops_documents": "mops_documents",
    }
    for source_key, target_key in mapping.items():
        if not _has_value(pack.get(target_key)):
            pack[target_key] = free_sources.get(source_key) or {}


def _free_sources_have_usable_data(free_sources: dict[str, Any]) -> bool:
    for key in ("valuation", "tdcc", "gross_margin_cache", "mops_documents"):
        if _has_value(free_sources.get(key)):
            return True
    return False


def _status_for_exception(exc: BaseException) -> tuple[str, str | None]:
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(token in text for token in ("quota", "rate limit", "ratelimit", "429", "cooldown", "exhausted", "額度", "冷卻", "限流")):
        return "skipped", "source_quota_or_cooldown"
    return "failed", None


def _value_pack_missing_status(pack: dict[str, Any]) -> list[str] | None:
    missing = []
    for field in ("financial_detail", "gross_margin_cache", "chip_backup_summary", "valuation_data", "tdcc_data", "mops_documents", "source_events", "company_knowledge"):
        if not _has_value(pack.get(field)):
            missing.append(field)
    return missing or None


def _pack_by_code(pack: list[Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in pack:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or item.get("stock_id") or "").strip()
        if code:
            result[code] = dict(item)
    return result


def _chip_summary(chip: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(chip, dict) or not chip:
        return {"status": "no data"}
    if isinstance(chip.get("summary"), dict):
        return chip["summary"]
    return {
        "status": chip.get("status") or "covered",
        "top3_holders": chip.get("top3_holders"),
        "holding_ratio": chip.get("holding_ratio"),
        "source": chip.get("source"),
    }


def _tail_records(frame: pd.DataFrame, count: int) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    safe = frame.tail(count).copy()
    return safe.where(pd.notnull(safe), None).to_dict("records")


def _symbol_for_code(code: str) -> str:
    try:
        for entry in load_stock_universe(False):
            if entry.code == code:
                return entry.symbol
    except Exception:
        pass
    return f"{code}.TW" if code else code


def _has_value(value: Any) -> bool:
    if value is None or value == "" or value == [] or value == {}:
        return False
    if isinstance(value, dict):
        status = str(value.get("status") or "").lower()
        if status in {"missing", "empty", "unavailable", "failed", "no data"}:
            return False
    return True


def _record(attempts: list[dict[str, Any]], field: str, status: str, **metadata: Any) -> None:
    item = {
        "field": field,
        "status": status,
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, BaseException):
            item["error"] = str(value)[:240]
        else:
            item[key] = value
    attempts.append(item)
