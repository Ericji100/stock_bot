from __future__ import annotations

import unittest

from research_center.command_parser import parse_command_text
from research_center.evidence_pack_service import build_unified_evidence_pack


class EvidencePackServiceTests(unittest.TestCase):
    def test_unified_evidence_pack_collects_shared_layers(self):
        request = parse_command_text("/research 2330 --deep")
        pack = build_unified_evidence_pack(request, {
            "feature_pack": {"schema_version": "feature_pack_v2", "scope": "single_stock"},
            "data_gap_summary": {"schema_version": "data_gap_v1", "missing_fields": ["financial_data"]},
            "local_scoring": {"scores": [{"score_name": "test"}]},
            "news_events": [{"event_type": "news_financial", "title": "news"}],
        })

        self.assertEqual(pack["schema_version"], "evidence_pack_v1")
        self.assertGreaterEqual(pack["item_count"], 4)
        self.assertIn("data_gap_summary", {item["type"] for item in pack["items"]})

    def test_value_scan_pack_includes_ai_candidate_evidence_pack(self):
        request = parse_command_text("/value_scan 精選選股 --deep")
        pack = build_unified_evidence_pack(request, {
            "ai_candidate_evidence_pack": [{"code": "2330", "financial_detail": {"status": "covered"}}],
        })

        self.assertIn("ai_candidate_evidence_pack", {item["type"] for item in pack["items"]})


if __name__ == "__main__":
    unittest.main()
