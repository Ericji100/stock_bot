#!/usr/bin/env python
"""
Live small-flow smoke test for /value_scan Tavily search landing.

This script consumes a very small amount of Tavily quota: it runs only one
/value_scan discovery task, one query, and requests one search result. It does
not call AI models and does not run Tavily Extract.

Usage:
    python scripts/smoke_value_scan_tavily_live.py
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from research_center.command_parser import parse_command_text
from research_center.config import ResearchCenterConfig, load_research_config
from research_center.models import SourceItem
from research_center.orchestrator import ResearchCenter


DEFAULT_COMMAND = "/value_scan 精選選股 --deep --top 1"
ROOT_DIR = Path(__file__).resolve().parents[1]


def _candidate() -> dict[str, Any]:
    return {
        "code": "2330",
        "name": "台積電",
        "symbol": "2330.TW",
        "industry": "半導體業",
        "price": 900,
        "rerating_score": 88,
        "verification_score": 60,
        "old_market_label": "晶圓代工",
        "new_market_label": "AI 半導體供應鏈",
        "rerating_evidence": ["AI/HPC 需求支撐先進製程"],
        "counter_evidence": ["估值與景氣循環仍需追蹤"],
        "data_gaps": ["最新法說細節待查"],
        "source_events": [],
        "financial_detail": {"status": "partial", "eps": 10.0},
    }


def _structured_data() -> tuple[dict[str, Any], list[SourceItem]]:
    row = _candidate()
    data = {
        "candidate_pool": "精選選股",
        "candidate_source_policy": {"source": "live_smoke_fixture", "status": "ok"},
        "report_date": "2026-06-02",
        "top_n": 1,
        "total_candidate_count": 1,
        "ai_candidate_limit": 1,
        "value_scan_sort_policy": "live_smoke_fixture",
        "ai_candidates": [row],
        "candidates": [row],
        "local_ranking": [{"code": row["code"], "name": row["name"], "rerating_score": row["rerating_score"]}],
        "ai_candidate_evidence_pack": [
            {
                "code": row["code"],
                "name": row["name"],
                "old_market_label": row["old_market_label"],
                "new_market_label": row["new_market_label"],
                "rerating_score": row["rerating_score"],
                "verification_score": row["verification_score"],
                "financial_detail": row["financial_detail"],
                "source_events": [],
                "missing_data_status": ["最新法說細節待查"],
            }
        ],
        "source_events": [],
        "scoring_rules": {"smoke": True},
        "verification_policy": "live Tavily smoke：只驗證外部來源落地，不代表真實投資結論。",
        "notes": ["本 smoke test 只消耗極小量 Tavily Search 額度。"],
    }
    return data, []


def _config_from_real_settings() -> ResearchCenterConfig | None:
    real = load_research_config()
    tavily_keys = real.tavily_api_keys or ((real.tavily_api_key,) if real.tavily_api_key else ())
    tavily_keys = tuple(key for key in tavily_keys if key)
    if not tavily_keys:
        return None
    return ResearchCenterConfig(
        model=real.model,
        fallback_models=real.fallback_models,
        enable_grounding=False,
        api_key=None,
        minimax_api_key=None,
        enable_low_model_digest=False,
        opencode_api_key=None,
        enable_opencode_analysis=False,
        enable_minimax_search=False,
        enable_minimax_comparison=False,
        enable_tavily_search=True,
        enable_tavily_extract=False,
        gemini_search_mode="off",
        tavily_api_key=tavily_keys[0],
        tavily_api_keys=tavily_keys,
        tavily_monthly_credit_limit=real.tavily_monthly_credit_limit,
        tavily_credit_reserve=0,
        tavily_search_depth="basic",
        tavily_extract_depth="basic",
        tavily_max_results_per_query=1,
        tavily_max_extract_urls_per_task=0,
        report_root=ROOT_DIR / "reports" / "_smoke_value_scan_live",
        database_path=ROOT_DIR / "reports" / "_smoke_value_scan_live" / "smoke_research.db",
        output_formats=("md", "html", "json"),
    )


def run_smoke(command: str) -> dict[str, Any]:
    config = _config_from_real_settings()
    if config is None:
        return {"status": "skipped", "reason": "tavily_api_key_not_configured", "errors": []}

    config.report_root.mkdir(parents=True, exist_ok=True)
    request = parse_command_text(command, user_id="smoke_value_scan_tavily_live")
    center = ResearchCenter(config=config)
    center.tavily_search.max_results_per_query = 1

    progress_messages: list[str] = []

    def progress(message: str) -> None:
        progress_messages.append(message)

    structured_data, sources = _structured_data()
    with (
        patch("research_center.orchestrator.collect_structured_data", return_value=(structured_data, sources)),
        patch("research_center.orchestrator._enrich_sources_with_web_fetch", return_value=None),
        patch("research_center.orchestrator.persist_search_sources_to_news", return_value=None),
        patch("research_center.orchestrator.write_knowledge_draft", return_value=None),
        patch.object(center.gemini, "generate_report", side_effect=RuntimeError("smoke: AI disabled")),
    ):
        original_build = center._gemini_discovery_runner.run_discovery_flow

        def limited_discovery(req, srcs, data, use_grounding, progress_cb):
            from research_center.prompt_registry import build_grounding_discovery_prompts
            from research_center.orchestrator import _build_search_query_log

            tasks = build_grounding_discovery_prompts(req, structured_data=data, source_list=srcs)[:1]
            for task in tasks:
                task["queries"] = (task.get("queries") or [])[:1]
            data["search_query_log"] = _build_search_query_log(tasks)
            center._gemini_discovery_runner._run_tavily(req, tasks, srcs, data, progress_cb)
            return srcs, False

        center._gemini_discovery_runner.run_discovery_flow = limited_discovery
        try:
            result = center.run(replace(request, output_formats=("md", "html", "json")), progress=progress)
        finally:
            center._gemini_discovery_runner.run_discovery_flow = original_build

    metadata = result.report_json.get("metadata") or {}
    shared = metadata.get("shared_data_layer") or {}
    search_log = shared.get("search_query_log") or {}
    tavily_diag = metadata.get("tavily_search_discovery") or {}
    providers = search_log.get("providers") or []
    provider_count = sum(int(item.get("source_count") or 0) for item in providers if isinstance(item, dict))
    source_count = len(result.sources)

    errors: list[str] = []
    if search_log.get("task_count") != 1:
        errors.append(f"expected 1 limited task, got {search_log.get('task_count')}")
    if not providers:
        errors.append("search_query_log.providers missing")
    if not tavily_diag.get("enabled"):
        errors.append(f"tavily_search_discovery not enabled: {tavily_diag}")
    if not (tavily_diag.get("runs") or []):
        errors.append("tavily_search_discovery.runs missing")
    if provider_count < 1 and source_count < 1:
        errors.append("no external source landed in sources/provider diagnostics")

    return {
        "status": "ok" if not errors else "failed",
        "command": command,
        "report_id": result.artifacts.report_id,
        "artifact_json": str(result.artifacts.json_path),
        "ai_used": result.ai_used,
        "fallback_reason_present": bool(result.fallback_reason),
        "search_task_count": search_log.get("task_count"),
        "search_total_query_count": search_log.get("total_query_count"),
        "provider_entries": providers,
        "tavily_status": [run.get("status") for run in (tavily_diag.get("runs") or []) if isinstance(run, dict)],
        "source_count": source_count,
        "progress_tail": progress_messages[-10:],
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Live small-flow /value_scan Tavily smoke test.")
    parser.add_argument("--command", default=DEFAULT_COMMAND, help="Command text to parse and run.")
    args = parser.parse_args()
    result = run_smoke(args.command)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str), flush=True)
    return 0 if result["status"] in {"ok", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
