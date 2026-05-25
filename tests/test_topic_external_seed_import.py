from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from research_center.command_parser import parse_command_text
from research_center.topic_import_service import import_topic_change_pack
from research_center.topic_models import TopicChangeStatus
from research_center.topic_models import TopicChangeMode
from research_center.topic_models import TopicActionType
from research_center.topic_seed_service import build_topic_seed_prompt
from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache


def _sample_topic(index: int) -> dict:
    return {
        "action_type": "create_theme",
        "theme_id": f"sample_topic_{index:02d}",
        "theme_name": f"Sample Topic {index:02d}",
        "keywords": ["AI", "server"],
        "industries": ["electronics"],
        "supply_chain_role": "sample supply chain",
        "confidence": "medium",
        "reason": "sample reason",
        "affected_companies": [
            {
                "company_code": f"23{index:02d}",
                "company_name": "Sample Co",
                "role": "sample role",
                "evidence": [{"source": "Sample", "content": "sample"}],
            }
        ],
        "risk_notes": ["sample risk"],
        "missing_data": ["sample missing"],
        "supply_chain_nodes": [
            {
                "company_code": f"23{index:02d}",
                "company_name": "Sample Co",
                "role": "sample role",
            }
        ],
        "evidence": [
            {
                "source": "Sample Source",
                "source_level": "L2_media",
                "content": "sample evidence",
            }
        ],
    }


