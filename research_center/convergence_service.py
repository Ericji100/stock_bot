from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from .models import CandidateSnapshot, CommandRequest, DataSourceSummary, ReportMetadata, SourceItem


def attach_convergence_fields(
    request: CommandRequest,
    structured_data: dict[str, Any],
    sources: list[SourceItem] | None = None,
    *,
    ai_used: bool = False,
    fallback_reason: str | None = None,
    report_id: str | None = None,
    report_variant: str | None = None,
) -> dict[str, Any]:
    structured_data["candidate_snapshot"] = build_candidate_snapshots(request, structured_data)
    structured_data["data_source_summary"] = build_data_source_summary(request, structured_data, sources or [])
    structured_data["report_metadata"] = build_report_metadata(
        request,
        structured_data,
        ai_used=ai_used,
        fallback_reason=fallback_reason,
        report_id=report_id,
        report_variant=report_variant,
    )
    return structured_data


def build_report_convergence_layer(
    request: CommandRequest,
    structured_data: dict[str, Any] | None,
    sources: list[SourceItem],
    *,
    ai_used: bool,
    fallback_reason: str | None,
    report_id: str | None = None,
    report_variant: str | None = None,
) -> dict[str, Any]:
    data = structured_data or {}
    candidate_snapshot = build_candidate_snapshots(request, data)
    data_source_summary = build_data_source_summary(request, data, sources)
    report_metadata = build_report_metadata(
        request,
        data,
        ai_used=ai_used,
        fallback_reason=fallback_reason,
        report_id=report_id,
        report_variant=report_variant,
    )
    return {
        "schema_version": "convergence_report_layer_v1",
        "report_metadata": report_metadata,
        "data_source_summary": data_source_summary,
        "candidate_snapshot": candidate_snapshot,
    }


def build_candidate_snapshots(request: CommandRequest, structured_data: dict[str, Any] | None) -> list[dict[str, Any]]:
    data = structured_data or {}
    rows = _candidate_rows_for_request(request, data)
    snapshots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        snapshot = candidate_snapshot_from_row(
            row,
            source_command=request.command,
            source_pool=_source_pool_for_request(request, data),
            data_date=_data_date(data, request),
        )
        code = snapshot.get("code")
        key = f"{code}|{snapshot.get('source_strategy')}|{snapshot.get('stage')}"
        if not code or key in seen:
            continue
        seen.add(key)
        snapshots.append(snapshot)
    return snapshots


