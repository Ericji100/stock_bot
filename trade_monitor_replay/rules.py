from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT


class ReplayRuleError(ValueError):
    pass


@dataclass(frozen=True)
class ReplayRulePackage:
    version: str
    source_version: str
    prompt: str
    prompt_sha256: str
    schema: dict[str, Any]
    schema_text: str
    schema_sha256: str
    contract_version: int
    memory_version: int
    source_schema_sha256: str
    analysis_mode: str
    decision_authority: str
    execution_profile: str
    trade_direction_policy: str
    trade_setup_policy: str
    model_rules: str
    model_rules_sha256: str


def load_rule_package(manifest_path: Path) -> ReplayRulePackage:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayRuleError("回放規則manifest無法讀取。") from exc
    if not isinstance(manifest, dict):
        raise ReplayRuleError("回放規則manifest格式錯誤。")
    prompt_path = _project_path(manifest.get("analysis_prompt_path"))
    source_schema_path = _project_path(manifest.get("analysis_schema_path"))
    schema_path = _project_path(manifest.get("replay_schema_path"))
    prompt = _read_text(prompt_path, "analysis prompt")
    source_schema_text = _read_text(source_schema_path, "source analysis schema")
    schema_text = _read_text(schema_path, "replay analysis schema")
    prompt_hash = _sha256(prompt.encode("utf-8"))
    source_schema_hash = _sha256(source_schema_text.encode("utf-8"))
    schema_hash = _sha256(schema_text.encode("utf-8"))
    if prompt_hash != manifest.get("analysis_prompt_sha256"):
        raise ReplayRuleError("正式分析規則已改變，與回放固定雜湊不符。")
    if source_schema_hash != manifest.get("analysis_schema_sha256"):
        raise ReplayRuleError("正式分析schema已改變，與回放來源雜湊不符。")
    if schema_hash != manifest.get("replay_schema_sha256"):
        raise ReplayRuleError("回放精簡schema已改變，與固定雜湊不符。")
    model_rules = prompt
    model_rules_hash = prompt_hash
    model_rules_path_value = manifest.get("ai_decision_rubric_path")
    if model_rules_path_value is not None:
        model_rules_path = _project_path(model_rules_path_value)
        model_rules = _read_text(model_rules_path, "AI decision rubric")
        model_rules_hash = _sha256(model_rules.encode("utf-8"))
        if model_rules_hash != manifest.get("ai_decision_rubric_sha256"):
            raise ReplayRuleError("AI決策規則包已改變，與固定雜湊不符。")
    addendum_path_value = manifest.get("ai_decision_addendum_path")
    if addendum_path_value is not None:
        addendum_path = _project_path(addendum_path_value)
        addendum = _read_text(addendum_path, "AI decision addendum")
        addendum_hash = _sha256(addendum.encode("utf-8"))
        if addendum_hash != manifest.get("ai_decision_addendum_sha256"):
            raise ReplayRuleError("AI決策追加規則已改變，與固定雜湊不符。")
        model_rules = f"{model_rules.rstrip()}\n\n{addendum.lstrip()}"
        model_rules_hash = _sha256(model_rules.encode("utf-8"))
    try:
        schema = json.loads(schema_text)
    except json.JSONDecodeError as exc:
        raise ReplayRuleError("固定分析schema不是有效JSON。") from exc
    contract_version = int(manifest.get("contract_version"))
    default_analysis_mode = "PROGRAM_ONLY" if contract_version == 6 else "AI_ASSISTED"
    analysis_mode = str(manifest.get("analysis_mode") or default_analysis_mode).strip().upper()
    if analysis_mode not in {"PROGRAM_ONLY", "AI_ASSISTED", "AI_HYBRID"}:
        raise ReplayRuleError("analysis_mode只允許PROGRAM_ONLY、AI_ASSISTED或AI_HYBRID。")
    decision_authority = str(
        manifest.get("decision_authority")
        or ("AI_WITH_PROGRAM_GUARDRAILS" if analysis_mode == "AI_HYBRID" else "PROGRAM")
    ).strip().upper()
    if analysis_mode == "AI_HYBRID" and decision_authority != "AI_WITH_PROGRAM_GUARDRAILS":
        raise ReplayRuleError("AI_HYBRID必須使用AI_WITH_PROGRAM_GUARDRAILS決策權限。")
    execution_profile = str(manifest.get("execution_profile") or "legacy").strip()
    if not execution_profile:
        raise ReplayRuleError("execution_profile不得空白。")
    trade_direction_policy = str(
        manifest.get("trade_direction_policy") or "BOTH"
    ).strip().upper()
    if trade_direction_policy not in {"BOTH", "LONG_ONLY"}:
        raise ReplayRuleError("trade_direction_policy只允許BOTH或LONG_ONLY。")
    if trade_direction_policy == "LONG_ONLY" and analysis_mode != "AI_HYBRID":
        raise ReplayRuleError("LONG_ONLY目前只支援AI_HYBRID回放。")
    trade_setup_policy = str(
        manifest.get("trade_setup_policy") or "ALL"
    ).strip().upper()
    if trade_setup_policy not in {"ALL", "LONG_Q2_Q4_ONLY"}:
        raise ReplayRuleError("trade_setup_policy只允許ALL或LONG_Q2_Q4_ONLY。")
    if trade_setup_policy == "LONG_Q2_Q4_ONLY" and trade_direction_policy != "LONG_ONLY":
        raise ReplayRuleError("LONG_Q2_Q4_ONLY必須搭配LONG_ONLY方向政策。")
    return ReplayRulePackage(
        version=_required_text(manifest.get("replay_rule_version"), "replay_rule_version"),
        source_version=_required_text(manifest.get("source_version"), "source_version"),
        prompt=prompt,
        prompt_sha256=prompt_hash,
        schema=schema,
        schema_text=schema_text,
        schema_sha256=schema_hash,
        contract_version=contract_version,
        memory_version=int(manifest.get("memory_version")),
        source_schema_sha256=source_schema_hash,
        analysis_mode=analysis_mode,
        decision_authority=decision_authority,
        execution_profile=execution_profile,
        trade_direction_policy=trade_direction_policy,
        trade_setup_policy=trade_setup_policy,
        model_rules=model_rules,
        model_rules_sha256=model_rules_hash,
    )


def _project_path(value: Any) -> Path:
    text = _required_text(value, "path")
    path = Path(text)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve()
    if PROJECT_ROOT.resolve() not in resolved.parents:
        raise ReplayRuleError("回放規則只能引用專案內的固定檔案。")
    return resolved


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReplayRuleError(f"{label}無法讀取。") from exc


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ReplayRuleError(f"{field}不得空白。")
    return text


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
