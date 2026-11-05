import unittest

from research_center.topic_models import TopicActionType
from research_center.topic_schema_normalizer import (
    normalize_topic_candidate,
    normalize_topic_detail_action,
    normalize_topic_detail_actions,
    slugify_theme_id,
)


class TopicSchemaNormalizerTests(unittest.TestCase):
    def test_summary_alias_retains_evidence_without_inventing_a_quote(self):
        action = normalize_topic_detail_action({
            'theme_id': 'grid', 'theme_name': 'Grid',
            'evidence': [{'source_id': 'S148', 'url': 'https://example.com/grid',
                          'summary': 'Reported orders, not recognized revenue',
                          'published_date': '2026-10-14', 'source_level': 'L2_media'}],
        })
        self.assertEqual(len(action.evidence), 1)
        evidence = action.evidence[0]
        self.assertEqual(evidence.content, 'Reported orders, not recognized revenue')
        self.assertEqual(evidence.publish_date, '2026-10-14')
        self.assertEqual(evidence.source_id, 'S148')
        self.assertIsNone(evidence.supporting_quote)
        self.assertEqual(evidence.source_level.value, 'L2_media')

    def test_source_reference_only_is_retained_without_fabricated_content(self):
        action = normalize_topic_detail_action({
            'theme_id': 'grid', 'theme_name': 'Grid',
            'evidence': [{'source_id': 'S148', 'url': 'https://example.com/grid'}, {}],
        })
        self.assertEqual(len(action.evidence), 1)
        self.assertEqual(action.evidence[0].content, '')
        self.assertIsNone(action.evidence[0].supporting_quote)

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

    def test_evidence_supports_alias_preserves_real_source_identity(self):
        action = normalize_topic_detail_action({'theme_id':'ai_materials','theme_name':'AI材料',
            'evidence':[{'source_id':'S093','url':'https://example.com/article',
                         'supports':'媒體點名，仍待公司確認','supporting_quote':'來源原句不可遺失'}]})
        self.assertEqual(len(action.evidence), 1)
        self.assertEqual(action.evidence[0].source_id, 'S093')
        self.assertEqual(action.evidence[0].content, '媒體點名，仍待公司確認')
        self.assertEqual(action.evidence[0].supporting_quote, '來源原句不可遺失')
        from research_center.topic_models import TopicEvidence
        self.assertEqual(TopicEvidence.from_dict(action.evidence[0].to_dict()), action.evidence[0])

    def test_same_code_role_is_not_lost_between_candidate_and_relation(self):
        action = normalize_topic_detail_action({'theme_id':'ai_materials','theme_name':'AI材料',
            'affected_companies':[{'company_code':'2330','company_name':'台積電','role':'模型原有角色，待核'}],
            'company_relations':[{'company_code':'2330','company_name':'台積電'},
                                 {'company_code':'2241','company_name':'艾姆勒'}]})
        self.assertEqual(action.company_relations[0]['role'],'模型原有角色，待核')
        self.assertEqual(action.company_relations[0]['verification_status'],'candidate')
        self.assertEqual(action.company_relations[1]['role'],'待補題材角色')


if __name__ == "__main__":
    unittest.main()
