from __future__ import annotations

import unittest

from research_center.command_parser import parse_command_text
from research_center.data_gap_service import build_data_gap_summary


class DataGapServiceTests(unittest.TestCase):
    def test_research_gap_summary_prioritizes_missing_financial_fields(self):
        request = parse_command_text("/research 2330 --deep")
        summary = build_data_gap_summary(request, {"stock": {"code": "2330"}})

        self.assertEqual(summary["schema_version"], "data_gap_v1")
        self.assertIn("financial_data", summary["missing_fields"])
        self.assertTrue(summary["backfill_recommended"])
        self.assertEqual(summary["priority_gaps"][0]["priority"], "high")

    def test_value_scan_gap_summary_detects_missing_ai_pack(self):
        request = parse_command_text("/value_scan 精選選股 --deep")
        summary = build_data_gap_summary(request, {"candidate_pool": "精選選股"})

        self.assertIn("ai_candidate_evidence_pack", summary["missing_fields"])
        self.assertTrue(any(row["field"] == "ai_candidate_evidence_pack" for row in summary["priority_gaps"]))


if __name__ == "__main__":
    unittest.main()
