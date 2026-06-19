from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
from tools.ai_report_coverage_check import scan_reports, write_summary


class AiReportCoverageCheckTests(unittest.TestCase):
    def test_scan_reports_reads_metadata_coverage(self) -> None:
        tmp = ensure_test_cache_dir("ai_report_coverage_check/metadata")
        try:
            report_path = tmp / "research" / "2330" / "report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "metadata": {
                            "report_id": "research_2330",
                            "report_type": "research",
                            "target": "2330",
                            "ai_workflow_coverage": {
                                "schema_version": "ai_workflow_coverage_v1",
                                "status": "aligned",
                                "missing_capabilities": [],
                                "not_applicable": [],
                                "dedupe_strategy": "stock_index",
                            },
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            rows = scan_reports(tmp)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["report_id"], "research_2330")
            self.assertEqual(rows[0]["coverage_status"], "aligned")
            self.assertEqual(rows[0]["dedupe_strategy"], "stock_index")
            self.assertEqual(rows[0]["narrative_quality_status"], "not_available")
        finally:
            safe_remove_test_cache("ai_report_coverage_check/metadata")

    def test_scan_reports_reads_high_model_package_fallback(self) -> None:
        tmp = ensure_test_cache_dir("ai_report_coverage_check/high_package")
        try:
            report_path = tmp / "theme" / "report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "metadata": {
                            "report_type": "theme",
                            "target": "AI電源",
                            "high_model_input_package": {
                                "ai_workflow_coverage": {
                                    "schema_version": "ai_workflow_coverage_v1",
                                    "status": "partial",
                                    "missing_capabilities": ["low_model_digest"],
                                    "not_applicable": ["html_sections"],
                                    "dedupe_strategy": "source_index",
                                }
                            },
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            rows = scan_reports(tmp)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["coverage_status"], "partial")
            self.assertEqual(rows[0]["missing_capabilities"], ["low_model_digest"])
            self.assertEqual(rows[0]["not_applicable"], ["html_sections"])
        finally:
            safe_remove_test_cache("ai_report_coverage_check/high_package")

    def test_write_summary_outputs_human_readable_table(self) -> None:
        tmp = ensure_test_cache_dir("ai_report_coverage_check/summary")
        try:
            out_path = tmp / "summary.md"
            write_summary(
                [
                    {
                        "path": str(tmp / "report.json"),
                        "report_type": "news",
                        "target": "market",
                        "coverage_status": "aligned",
                        "missing_capabilities": [],
                        "not_applicable": ["html_sections"],
                        "dedupe_strategy": "maintenance_pack",
                    }
                ],
                out_path,
            )

            markdown = out_path.read_text(encoding="utf-8")
            self.assertIn("AI Report Coverage Check", markdown)
            self.assertIn("覆蓋狀態", markdown)
            self.assertIn("推演骨架", markdown)
            self.assertIn("aligned", markdown)
            self.assertIn("html_sections", markdown)
        finally:
            safe_remove_test_cache("ai_report_coverage_check/summary")

    def test_scan_reports_checks_narrative_quality_sections(self) -> None:
        tmp = ensure_test_cache_dir("ai_report_coverage_check/narrative")
        try:
            report_path = tmp / "value_scan" / "report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "metadata": {
                            "report_id": "value_scan_test",
                            "report_type": "value_scan",
                            "target": "精選選股",
                            "ai_workflow_coverage": {
                                "schema_version": "ai_workflow_coverage_v1",
                                "status": "aligned",
                                "missing_capabilities": [],
                                "not_applicable": [],
                            },
                        },
                        "markdown": "\n".join(
                            [
                                "## 市場正在交易什麼故事",
                                "## 早期蛛絲馬跡",
                                "## 下一波可能發酵的催化劑",
                                "## 如果要大漲，還缺什麼訊號",
                                "## 反向驗證與失敗條件",
                                "## 想像力結論 market_hypothesis",
                            ]
                        ),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            rows = scan_reports(tmp)

            self.assertEqual(rows[0]["narrative_quality_status"], "aligned")
            self.assertEqual(rows[0]["narrative_missing_sections"], [])
        finally:
            safe_remove_test_cache("ai_report_coverage_check/narrative")

    def test_scan_reports_marks_report_without_coverage_as_missing(self) -> None:
        tmp = ensure_test_cache_dir("ai_report_coverage_check/missing")
        try:
            report_path = tmp / "macro" / "report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(
                    {
                        "metadata": {
                            "report_id": "macro_taiwan",
                            "report_type": "macro",
                            "target": "台股",
                        },
                        "markdown": "# macro",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            rows = scan_reports(tmp)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["coverage_status"], "missing")
            self.assertIn("high_model_input_package", rows[0]["missing_capabilities"])
            self.assertIsNone(rows[0]["dedupe_strategy"])
        finally:
            safe_remove_test_cache("ai_report_coverage_check/missing")

    def test_scan_reports_can_filter_to_coverage_only(self) -> None:
        tmp = ensure_test_cache_dir("ai_report_coverage_check/coverage_only")
        try:
            report_path = tmp / "macro" / "report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps({"metadata": {"report_id": "macro_taiwan"}, "markdown": "# macro"}, ensure_ascii=False),
                encoding="utf-8",
            )

            rows = scan_reports(tmp, include_missing=False)

            self.assertEqual(rows, [])
        finally:
            safe_remove_test_cache("ai_report_coverage_check/coverage_only")


if __name__ == "__main__":
    unittest.main()