def candidate_snapshot_from_row(
    row: dict[str, Any],
    *,
    source_command: str,
    source_pool: str | None = None,
    data_date: str | None = None,
    source_strategy: str | None = None,
) -> dict[str, Any]:
    code = str(row.get("code") or row.get("stock_id") or row.get("symbol") or "").strip()
    symbol = str(row.get("symbol") or "").strip() or None
    strategy = source_strategy or _source_strategy(row, source_command)
    stage = _candidate_stage(row, source_command)
    snapshot = CandidateSnapshot(
        code=code,
        name=str(row.get("name") or row.get("stock_name") or "").strip(),
        market=row.get("market"),
        symbol=symbol,
        source_command=source_command,
        source_strategy=strategy,
        source_pool=source_pool,
        signal_date=_str_or_none(row.get("signal_date") or row.get("scan_date") or row.get("report_date") or data_date),
        data_date=data_date,
        signal_type=_signal_type(row, source_command),
        signal_strength=_signal_strength(row),
        stage=stage,
        technical_signals=_list_of_dicts(row.get("technical_signals")),
        chip_signals=_chip_signals(row),
        revenue_signals=_revenue_signals(row),
        theme_signals=_theme_signals(row),
        news_signals=_list_of_dicts(row.get("news_items") or row.get("news_signals")),
        early_stage_flags=_early_stage_flags(row, stage),
        overheat_flags=_overheat_flags(row),
        risk_flags=_risk_flags(row),
        local_scores=_local_scores(row),
        evidence_refs=_evidence_refs(row),
        raw_snapshot_ref=_str_or_none(row.get("raw_snapshot_ref")),
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    return _drop_empty(asdict(snapshot))


def build_data_source_summary(
    request: CommandRequest,
    structured_data: dict[str, Any] | None,
    sources: list[SourceItem],
) -> list[dict[str, Any]]:
    data = structured_data or {}
    items: list[dict[str, Any]] = []
    for policy_key, data_type in (
        ("candidate_source_policy", "candidate_pool"),
        ("price_data_policy", "price"),
        ("strong_stock_policy", "theme_radar_candidates"),
        ("data_quality", "data_quality"),
        ("data_coverage", "data_coverage"),
        ("data_gap_summary", "data_gap"),
    ):
        value = data.get(policy_key)
        if isinstance(value, dict):
            items.append(_summary_from_policy(policy_key, data_type, value, request, data))
    for source in sources:
        items.append(_summary_from_source_item(source, request, data))
    if not items:
        items.append(asdict(DataSourceSummary(
            data_type=request.command,
            provider="local_structured_data",
            source_name="structured_data",
            as_of_date=_data_date(data, request),
            status="unknown",
            diagnostics={"note": "No explicit data source policy or source items were attached."},
        )))
    return [_drop_empty(item) for item in items]


def build_report_metadata(
    request: CommandRequest,
    structured_data: dict[str, Any] | None,
    *,
    ai_used: bool,
    fallback_reason: str | None,
    report_id: str | None = None,
    report_variant: str | None = None,
) -> dict[str, Any]:
    data = structured_data or {}
    ai_status = str(data.get("ai_status") or ("fallback_success" if fallback_reason else ("ai_success" if ai_used else "not_used")))
    metadata = ReportMetadata(
        report_id=report_id,
        report_type="stock" if request.command == "research" else request.command,
        command=request.command,
        target=request.target or request.market_scope or request.theme_scope or request.candidate_pool or "latest",
        report_date=_data_date(data, request, prefer_report=True),
        data_date=_data_date(data, request),
        model=str(data.get("analysis_model") or data.get("analysis_model_choice") or request.ai_model or "") or None,
        mode=request.mode,
        source_pool=_source_pool_for_request(request, data),
        ai_used=ai_used,
        ai_status=ai_status,
        fallback_reason=fallback_reason,
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    payload = asdict(metadata)
    if report_variant:
        payload["report_variant"] = report_variant
    return _drop_empty(payload)


def _candidate_rows_for_request(request: CommandRequest, data: dict[str, Any]) -> list[dict[str, Any]]:
    if request.command == "value_scan":
        return list(data.get("ai_candidates") or data.get("candidates") or [])
    if request.command == "theme_radar":
        return list(data.get("strong_stocks") or [])
    if request.command == "theme_flow":
        return list(data.get("related_stocks") or [])
    if request.command == "research":
        stock = data.get("stock") or {}
        snapshot = data.get("local_rerating_snapshot") or {}
        if isinstance(stock, dict) or isinstance(snapshot, dict):
            return [{**(snapshot if isinstance(snapshot, dict) else {}), **(stock if isinstance(stock, dict) else {})}]
    return list(data.get("candidate_snapshot") or data.get("candidates") or [])


def _source_pool_for_request(request: CommandRequest, data: dict[str, Any]) -> str | None:
    policy = data.get("candidate_source_policy") if isinstance(data.get("candidate_source_policy"), dict) else {}
    return (
        request.candidate_pool
        or request.source
        or data.get("candidate_pool")
        or policy.get("source")
        or data.get("source")
    )


def _data_date(data: dict[str, Any], request: CommandRequest, *, prefer_report: bool = False) -> str | None:
    keys = ("report_date", "market_data_date", "data_date") if prefer_report else ("market_data_date", "data_date", "report_date")
    for key in keys:
        if data.get(key):
            return str(data.get(key))
    if request.report_date:
        return request.report_date.isoformat()
    return None


def _summary_from_policy(
    policy_key: str,
    data_type: str,
    policy: dict[str, Any],
    request: CommandRequest,
    data: dict[str, Any],
) -> dict[str, Any]:
    missing = policy.get("missing_fields") or policy.get("missing") or []
    if isinstance(missing, str):
        missing = [missing]
    fallback_chain = policy.get("fallback_chain") or []
    if isinstance(fallback_chain, str):
        fallback_chain = [fallback_chain]
    status = str(policy.get("status") or policy.get("data_status") or "unknown")
    return asdict(DataSourceSummary(
        data_type=data_type,
        provider=str(policy.get("provider") or policy.get("source") or "local"),
        source_name=policy_key,
        source_path_or_url=_str_or_none(policy.get("source_path") or policy.get("source_url")),
        as_of_date=_str_or_none(policy.get("report_date") or policy.get("as_of_date") or _data_date(data, request)),
        fetch_time=_str_or_none(policy.get("fetch_time") or policy.get("generated_at") or data.get("report_generated_at")),
        status=_normalize_status(status),
        row_count=_int_or_none(policy.get("candidate_count") or policy.get("row_count") or policy.get("input_record_count")),
        fallback_used=bool(policy.get("fallback_used") or "fallback" in status.lower()),
        fallback_chain=[str(item) for item in fallback_chain],
        missing_fields=[str(item) for item in missing],
        warning_flags=[str(item) for item in (policy.get("warning_flags") or policy.get("warnings") or [])],
        freshness=_str_or_none(policy.get("freshness")),
        diagnostics={key: _json_safe(value) for key, value in policy.items() if key not in {"missing_fields", "missing", "warning_flags", "warnings"}},
    ))


def _summary_from_source_item(source: SourceItem, request: CommandRequest, data: dict[str, Any]) -> dict[str, Any]:
    provider = source.provider or source.fetch_provider or source.provider_detail or "source_item"
    return asdict(DataSourceSummary(
        data_type="external_source",
        provider=str(provider),
        source_name=source.title,
        source_path_or_url=source.url,
        as_of_date=source.published_date or _data_date(data, request),
        status=_normalize_status(source.fetch_status or "ok"),
        diagnostics={
            "source_id": source.source_id,
            "source_level": source.source_level,
            "fetch_quality": source.fetch_quality,
            "failure_reason": source.failure_reason,
            "found_by": source.found_by,
        },
    ))


def _source_strategy(row: dict[str, Any], command: str) -> str | None:
    for key in ("source_strategy", "scan_type", "strategy", "early_type", "signal_type"):
        if row.get(key):
            return str(row.get(key))
    strategies = row.get("radar_strategy_codes") or row.get("strategy_codes") or row.get("source_labels")
    if isinstance(strategies, (list, tuple, set)) and strategies:
        return ",".join(str(item) for item in strategies)
    return command


def _candidate_stage(row: dict[str, Any], command: str) -> str:
    if row.get("stage"):
        return str(row.get("stage"))
    if row.get("overheat_flags"):
        return "overheated"
    if row.get("early_type") or row.get("early_signal_priority"):
        return "early_single_signal"
    strategies = row.get("strategy_codes") or row.get("radar_strategy_codes") or row.get("source_labels") or []
    if isinstance(strategies, (list, tuple, set)) and len(strategies) >= 2:
        return "cross_confirmed"
    if row.get("total_score") or row.get("rerating_score"):
        return "momentum_confirmed" if command in {"radar", "theme_radar"} else "watch_only"
    return "watch_only"


def _signal_type(row: dict[str, Any], command: str) -> str | None:
    if row.get("signal_type"):
        return str(row.get("signal_type"))
    if row.get("technical_signals"):
        return "technical"
    if row.get("chip_grades") or row.get("chip_signals"):
        return "chip"
    if row.get("revenue_yoy") is not None or row.get("latest_monthly_revenue") is not None:
        return "revenue"
    if row.get("theme_matches") or row.get("theme_signals"):
        return "theme"
    return command


def _signal_strength(row: dict[str, Any]) -> float | None:
    for key in ("signal_strength", "total_score", "rerating_score", "verification_score", "early_signal_priority", "theme_score"):
        value = row.get(key)
        number = _float_or_none(value)
        if number is not None:
            return number
    return None


def _chip_signals(row: dict[str, Any]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    chip_grades = row.get("chip_grades") or {}
    if isinstance(chip_grades, dict):
        signals.extend({"strategy": str(key), "grade": value} for key, value in chip_grades.items())
    if isinstance(row.get("chip_signals"), list):
        signals.extend(item for item in row["chip_signals"] if isinstance(item, dict))
    return signals


def _revenue_signals(row: dict[str, Any]) -> list[dict[str, Any]]:
    signals = []
    for key in ("latest_monthly_revenue", "revenue_yoy", "monthly_revenue"):
        if row.get(key) is not None:
            signals.append({"field": key, "value": row.get(key)})
    if isinstance(row.get("revenue_history"), list):
        signals.append({"field": "revenue_history", "items": row.get("revenue_history")[:6]})
    return signals


def _theme_signals(row: dict[str, Any]) -> list[dict[str, Any]]:
    values = row.get("theme_signals") or row.get("theme_matches") or []
    if isinstance(values, list):
        return [item for item in values if isinstance(item, dict)]
    return []


def _early_stage_flags(row: dict[str, Any], stage: str) -> list[str]:
    flags = [str(item) for item in (row.get("early_stage_flags") or [])]
    if stage == "early_single_signal" and not flags:
        if row.get("early_type"):
            flags.append(str(row.get("early_type")))
        elif row.get("early_signal_priority"):
            flags.append("early_signal_priority")
    return flags


def _overheat_flags(row: dict[str, Any]) -> list[str]:
    flags = [str(item) for item in (row.get("overheat_flags") or [])]
    news_count = _int_or_none(row.get("news_count"))
    if news_count is not None and news_count >= 8:
        flags.append("news_volume_high")
    return flags


def _risk_flags(row: dict[str, Any]) -> list[str]:
    flags = [str(item) for item in (row.get("risk_flags") or [])]
    for item in row.get("counter_evidence") or []:
        text = str(item)
        if text and text not in flags:
            flags.append(text)
    return flags[:20]


def _local_scores(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "total_score",
        "score_components",
        "rerating_score",
        "verification_score",
        "early_signal_priority",
        "radar_score",
        "radar_score_components",
    )
    return {key: _json_safe(row.get(key)) for key in keys if row.get(key) is not None}


def _evidence_refs(row: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("evidence_refs", "source_ids"):
        value = row.get(key)
        if isinstance(value, list):
            refs.extend(str(item) for item in value)
    for source in row.get("ai_sources") or row.get("web_sources") or []:
        if isinstance(source, dict) and source.get("source_id"):
            refs.append(str(source.get("source_id")))
    return sorted(set(refs))[:30]


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_json_safe(item) for item in value if isinstance(item, dict)]


def _normalize_status(value: str) -> str:
    lowered = value.lower()
    if lowered in {"ok", "success", "ready", "covered", "complete", "market_movers"}:
        return "ok"
    if "fallback" in lowered:
        return "fallback"
    if lowered in {"missing", "empty", "no data"}:
        return "missing"
    if lowered in {"partial", "warning"}:
        return "partial"
    if lowered in {"skipped", "skip"}:
        return "skipped"
    if "error" in lowered or "failed" in lowered:
        return "error"
    return lowered or "unknown"


def _drop_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != [] and item != {}
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
