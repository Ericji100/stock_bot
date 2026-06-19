#!/usr/bin/env python
"""
Dry-run smoke test for /value_scan discovery prompts and query logs.

This script does not call AI models or external search providers. It verifies
that /value_scan produces focused search tasks, preserves candidate batching,
injects evidence_role into discovery prompts, and asks the search整理代理 to
classify findings by evidence_usage.

Usage:
    python scripts/smoke_value_scan_discovery.py
    python scripts/smoke_value_scan_discovery.py --command "/value_scan 精選選股 --deep --top 30"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from research_center.command_parser import parse_command_text
from research_center.models import SourceItem
from research_center.orchestrator import _build_search_query_log
from research_center.prompt_registry import build_grounding_discovery_prompts


DEFAULT_COMMAND = "/value_scan 精選選股 --deep --top 30"


def _sample_candidates() -> list[dict[str, str]]:
    return [
        {"code": "2330", "name": "台積電"},
        {"code": "2454", "name": "聯發科"},
        {"code": "6217", "name": "中探針"},
        {"code": "6669", "name": "緯穎"},
        {"code": "2308", "name": "台達電"},
        {"code": "3037", "name": "欣興"},
    ]


def _assert_contains(name: str, text: str, terms: list[str]) -> list[str]:
    return [f"{name} missing: {term}" for term in terms if term not in text]


def run_smoke(command: str) -> dict[str, Any]:
    request = parse_command_text(command, user_id="smoke_value_scan_discovery")
    structured_data = {
        "ai_candidates": _sample_candidates(),
        "candidate_pool": request.candidate_pool or "精選選股",
        "local_rerating_snapshot": {"score": 82, "label": "中高價"},
    }
    source_list = [
        SourceItem(
            source_id="S001",
            title="MOPS sample",
            url="https://mops.twse.com.tw/",
            source_level="Level 1",
        )
    ]

    prompts = build_grounding_discovery_prompts(request, structured_data=structured_data, source_list=source_list)
    query_log = _build_search_query_log(prompts)
    prompt_text = "\n\n".join(str(item.get("prompt") or "") for item in prompts)
    queries_text = "\n".join(query for task in query_log["tasks"] for query in task["queries"])
    roles_text = "\n".join(str(item.get("evidence_role") or "") for item in prompts)
    labels = [str(item.get("label") or "") for item in prompts]

    errors: list[str] = []
    expected_labels = [
        "官方公告與月營收",
        "產品客戶與供應鏈驗證",
        "舊標籤與新標籤重估",
        "法人籌碼與資金確認",
        "反證與重估失敗風險",
    ]
    for label in expected_labels:
        if label not in labels:
            errors.append(f"task label missing: {label}")

    errors.extend(_assert_contains("queries", queries_text, [
        "MOPS",
        "重大訊息",
        "月營收",
        "法說會",
        "新產品",
        "新客戶",
        "供應鏈",
        "價值重估",
        "外資",
        "投信",
        "TDCC",
        "庫存",
        "毛利 下滑",
        "site:",
    ]))
    errors.extend(_assert_contains("prompt", prompt_text, [
        "本任務預期來源用途",
        "支持重估、支持反證、只作情緒或資料不足",
        "evidence_usage",
        "supports_rerating",
        "supports_counter_evidence",
        "sentiment_only",
        "insufficient",
    ]))
    errors.extend(_assert_contains("roles", roles_text, ["支持重估", "支持反證", "只作情緒", "資料不足"]))

    first_task_queries = query_log["tasks"][0]["queries"] if query_log["tasks"] else []
    stock_lines = [
        line for line in first_task_queries
        if any(code in str(line) for code in ("2330", "2454", "6217", "6669", "2308", "3037"))
    ]
    if len(stock_lines) < 2:
        errors.append("candidate batching failed: expected candidates to span multiple query lines")
    if stock_lines and not any("3037" in str(line) for line in stock_lines[1:]):
        errors.append("candidate batching failed: later candidate batch missing 3037")

    return {
        "status": "ok" if not errors else "failed",
        "command": command,
        "task_count": query_log["task_count"],
        "total_query_count": query_log["total_query_count"],
        "labels": labels,
        "evidence_roles": [str(item.get("evidence_role") or "") for item in prompts],
        "first_task_query_count": query_log["tasks"][0]["query_count"] if query_log["tasks"] else 0,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run /value_scan discovery smoke test.")
    parser.add_argument("--command", default=DEFAULT_COMMAND, help="Command text to parse and dry-run.")
    args = parser.parse_args()

    result = run_smoke(args.command)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
