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


if __name__ == "__main__":
    unittest.main()
