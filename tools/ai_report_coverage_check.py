from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORTS_ROOT = ROOT / "reports"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_center.ai_workflow_service import AI_WORKFLOW_STANDARD_CAPABILITIES


NARRATIVE_REQUIREMENTS = {
    "market_story": ("市場正在交易什麼故事", "市場推演摘要"),
    "early_clues": ("早期蛛絲馬跡",),
    "catalysts": ("下一波可能發酵的催化劑", "催化劑"),
    "missing_breakout_signal": ("如果要大漲，還缺什麼訊號",),
    "failure_conditions": ("反向驗證與失敗條件", "失敗條件"),
    "imagination_conclusion": ("想像力結論", "market_hypothesis"),
}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _coverage_from_report(report: dict[str, Any]) -> dict[str, Any]:
    metadata = report.get("metadata") if isinstance(report.get("metadata"), dict) else {}
    coverage = metadata.get("ai_workflow_coverage")
    if isinstance(coverage, dict):
        return coverage
    high_package = metadata.get("high_model_input_package")
    high_package = high_package if isinstance(high_package, dict) else {}
    coverage = high_package.get("ai_workflow_coverage")
    return coverage if isinstance(coverage, dict) else {}


def _looks_like_report_json(report: dict[str, Any]) -> bool:
    metadata = report.get("metadata")
    if isinstance(metadata, dict):
        if metadata.get("report_id") or metadata.get("report_type") or metadata.get("target"):
            return True
    return any(key in report for key in ("report_id", "report_type", "markdown", "summary", "scores"))


def _report_markdown(report: dict[str, Any], path: Path | None = None) -> str:
    metadata = report.get("metadata") if isinstance(report.get("metadata"), dict) else {}
    for value in (report.get("markdown"), report.get("content"), metadata.get("markdown")):
        if isinstance(value, str) and value.strip():
            return value
    if path is not None:
        for candidate in (path.with_suffix(".md"), Path(str(path).replace(".json", ".md"))):
            if candidate.exists():
                try:
                    text = candidate.read_text(encoding="utf-8-sig")
                except Exception:
                    continue
                if text.strip():
                    return text
    if isinstance(report.get("summary"), str) and report["summary"].strip():
        return report["summary"]
    return ""


def _narrative_quality_from_report(report: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    markdown = _report_markdown(report, path)
    if not markdown:
        return {
            "schema_version": "report_narrative_quality_v1",
            "status": "not_available",
            "missing_sections": list(NARRATIVE_REQUIREMENTS),
            "checked_sections": list(NARRATIVE_REQUIREMENTS),
        }
    missing = [
        key
        for key, terms in NARRATIVE_REQUIREMENTS.items()
        if not any(term in markdown for term in terms)
    ]
    return {
        "schema_version": "report_narrative_quality_v1",
        "status": "aligned" if not missing else "missing",
        "missing_sections": missing,
        "checked_sections": list(NARRATIVE_REQUIREMENTS),
    }


def scan_reports(
    root: Path = REPORTS_ROOT,
    *,
    limit: int | None = None,
    include_missing: bool = True,
) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    paths = sorted(root.glob("**/*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    rows: list[dict[str, Any]] = []
    for path in paths:
        report = _read_json(path)
        if not report:
            continue
        if not _looks_like_report_json(report):
            continue
        coverage = _coverage_from_report(report)
        if not coverage and not include_missing:
            continue
        metadata = report.get("metadata") if isinstance(report.get("metadata"), dict) else {}
        if coverage:
            coverage_status = coverage.get("status")
            missing_capabilities = coverage.get("missing_capabilities") or []
            not_applicable = coverage.get("not_applicable") or []
            dedupe_strategy = coverage.get("dedupe_strategy")
            schema_version = coverage.get("schema_version")
        else:
            coverage_status = "missing"
            missing_capabilities = list(AI_WORKFLOW_STANDARD_CAPABILITIES)
            not_applicable = []
            dedupe_strategy = None
            schema_version = None
        narrative_quality = _narrative_quality_from_report(report, path)
        rows.append(
            {
                "path": str(path),
                "report_id": metadata.get("report_id") or report.get("report_id") or path.stem,
                "report_type": metadata.get("report_type") or report.get("report_type") or path.parent.name,
                "target": metadata.get("target") or report.get("target"),
                "coverage_status": coverage_status,
                "missing_capabilities": missing_capabilities,
                "not_applicable": not_applicable,
                "dedupe_strategy": dedupe_strategy,
                "schema_version": schema_version,
                "narrative_quality_status": narrative_quality["status"],
                "narrative_missing_sections": narrative_quality["missing_sections"],
            }
        )
        if limit and len(rows) >= limit:
            break
    return rows


def write_summary(rows: list[dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# AI Report Coverage Check",
        "",
        "| 報告檔 | 類型 | 目標 | 覆蓋狀態 | 缺少能力 | 不適用項目 | 去重策略 | 推演骨架 | 缺少推演章節 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        missing = ", ".join(str(item) for item in row.get("missing_capabilities") or []) or "-"
        not_applicable = ", ".join(str(item) for item in row.get("not_applicable") or []) or "-"
        narrative_missing = ", ".join(str(item) for item in row.get("narrative_missing_sections") or []) or "-"
        lines.append(
            f"| `{Path(str(row.get('path'))).name}` | {row.get('report_type') or '-'} | "
            f"{row.get('target') or '-'} | {row.get('coverage_status') or '-'} | "
            f"{missing} | {not_applicable} | {row.get('dedupe_strategy') or '-'} | "
            f"{row.get('narrative_quality_status') or '-'} | {narrative_missing} |"
        )
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Check ai_workflow_coverage in generated report JSON files.")
    parser.add_argument("--root", default=str(REPORTS_ROOT))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--out", default=str(ROOT / "logs" / "ai_report_coverage_check" / "summary.md"))
    parser.add_argument("--fail-on-partial", action="store_true")
    parser.add_argument("--coverage-only", action="store_true", help="Only list reports that already have coverage metadata.")
    args = parser.parse_args()

    rows = scan_reports(Path(args.root), limit=args.limit, include_missing=not args.coverage_only)
    write_summary(rows, Path(args.out))
    partial = [row for row in rows if row.get("coverage_status") != "aligned"]
    print(f"Checked reports: {len(rows)}")
    print(f"Summary: {args.out}")
    if partial:
        print("Partial or missing coverage reports:")
        for row in partial:
            print(f"- {row.get('report_id')}: {row.get('coverage_status')} {row.get('missing_capabilities')}")
        if args.fail_on_partial:
            raise SystemExit(1)


if __name__ == "__main__":
    sys.exit(main())
