"""Staged topic-maintenance pipeline.

This module keeps AI outputs small and lets local code assemble the final
TopicChangePack. The goal is to avoid relying on one giant AI-generated JSON
object.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .topic_deduper import decide_topic_action_type
from .topic_models import TopicChangeMode, TopicChangePack, TopicChangeStatus
from .topic_quality import normalize_change_pack_quality
from .topic_schema_normalizer import (
    normalize_topic_candidate,
    normalize_topic_detail_actions,
)


AiJsonCall = Callable[[str, str], dict[str, Any] | list[Any]]

CANDIDATE_PROMPT_LIMITS = {
    "webfetch_evidence_json": 2200,
    "web_fetched_sources_json": 1000,
    "discovery_sources_json": 2200,
    "recent_scan_candidates_json": 700,
    "market_signals_json": 500,
    "external_topic_source_caches_json": 500,
    "existing_topic_profiles_json": 600,
    "company_topic_map_json": 500,
    "supply_chain_nodes_json": 500,
    "company_knowledge_json": 500,
    "low_model_digest_json": 400,
}

CANDIDATE_RETRY_PROMPT_LIMITS = {
    "webfetch_evidence_json": 1400,
    "web_fetched_sources_json": 600,
    "discovery_sources_json": 1400,
    "recent_scan_candidates_json": 400,
    "market_signals_json": 300,
    "external_topic_source_caches_json": 300,
    "existing_topic_profiles_json": 350,
    "company_topic_map_json": 300,
    "supply_chain_nodes_json": 300,
    "company_knowledge_json": 300,
    "low_model_digest_json": 250,
}

DETAIL_PROMPT_LIMITS = {
    "webfetch_evidence_json": 1800,
    "web_fetched_sources_json": 800,
    "existing_topic_profiles_json": 350,
    "company_topic_map_json": 350,
    "supply_chain_nodes_json": 350,
    "company_knowledge_json": 350,
    "low_model_digest_json": 500,
}

DETAIL_BATCH_SIZE = 2
DETAIL_AI_BATCH_LIMITS = {
    TopicChangeMode.INITIAL: 4,
    TopicChangeMode.UPDATE: 8,
}
DETAIL_STAGE_PROMPT_HARD_CHARS = 18000
CANDIDATE_STAGE_PROMPT_HARD_CHARS = 15000

LOCAL_FALLBACK_TOPIC_RULES = [
    {
        "theme_id": "mlcc_passive_components",
        "theme_name": "MLCC與被動元件",
        "keywords": ["MLCC", "被動元件", "國巨", "華新科", "漲價", "缺貨"],
    },
    {
        "theme_id": "heavy_electrical_power_grid",
        "theme_name": "重電與強韌電網",
        "keywords": ["重電", "變壓器", "電網", "強韌電網", "華城", "士電"],
    },
    {
        "theme_id": "ai_server_power_management",
        "theme_name": "AI伺服器電源與BBU",
        "keywords": ["BBU", "電源", "電源供應", "AI資料中心", "台達電", "儲能"],
    },
    {
        "theme_id": "pcb_ccl_high_speed_material",
        "theme_name": "PCB/CCL高速傳輸材料",
        "keywords": ["PCB", "CCL", "銅箔基板", "IC載板", "高頻高速", "ABF"],
    },
    {
        "theme_id": "advanced_packaging_testing",
        "theme_name": "先進封裝與測試",
        "keywords": ["CoWoS", "SoIC", "先進封裝", "封測", "測試介面"],
    },
    {
        "theme_id": "memory_recovery",
        "theme_name": "記憶體景氣復甦",
        "keywords": ["記憶體", "DRAM", "NAND", "HBM", "模組"],
    },
    {
        "theme_id": "optical_communication_cpo",
        "theme_name": "光通訊與CPO",
        "keywords": ["CPO", "光通訊", "矽光子", "光收發", "800G", "1.6T"],
    },
    {
        "theme_id": "financial_high_dividend",
        "theme_name": "金融股與高股息輪動",
        "keywords": ["金融", "金控", "壽險", "高股息", "ETF", "殖利率"],
    },
    {
        "theme_id": "robotics_automation",
        "theme_name": "機器人與自動化",
        "keywords": ["機器人", "自動化", "物理AI", "工業電腦", "伺服馬達"],
    },
    {
        "theme_id": "shipping_logistics_cycle",
        "theme_name": "航運與物流景氣循環",
        "keywords": ["航運", "貨櫃", "散裝", "運價", "物流", "BDI"],
    },
]


def run_topic_pipeline(
    *,
    mode: TopicChangeMode,
    ai_model: str,
    change_id: str,
    iso_ts: str,
    structured_data: dict[str, Any],
    prompt_variables: dict[str, str],
    load_prompt: Callable[[str], str],
    render_prompt: Callable[[str, dict[str, str]], str],
    call_ai_json: AiJsonCall,
    progress: Callable[[str], None] | None = None,
) -> tuple[TopicChangePack, list[dict[str, Any]]]:
    """Run staged topic maintenance and return (pack, stage_logs)."""
    stage_logs: list[dict[str, Any]] = []

    def emit(message: str) -> None:
        if progress:
            progress(message)

    existing_profiles = structured_data.get("existing_topic_profiles", []) or []
    candidates = _extract_candidates(
        prompt_variables=prompt_variables,
        load_prompt=load_prompt,
        render_prompt=render_prompt,
        call_ai_json=call_ai_json,
        stage_logs=stage_logs,
        emit=emit,
    )
    if not candidates:
        candidates = _fallback_candidates_from_evidence(structured_data)
        if candidates:
            stage_logs.append({"stage": "candidate_fallback", "count": len(candidates)})

    target_count = 12 if mode == TopicChangeMode.INITIAL else 8
    max_count = 12 if mode == TopicChangeMode.INITIAL else 8
    selected = candidates[:max_count]
    ai_detail_batch_limit = DETAIL_AI_BATCH_LIMITS[mode]
    detail_batch_size = 1 if mode == TopicChangeMode.UPDATE else DETAIL_BATCH_SIZE

    actions = []
    for batch_index, batch in enumerate(_chunks(selected, detail_batch_size), start=1):
        default_types = []
        for candidate in batch:
            action_type, target_theme_id = decide_topic_action_type(candidate, existing_profiles)
            candidate["action_type"] = action_type.value
            if target_theme_id:
                candidate["target_theme_id"] = target_theme_id
            default_types.append(action_type)

        if batch_index <= ai_detail_batch_limit:
            payload = _expand_batch(
                batch=batch,
                batch_index=batch_index,
                prompt_variables=prompt_variables,
                load_prompt=load_prompt,
                render_prompt=render_prompt,
                call_ai_json=call_ai_json,
                stage_logs=stage_logs,
                emit=emit,
            )
            batch_actions = normalize_topic_detail_actions(payload, fallback_candidates=batch)
        else:
            stage_logs.append({
                "stage": f"detail_expand_{batch_index}_local_budget_fallback",
                "count": len(batch),
                "warning": "已達 AI 細節擴寫批次上限，剩餘候選改用本地保守擴寫，避免題材庫維護逾時。",
            })
            batch_actions = normalize_topic_detail_actions(batch, fallback_candidates=batch)
        if not batch_actions:
            stage_logs.append({
                "stage": f"detail_expand_{batch_index}_local_fallback",
                "count": len(batch),
            })
            batch_actions = normalize_topic_detail_actions(batch, fallback_candidates=batch)
        for idx, action in enumerate(batch_actions):
            if idx < len(batch):
                action.action_type = default_types[idx]
                if batch[idx].get("target_theme_id"):
                    action.target_theme_id = batch[idx]["target_theme_id"]
        actions.extend(batch_actions)
        if len(actions) >= target_count:
            break

    warnings: list[str] = []
    company_knowledge_updates: dict[str, Any] = {}
    for log in stage_logs:
        if log.get("error"):
            warnings.append(f"{log.get('stage')}: {log.get('error')}")
        if log.get("warning"):
            warnings.append(f"{log.get('stage')}: {log.get('warning')}")
        if isinstance(log.get("company_knowledge_updates"), dict):
            company_knowledge_updates = _merge_company_knowledge_updates(
                company_knowledge_updates,
                log["company_knowledge_updates"],
            )

    pack = TopicChangePack(
        change_id=change_id,
        parent_change_id=None,
        mode=mode,
        status=TopicChangeStatus.PENDING,
        model=ai_model,
        created_at=iso_ts,
        updated_at=iso_ts,
        summary=_build_summary(mode, actions, len(candidates)),
        confidence="medium",
        actions=actions,
        warnings=warnings,
        sources=_sources_from_structured_data(structured_data),
        company_knowledge_updates=company_knowledge_updates,
        extra={
            "pipeline": "staged_v1",
            "candidate_count": len(candidates),
            "action_count": len(actions),
        },
    )
    if not pack.actions:
        pack.status = TopicChangeStatus.FAILED
        pack.warnings.append("AI 未產生可套用的題材變更，請拒絕此變更包或重新執行 /topic_maintain。")
    if _stage_logs_expose_model_reasoning(stage_logs):
        pack.status = TopicChangeStatus.FAILED
        warning = "AI 回應包含模型思考草稿（<think> 或英文推理），此變更包不可套用，請重新執行 /topic_maintain。"
        if warning not in pack.warnings:
            pack.warnings.append(warning)
    normalize_change_pack_quality(pack)
    return pack, stage_logs


def _stage_logs_expose_model_reasoning(stage_logs: list[dict[str, Any]]) -> bool:
    patterns = (
        "<think>",
        "</think>",
        "The user wants",
        "Let me",
        "I need to",
        "Now let me",
        "model_reasoning_exposed",
    )
    for log in stage_logs:
        for value in log.values():
            if not isinstance(value, str):
                continue
            lowered = value.lower()
            if any(pattern.lower() in lowered for pattern in patterns):
                return True
    return False


def _extract_candidates(
    *,
    prompt_variables: dict[str, str],
    load_prompt: Callable[[str], str],
    render_prompt: Callable[[str, dict[str, str]], str],
    call_ai_json: AiJsonCall,
    stage_logs: list[dict[str, Any]],
    emit: Callable[[str], None],
) -> list[dict[str, Any]]:
    template = load_prompt("topic_candidate_extract")
    if not template:
        stage_logs.append({"stage": "candidate_extract", "error": "missing prompt"})
        return []
    variables = _compact_prompt_variables(prompt_variables, CANDIDATE_PROMPT_LIMITS)
    prompt = render_prompt(template, variables)
    prompt = _append_low_model_digest_block(
        prompt,
        variables,
        max_chars=CANDIDATE_PROMPT_LIMITS["low_model_digest_json"],
    )
    prompt_chars = len(prompt)
    emit(f"AI 產生候選題材 prompt={prompt_chars} chars")
    if prompt_chars > CANDIDATE_STAGE_PROMPT_HARD_CHARS:
        stage_logs.append({
            "stage": "candidate_extract",
            "error": f"prompt_too_large:{prompt_chars}",
            "warning": "候選題材萃取 prompt 過大，改用本地 evidence 規則產生候選，避免題材庫維護逾時。",
            "prompt_chars": prompt_chars,
        })
        return []
    try:
        payload = call_ai_json(prompt, "candidate_extract")
        if _payload_exposes_model_reasoning(payload):
            stage_logs.append({"stage": "candidate_extract", "error": "model_reasoning_exposed"})
        if isinstance(payload, dict) and isinstance(payload.get("company_knowledge_updates"), dict):
            stage_logs.append({
                "stage": "candidate_extract_company_knowledge",
                "company_knowledge_updates": payload["company_knowledge_updates"],
            })
        candidates = _candidates_from_payload(payload)
        stage_logs.append({"stage": "candidate_extract", "count": len(candidates), "prompt_chars": prompt_chars})
        return candidates
    except Exception as exc:
        if _should_retry_candidate_error(exc):
            candidates = _retry_candidate_extract(
                prompt_variables=prompt_variables,
                template=template,
                render_prompt=render_prompt,
                call_ai_json=call_ai_json,
                stage_logs=stage_logs,
                reason=str(exc),
            )
            if candidates:
                stage_logs.append({
                    "stage": "candidate_extract_recovered",
                    "count": len(candidates),
                    "reason": str(exc),
                })
                return candidates
        stage_logs.append({"stage": "candidate_extract", "error": str(exc), "prompt_chars": prompt_chars})
        return []


def _candidates_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw_items = (
            payload.get("candidates")
            or payload.get("topics")
            or payload.get("items")
            or payload.get("actions")
        )
    else:
        raw_items = payload
    candidates = []
    for idx, item in enumerate(raw_items or []):
        candidate = normalize_topic_candidate(item, idx)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _should_retry_candidate_error(exc: Exception) -> bool:
    text = str(exc)
    retry_markers = (
        "ReadTimeout",
        "timed out",
        "timeout",
        "status=timeout",
        "MiniMax API request failed",
    )
    return any(marker.lower() in text.lower() for marker in retry_markers)


def _retry_candidate_extract(
    *,
    prompt_variables: dict[str, str],
    template: str,
    render_prompt: Callable[[str, dict[str, str]], str],
    call_ai_json: AiJsonCall,
    stage_logs: list[dict[str, Any]],
    reason: str,
) -> list[dict[str, Any]]:
    variables = _compact_prompt_variables(prompt_variables, CANDIDATE_RETRY_PROMPT_LIMITS)
    prompt = render_prompt(template, variables)
    prompt = _append_low_model_digest_block(
        prompt,
        variables,
        max_chars=CANDIDATE_RETRY_PROMPT_LIMITS["low_model_digest_json"],
    )
    try:
        payload = call_ai_json(prompt, "candidate_extract_retry")
        if _payload_exposes_model_reasoning(payload):
            stage_logs.append({"stage": "candidate_extract_retry", "error": "model_reasoning_exposed"})
            return []
        candidates = _candidates_from_payload(payload)
        stage_logs.append({
            "stage": "candidate_extract_retry",
            "count": len(candidates),
            "reason": reason,
            "prompt_chars": len(prompt),
        })
        return candidates
    except Exception as retry_exc:
        stage_logs.append({
            "stage": "candidate_extract_retry",
            "error": str(retry_exc),
            "reason": reason,
            "prompt_chars": len(prompt),
        })
        return []


def _expand_batch(
    *,
    batch: list[dict[str, Any]],
    batch_index: int,
    prompt_variables: dict[str, str],
    load_prompt: Callable[[str], str],
    render_prompt: Callable[[str, dict[str, str]], str],
    call_ai_json: AiJsonCall,
    stage_logs: list[dict[str, Any]],
    emit: Callable[[str], None],
) -> Any:
    template = load_prompt("topic_detail_expand")
    if not template:
        stage_logs.append({"stage": f"detail_expand_{batch_index}", "error": "missing prompt"})
        return {}
    variables = _compact_detail_prompt_variables(prompt_variables)
    variables["topic_candidates_json"] = json.dumps(
        [_compact_detail_candidate(candidate) for candidate in batch],
        ensure_ascii=False,
        indent=2,
    )
    prompt = render_prompt(template, variables)
    prompt = _append_low_model_digest_block(
        prompt,
        variables,
        max_chars=DETAIL_PROMPT_LIMITS["low_model_digest_json"],
    )
    prompt_chars = len(prompt)
    emit(f"AI 補題材細節 batch {batch_index} prompt={prompt_chars} chars")
    if prompt_chars > DETAIL_STAGE_PROMPT_HARD_CHARS:
        stage_logs.append({
            "stage": f"detail_expand_{batch_index}",
            "error": f"prompt_too_large:{prompt_chars}",
            "warning": "細節擴寫 prompt 過大，改用本地保守擴寫，避免題材庫維護卡住。",
            "prompt_chars": prompt_chars,
        })
        return {}
    try:
        payload = call_ai_json(prompt, f"detail_expand_{batch_index}")
        if _payload_exposes_model_reasoning(payload):
            stage_logs.append({"stage": f"detail_expand_{batch_index}", "error": "model_reasoning_exposed"})
        if isinstance(payload, dict) and isinstance(payload.get("company_knowledge_updates"), dict):
            stage_logs.append({
                "stage": f"detail_expand_{batch_index}_company_knowledge",
                "company_knowledge_updates": payload["company_knowledge_updates"],
            })
        count = len(payload.get("actions", [])) if isinstance(payload, dict) else len(payload or [])
        if count <= 0:
            retry_payload = _retry_detail_batch(
                prompt=prompt,
                stage=f"detail_expand_{batch_index}",
                call_ai_json=call_ai_json,
                stage_logs=stage_logs,
                reason="empty_actions",
            )
            retry_count = len(retry_payload.get("actions", [])) if isinstance(retry_payload, dict) else len(retry_payload or [])
            if retry_count > 0:
                stage_logs.append({
                    "stage": f"detail_expand_{batch_index}_recovered",
                    "count": retry_count,
                    "reason": "empty_actions",
                })
                return retry_payload
        stage_logs.append({"stage": f"detail_expand_{batch_index}", "count": count, "prompt_chars": prompt_chars})
        return payload
    except Exception as exc:
        if _should_retry_detail_error(exc):
            retry_payload = _retry_detail_batch(
                prompt=prompt,
                stage=f"detail_expand_{batch_index}",
                call_ai_json=call_ai_json,
                stage_logs=stage_logs,
                reason=str(exc),
            )
            retry_count = len(retry_payload.get("actions", [])) if isinstance(retry_payload, dict) else len(retry_payload or [])
            if retry_count > 0:
                stage_logs.append({
                    "stage": f"detail_expand_{batch_index}_recovered",
                    "count": retry_count,
                    "reason": str(exc),
                    "prompt_chars": prompt_chars,
                })
                return retry_payload
        stage_logs.append({"stage": f"detail_expand_{batch_index}", "error": str(exc), "prompt_chars": prompt_chars})
        return {}


def _should_retry_detail_error(exc: Exception) -> bool:
    text = str(exc)
    retry_markers = (
        "Expecting property name",
        "Expecting ',' delimiter",
        "Unterminated string",
        "Invalid control character",
        "JSONDecodeError",
        "ReadTimeout",
        "timed out",
        "timeout",
        "status=timeout",
        "MiniMax API request failed",
    )
    return any(marker.lower() in text.lower() for marker in retry_markers)


def _retry_detail_batch(
    *,
    prompt: str,
    stage: str,
    call_ai_json: AiJsonCall,
    stage_logs: list[dict[str, Any]],
    reason: str,
) -> dict[str, Any] | list[Any]:
    retry_prompt = (
        prompt
        + "\n\n"
        + "重要：上一輪輸出無法使用。請只輸出合法 JSON object；根物件只能包含 actions。"
        + " 不要 Markdown、不要註解、不要多餘文字。"
    )
    try:
        payload = call_ai_json(retry_prompt, f"{stage}_retry")
        count = len(payload.get("actions", [])) if isinstance(payload, dict) else len(payload or [])
        stage_logs.append({
            "stage": f"{stage}_retry",
            "count": count,
            "reason": reason,
            "prompt_chars": len(retry_prompt),
        })
        return payload
    except Exception as retry_exc:
        stage_logs.append({
            "stage": f"{stage}_retry",
            "error": str(retry_exc),
            "reason": reason,
            "prompt_chars": len(retry_prompt),
        })
        return {}


def _compact_detail_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Keep detail-stage candidates small while preserving auditable evidence hints."""
    if not isinstance(candidate, dict):
        return {}
    compact = {
        "theme_id": candidate.get("theme_id") or "",
        "theme_name": candidate.get("theme_name") or "",
        "keywords": _bounded_list(candidate.get("keywords"), limit=10, max_string=80),
        "industries": _bounded_list(candidate.get("industries"), limit=8, max_string=80),
        "reason": _limit_text(candidate.get("reason") or candidate.get("description") or "", 700),
        "candidate_companies": _compact_candidate_companies(candidate.get("candidate_companies")),
        "source_refs": _compact_source_refs(candidate.get("source_refs") or candidate.get("evidence")),
    }
    for key in ("action_type", "target_theme_id", "rank", "confidence"):
        if candidate.get(key) is not None:
            compact[key] = candidate[key]
    return compact


