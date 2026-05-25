import unittest

from research_center.models import CommandRequest, SourceItem
from research_center.search_source_normalizer import normalize_source_items


class SearchSourceNormalizerTests(unittest.TestCase):
    def test_adds_provider_found_by_and_command_section(self):
        request = CommandRequest(command="research", raw_text="/research 2330")
        source = SourceItem(source_id="", title="Title", url="https://example.com", source_level="Level 3")

        normalized = normalize_source_items([source], request, provider="tavily_search", query_intent="discovery")

        self.assertEqual(normalized[0].source_id, "S001")
        self.assertEqual(normalized[0].provider, "tavily_search")
        self.assertIn("tavily_search", normalized[0].found_by)
        self.assertIn("discovery", normalized[0].found_by)
        self.assertIn("research", normalized[0].used_in_section)

    def test_preserves_fetch_and_date_fields(self):
        request = CommandRequest(command="value_scan", raw_text="/value_scan 精選選股")
        source = SourceItem(
            source_id="S009",
            title="Title",
            url="https://example.com",
            source_level="Level 2",
            published_date="2026-05-24",
            snippet="snippet",
            fetch_provider="requests_bs4",
            fetch_status="success",
            failure_reason=None,
        )

        normalized = normalize_source_items([source], request, provider="gemini_search")

        self.assertEqual(normalized[0].source_id, "S009")
        self.assertEqual(normalized[0].published_date, "2026-05-24")
        self.assertEqual(normalized[0].snippet, "snippet")
        self.assertEqual(normalized[0].fetch_provider, "requests_bs4")
        self.assertEqual(normalized[0].fetch_status, "success")

    def test_does_not_duplicate_metadata(self):
        request = CommandRequest(command="theme", raw_text="/theme AI")
        source = SourceItem(
            source_id="S001",
            title="Title",
            url="https://example.com",
            source_level="Level 3",
            used_in_section=["theme"],
            provider="gemini_search",
            found_by=["gemini_search", "discovery"],
        )

        normalized = normalize_source_items([source], request, provider="gemini_search", query_intent="discovery")

        self.assertEqual(normalized[0].found_by.count("gemini_search"), 1)
        self.assertEqual(normalized[0].found_by.count("discovery"), 1)
        self.assertEqual(normalized[0].used_in_section.count("theme"), 1)


if __name__ == "__main__":
    unittest.main()
