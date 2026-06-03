import unittest


class TopicPromptContracts(unittest.TestCase):
    def test_topic_maintain_prompt_requires_structured_company_and_node_fields(self):
        from research_center.topic_maintain_service import _load_prompt

        prompt_text = _load_prompt("topic_maintain")
        self.assertIn("嚴格結構化欄位規則", prompt_text)
        self.assertIn("`affected_companies` 必須是 object list", prompt_text)
        self.assertIn("每個 `supply_chain_nodes[]` 項目必須包含", prompt_text)
        for field in (
            "company_code",
            "company_name",
            "role",
            "theme_id",
            "confidence",
            "source_level",
            "evidence",
            "risk_notes",
            "missing_data",
        ):
            self.assertIn(field, prompt_text)

    def test_topic_maintain_prompt_keeps_imagination_with_verification_boundaries(self):
        from research_center.topic_maintain_service import _load_prompt

        prompt_text = _load_prompt("topic_maintain")
        self.assertIn("市場想像與候選題材推演", prompt_text)
        self.assertIn("verified", prompt_text)
        self.assertIn("candidate", prompt_text)
        self.assertIn("市場可能買單故事", prompt_text)
        self.assertIn("失敗條件", prompt_text)


if __name__ == "__main__":
    unittest.main()
