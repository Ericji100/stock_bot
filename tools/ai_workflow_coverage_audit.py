from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_center.ai_workflow_service import (  # noqa: E402
    LOW_MODEL_DIGEST_SCHEMA_VERSION,
    build_ai_workflow_coverage,
    build_high_model_input_package,
)
from research_center.models import CommandRequest, SourceItem  # noqa: E402

AUDIT_ROOT = ROOT / "logs" / "ai_workflow_coverage_audit"
AI_COMMANDS = [
    "research",
    "value_scan",
    "macro",
    "theme",
    "theme_radar",
    "theme_flow",
    "sector_strength",
    "radar",
    "news",
    "topic_maintain",
]


def _request_for(command: str) -> CommandRequest:
    return CommandRequest(command=command, raw_text=f"/{command}", target="market", mode="deep")


def _sources() -> list[SourceItem]:
    return [
        SourceItem(
            source_id="S001",
            title="官方公告與產業新聞",
            url="https://example.com/source",
            source_level="Level 1",
            provider="unit",
            published_date="2026-06-05",
            snippet="AI 指令最佳化覆蓋度測試來源。",
        )
    ]


def _low_digest() -> dict[str, Any]:
    return {
        "schema_version": LOW_MODEL_DIGEST_SCHEMA_VERSION,
        "status": "skipped",
        "model": "MiniMax-M3",
        "reason": "coverage audit does not consume low model quota",
    }


def _base_structured_data() -> dict[str, Any]:
    stocks = [
        {
            "code": f"23{i:02d}",
            "name": f"測試股{i}",
            "industry": "電子零組件",
            "primary_subsector": "電源",
            "trend_score": 80 + i,
            "theme_matches": [{"theme_name": "AI電源", "relation_score": 90}],
        }
        for i in range(12)
    ]
    return {
        "stock": {"code": "2330", "name": "台積電"},
        "price_data": {"close": 1000},
        "technical_data": {"trend_score": 80},
        "institutional_data": [{"date": "2026-06-01", "net_buy": 1}],
        "margin_data": [{"date": "2026-06-01", "margin_balance": 1}],
        "revenue_data": [{"month": "2026-05", "yoy": 10}],
        "financial_data": [{"quarter": "2026Q1", "eps": 1}],
        "local_scoring": {"scores": [{"name": "營收成長性", "score": 80}]},
        "local_ranking": [{"code": "2330", "score": 80}],
        "topic_context": {"matched_topics": [{"topic": "AI"}]},
        "unified_evidence_pack": {"items": [{"source_id": "S001", "type": "news", "summary": "AI demand"}]},
        "ai_candidates": [{"code": "2330", "name": "台積電", "rerating_score": 80}],
        "ai_candidate_evidence_pack": [{"code": "2330", "evidence": "AI"}],
        "quantitative_market": {"twse": {"score": 70}},
        "volatility": {"vix_proxy": 20},
        "industry_flow": [{"industry": "半導體", "flow": 1}],
        "fear_greed": {"score": 55},
        "market_score": {"total": 70},
        "global_public_macro": [{"event": "Fed"}],
        "theme": {"name": "AI電源"},
        "matched_companies": stocks[:5],
        "supply_chain_profile": {"layers": [{"name": "電源", "companies": ["台達電"]}]},
        "theme_rankings": [
            {
                "theme_id": "ai_power",
                "theme_name": "AI電源",
                "theme_strength_score": 91,
                "representative_stocks": stocks[:6],
                "candidate_stocks": stocks[6:],
            }
        ],
        "sector_rankings": [{"sector": "電子零組件", "representative_stocks": stocks[:4]}],
        "subsector_rankings": [{"subsector": "電源", "strong_samples": stocks[:5]}],
        "strong_stocks": stocks,
        "related_stocks": stocks[:6],
        "layers": [{"layer": 1, "representative_stocks": stocks[:3]}],
        "layer_market_validation": [{"layer": 1, "status": "validated"}],
        "next_layer_candidates": [{"code": "6282", "name": "康舒"}],
        "market_movers": {"top_gainers": stocks[:8], "top_volume_surge": stocks[4:]},
        "candidates": stocks[:5],
        "evidence_pack": {"items": [{"summary": "radar evidence"}]},
        "ai_compact_pack": {"candidates": [{"code": "2300"}]},
        "feature_pack": {"scope": "unit"},
        "data_coverage": {"status": "complete"},
        "news_batch": [{"title": "AI news", "source": "news"}],
        "news_context": {"items": [{"title": "AI news"}]},
        "sources": [{"title": "news", "url": "https://example.com"}],
        "existing_profiles": [{"topic": "AI電源"}],
        "source_candidates": [{"topic": "AI電源", "source": "news"}],
        "candidate_topics": [{"topic": "AI電源"}],
        "candidate_companies": [{"code": "2308", "name": "台達電"}],
        "change_pack": {"updates": [{"topic": "AI電源"}]},
        "low_model_digest": _low_digest(),
    }


