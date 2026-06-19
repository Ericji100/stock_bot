from __future__ import annotations

import unittest

from research_center.command_parser import parse_command_text
from research_center.models import SourceItem
from research_center.report_quality_service import (
    build_data_completeness_matrix,
    build_report_evidence_pack,
    build_report_quality_layer,
)


class ReportQualityServiceTests(unittest.TestCase):
    def test_value_scan_quality_layer_has_fixed_sections(self):
        request = parse_command_text("/value_scan 精選選股 --deep")
        structured_data = {
            "candidate_pool": "精選選股",
            "ai_candidates": [{"code": "2330", "name": "台積電"}],
            "ai_candidate_evidence_pack": [{"code": "2330", "name": "台積電", "financial_detail": {"status": "covered"}}],
            "news_context": {"usable_count": 1},
            "feature_pack": {"scope": "candidate_pool"},
            "data_coverage": {"status": "partial"},
        }

        quality = build_report_quality_layer(
            request,
            structured_data,
            [SourceItem("S001", "TWSE", "https://www.twse.com.tw", "Level 1")],
        )

        self.assertEqual(quality["schema_version"], "report_quality_v1")
        self.assertIn("evidence_pack", quality)
        self.assertIn("data_completeness_matrix", quality)
        self.assertIn("source_quality", quality)
        self.assertIn("missing_data_policy", quality)
        self.assertEqual(quality["evidence_pack"]["ai_candidate_count"], 1)

    def test_source_coverage_splits_explicit_inferred_and_unknown_dates(self):
        request = parse_command_text("/theme 功率半導體")
        sources = [
            SourceItem("S001", "A", "https://example.com/a", "Level 2", published_date="2026-05-24", found_by=["source_date:explicit"]),
            SourceItem("S002", "B", "https://example.com/b", "Level 2", published_date="2026-05-25", found_by=["source_date:inferred"]),
            SourceItem("S003", "C", "https://example.com/c", "Level 2"),
        ]

        quality = build_report_quality_layer(request, {}, sources)
        summary = quality["source_coverage_summary"]

        self.assertEqual(summary["dated_sources"], 2)
        self.assertEqual(summary["explicit_dated_sources"], 1)
        self.assertEqual(summary["inferred_dated_sources"], 1)
        self.assertEqual(summary["undated_sources"], 1)

    def test_research_evidence_pack_preserves_core_inputs(self):
        request = parse_command_text("/research 2330 --deep")
        structured_data = {
            "stock": {"code": "2330", "name": "台積電"},
            "price_data": {"price": 900},
            "financial_data": [{"EPS": 10}],
            "local_rerating_snapshot": {"rerating_score": 70},
            "company_knowledge": {"product_lines": ["晶圓代工"]},
        }

        pack = build_report_evidence_pack(request, structured_data)

        self.assertEqual(pack["scope"], "single_stock")
        self.assertEqual(pack["stock"]["code"], "2330")
        self.assertIn("financial_data", pack)
        self.assertIn("local_rerating_snapshot", pack)
        self.assertIn("company_knowledge", pack)

    def test_macro_completeness_matrix_uses_macro_fields(self):
        request = parse_command_text("/macro global --deep")
        structured_data = {
            "quantitative_market": {"status": "covered"},
            "market_score": {"total": 70},
            "news_context": {"usable_count": 3},
            "feature_pack": {"scope": "macro"},
        }

        matrix = build_data_completeness_matrix(request, structured_data)
        fields = {row["field"]: row for row in matrix}

        self.assertIn("quantitative_market", fields)
        self.assertIn("market_score", fields)
        self.assertIn("fear_greed", fields)
        self.assertTrue(fields["quantitative_market"]["available"])
        self.assertFalse(fields["fear_greed"]["available"])

    def test_theme_radar_completeness_uses_market_radar_fields(self):
        request = parse_command_text("/theme_radar")
        structured_data = {
            "market_movers": {"top_gainers": [{"code": "2330"}]},
            "theme_rankings": [{"theme_id": "ai_server"}],
            "sector_strength": {"sector_rankings": [{"sector": "半導體業"}]},
            "news_context": {"usable_count": 3},
            "feature_pack": {"scope": "theme_radar"},
            "data_coverage": {"status": "complete"},
        }

        quality = build_report_quality_layer(request, structured_data, [])
        fields = {row["field"]: row for row in quality["data_completeness_matrix"]}

        self.assertEqual(quality["data_coverage_score"], 100)
        self.assertIn("market_movers", fields)
        self.assertIn("theme_rankings", fields)
        self.assertNotIn("matched_companies", fields)
        self.assertNotIn("supply_chain_profile", fields)

    def test_sector_strength_completeness_uses_sector_fields(self):
        request = parse_command_text("/sector_strength")
        structured_data = {
            "market_movers": {"top_gainers": [{"code": "2330"}]},
            "sector_rankings": [{"sector": "半導體業"}],
            "news_context": {"usable_count": 3},
            "feature_pack": {"scope": "sector_strength"},
            "data_coverage": {"status": "complete"},
        }

        quality = build_report_quality_layer(request, structured_data, [])
        fields = {row["field"]: row for row in quality["data_completeness_matrix"]}

        self.assertEqual(quality["data_coverage_score"], 100)
        self.assertIn("sector_rankings", fields)
        self.assertNotIn("topic_context", fields)
        self.assertNotIn("company_knowledge_summary", fields)

    def test_theme_flow_completeness_remains_strict_for_topic_depth(self):
        request = parse_command_text("/theme_flow AI電源")
        structured_data = {
            "theme": {"theme_id": "power_supply"},
            "layers": [{"layer": 1}],
            "layer_market_validation": [{"layer": 1, "status": "尚未從盤面驗證"}],
            "related_stocks": [{"code": "2308"}],
            "news_context": {"usable_count": 3},
            "feature_pack": {"scope": "theme_flow"},
            "data_coverage": {"status": "complete"},
        }

        quality = build_report_quality_layer(request, structured_data, [])
        fields = {row["field"]: row for row in quality["data_completeness_matrix"]}

        self.assertLess(quality["data_coverage_score"], 100)
        self.assertIn("supply_chain_profile", fields)
        self.assertFalse(fields["supply_chain_profile"]["available"])
        self.assertIn("company_knowledge_summary", fields)
        self.assertFalse(fields["company_knowledge_summary"]["available"])


if __name__ == "__main__":
    unittest.main()
