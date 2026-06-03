#!/usr/bin/env python
"""
Local smoke test for /value_scan report artifact generation.

This script does not call AI models or external search providers. It patches the
structured-data collection step with a small local candidate set, lets the normal
ResearchCenter orchestration build discovery query logs and report artifacts, and
then verifies the generated Markdown / HTML / JSON files.

Usage:
    python scripts/smoke_value_scan_report_local.py
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
from research_center.config import ResearchCenterConfig
from research_center.models import SourceItem
from research_center.orchestrator import ResearchCenter


DEFAULT_COMMAND = "/value_scan 精選選股 --deep --top 3"
ROOT_DIR = Path(__file__).resolve().parents[1]


def _candidate(code: str, name: str, score: int) -> dict[str, Any]:
    return {
        "code": code,
        "name": name,
        "symbol": f"{code}.TW",
        "industry": "電子零組件業",
        "price": 80 + score,
        "rerating_score": score,
        "verification_score": 55,
        "old_market_label": "傳統零組件",
        "new_market_label": "AI 伺服器供應鏈",
        "rerating_evidence": ["產品線切入高速傳輸", "法人開始討論新應用"],
        "counter_evidence": ["月營收仍待連續驗證"],
        "data_gaps": ["法說會細節待補"],
        "source_events": [
            {
                "title": f"{name} MOPS 重大訊息入口",
                "url": "https://mops.twse.com.tw/",
                "source_level": "Level 1",
                "event_type": "mops_reference",
            }
        ],
        "financial_detail": {"status": "partial", "eps": 2.1, "gross_margin": 31.5},
        "cross_validation": {"verification_score": 55, "coverage": "partial"},
    }


def _evidence_pack(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": row["code"],
        "name": row["name"],
        "old_market_label": row["old_market_label"],
        "new_market_label": row["new_market_label"],
        "rerating_score": row["rerating_score"],
        "verification_score": row["verification_score"],
        "financial_detail": row["financial_detail"],
        "gross_margin_cache": {"status": "covered", "gross_margin": 31.5},
        "chip_backup_summary": {"status": "partial", "foreign_buy": 1200},
        "valuation_data": {"status": "partial", "pe": 18.2},
        "tdcc_data": {"status": "partial", "large_holder_change": "待驗證"},
        "mops_documents": [{"title": "MOPS 重大訊息入口", "url": "https://mops.twse.com.tw/"}],
        "source_events": row["source_events"],
        "company_knowledge": {"status": "partial", "products": ["高速傳輸零組件"]},
        "missing_data_status": ["法說會細節待補"],
    }


def _structured_data() -> tuple[dict[str, Any], list[SourceItem]]:
    rows = [
        _candidate("2330", "台積電", 88),
        _candidate("2454", "聯發科", 82),
        _candidate("6217", "中探針", 76),
    ]
    data = {
        "candidate_pool": "精選選股",
        "candidate_source_policy": {"source": "smoke_fixture", "status": "ok"},
        "report_date": "2026-06-02",
        "top_n": 3,
        "total_candidate_count": len(rows),
        "ai_candidate_limit": 3,
        "value_scan_sort_policy": "smoke_fixture_order",
        "ai_candidates": rows,
        "candidates": rows,
        "local_ranking": [
            {
                "code": row["code"],
                "name": row["name"],
                "rerating_score": row["rerating_score"],
                "verification_score": row["verification_score"],
            }
            for row in rows
        ],
        "ai_candidate_evidence_pack": [_evidence_pack(row) for row in rows],
        "source_events": [event for row in rows for event in row["source_events"]],
        "scoring_rules": {"smoke": True},
        "verification_policy": "smoke fixture：只驗證本地報告流程，不代表真實投資結論。",
        "notes": ["本 smoke test 不呼叫外部搜尋或 AI。"],
    }
    sources = [
        SourceItem(
            source_id="S001",
            title="MOPS smoke fixture",
            url="https://mops.twse.com.tw/",
            source_level="Level 1",
            provider="smoke_fixture",
        )
    ]
    return data, sources


def _config(tmp_dir: Path) -> ResearchCenterConfig:
    return ResearchCenterConfig(
        enable_grounding=False,
        api_key=None,
        minimax_api_key=None,
        enable_low_model_digest=False,
        opencode_api_key=None,
        enable_opencode_analysis=False,
        enable_minimax_search=False,
        enable_minimax_comparison=False,
        enable_tavily_search=False,
        enable_tavily_extract=False,
        gemini_search_mode="off",
        tavily_api_key=None,
        tavily_api_keys=(),
        report_root=tmp_dir,
        database_path=tmp_dir / "smoke_research.db",
        output_formats=("md", "html", "json"),
    )


def run_smoke(command: str) -> dict[str, Any]:
    request = parse_command_text(command, user_id="smoke_value_scan_report")
    tmp_dir = ROOT_DIR / "reports" / "_smoke_value_scan"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    center = ResearchCenter(config=_config(tmp_dir))
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
        result = center.run(replace(request, output_formats=("md", "html", "json")), progress=progress)

    metadata = result.report_json.get("metadata") or {}
    shared = metadata.get("shared_data_layer") or {}
    search_log = shared.get("search_query_log") or {}
    structured_snapshot = result.report_json.get("structured_data") or {}
    evidence_pack = structured_snapshot.get("ai_candidate_evidence_pack") or []
    artifacts = result.artifacts
    paths = {
        "markdown": str(artifacts.markdown_path),
        "html": str(artifacts.html_path),
        "json": str(artifacts.json_path),
        "sources": str(artifacts.sources_path),
    }

    errors: list[str] = []
    if result.status != "success":
        errors.append(f"unexpected result status: {result.status}")
    if not result.fallback_reason:
        errors.append("fallback_reason missing; smoke should avoid real AI calls")
    if search_log.get("task_count") != 5:
        errors.append(f"expected 5 discovery tasks, got {search_log.get('task_count')}")
    if not search_log.get("total_query_count"):
        errors.append("search_query_log.total_query_count missing")
    if len(evidence_pack) < 3:
        errors.append(f"expected at least 3 evidence pack rows, got {len(evidence_pack)}")
    for label, path in paths.items():
        if not Path(path).exists():
            errors.append(f"{label} artifact missing: {path}")
    if "價值重估掃描報告" not in result.markdown:
        errors.append("markdown does not look like a value_scan report")

    return {
        "status": "ok" if not errors else "failed",
        "command": command,
        "report_id": artifacts.report_id,
        "tmp_dir": str(tmp_dir),
        "artifacts": paths,
        "fallback_reason_present": bool(result.fallback_reason),
        "ai_used": result.ai_used,
        "search_task_count": search_log.get("task_count"),
        "search_total_query_count": search_log.get("total_query_count"),
        "value_scan_candidate_count": metadata.get("value_scan_candidate_count"),
        "evidence_pack_count": len(evidence_pack),
        "progress_tail": progress_messages[-8:],
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Local /value_scan report generation smoke test.")
    parser.add_argument("--command", default=DEFAULT_COMMAND, help="Command text to parse and run.")
    args = parser.parse_args()
    result = run_smoke(args.command)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str), flush=True)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