class TopicExternalSeedImportTests(unittest.TestCase):
    def tearDown(self):
        safe_remove_test_cache("topic_external_seed_import")

    def test_seed_prompt_is_copyable_json_only_instruction(self):
        prompt = build_topic_seed_prompt()
        self.assertIn("角色設定", prompt)
        self.assertIn("高階投研分析 AI", prompt)
        self.assertIn("只輸出 JSON object", prompt)
        self.assertIn("actions 至少 12 筆", prompt)
        self.assertIn("summary、confidence、actions、warnings、sources", prompt)
        self.assertIn("company_relations", prompt)
        self.assertIn("revenue_exposure", prompt)
        self.assertIn("company_knowledge_updates", prompt)
        self.assertIn("即時外部網路資料搜尋", prompt)
        self.assertIn("不要捏造百分比", prompt)
        self.assertIn("theme_id", prompt)
        self.assertIn("affected_companies", prompt)
        self.assertIn("supply_chain_nodes", prompt)

    def test_parse_topic_import_preserves_json_payload(self):
        payload = '{"summary":"ok","actions":[]}'
        req = parse_command_text(f"/topic_import --model minimax {payload}")
        self.assertEqual(req.command, "topic_import")
        self.assertEqual(req.ai_model, "minimax")
        self.assertEqual(req.target, payload)

    def test_import_actions_json_saves_pending_change_pack(self):
        payload = {
            "summary": "external import",
            "confidence": "medium",
            "actions": [_sample_topic(i) for i in range(1, 13)],
        }
        cache_dir = ensure_test_cache_dir("topic_external_seed_import/actions")
        raw_path = cache_dir / "raw.json"
        saved = []
        with patch("research_center.topic_import_service.raw_response_path", return_value=raw_path), patch(
            "research_center.topic_import_service.save_change_pack",
            side_effect=lambda pack: saved.append(pack) or pack.change_id,
        ):
            pack = import_topic_change_pack(json.dumps(payload, ensure_ascii=False), model="minimax", user_id="tester")

        self.assertEqual(pack.status, TopicChangeStatus.PENDING)
        self.assertEqual(pack.model, "minimax")
        self.assertEqual(len(pack.actions), 12)
        self.assertEqual(len(saved), 1)
        self.assertTrue(raw_path.exists())

    def test_import_topics_list_variant_converts_to_actions(self):
        payload = {"summary": "external topics", "topics": [_sample_topic(i) for i in range(1, 13)]}
        cache_dir = ensure_test_cache_dir("topic_external_seed_import/topics")
        raw_path = cache_dir / "raw.json"
        with patch("research_center.topic_import_service.raw_response_path", return_value=raw_path), patch(
            "research_center.topic_import_service.save_change_pack", return_value="change_test"
        ):
            pack = import_topic_change_pack(json.dumps(payload, ensure_ascii=False), model="external")
        self.assertEqual(len(pack.actions), 12)
        self.assertEqual(pack.actions[0].theme_id, "sample_topic_01")

    def test_import_preserves_structured_affected_companies(self):
        payload = {
            "summary": "structured companies",
            "actions": [_sample_topic(i) for i in range(1, 13)],
        }
        payload["actions"][0]["affected_companies"] = [
            {
                "company_code": "2330",
                "company_name": "TSMC",
                "role": "advanced packaging",
                "evidence": [{"source": "TSMC", "content": "CoWoS"}],
            }
        ]
        cache_dir = ensure_test_cache_dir("topic_external_seed_import/structured_companies")
        raw_path = cache_dir / "raw.json"
        with patch("research_center.topic_import_service.raw_response_path", return_value=raw_path), patch(
            "research_center.topic_import_service.save_change_pack", return_value="change_test"
        ):
            pack = import_topic_change_pack(json.dumps(payload, ensure_ascii=False), model="external")
        company = pack.actions[0].affected_companies[0]
        self.assertIsInstance(company, dict)
        self.assertEqual(company["company_code"], "2330")
        self.assertEqual(company["evidence"][0]["source"], "TSMC")

    def test_import_preserves_company_relations(self):
        payload = {
            "summary": "structured relations",
            "actions": [_sample_topic(i) for i in range(1, 13)],
        }
        payload["actions"][0]["company_relations"] = [
            {
                "company_code": "2382",
                "company_name": "Quanta",
                "role": "AI server assembly",
                "relation_strength": "high",
                "relation_type": "direct",
                "products": ["AI server"],
                "customers": ["CSP"],
                "revenue_exposure": {"level": "high", "description": "AI server exposure"},
                "benefit_logic": "AI server demand",
                "evidence": [{"source": "IR", "content": "AI server"}],
            }
        ]
        cache_dir = ensure_test_cache_dir("topic_external_seed_import/company_relations")
        raw_path = cache_dir / "raw.json"
        with patch("research_center.topic_import_service.raw_response_path", return_value=raw_path), patch(
            "research_center.topic_import_service.save_change_pack", return_value="change_test"
        ):
            pack = import_topic_change_pack(json.dumps(payload, ensure_ascii=False), model="external")
        relation = pack.actions[0].company_relations[0]
        self.assertEqual(relation["company_code"], "2382")
        self.assertEqual(relation["revenue_exposure"]["level"], "high")

    def test_import_preserves_company_knowledge_updates(self):
        payload = {
            "summary": "company knowledge",
            "actions": [_sample_topic(i) for i in range(1, 13)],
            "company_knowledge_updates": {
                "companies": {
                    "2330": {
                        "company_name": "TSMC",
                        "product_lines": ["CoWoS"],
                        "customers": ["AI chip customers"],
                    }
                }
            },
        }
        cache_dir = ensure_test_cache_dir("topic_external_seed_import/company_knowledge")
        raw_path = cache_dir / "raw.json"
        with patch("research_center.topic_import_service.raw_response_path", return_value=raw_path), patch(
            "research_center.topic_import_service.save_change_pack", return_value="change_test"
        ):
            pack = import_topic_change_pack(json.dumps(payload, ensure_ascii=False), model="external")
        self.assertEqual(pack.company_knowledge_updates["companies"]["2330"]["product_lines"], ["CoWoS"])

    def test_import_annotates_quality_summary_and_statuses(self):
        payload = {
            "summary": "quality import",
            "actions": [_sample_topic(i) for i in range(1, 13)],
        }
        payload["actions"][0]["company_relations"] = [
            {
                "company_code": "2308",
                "company_name": "Delta",
                "role": "power",
                "evidence": [{"source": "Media", "source_level": "L2_media", "content": "power"}],
            },
            {
                "company_code": "9999",
                "company_name": "Candidate",
                "role": "rumor",
                "verification_status": "candidate",
            },
        ]
        cache_dir = ensure_test_cache_dir("topic_external_seed_import/quality")
        raw_path = cache_dir / "raw.json"
        with patch("research_center.topic_import_service.raw_response_path", return_value=raw_path), patch(
            "research_center.topic_import_service.save_change_pack", return_value="change_test"
        ):
            pack = import_topic_change_pack(json.dumps(payload, ensure_ascii=False), model="external")
        statuses = [item["verification_status"] for item in pack.actions[0].company_relations]
        self.assertEqual(statuses, ["inferred", "candidate"])
        self.assertIn("quality_summary", pack.extra)

    def test_import_update_actions_infers_update_mode(self):
        payload = {
            "summary": "external update",
            "mode": "initial",
            "warnings": ["初始化未產生任何 create_theme actions，無法建立題材庫。"],
            "actions": [
                {
                    "action_type": "update_theme",
                    "theme_id": "advanced_packaging",
                    "theme_name": "先進封裝",
                    "reason": "補強既有題材供應鏈資料",
                    "supply_chain_nodes": [
                        {
                            "node_id": "advanced_packaging_2330",
                            "theme_id": "advanced_packaging",
                            "company_code": "2330",
                            "company_name": "台積電",
                            "role": "CoWoS 先進封裝",
                            "verification_status": "inferred",
                            "evidence": [{"source": "TSMC", "source_level": "L1_official", "content": "CoWoS"}],
                        }
                    ],
                }
            ],
        }
        cache_dir = ensure_test_cache_dir("topic_external_seed_import/update_actions")
        raw_path = cache_dir / "raw.json"
        with patch("research_center.topic_import_service.raw_response_path", return_value=raw_path), patch(
            "research_center.topic_import_service.save_change_pack", return_value="change_test"
        ):
            pack = import_topic_change_pack(json.dumps(payload, ensure_ascii=False), model="external")
        self.assertEqual(pack.mode, TopicChangeMode.UPDATE)
        self.assertEqual(pack.status, TopicChangeStatus.PENDING)
        self.assertEqual(pack.actions[0].action_type, TopicActionType.UPDATE_THEME)
        self.assertIn("系統已自動改為 update 模式", " ".join(pack.warnings))
        self.assertNotIn("無法建立題材庫", " ".join(pack.warnings))

    def test_import_naked_supply_chain_nodes_converts_to_update_actions(self):
        payload = [
            {
                "node_id": "advanced_packaging_2330",
                "theme_id": "advanced_packaging",
                "company_code": "2330",
                "company_name": "台積電",
                "role": "CoWoS 先進封裝",
                "confidence": "high",
                "source_level": "L1_official",
                "evidence": [{"source": "TSMC", "source_level": "L1_official", "content": "CoWoS"}],
                "risk_notes": ["客戶集中"],
                "missing_data": ["營收占比"],
            },
            {
                "node_id": "advanced_packaging_3711",
                "theme_id": "advanced_packaging",
                "company_code": "3711",
                "company_name": "日月光投控",
                "role": "封測",
                "confidence": "medium",
                "source_level": "L2_media",
                "evidence": [{"source": "Media", "source_level": "L2_media", "content": "封測"}],
            },
        ]
        cache_dir = ensure_test_cache_dir("topic_external_seed_import/supply_nodes")
        raw_path = cache_dir / "raw.json"
        with patch("research_center.topic_import_service.raw_response_path", return_value=raw_path), patch(
            "research_center.topic_import_service.save_change_pack", return_value="change_test"
        ):
            pack = import_topic_change_pack(json.dumps(payload, ensure_ascii=False), model="external")
        self.assertEqual(pack.mode, TopicChangeMode.UPDATE)
        self.assertEqual(pack.status, TopicChangeStatus.PENDING)
        self.assertEqual(len(pack.actions), 1)
        self.assertEqual(pack.actions[0].action_type, TopicActionType.UPDATE_THEME)
        self.assertEqual(pack.actions[0].theme_id, "advanced_packaging")
        self.assertEqual(len(pack.actions[0].supply_chain_nodes), 2)
        self.assertEqual(len(pack.actions[0].affected_companies), 2)


if __name__ == "__main__":
    unittest.main()
