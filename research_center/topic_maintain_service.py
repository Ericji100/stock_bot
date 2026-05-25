"""Topic maintain service - generates topic change packs via AI."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .command_parser import CommandRequest
from .topic_models import (
    TopicActionType,
    TopicChangeAction,
    TopicChangeMode,
    TopicChangePack,
    TopicChangeStatus,
    TopicSourceLevel,
)
from .topic_repository import (
    backup_topic_files,
    is_formal_library_empty,
    load_change_pack,
    load_company_knowledge_data,
    load_topic_profiles,
    raw_response_path,
    save_change_pack,
    write_topic_audit_log,
)
from .topic_quality import normalize_change_pack_quality


def _load_prompt(name: str) -> str:
    root = Path(__file__).resolve().parents[1]
    # Prefer prompt/topic/ directory, fallback to config/prompts/
    topic_path = root / "prompt" / "topic" / f"{name}.md"
    if topic_path.exists():
        return topic_path.read_text(encoding="utf-8")
    config_path = root / "config" / "prompts" / f"{name}.md"
    if config_path.exists():
        return config_path.read_text(encoding="utf-8")
    return ""


def _render_prompt(template: str, variables: dict[str, str]) -> str:
    """Replace {placeholder} tokens in template with variables values."""
    result = template
    for key, value in variables.items():
        result = result.replace("{" + key + "}", value)
    return result


def _slugify(text: str) -> str:
    """Convert text to safe snake_case theme_id."""
    text = text.lower().strip()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^\w_]", "", text)
    return text[:40]


_LEVEL_ALIASES: dict[str, TopicSourceLevel] = {
    "l1": TopicSourceLevel.L1_OFFICIAL,
    "l1_official": TopicSourceLevel.L1_OFFICIAL,
    "level 1": TopicSourceLevel.L1_OFFICIAL,
    "level_1": TopicSourceLevel.L1_OFFICIAL,
    "l2": TopicSourceLevel.L2_MEDIA,
    "l2_media": TopicSourceLevel.L2_MEDIA,
    "level 2": TopicSourceLevel.L2_MEDIA,
    "level_2": TopicSourceLevel.L2_MEDIA,
    "l3": TopicSourceLevel.L3_COMMUNITY,
    "l3_community": TopicSourceLevel.L3_COMMUNITY,
    "level 3": TopicSourceLevel.L3_COMMUNITY,
    "level_3": TopicSourceLevel.L3_COMMUNITY,
}


def _normalize_source_level(raw: str) -> TopicSourceLevel:
    key = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if key in _LEVEL_ALIASES:
        return _LEVEL_ALIASES[key]
    raw_key = raw.strip().lower()
    if raw_key in _LEVEL_ALIASES:
        return _LEVEL_ALIASES[raw_key]
    try:
        return TopicSourceLevel(raw)
    except ValueError:
        return TopicSourceLevel.L2_MEDIA


class TopicMaintainAIError(Exception):
    """Raised when AI call or JSON parsing fails in topic maintain."""

    def __init__(self, message: str, raw_response: str = ""):
        super().__init__(message)
        self.message = message
        self.raw_response = raw_response


def _source_item_to_dict(source: Any) -> dict[str, Any]:
    """Convert a SourceItem (or similar object) to a plain dict for JSON serialization."""
    return {
        "title": getattr(source, "title", "") or "",
        "url": getattr(source, "url", "") or "",
        "snippet": getattr(source, "snippet", "") or "",
        "published_date": str(getattr(source, "published_date", "") or ""),
        "source_level": str(getattr(source, "source_level", "") or ""),
        "provider": str(getattr(source, "provider", "") or ""),
        "provider_detail": str(getattr(source, "provider_detail", "") or ""),
    }


def _json_safe(value: Any) -> Any:
    """Recursively convert topic-maintain data into JSON-serializable values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if hasattr(value, "url") and hasattr(value, "title"):
        return _source_item_to_dict(value)
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _normalize_company_knowledge_updates(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    companies = value.get("companies")
    if isinstance(companies, dict):
        return {"companies": companies}
    return {"companies": value}


def _extract_chat_completion_content(value: Any) -> str | None:
    """Extract content from OpenAI/DeepSeek/MiniMax/Gemini chat completion wrapper dict.

    Handles:
    - OpenAI/DeepSeek/MiniMax: choices[0].message.content
    - Gemini: candidates[0].content.parts[0].text (or candidates[0].content.parts[].text joined)

    Returns:
        str: The content string, or None if not a recognized wrapper format.
    """
    if not isinstance(value, dict):
        return None

    # OpenAI/DeepSeek/MiniMax format: choices[0].message.content
    choices = value.get("choices")
    if isinstance(choices, list) and choices:
        first_choice = choices[0]
        if isinstance(first_choice, dict):
            message = first_choice.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content

    # Gemini format: candidates[0].content.parts[0].text
    candidates = value.get("candidates")
    if isinstance(candidates, list) and candidates:
        first_candidate = candidates[0]
        if isinstance(first_candidate, dict):
            content_block = first_candidate.get("content")
            if isinstance(content_block, dict):
                parts = content_block.get("parts")
                if isinstance(parts, list) and parts:
                    # Concatenate all part texts
                    texts = []
                    for part in parts:
                        if isinstance(part, dict):
                            part_text = part.get("text")
                            if isinstance(part_text, str):
                                texts.append(part_text)
                    if texts:
                        return "".join(texts)

    return None


def _ai_response_to_text(result: Any) -> str:
    """Normalize AI response to a string.

    Handles:
    - Objects with .raw attribute (dict, str, or other)
    - DeepSeek/OpenAI chat completion wrapper dict (extracts choices[0].message.content)
    - Plain dict (convert to JSON string)
    - Plain str (return as-is)
    - Other types (convert via str())
    """
    raw = getattr(result, "raw", result)
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        # Check if it's a chat completion wrapper
        content = _extract_chat_completion_content(raw)
        if content is not None:
            return content
        return json.dumps(_json_safe(raw), ensure_ascii=False)
    return str(raw)


def _parse_ai_json_response(raw_response: Any) -> dict[str, Any]:
    """Parse AI JSON response, handling chat completion wrapper, dict, str, and markdown code blocks.

    Returns:
        dict: The parsed JSON response

    Raises:
        json.JSONDecodeError: If the response cannot be parsed as valid JSON
    """
    # If it's a dict, check if it's a chat completion wrapper first
    if isinstance(raw_response, dict):
        content = _extract_chat_completion_content(raw_response)
        if content is not None:
            # Parse the extracted content as JSON
            json_text = content.strip()
            if json_text.startswith("```"):
                json_text = re.sub(r"^```(?:json)?\s*", "", json_text)
                json_text = re.sub(r"\s*```$", "", json_text)
            return json.loads(json_text)
        # Not a chat completion wrapper, return as-is
        return raw_response

    if not isinstance(raw_response, str):
        raw_response = _ai_response_to_text(raw_response)

    json_text = raw_response.strip()
    # Remove <think>... blocks (MiniMax reasoning output)
    json_text = re.sub(r"<think>.*?</think>", "", json_text, flags=re.DOTALL | re.IGNORECASE).strip()
    if json_text.startswith("```"):
        json_text = re.sub(r"^```(?:json)?\s*", "", json_text)
        json_text = re.sub(r"\s*```$", "", json_text)
    try:
        return json.loads(json_text)
    except json.JSONDecodeError:
        # Fallback: try to extract first { ... } JSON object from text
        # This handles cases where there's surrounding text or reasoning content remains
        match = re.search(r"\{[^{}]*\}", json_text, re.DOTALL)
        if match:
            # Try to find a more complete JSON object by finding the outermost braces
            first_brace = json_text.find("{")
            last_brace = json_text.rfind("}")
            if first_brace != -1 and last_brace > first_brace:
                extracted = json_text[first_brace : last_brace + 1]
                try:
                    return json.loads(extracted)
                except json.JSONDecodeError:
                    pass
        raise


def run_topic_maintain(
    request: CommandRequest,
    center: Any,
    progress: Callable[[str], None] | None = None,
    skip_webfetch_evidence: bool = False,
) -> TopicChangePack:
    """Generate a topic change pack (initial or update)."""
    def emit(msg: str) -> None:
        if progress:
            progress(f"[AI題材庫] /topic_maintain | {msg}")

    emit("開始執行題材知識庫維護")

    ai_model = request.ai_model or "gemini"
    report_date = (request.report_date or datetime.now().astimezone().date()).isoformat() if request.report_date else datetime.now().astimezone().strftime("%Y-%m-%d")

    # Determine mode
    mode = TopicChangeMode.INITIAL if is_formal_library_empty() else TopicChangeMode.UPDATE
    emit(f"判斷模式：{'初始化' if mode == TopicChangeMode.INITIAL else '更新'}")

    # Load prompt template - single unified prompt
    template = _load_prompt("topic_maintain")
    if not template:
        raise TopicMaintainAIError("找不到 prompt：topic_maintain.md")

    # Collect structured data via center's existing data services
    emit("收集全市場股票與來源資料")
    try:
        from .data_services import collect_structured_data
        structured_data, base_sources = collect_structured_data(request, progress=progress)
        structured_data["base_sources"] = [_source_item_to_dict(s) for s in (base_sources or [])]
    except Exception as exc:
        emit(f"資料收集略過：{exc}")
        structured_data = {}
        base_sources = []

    # Run discovery flow if center available
    discovery_sources = []
    if center is not None and hasattr(center, "_gemini_discovery_runner"):
        try:
            emit("執行discovery搜尋")
            sources_out, _ = center._gemini_discovery_runner.run_discovery_flow(
                request, sources=list(base_sources or []), structured_data=structured_data,
                use_grounding=True, progress=progress,
            )
            from .date_aware_context import filter_and_sort_sources_for_analysis_date
            discovery_sources, dropped_sources = filter_and_sort_sources_for_analysis_date(sources_out, request)
            if dropped_sources:
                structured_data["date_aware_source_filter"] = {
                    "dropped_after_analysis_date_count": len(dropped_sources),
                }
            emit(f"Discovery完成，取得 {len(discovery_sources)} 筆來源")
        except Exception as exc:
            emit(f"Discovery略過：{exc}")

        # WebFetch enrichment - topic_maintain always runs WebFetch for latest data when discovery sources exist
        if discovery_sources and (request.command == "topic_maintain" or request.report_date is None):
            try:
                emit("WebFetch enrichment 開始")
                from .web_fetch_enrichment import _enrich_sources_with_web_fetch
                _enrich_sources_with_web_fetch(request, discovery_sources, structured_data, progress)
                emit(f"WebFetch完成")
            except Exception as exc:
                emit(f"WebFetch略過：{exc}")

        # Extract structured evidence from WebFetch results using rule-based extraction (no AI)
        if skip_webfetch_evidence:
            structured_data["webfetch_evidence"] = {
                "skipped": True,
                "reason": "smoke_test_fast_mode",
                "items": [],
            }
        elif structured_data.get("web_fetched_sources"):
            try:
                from .topic_evidence_extractor import build_topic_evidence_candidates
                emit("整理 WebFetch evidence candidates")
                evidence = build_topic_evidence_candidates(
                    structured_data.get("web_fetched_sources", []),
                    existing_topic_profiles=structured_data.get("existing_topic_profiles"),
                    company_universe=structured_data.get("candidate_companies"),
                    max_items=80,
                )
                structured_data["webfetch_evidence"] = evidence
                emit(f"WebFetch evidence candidates完成，共 {len(evidence.get('items', []))} 筆")
            except Exception as exc:
                emit(f"WebFetch evidence candidates略過：{exc}")
                structured_data["webfetch_evidence"] = {"mode": "rule_based", "items": [], "warnings": [str(exc)]}

    # Build prompt
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    change_id = f"change_{timestamp}"
    iso_ts = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")

    # Serialize collected data for prompt injection
    def _json_dumps(data: Any, max_chars: int) -> str:
        if data is None:
            return ""
        text = json.dumps(_json_safe(data), ensure_ascii=False, indent=2)
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... [truncated, total {len(text)} chars]"
        return text

    structured_data = _json_safe(structured_data)
    sd_json = _json_dumps(structured_data, 20000)
    discovery_json = _json_dumps(discovery_sources, 20000)
    webfetch_json = _json_dumps(structured_data.get("web_fetched_sources", []), 30000)
    profiles_json = _json_dumps(structured_data.get("existing_topic_profiles", []), 10000)
    ctm_json = _json_dumps(structured_data.get("company_topic_map", {}), 10000)
    sc_json = _json_dumps(structured_data.get("supply_chain_nodes", []), 10000)
    scan_json = _json_dumps(structured_data.get("recent_scans", []), 10000)
    scan_candidates_json = _json_dumps(structured_data.get("recent_scan_candidates", []), 20000)
    signals_json = _json_dumps(structured_data.get("market_signals", {}), 20000)
    candidates_json = _json_dumps(structured_data.get("candidate_companies", []), 10000)
    diag_json = _json_dumps(structured_data.get("web_fetch_diagnostics", {}), 5000)
    base_sources_json = _json_dumps(structured_data.get("base_sources", []), 15000)
    webfetch_evidence_json = _json_dumps(structured_data.get("webfetch_evidence", {}), 30000)
    company_knowledge_json = _json_dumps(load_company_knowledge_data(), 20000)
    recent_theme_reports_json = _json_dumps(structured_data.get("recent_theme_reports", []), 15000)
    external_sources_json = _json_dumps(structured_data.get("external_topic_source_caches", {}), 30000)

    emit(f"結構化資料摘要：{len(sd_json)} chars")
    emit(f"Discovery來源：{len(discovery_sources)} 筆")
    emit(f"WebFetch成功：{len(structured_data.get('web_fetched_sources', []))} 筆")
    emit(f"既有題材庫：{len(structured_data.get('existing_topic_profiles', []))} 項")

    variables = {
        "report_date": report_date,
        "model": ai_model,
        "mode": mode.value,
        "timestamp": timestamp,
        "iso_timestamp": iso_ts,
        "theme": request.target or request.theme_scope or "",
        "structured_data_json": sd_json,
        "discovery_sources_json": discovery_json,
        "web_fetched_sources_json": webfetch_json,
        "recent_scan_candidates_json": scan_candidates_json,
        "market_signals_json": signals_json,
        "base_sources_json": base_sources_json,
        "webfetch_evidence_json": webfetch_evidence_json,
        "external_topic_source_caches_json": external_sources_json,
        "recent_theme_reports_json": recent_theme_reports_json,
        "existing_topic_profiles_json": profiles_json,
        "company_topic_map_json": ctm_json,
        "supply_chain_nodes_json": sc_json,
        "company_knowledge_json": company_knowledge_json,
        "recent_scans_json": scan_json,
        "candidate_companies_json": candidates_json,
        "search_diagnostics_json": diag_json,
    }
    prompt = _render_prompt(template, variables)

    # Save prompt log
    from .prompt_logging import write_prompt_log
    prompt_log_p = write_prompt_log(request, prompt, ai_model, False, discovery_sources, {"purpose": f"topic_maintain_{mode.value}"})
    emit("Prompt log 已保存")

    # Call AI
    emit(f"呼叫 AI：{ai_model}")
    raw_response = ""
    try:
        if ai_model == "deepseek":
            if center is None or not hasattr(center, "opencode"):
                raise TopicMaintainAIError("DeepSeek model not available")
            result = center.opencode.generate_report(prompt)
            raw_response = _ai_response_to_text(result)
        elif ai_model == "minimax":
            if center is None or not hasattr(center, "minimax"):
                raise TopicMaintainAIError("MiniMax model not available")
            if not hasattr(center.minimax, "generate_json"):
                raise TopicMaintainAIError("MiniMax JSON-only method not available")
            result = center.minimax.generate_json(prompt)
            raw_response = _ai_response_to_text(result)
        else:
            if center is None or not hasattr(center, "gemini"):
                raise TopicMaintainAIError("Gemini model not available")
            result = center.gemini.generate_report(prompt, enable_grounding=False)
            raw_response = _ai_response_to_text(result)
    except Exception as exc:
        emit(f"AI 呼叫失敗：{exc}")
        raise TopicMaintainAIError(f"AI 呼叫失敗：{exc}", str(raw_response))

    # Save raw response
    raw_p = raw_response_path(change_id)
    try:
        # Try to parse as JSON and re-save as valid JSON
        json.loads(raw_response)
        raw_p.write_text(raw_response, encoding="utf-8")
    except json.JSONDecodeError:
        # Wrap in a JSON structure
        wrapped = json.dumps({"raw": raw_response}, ensure_ascii=False, indent=2)
        raw_p.write_text(wrapped, encoding="utf-8")
    emit("Raw response 已保存")

    # Parse JSON
    try:
        pack_data = _parse_ai_json_response(raw_response)
        # Override required fields
        pack_data["change_id"] = change_id
        pack_data["mode"] = mode.value
        pack_data["status"] = TopicChangeStatus.PENDING.value
        pack_data["model"] = ai_model
        pack_data["created_at"] = iso_ts
        pack_data["updated_at"] = iso_ts
        pack_data["raw_response_path"] = str(raw_p)
        pack_data["prompt_log_path"] = str(prompt_log_p)
        pack_data["company_knowledge_updates"] = _normalize_company_knowledge_updates(
            pack_data.get("company_knowledge_updates", {})
        )
        pack = TopicChangePack.from_dict(pack_data)
        normalize_change_pack_quality(pack)
    except json.JSONDecodeError as exc:
        preview = raw_response[:300] if raw_response else "(empty)"
        emit("JSON 解析失敗：模型未回傳有效 JSON")
        # raw_response_path and prompt_log_path are saved in the pack for debugging,
        # but must NOT be shown to Telegram users.
        raise TopicMaintainAIError(
            "JSON 解析失敗：模型未回傳有效 JSON。請改用其他模型或重新執行 /topic_maintain。",
            raw_response,
        )

    # Validate actions
    if not pack.actions:
        emit("警告：無有效 action，標記為 failed")
        pack.status = TopicChangeStatus.FAILED
        if not pack.warnings:
            pack.warnings = []
        pack.warnings.append("AI 未產生可套用的題材變更，請拒絕此變更包或重新執行 /topic_maintain。")

    # Initialization quality checks (mode == initial)
    if pack.mode == TopicChangeMode.INITIAL:
        _validate_initial_change_pack_quality(pack)

    # Save change pack
    save_change_pack(pack)
    emit(f"變更包已保存：{change_id}")
    return pack


def _normalize_topic_action_defaults(action: TopicChangeAction) -> list[str]:
    """Fill missing non-critical fields with safe defaults. Returns list of patched field names."""
    patched: list[str] = []
    if not action.affected_companies:
        action.affected_companies = []
        patched.append("affected_companies")
    if not action.risk_notes:
        action.risk_notes = ["待後續維護補強"]
        patched.append("risk_notes")
    if not action.missing_data:
        action.missing_data = ["待後續維護補強"]
        patched.append("missing_data")
    if not action.supply_chain_nodes:
        action.supply_chain_nodes = [{
            "company_code": "",
            "company_name": "",
            "role": "待補供應鏈或題材關聯",
            "upstream": [],
            "downstream": [],
            "product_keywords": [],
        }]
        patched.append("supply_chain_nodes")
    return patched


def _validate_initial_change_pack_quality(pack: TopicChangePack) -> None:
    """Validate initial change pack quality.

    Only marks FAILED for truly fatal issues (no actions, no create_theme, missing theme_id).
    Non-critical missing fields are auto-filled with defaults and kept as PENDING.
    Modifies pack.status, pack.warnings, and action fields in-place.
    """
    if pack.mode != TopicChangeMode.INITIAL:
        return

    create_actions = [a for a in pack.actions if a.action_type == TopicActionType.CREATE_THEME]

    # Fatal: no create_theme actions at all
    if len(create_actions) < 1:
        pack.status = TopicChangeStatus.FAILED
        pack.warnings.append("初始化未產生任何 create_theme actions，無法建立題材庫。")
        return

    # Fatal: missing theme_id (cannot identify the theme)
    fatal_missing_theme_id = [a for a in create_actions if not a.theme_id]
    if fatal_missing_theme_id:
        pack.status = TopicChangeStatus.FAILED
        pack.warnings.append(f"有 {len(fatal_missing_theme_id)} 個 create_theme 缺少 theme_id，無法識別題材。")

    # Auto-fill non-critical missing fields
    patched_any = False
    for action in create_actions:
        if _normalize_topic_action_defaults(action):
            patched_any = True

    if patched_any:
        existing = set(pack.warnings)
        msg = "部分題材資料尚未完整，系統已補入待補欄位，後續維護會持續修正。"
        if msg not in existing:
            pack.warnings.append(msg)

    # snake_case suggestion (warning only, never fails)
    import re
    for action in create_actions:
        if action.theme_id and not re.match(r"^[a-z][a-z0-9_]*$", action.theme_id):
            msg = f"theme_id '{action.theme_id}' 建議改為 snake_case 格式。"
            if msg not in set(pack.warnings):
                pack.warnings.append(msg)



