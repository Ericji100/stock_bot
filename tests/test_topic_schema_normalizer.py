import unittest

from research_center.topic_models import TopicActionType
from research_center.topic_schema_normalizer import (
    normalize_topic_candidate,
    normalize_topic_detail_action,
    normalize_topic_detail_actions,
    slugify_theme_id,
)


class TopicSchemaNormalizerTests(unittest.TestCase):
    def test_slugify_theme_id(self):
        self.assertEqual(slugify_theme_id("AI Server Cooling"), "ai_server_cooling")

    def test_normalize_topic_candidate(self):
        result = normalize_topic_candidate({
            "theme_name": "AI伺服器散熱",
            "keywords": ["散熱"],
            "candidate_companies": ["3324"],
        })
        self.assertEqual(result["theme_id"], "ai")
        self.assertEqual(result["theme_name"], "AI伺服器散熱")
        self.assertEqual(result["candidate_companies"][0]["company_code"], "3324")

    def test_normalize_topic_detail_action_fills_missing_fields(self):
        action = normalize_topic_detail_action({
            "theme_id": "ai_server_cooling",
            "theme_name": "AI伺服器散熱",
            "evidence": [{"source": "新聞", "source_level": "L2_media", "content": "散熱需求提升"}],
        })
        self.assertEqual(action.action_type, TopicActionType.CREATE_THEME)
        self.assertEqual(action.theme_id, "ai_server_cooling")
        self.assertTrue(action.risk_notes)
        self.assertTrue(action.missing_data)
        self.assertTrue(action.supply_chain_nodes)

    def test_normalize_topic_detail_actions_accepts_actions_wrapper(self):
        actions = normalize_topic_detail_actions({
            "actions": [{
                "action_type": "update_theme",
                "theme_id": "networking",
                "theme_name": "高速網通",
            }]
        })
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action_type, TopicActionType.UPDATE_THEME)


if __name__ == "__main__":
    unittest.main()