def _maintenance_coverage(command: str) -> dict[str, Any]:
    strategy = "news_batch_deduped_classification" if command == "news" else "topic_change_pack_batches"
    return build_ai_workflow_coverage(
        command,
        local_data_package=True,
        low_model_digest=_low_digest(),
        high_model_input_package=True,
        dedupe_strategy=strategy,
        source_index=True,
        input_audit=True,
        html_sections=False,
        diagnostics={"audit": "offline_coverage_gate"},
        not_applicable=["html_sections"],
    )


def audit_command(command: str) -> dict[str, Any]:
    if command in {"news", "topic_maintain"}:
        coverage = _maintenance_coverage(command)
        return {
            "command": command,
            "status": coverage.get("status"),
            "missing_capabilities": coverage.get("missing_capabilities") or [],
            "not_applicable": coverage.get("not_applicable") or [],
            "dedupe_strategy": coverage.get("dedupe_strategy"),
            "checks": coverage.get("checks") or {},
        }
    package = build_high_model_input_package(
        _request_for(command),
        _base_structured_data(),
        _sources(),
        prompt_chars_estimate=400_000,
    )
    coverage = package.get("ai_workflow_coverage") or {}
    return {
        "command": command,
        "status": coverage.get("status"),
        "missing_capabilities": coverage.get("missing_capabilities") or [],
        "not_applicable": coverage.get("not_applicable") or [],
        "dedupe_strategy": coverage.get("dedupe_strategy"),
        "checks": coverage.get("checks") or {},
        "input_mode": package.get("input_mode"),
    }


def run_audit(commands: list[str] | None = None) -> tuple[Path, list[dict[str, Any]]]:
    selected = list(commands or AI_COMMANDS)
    run_dir = AUDIT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    rows = [audit_command(command) for command in selected]
    (run_dir / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "summary.md").write_text(_summary_markdown(rows, run_dir), encoding="utf-8")
    return run_dir, rows


def _summary_markdown(rows: list[dict[str, Any]], run_dir: Path) -> str:
    lines = [
        "# AI Workflow Coverage Audit",
        "",
        f"Run directory: `{run_dir}`",
        "",
        "| 指令 | 覆蓋度 | 待補 | 不適用 | 去重策略 |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        missing = "、".join(str(item) for item in row.get("missing_capabilities") or []) or "-"
        not_applicable = "、".join(str(item) for item in row.get("not_applicable") or []) or "-"
        lines.append(
            f"| `{row.get('command')}` | {row.get('status')} | {missing} | "
            f"{not_applicable} | {row.get('dedupe_strategy') or '-'} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline AI workflow coverage audit. Does not call external APIs.")
    parser.add_argument("--command", action="append", choices=AI_COMMANDS)
    args = parser.parse_args()
    run_dir, rows = run_audit(args.command)
    partial = [row for row in rows if row.get("status") != "aligned"]
    print(f"Coverage audit completed: {run_dir}")
    print(f"Summary: {run_dir / 'summary.md'}")
    if partial:
        print("Partial commands: " + ", ".join(str(row.get("command")) for row in partial))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
