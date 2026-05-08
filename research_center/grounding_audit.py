from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .command_parser import parse_command_text
from .config import load_research_config
from .prompt_registry import prompt_metadata
from .gemini_service import build_prompt

AUDIT_COMMANDS = [
    "/research 5425",
    "/research 5425 --score",
    "/research 5425 --deep",
    "/research 5425 --source-only",
    "/research 5425 --date 2026-01-01",
    "/macro 台股",
    "/macro 台股 --brief",
    "/macro 台股 --deep",
    "/macro 台股 --source-only",
    "/macro 台股 --date 2026-01-01",
    "/theme AI伺服器",
    "/theme AI伺服器 --deep --top 30",
    "/theme AI伺服器 --source-only",
    "/theme AI伺服器 --date 2026-01-01",
    "/value_scan 精選選股 --top 10",
    "/value_scan 精選選股 --deep --top 30",
    "/value_scan 我的持股 --source-only",
    "/value_scan 精選選股 --date 2026-01-01",
    "/report latest",
]


def audit_grounding_matrix(commands: list[str] | None = None) -> list[dict[str, Any]]:
    config = load_research_config()
    rows: list[dict[str, Any]] = []
    for raw in commands or AUDIT_COMMANDS:
        request = parse_command_text(raw)
        should_call_ai = request.command != "report" and not request.source_only
        grounding_enabled = bool(config.enable_grounding and should_call_ai and request.report_date is None)
        prompt = "" if request.command == "report" else build_prompt(request, structured_data={}, source_list=[])
        rows.append(
            {
                "raw_text": raw,
                "command": request.command,
                "mode": request.mode,
                "report_date": request.report_date.isoformat() if request.report_date else None,
                "source_only": request.source_only,
                "should_call_ai": should_call_ai,
                "grounding_enabled": grounding_enabled,
                "has_gemini_search_task": "Gemini Search 任務" in prompt,
                "prompt_template": prompt_metadata(request).get("template") if request.command != "report" else None,
                "expected": _expected_label(should_call_ai, grounding_enabled, request.report_date is not None),
            }
        )
    return rows


def _expected_label(should_call_ai: bool, grounding_enabled: bool, historical: bool) -> str:
    if not should_call_ai:
        return "不應搜尋：report 或 source-only"
    if historical:
        return "不應搜尋：--date 歷史模式會停用 Gemini Search"
    if grounding_enabled:
        return "應搜尋：非歷史 AI 模式"
    return "不應搜尋：設定檔關閉 grounding"


if __name__ == "__main__":
    import json
    print(json.dumps(audit_grounding_matrix(), ensure_ascii=False, indent=2))
