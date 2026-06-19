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

    def test_extracts_date_from_title_snippet_and_url(self):
        request = CommandRequest(command="theme", raw_text="/theme 功率半導體")
        sources = [
            SourceItem(source_id="", title="2026/05/24 功率半導體漲價", url="https://example.com/a", source_level="Level 2"),
            SourceItem(source_id="", title="Title", url="https://example.com/b", source_level="Level 2", snippet='{"datePublished":"2026-05-25"}'),
            SourceItem(source_id="", title="Title", url="https://example.com/2026/05/26/news.html", source_level="Level 2"),
        ]

        normalized = normalize_source_items(sources, request, provider="tavily_search")

        self.assertEqual([item.published_date for item in normalized], ["2026-05-24", "2026-05-25", "2026-05-26"])

    def test_extracts_roc_and_english_month_dates(self):
        request = CommandRequest(command="macro", raw_text="/macro")
        sources = [
            SourceItem(source_id="", title="112/05/24 台股新聞", url="https://example.com/a", source_level="Level 2"),
            SourceItem(source_id="", title="Published: Jun 5, 2026 market update", url="https://example.com/b", source_level="Level 2"),
        ]

        normalized = normalize_source_items(sources, request, provider="gemini_grounding")

        self.assertEqual([item.published_date for item in normalized], ["2023-05-24", "2026-06-05"])

    def test_repairs_mojibake_source_title(self):
        request = CommandRequest(command="macro", raw_text="/macro")
        source = SourceItem(
            source_id="S001",
            title="é¦–é  - TWSE è‡ºç£è­‰åˆ¸äº¤æ˜“æ‰€",
            url="https://www.twse.com.tw/",
            source_level="Level 1",
        )

        normalized = normalize_source_items([source], request)

        self.assertIn("TWSE", normalized[0].title)
        self.assertIn("證券交易所", normalized[0].title)
        self.assertNotIn("é¦", normalized[0].title)

    def test_keeps_unknown_date_when_no_explicit_date(self):
        request = CommandRequest(command="theme", raw_text="/theme 功率半導體")
        source = SourceItem(source_id="", title="功率半導體漲價", url="https://example.com/news", source_level="Level 2")

        normalized = normalize_source_items([source], request, provider="tavily_search")

        self.assertIsNone(normalized[0].published_date)


if __name__ == "__main__":
    unittest.main()
