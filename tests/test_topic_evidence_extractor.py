"""Tests for topic_evidence_extractor.py - rule-based evidence candidates."""
import json
import unittest

from research_center.topic_evidence_extractor import (
    build_topic_evidence_candidates,
)


class TestTopicEvidenceExtractor(unittest.TestCase):
    def test_basic_news_source_produces_candidate(self):
        sources = [
            {
                "title": "AI伺服器需求爆發",
                "url": "https://example.com/ai-server",
                "snippet": "全球CSP擴大AI伺服器資本支出",
                "provider": "DIGITIMES",
                "published_date": "2026-05-18",
                "source_level": "L2_media",
            }
        ]
        result = build_topic_evidence_candidates(sources)
        self.assertEqual(result["mode"], "rule_based")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["title"], "AI伺服器需求爆發")
        self.assertIn("AI伺服器", result["items"][0]["matched_keywords"])

    def test_keywords_from_title_and_snippet(self):
        sources = [
            {
                "title": "液冷散熱技術突破",
                "url": "https://example.com/cooling",
                "snippet": "AI伺服器液冷散熱方案",
                "provider": "TechNews",
                "published_date": "2026-05-15",
            }
        ]
        result = build_topic_evidence_candidates(sources)
        item = result["items"][0]
        self.assertIn("散熱", item["matched_keywords"])
        self.assertIn("AI伺服器", item["matched_keywords"])

    def test_company_names_detected(self):
        sources = [
            {
                "title": "雙鴻散熱模組",
                "url": "https://example.com/test",
                "snippet": "雙鴻與奇鋐合作開發液冷散熱",
                "provider": "News",
                "published_date": "2026-05-10",
            }
        ]
        company_universe = [
            {"name": "雙鴻", "code": "3324"},
            {"name": "奇鋐", "code": "3017"},
        ]
        result = build_topic_evidence_candidates(sources, company_universe=company_universe)
        item = result["items"][0]
        self.assertIn("雙鴻", item["mentioned_companies"])
        self.assertIn("奇鋐", item["mentioned_companies"])

    def test_empty_sources_returns_empty_items(self):
        result = build_topic_evidence_candidates([])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["mode"], "rule_based")

    def test_output_is_json_serializable(self):
        sources = [
            {
                "title": "AI伺服器",
                "url": "https://example.com",
                "snippet": "test snippet",
                "provider": "Test",
                "published_date": "2026-05-18",
            }
        ]
        result = build_topic_evidence_candidates(sources)
        # Should not raise
        json.dumps(result, ensure_ascii=False)

    def test_max_items_respected(self):
        sources = [
            {
                "title": f"Title {i}",
                "url": f"https://example.com/{i}",
                "snippet": "AI伺服器",
                "provider": "Test",
                "published_date": "2026-05-18",
            }
            for i in range(120)
        ]
        result = build_topic_evidence_candidates(sources, max_items=50)
        self.assertEqual(len(result["items"]), 50)

    def test_source_without_title_or_snippet_skipped(self):
        sources = [
            {
                "url": "https://example.com",
                "provider": "Test",
                "published_date": "2026-05-18",
            }
        ]
        result = build_topic_evidence_candidates(sources)
        self.assertEqual(result["items"], [])

    def test_snippet_truncated_to_300_chars(self):
        long_snippet = "A" * 500
        sources = [
            {
                "title": "Test",
                "url": "https://example.com",
                "snippet": long_snippet,
                "provider": "Test",
                "published_date": "2026-05-18",
            }
        ]
        result = build_topic_evidence_candidates(sources)
        self.assertEqual(len(result["items"][0]["snippet"]), 300)

    def test_possible_topics_from_keywords(self):
        sources = [
            {
                "title": "AI伺服器液冷散熱",
                "url": "https://example.com",
                "snippet": "AI伺服器液冷散熱技術",
                "provider": "Test",
                "published_date": "2026-05-18",
            }
        ]
        result = build_topic_evidence_candidates(sources)
        item = result["items"][0]
        self.assertIn("AI伺服器", item["possible_topics"])

    def test_existing_topics_keywords_injected(self):
        sources = [
            {
                "title": "AI伺服器散熱需求",
                "url": "https://example.com",
                "snippet": "AI伺服器液冷散熱技術",
                "provider": "Test",
                "published_date": "2026-05-18",
            }
        ]
        existing = [
            {"theme_id": "ai_server", "theme_name": "AI伺服器", "keywords": ["AI伺服器"]}
        ]
        result = build_topic_evidence_candidates(sources, existing_topic_profiles=existing)
        item = result["items"][0]
        self.assertIn("AI伺服器", item["matched_keywords"])


if __name__ == "__main__":
    unittest.main()