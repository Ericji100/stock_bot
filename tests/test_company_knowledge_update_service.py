from __future__ import annotations

import json
import unittest

from research_center.command_parser import parse_command_text
from research_center.company_knowledge_update_service import (
    attach_company_knowledge_autofill,
    source_quality_score,
    update_missing_company_knowledge,
)
from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache


class CompanyKnowledgeUpdateServiceTests(unittest.TestCase):
    def setUp(self):
        self.cache_name = "company_knowledge_update_service"
        self.tmp = ensure_test_cache_dir(self.cache_name)
        self.path = self.tmp / "company_knowledge.json"

    def tearDown(self):
        safe_remove_test_cache(self.cache_name)

    def _write_knowledge(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _read_knowledge(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_missing_company_knowledge_is_filled_from_high_quality_source(self):
        self._write_knowledge({"metadata": {}, "companies": {}})
        rows = [{
            "code": "2330",
            "name": "台積電",
            "industry": "半導體",
            "new_market_label": "AI 伺服器 CoWoS",
            "source_events": [{
                "title": "台積電法說會說明 AI 與 CoWoS 需求",
                "source_url": "https://mops.twse.com.tw/mops/web/t05st02",
                "source_level": "Level 1",
            }],
        }]

        result = update_missing_company_knowledge(rows, knowledge_path=self.path)
        data = self._read_knowledge()

        self.assertEqual(result["updated_count"], 1)
        self.assertIn("2330", data["companies"])
        self.assertIn("CoWoS", data["companies"]["2330"]["product_lines"])
        self.assertTrue(data["companies"]["2330"]["evidence_sources"])

    def test_low_quality_forum_source_does_not_write_company_knowledge(self):
        self._write_knowledge({"metadata": {}, "companies": {}})
        rows = [{
            "code": "1234",
            "name": "測試公司",
            "industry": "半導體",
            "source_events": [{
                "title": "網友討論測試公司 AI 題材",
                "source_url": "https://www.ptt.cc/bbs/Stock/M.1.html",
                "source_level": "Level 4",
            }],
        }]

        result = update_missing_company_knowledge(rows, knowledge_path=self.path)
        data = self._read_knowledge()

        self.assertEqual(result["updated_count"], 0)
        self.assertNotIn("1234", data["companies"])
        self.assertGreater(result["low_quality_rejected_count"], 0)

    def test_existing_core_knowledge_is_not_overwritten(self):
        self._write_knowledge({
            "metadata": {},
            "companies": {
                "2330": {
                    "company_name": "原名稱",
                    "product_lines": ["既有產品"],
                    "supply_chain_roles": ["existing_role"],
                    "evidence_sources": [{"title": "old"}],
                }
            },
        })
        rows = [{
            "code": "2330",
            "name": "台積電",
            "industry": "半導體",
            "new_market_label": "AI 伺服器",
            "source_events": [{
                "title": "台積電法說會 AI 伺服器",
                "source_url": "https://mops.twse.com.tw/mops/web/t05st02",
                "source_level": "Level 1",
            }],
        }]

        result = update_missing_company_knowledge(rows, knowledge_path=self.path)
        data = self._read_knowledge()

        self.assertEqual(result["updated_count"], 0)
        self.assertEqual(data["companies"]["2330"]["product_lines"], ["既有產品"])

    def test_attach_refreshes_value_scan_evidence_pack(self):
        self._write_knowledge({"metadata": {}, "companies": {}})
        request = parse_command_text("/value_scan 精選選股 --deep")
        structured_data = {
            "ai_candidates": [{
                "code": "2330",
                "name": "台積電",
                "industry": "半導體",
                "new_market_label": "AI 伺服器 CoWoS",
                "source_events": [{
                    "title": "台積電法說會 AI 伺服器 CoWoS",
                    "source_url": "https://mops.twse.com.tw/mops/web/t05st02",
                    "source_level": "Level 1",
                }],
            }],
            "ai_candidate_evidence_pack": [{
                "code": "2330",
                "name": "台積電",
                "company_knowledge": {"status": "missing"},
                "missing_data_status": ["company_knowledge"],
            }],
        }

        attach_company_knowledge_autofill(request, structured_data, knowledge_path=self.path)

        pack = structured_data["ai_candidate_evidence_pack"][0]
        self.assertEqual(pack["company_knowledge"]["status"], "covered")
        self.assertIsNone(pack["missing_data_status"])

    def test_source_quality_rejects_social_urls(self):
        quality = source_quality_score({
            "title": "社群貼文",
            "url": "https://www.dcard.tw/f/stock/p/1",
            "source_level": "Level 2",
        })
        self.assertFalse(quality["usable_for_company_knowledge"])


if __name__ == "__main__":
    unittest.main()
