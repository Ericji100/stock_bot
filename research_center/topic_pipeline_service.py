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

DETAIL_PROMPT_LIMITS = {
    "webfetch_evidence_json": 3500,
    "web_fetched_sources_json": 1500,
    "existing_topic_profiles_json": 700,
    "company_topic_map_json": 700,
    "supply_chain_nodes_json": 700,
    "company_knowledge_json": 700,
    "low_model_digest_json": 1200,
}

DETAIL_BATCH_SIZE = 2


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

    target_count = 16 if mode == TopicChangeMode.INITIAL else 8
    max_count = 20 if mode == TopicChangeMode.INITIAL else 10
    selected = candidates[:max_count]

    actions = []
    for batch_index, batch in enumerate(_chunks(selected, DETAIL_BATCH_SIZE), start=1):
        default_types = []
        for candidate in batch:
            action_type, target_theme_id = decide_topic_action_type(candidate, existing_profiles)
            candidate["action_type"] = action_type.value
            if target_theme_id:
                candidate["target_theme_id"] = target_theme_id
            default_types.append(action_type)

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
    normalize_change_pack_quality(pack)
    return pack, stage_logs


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
    prompt = render_prompt(template, prompt_variables)
    prompt = _append_low_model_digest_block(prompt, prompt_variables)
    try:
        emit("AI 產生候選題材")
        payload = call_ai_json(prompt, "candidate_extract")
        if isinstance(payload, dict) and isinstance(payload.get("company_knowledge_updates"), dict):
            stage_logs.append({
                "stage": "candidate_extract_company_knowledge",
                "company_knowledge_updates": payload["company_knowledge_updates"],
            })
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
        stage_logs.append({"stage": "candidate_extract", "count": len(candidates)})
        return candidates
    except Exception as exc:
        stage_logs.append({"stage": "candidate_extract", "error": str(exc)})
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
    variables["topic_candidates_json"] = json.dumps(batch, ensure_ascii=False, indent=2)
    prompt = render_prompt(template, variables)
    prompt = _append_low_model_digest_block(
        prompt,
        variables,
        max_chars=DETAIL_PROMPT_LIMITS["low_model_digest_json"],
    )
    try:
        emit(f"AI 補題材細節 batch {batch_index}")
        payload = call_ai_json(prompt, f"detail_expand_{batch_index}")
        if isinstance(payload, dict) and isinstance(payload.get("company_knowledge_updates"), dict):
            stage_logs.append({
                "stage": f"detail_expand_{batch_index}_company_knowledge",
                "company_knowledge_updates": payload["company_knowledge_updates"],
            })
        count = len(payload.get("actions", [])) if isinstance(payload, dict) else len(payload or [])
        stage_logs.append({"stage": f"detail_expand_{batch_index}", "count": count})
        return payload
    except Exception as exc:
        stage_logs.append({"stage": f"detail_expand_{batch_index}", "error": str(exc)})
        return {}


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
    return candidates


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
    compacted = dict(variables)
    for key, max_chars in DETAIL_PROMPT_LIMITS.items():
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
