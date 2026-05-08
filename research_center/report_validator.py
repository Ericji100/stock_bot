from __future__ import annotations

import re
from typing import Any

from .models import CommandRequest, SourceItem

REQUIRED_SCHEMA_KEYS = {
    "report_title",
    "report_type",
    "target",
    "mode",
    "report_date",
    "summary",
    "sections",
    "scores",
    "risks",
    "positive_factors",
    "watch_items",
    "sources",
}

FORBIDDEN_PATTERNS = ("保證獲利", "必漲", "一定買入", "自動下單", "穩賺", "保證達成")

EXPECTED_SECTIONS = {
    "research": ["摘要", "基本資料", "股價", "營收", "財報", "籌碼", "風險", "資料來源"],
    "macro": ["市場", "指數", "波動", "資金", "風險", "資料來源"],
    "theme": ["題材", "供應鏈", "受惠", "風險", "資料來源"],
    "value_scan": ["價值重估", "候選", "排名", "舊市場標籤", "新市場標籤", "風險", "資料來源"],
}


def validate_report(markdown: str, request: CommandRequest, sources: list[SourceItem], report_json: dict[str, Any]) -> dict[str, Any]:
    headings = _headings(markdown)
    source_refs = sorted(set(re.findall(r"\[S\d{3}\]", markdown)))
    expected = EXPECTED_SECTIONS.get(request.command, [])
    missing_sections = [section for section in expected if not _contains_heading(headings, section)]
    schema_errors = _schema_errors(report_json)
    forbidden_hits = [pattern for pattern in FORBIDDEN_PATTERNS if pattern in markdown]
    source_list_present = any("資料來源" in heading or "來源" in heading for heading in headings) or "[S001]" in markdown
    has_scores_when_required = True
    if ((request.command == "research" and request.mode in {"score", "deep"}) or request.command == "value_scan") and not report_json.get("scores"):
        has_scores_when_required = False

    warnings = []
    if not source_list_present:
        warnings.append("缺少資料來源章節或來源引用。")
    if sources and not source_refs:
        warnings.append("報告未引用任何 [Sxxx] 來源代號。")
    if not has_scores_when_required:
        warnings.append("評分模式缺少 scores 結構化資料。")
    missing_value_scan_candidates = _missing_value_scan_candidates(markdown, request, report_json)
    if missing_value_scan_candidates:
        warnings.append("/value_scan missing per-candidate rerating analysis: " + ", ".join(missing_value_scan_candidates))
    if forbidden_hits:
        warnings.append("報告含禁止語句：" + ", ".join(forbidden_hits))

    passed = not missing_sections and not schema_errors and not forbidden_hits and source_list_present and has_scores_when_required and not missing_value_scan_candidates
    return {
        "passed": passed,
        "missing_sections": missing_sections,
        "schema_errors": schema_errors,
        "forbidden_hits": forbidden_hits,
        "source_refs": source_refs,
        "source_list_present": source_list_present,
        "missing_value_scan_candidates": missing_value_scan_candidates,
        "warnings": warnings,
    }


def append_qa_notes(markdown: str, qa: dict[str, Any]) -> str:
    if qa.get("passed"):
        return markdown
    lines = [markdown.rstrip(), "", "## 規格檢查提醒"]
    for warning in qa.get("warnings") or []:
        lines.append(f"- {warning}")
    missing = qa.get("missing_sections") or []
    if missing:
        lines.append("- 缺少或未明確命名章節：" + ", ".join(missing))
    schema_errors = qa.get("schema_errors") or []
    if schema_errors:
        lines.append("- JSON schema 修補提醒：" + ", ".join(schema_errors))
    return "\n".join(lines).strip() + "\n"



def _missing_value_scan_candidates(markdown: str, request: CommandRequest, report_json: dict[str, Any]) -> list[str]:
    if request.command != "value_scan":
        return []
    metadata = report_json.get("metadata") or {}
    candidates = metadata.get("value_scan_candidates") or []
    if not candidates:
        return []
    missing: list[str] = []
    for item in candidates:
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        if code and code in markdown:
            continue
        if name and name in markdown:
            continue
        missing.append(" ".join(part for part in [code, name] if part) or "unknown")
    return missing

def _headings(markdown: str) -> list[str]:
    return [line.lstrip("#").strip() for line in markdown.splitlines() if line.startswith("#")]


def _contains_heading(headings: list[str], keyword: str) -> bool:
    return any(keyword in heading for heading in headings)


def _schema_errors(report_json: dict[str, Any]) -> list[str]:
    errors = []
    missing = sorted(REQUIRED_SCHEMA_KEYS - set(report_json))
    if missing:
        errors.append("missing keys: " + ", ".join(missing))
    if not isinstance(report_json.get("sections"), list):
        errors.append("sections must be list")
    if not isinstance(report_json.get("scores"), list):
        errors.append("scores must be list")
    if not isinstance(report_json.get("sources"), list):
        errors.append("sources must be list")
    return errors