def _compact_candidate_companies(value: Any, *, limit: int = 16) -> list[dict[str, Any]]:
    companies = value if isinstance(value, list) else []
    compact: list[dict[str, Any]] = []
    for item in companies[:limit]:
        if isinstance(item, str):
            compact.append({"company_code": item, "company_name": "", "role": ""})
            continue
        if not isinstance(item, dict):
            continue
        compact.append({
            "company_code": item.get("company_code") or item.get("code") or "",
            "company_name": item.get("company_name") or item.get("name") or "",
            "role": _limit_text(item.get("role") or item.get("reason") or "", 120),
        })
    return compact


def _compact_source_refs(value: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    refs = value if isinstance(value, list) else []
    compact: list[dict[str, Any]] = []
    for item in refs[:limit]:
        if isinstance(item, str):
            compact.append({"title": _limit_text(item, 120), "claim": "", "url": ""})
            continue
        if not isinstance(item, dict):
            continue
        compact.append({
            "title": _limit_text(item.get("title") or item.get("source") or "", 120),
            "claim": _limit_text(item.get("claim") or item.get("snippet") or item.get("summary") or item.get("content") or "", 260),
            "url": _limit_text(item.get("url") or item.get("link") or "", 220),
            "source_level": _limit_text(item.get("source_level") or "", 40),
        })
    return compact


def _bounded_list(value: Any, *, limit: int, max_string: int) -> list[Any]:
    items = value if isinstance(value, list) else []
    return [_limit_text(item, max_string) if isinstance(item, str) else item for item in items[:limit]]


def _limit_text(value: Any, max_chars: int) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"... [truncated, total {len(text)} chars]"


def _payload_exposes_model_reasoning(payload: Any) -> bool:
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except Exception:
        text = str(payload)
    return _stage_logs_expose_model_reasoning([{"payload": text}])


def _fallback_candidates_from_evidence(structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = structured_data.get("webfetch_evidence") or {}
    items = evidence.get("items") if isinstance(evidence, dict) else []
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        hints = item.get("topic_hints") or item.get("matched_topics") or item.get("keywords") or []
        if isinstance(hints, str):
            hints = [hints]
        for hint in hints:
            name = str(hint or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            candidate = normalize_topic_candidate({
                "theme_name": name,
                "keywords": [name],
                "reason": item.get("claim") or item.get("snippet") or "由本地 evidence candidate 產生",
                "source_refs": item.get("sources") or [item],
                "candidate_companies": item.get("companies") or [],
            }, idx)
            if candidate:
                candidates.append(candidate)
    candidates.extend(_fallback_candidates_from_text_rules(structured_data, seen, len(candidates)))
    return candidates


def _fallback_candidates_from_text_rules(
    structured_data: dict[str, Any],
    seen: set[str],
    start_rank: int,
) -> list[dict[str, Any]]:
    text_items = _collect_fallback_text_items(structured_data)
    candidates: list[dict[str, Any]] = []
    for rule in LOCAL_FALLBACK_TOPIC_RULES:
        matched_items = []
        matched_keywords = []
        for text, source_ref in text_items:
            folded = text.lower()
            for keyword in rule["keywords"]:
                if str(keyword).lower() in folded:
                    matched_items.append(source_ref)
                    matched_keywords.append(keyword)
                    break
        if not matched_items:
            continue
        theme_name = rule["theme_name"]
        if theme_name in seen or rule["theme_id"] in seen:
            continue
        seen.add(theme_name)
        seen.add(rule["theme_id"])
        candidate = normalize_topic_candidate({
            "theme_id": rule["theme_id"],
            "theme_name": theme_name,
            "keywords": sorted(set(matched_keywords), key=str),
            "reason": "AI 候選萃取失敗時，由本地 evidence/discovery 關鍵詞規則產生的保底候選。",
            "source_refs": matched_items[:5],
            "candidate_companies": _candidate_companies_from_sources(matched_items),
            "rank": start_rank + len(candidates) + 1,
        }, start_rank + len(candidates))
        if candidate:
            candidates.append(candidate)
    return candidates


def _collect_fallback_text_items(structured_data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    collected: list[tuple[str, dict[str, Any]]] = []
    evidence = structured_data.get("webfetch_evidence") or {}
    evidence_items = evidence.get("items") if isinstance(evidence, dict) else []
    for item in evidence_items or []:
        if isinstance(item, dict):
            text = _join_text_fields(item, ("title", "claim", "snippet", "summary", "content"))
            if text:
                collected.append((text, item))
    for key in ("web_fetched_sources", "discovery_sources", "base_sources"):
        value = structured_data.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            text = _join_text_fields(item, ("title", "snippet", "summary", "content", "description"))
            if text:
                collected.append((text, item))
    return collected


def _join_text_fields(item: dict[str, Any], fields: tuple[str, ...]) -> str:
    parts = []
    for field in fields:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


def _candidate_companies_from_sources(items: list[dict[str, Any]]) -> list[Any]:
    companies: list[Any] = []
    seen: set[str] = set()
    for item in items:
        raw = item.get("companies") or item.get("candidate_companies") or []
        if isinstance(raw, (str, int)):
            raw = [raw]
        if not isinstance(raw, list):
            continue
        for company in raw:
            key = json.dumps(company, ensure_ascii=False, sort_keys=True) if isinstance(company, dict) else str(company)
            if key in seen:
                continue
            seen.add(key)
            companies.append(company)
    return companies[:10]


def _sources_from_structured_data(structured_data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("base_sources", "web_fetched_sources", "discovery_sources"):
        value = structured_data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)][:20]
    return []


def _build_summary(mode: TopicChangeMode, actions: list[Any], candidate_count: int) -> str:
    mode_text = "初始化" if mode == TopicChangeMode.INITIAL else "更新"
    return f"題材庫{mode_text}維護：候選 {candidate_count} 筆，產生 {len(actions)} 筆可審核變更。"


def _merge_company_knowledge_updates(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    result = dict(base or {})
    base_companies = result.get("companies") if isinstance(result.get("companies"), dict) else {}
    update_companies = update.get("companies") if isinstance(update.get("companies"), dict) else {}
    if update_companies:
        merged = dict(base_companies)
        for code, data in update_companies.items():
            if isinstance(data, dict) and isinstance(merged.get(code), dict):
                merged[code] = {**merged[code], **data}
            else:
                merged[code] = data
        result["companies"] = merged
    for key, value in update.items():
        if key != "companies":
            result[key] = value
    return result


def _compact_detail_prompt_variables(variables: dict[str, str]) -> dict[str, str]:
    return _compact_prompt_variables(variables, DETAIL_PROMPT_LIMITS)


def _compact_prompt_variables(variables: dict[str, str], limits: dict[str, int]) -> dict[str, str]:
    compacted = dict(variables)
    for key, max_chars in limits.items():
        if key in compacted:
            compacted[key] = _truncate_stage_text(str(compacted.get(key) or ""), max_chars)
    return compacted


def _append_low_model_digest_block(prompt: str, variables: dict[str, str], max_chars: int | None = None) -> str:
    digest = str(variables.get("low_model_digest_json") or "").strip()
    if not digest or digest in {"{}", "null"}:
        return prompt
    if max_chars is not None:
        digest = _truncate_stage_text(digest, max_chars)
    return (
        f"{prompt}\n\n"
        "## MiniMax M3 資料整理底稿\n"
        "以下底稿只可作為候選題材與證據對照參考；最終變更包仍必須由本階段 AI 重新審查、去重、驗證來源與反證。\n"
        f"{digest}"
    )


def _truncate_stage_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated for detail stage, total {len(text)} chars]"


def _chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index:index + size] for index in range(0, len(items), size)]
