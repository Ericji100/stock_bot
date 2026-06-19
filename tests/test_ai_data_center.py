from __future__ import annotations

import copy
import unittest

from research_center.ai_data_center import build_ai_data_center_bundle
from research_center.ai_context_policy import select_sources_for_ai_input, source_coverage_counts
from research_center.report_builder import build_report_json, render_html
from research_center.models import CommandRequest, SourceItem


class AiDataCenterTests(unittest.TestCase):
    def test_source_selection_keeps_official_and_risk_sources(self):
        request = CommandRequest(command="research", raw_text="/research 2330", target="2330", mode="deep")
        sources = [
            SourceItem("S001", "官方重大訊息", "https://mops.twse.com.tw/a", "Level 1", "2026-05-01"),
            SourceItem("S002", "毛利下滑風險", "https://news.example.com/risk", "Level 3", "2026-05-02", "庫存與毛利下滑"),
            SourceItem("S003", "論壇討論", "https://ptt.cc/a", "Level 4"),
        ]

        audit = select_sources_for_ai_input(request, sources, max_sources=2)

        selected_titles = [row["source"]["title"] for row in audit["selected_sources"]]
        self.assertIn("官方重大訊息", selected_titles)
        self.assertIn("毛利下滑風險", selected_titles)
        self.assertEqual(audit["omitted_source_count"], 1)

    def test_source_coverage_counts_date_status(self):
        sources = [
            SourceItem("S001", "明確日期", "https://example.com/a", "Level 2", "2026-05-24", found_by=["source_date:explicit"]),
            SourceItem("S002", "推測日期", "https://example.com/b", "Level 2", "2026-05-25", found_by=["source_date:inferred"]),
            SourceItem("S003", "無日期", "https://example.com/c", "Level 2"),
        ]

        coverage = source_coverage_counts(sources)

        self.assertEqual(coverage["dated_sources"], 2)
        self.assertEqual(coverage["explicit_dated_sources"], 1)
        self.assertEqual(coverage["inferred_dated_sources"], 1)
        self.assertEqual(coverage["undated_sources"], 1)

    def test_ai_data_center_bundle_contains_audit_confidence_and_three_layers(self):
        request = CommandRequest(command="research", raw_text="/research 2330 --deep", target="2330", mode="deep")
        structured = {
            "stock": {"code": "2330", "name": "台積電"},
            "price_data": [{"Close": 1000}],
            "institutional_data": [{"date": "2026-05-01"}],
            "margin_data": [{"date": "2026-05-01"}],
            "revenue_data": [{"month": "2026-04"}],
            "financial_data": [{"Quarter": "2026Q1"}],
            "local_scoring": {"scores": []},
            "unified_evidence_pack": {"items": [{"type": "financial"}]},
        }
        sources = [
            SourceItem("S001", "公開資訊觀測站重大訊息", "https://mops.twse.com.tw/a", "Level 1", "2026-05-01"),
            SourceItem("S002", "產業新聞", "https://ctee.com.tw/a", "Level 2", "2026-05-02"),
            SourceItem("S003", "庫存風險", "https://news.example.com/risk", "Level 3", "2026-05-03", "庫存風險"),
        ]

        bundle = build_ai_data_center_bundle(request, structured, sources)

        self.assertEqual(bundle["schema_version"], "ai_data_center_v1")
        self.assertIn("ai_prompt_context", bundle)
        self.assertIn("ai_input_audit", bundle)
        self.assertIn("report_confidence", bundle)
        self.assertIn("three_layer_context", bundle)
        self.assertEqual(bundle["three_layer_context"]["source_sufficiency"]["source_count"], 3)
        self.assertGreaterEqual(bundle["report_confidence"]["confidence_score"], 40)
        self.assertEqual(len(bundle["three_layer_context"]["raw_sources"]), 3)

    def test_ai_input_audit_does_not_mutate_structured_data(self):
        request = CommandRequest(command="research", raw_text="/research 2330 --deep", target="2330", mode="deep")
        structured = {
            "stock": {"code": "2330", "name": "TSMC"},
            "price_data": [{"Close": 1000}],
            "financial_data": [{"EPS": 10}],
        }
        original = copy.deepcopy(structured)

        bundle = build_ai_data_center_bundle(
            request,
            structured,
            [SourceItem("S001", "TWSE official", "https://www.twse.com.tw/a", "Level 1", "2026-05-01")],
        )

        self.assertEqual(structured, original)
        self.assertIn("ai_input_audit", bundle)
        self.assertGreater(bundle["ai_input_audit"]["context_size"]["prompt_context_chars"], 0)

    def test_ai_prompt_context_cleans_source_text_before_ai_input(self):
        request = CommandRequest(command="macro", raw_text="/macro 台股", market_scope="台股")
        mojibake_title = "é¦–é  - TWSE è‡ºç£è­‰åˆ¸äº¤æ˜“æ‰€"
        mojibake_snippet = "é¦–é  - TWSE è‡ºç£è­‰åˆ¸äº¤æ˜“æ‰€"
        bundle = build_ai_data_center_bundle(
            request,
            {"market_scope": "台股"},
            [SourceItem("S001", mojibake_title, "https://www.twse.com.tw/", "Level 1", snippet=mojibake_snippet)],
        )

        prompt_sources = bundle["ai_prompt_context"]["入模來源"]

        self.assertEqual(prompt_sources[0]["來源編號"], "S001")
        self.assertIn("TWSE", prompt_sources[0]["標題"])
        self.assertIn("臺灣證券交易所", prompt_sources[0]["標題"])
        self.assertIn("臺灣證券交易所", prompt_sources[0]["摘要"])
        self.assertNotIn("é¦", prompt_sources[0]["標題"])

    def test_report_json_and_html_include_ai_input_audit_tab(self):
        request = CommandRequest(command="research", raw_text="/research 2330 --deep", target="2330", mode="deep")
        structured = {
            "stock": {"code": "2330", "name": "TSMC"},
            "price_data": [{"Close": 1000}],
            "financial_data": [{"EPS": 10}],
            "revenue_data": [{"YoY": 20}],
            "local_scoring": {"scores": []},
        }
        sources = [
            SourceItem("S001", "TWSE official", "https://www.twse.com.tw/a", "Level 1", "2026-05-01"),
            SourceItem("S002", "Risk news", "https://example.com/risk", "Level 3", "2026-05-02", "risk"),
        ]
        bundle = build_ai_data_center_bundle(request, structured, sources)
        structured["ai_data_center"] = bundle
        structured["ai_input_audit"] = bundle["ai_input_audit"]
        structured["report_confidence"] = bundle["report_confidence"]
        structured["ai_prompt_context"] = bundle["ai_prompt_context"]

        report_json = build_report_json(
            request,
            "# Research\n\n## Sources\n- [S001] TWSE official",
            "summary",
            sources,
            True,
            None,
            structured,
        )
        rendered = render_html(report_json, "# Research\n\n## Sources\n- [S001] TWSE official")

        self.assertIn("ai_data_center", report_json["metadata"])
        self.assertIn("ai_input_audit", report_json["metadata"])
        self.assertIn("report_confidence", report_json["metadata"])
        self.assertIn('for="tab-ai-audit"', rendered)
        self.assertIn("AI 入模審計", rendered)


if __name__ == "__main__":
    unittest.main()
