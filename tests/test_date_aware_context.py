from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

import research_center.date_aware_context as dac
from research_center.date_aware_context import (
    analysis_date_for_request,
    attach_date_aware_context,
    augment_discovery_tasks_with_date_context,
    build_saved_news_context,
    date_window_policy_for_request,
    filter_and_sort_sources_for_analysis_date,
)
from research_center.models import CommandRequest, SourceItem
from research_center.news_models import NewsItem


class FakeNewsRepository:
    items: list[NewsItem] = []

    def __init__(self, db_path=None):
        self.db_path = db_path

    def query_all_recent(self, hours: int = 168) -> list[NewsItem]:
        return list(self.items)


class DateAwareContextTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeNewsRepository.items = []
        self.repo_patch = patch.object(dac, "NewsRepository", FakeNewsRepository)
        self.repo_patch.start()

    def tearDown(self) -> None:
        self.repo_patch.stop()

    def _save_news(self, title: str, published_at: str, *, summary: str = "台股 半導體 AI 新聞") -> None:
        FakeNewsRepository.items.append(
            NewsItem(
                title=title,
                url=f"https://example.com/{title}",
                source="test",
                published_at=published_at,
                category="AI / 半導體",
                related_symbols=["2330"],
                related_topics=["半導體"],
                summary=summary,
                full_text=summary,
                importance_score=8,
                created_at="2026-05-21T10:00:00",
            )
        )

    def test_analysis_date_uses_report_date_when_present(self) -> None:
        request = CommandRequest(command="macro", raw_text="/macro --date 2026-05-20", report_date=date(2026, 5, 20))
        self.assertEqual(analysis_date_for_request(request), date(2026, 5, 20))

    def test_macro_policy_is_short_window(self) -> None:
        request = CommandRequest(command="macro", raw_text="/macro")
        policy = date_window_policy_for_request(request, {})
        self.assertEqual(policy["windows"], [7, 14, 30])
        self.assertEqual(policy["min_items"], 8)

    def test_topic_initial_policy_is_longer_but_within_one_year(self) -> None:
        request = CommandRequest(command="topic_maintain", raw_text="/topic_maintain")
        policy = date_window_policy_for_request(request, {"topic_maintain_mode_hint": "initial"})
        self.assertEqual(policy["windows"], [180, 365])
        self.assertEqual(policy["min_items"], 60)

    def test_saved_news_context_excludes_future_news(self) -> None:
        self._save_news("before", "2026-05-20T09:00:00")
        self._save_news("future", "2026-05-22T09:00:00")
        request = CommandRequest(command="macro", raw_text="/macro --date 2026-05-20", report_date=date(2026, 5, 20))
        context = build_saved_news_context(request, {}, db_path=None)
        titles = [item["title"] for item in context["items"]]
        self.assertIn("before", titles)
        self.assertNotIn("future", titles)
        self.assertGreaterEqual(context["excluded_after_analysis_date_count"], 1)

    def test_attach_date_aware_context_adds_prompt_keys(self) -> None:
        self._save_news("台積電 AI 新聞", "2026-05-20T09:00:00")
        request = CommandRequest(command="research", raw_text="/research 2330", target="2330", report_date=date(2026, 5, 20))
        data = {"stock": {"code": "2330", "name": "台積電"}}
        attach_date_aware_context(request, data, db_path=None)
        self.assertIn("date_aware_context", data)
        self.assertIn("saved_news_context", data)
        self.assertEqual(data["analysis_date"], "2026-05-20")

    def test_augment_discovery_tasks_adds_date_queries(self) -> None:
        request = CommandRequest(command="theme", raw_text="/theme AI --date 2026-05-20", theme_scope="AI", report_date=date(2026, 5, 20))
        tasks = [{"label": "x", "queries": [{"title": "base", "items": ["AI 台股"]}], "objective": "find"}]
        out = augment_discovery_tasks_with_date_context(request, {}, tasks, max_added_per_task=3)
        flat = [q for q in out[0]["queries"] if isinstance(q, str)]
        self.assertTrue(any("2026-05-20" in q for q in flat))
        self.assertTrue(any("2026年5月" in q for q in flat))

    def test_theme_radar_date_queries_do_not_use_plain_market(self) -> None:
        request = CommandRequest(command="theme_radar", raw_text="/theme_radar", target="market", report_date=date(2026, 5, 24))
        tasks = [{"label": "x", "queries": [{"title": "base", "items": ["台股 題材"]}], "objective": "find"}]
        out = augment_discovery_tasks_with_date_context(request, {}, tasks, max_added_per_task=4)
        flat = [q for q in out[0]["queries"] if isinstance(q, str)]
        joined = "\n".join(flat).lower()
        self.assertIn("台股 題材 輪動 2026-05-24", joined)
        self.assertNotIn("market 2026-05-24", joined)

    def test_saved_news_context_filters_irrelevant_market_events(self) -> None:
        FakeNewsRepository.items.append(
            NewsItem(
                title="Farmers Market event calendar",
                url="https://example.com/farmers-market",
                source="test",
                published_at="2026-05-24T09:00:00",
                category="theme_radar",
                related_topics=["market"],
                summary="Local farmers market event.",
                full_text="Local farmers market event.",
                importance_score=8,
                created_at="2026-05-24T09:00:00",
            )
        )
        self._save_news("台股 AI 供應鏈新聞", "2026-05-24T09:00:00", summary="台股 上市櫃 AI 供應鏈 族群 輪動")
        request = CommandRequest(command="theme_radar", raw_text="/theme_radar", target="market", report_date=date(2026, 5, 24))
        context = build_saved_news_context(request, {}, db_path=None)
        titles = [item["title"] for item in context["items"]]
        self.assertIn("台股 AI 供應鏈新聞", titles)
        self.assertNotIn("Farmers Market event calendar", titles)

    def test_filter_and_sort_sources_excludes_after_analysis_date(self) -> None:
        request = CommandRequest(command="research", raw_text="/research 2330 --date 2026-05-20", report_date=date(2026, 5, 20))
        sources = [
            SourceItem("S001", "old", "https://a", "Level 2", published_date="2026-05-19"),
            SourceItem("S002", "future", "https://b", "Level 2", published_date="2026-05-21"),
            SourceItem("S003", "same", "https://c", "Level 2", published_date="2026-05-20"),
        ]
        kept, dropped = filter_and_sort_sources_for_analysis_date(sources, request)
        self.assertEqual([s.title for s in kept], ["same", "old"])
        self.assertEqual(dropped, ["S002"])

    def test_filter_and_sort_sources_drops_irrelevant_market_events(self) -> None:
        request = CommandRequest(command="theme_radar", raw_text="/theme_radar", target="market", report_date=date(2026, 5, 24))
        sources = [
            SourceItem("S001", "Farmers Market event", "https://example.com/farmers-market", "Level 3", published_date="2026-05-24"),
            SourceItem("S002", "台股 AI 供應鏈", "https://example.com/tw-stock", "Level 2", published_date="2026-05-24"),
        ]
        kept, dropped = filter_and_sort_sources_for_analysis_date(sources, request)
        self.assertEqual([s.title for s in kept], ["台股 AI 供應鏈"])
        self.assertEqual(dropped, ["S001"])


if __name__ == "__main__":
    unittest.main()
