from __future__ import annotations

import unittest

from research_center.command_parser import parse_command_text
from research_center.data_inventory_service import build_data_inventory
from research_center.event_store import extract_structured_events
from research_center.models import CommandRequest, SourceItem
from research_center.news_context_service import build_news_status, persist_search_sources_to_news
from research_center.news_source_filter import is_irrelevant_market_source
from research_center.stock_feature_pack_service import build_feature_pack

from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache


class SharedContextServiceTests(unittest.TestCase):
    def tearDown(self) -> None:
        safe_remove_test_cache("shared_context_services")

    def test_status_commands_parse(self):
        self.assertEqual(parse_command_text("/data_status 2330").command, "data_status")
        self.assertEqual(parse_command_text("/backfill_status").command, "backfill_status")
        self.assertEqual(parse_command_text("/news_status 2330 --days 14").lookback_days, 14)

    def test_feature_pack_for_value_scan_uses_ai_evidence_pack(self):
        request = CommandRequest(command="value_scan", raw_text="/value_scan pool", target="pool")
        data = {
            "candidate_pool": "pool",
            "ai_candidate_limit": 30,
            "ai_candidate_evidence_pack": [{"code": "2330", "financial_detail": {"gross_margin": 55}}],
            "news_context": {"status": "partial", "items": [{"title": "news"}]},
        }
        pack = build_feature_pack(request, data)
        self.assertEqual(pack["schema_version"], "feature_pack_v2")
        self.assertEqual(pack["scope"], "candidate_pool")
        self.assertEqual(pack["ai_candidate_count"], 1)
        self.assertEqual(pack["candidates"][0]["code"], "2330")

    def test_data_inventory_reports_missing_fields(self):
        request = CommandRequest(command="research", raw_text="/research 2330", target="2330")
        status = build_data_inventory(request, {"stock": {"code": "2330"}})
        self.assertEqual(status["status"], "partial")
        self.assertIn("price_data", status["missing_fields"])

    def test_search_sources_are_saved_to_news_repository(self):
        repo = _FakeNewsRepository()
        request = CommandRequest(command="research", raw_text="/research 2330", target="2330")
        source = SourceItem(
            source_id="S001",
            title="TSMC news",
            url="https://example.com/tsmc-news",
            source_level="Level 3",
            published_date="2026-05-20",
            snippet="TSMC summary",
            provider="gemini_search",
        )
        data: dict = {"stock": {"code": "2330", "name": "TSMC"}}
        status = persist_search_sources_to_news(request, data, [source], repository=repo)
        self.assertEqual(status["saved"], 1)
        news_status = build_news_status("2330", repository=repo)
        self.assertEqual(news_status["item_count"], 1)
        self.assertEqual(news_status["items"][0]["title"], "TSMC news")
        self.assertEqual(repo.items[0].news_origin, "research")

    def test_topic_maintain_sources_are_saved_to_news_repository(self):
        repo = _FakeNewsRepository()
        request = CommandRequest(command="topic_maintain", raw_text="/topic_maintain", target="topics")
        source = SourceItem(
            source_id="S001",
            title="Taiwan AI server supply chain news",
            url="https://example.com/topic-news",
            source_level="Level 2",
            published_date="2026-05-20",
            snippet="Taiwan AI server supply chain summary",
            provider="gemini_search",
        )

        status = persist_search_sources_to_news(request, {}, [source], repository=repo)

        self.assertEqual(status["saved"], 1)
        self.assertEqual(repo.items[0].news_origin, "research")

    def test_theme_radar_ignores_irrelevant_market_event_sources(self):
        repo = _FakeNewsRepository()
        request = CommandRequest(command="theme_radar", raw_text="/theme_radar", target="market")
        bad_source = SourceItem(
            source_id="S001",
            title="Farmers Market event calendar",
            url="https://example.com/farmers-market/2026-05-24",
            source_level="Level 3",
            published_date="2026-05-24",
            snippet="Local farmers market event.",
            provider="minimax_mcp_search",
        )
        good_source = SourceItem(
            source_id="S002",
            title="台股 AI 供應鏈族群輪動",
            url="https://example.com/tw-stock-ai",
            source_level="Level 2",
            published_date="2026-05-24",
            snippet="台股上市櫃 AI 供應鏈、PCB 與散熱族群量增。",
            provider="minimax_mcp_search",
        )

        status = persist_search_sources_to_news(request, {}, [bad_source, good_source], repository=repo)

        self.assertEqual(status["candidate_count"], 1)
        self.assertEqual(repo.items[0].title, "台股 AI 供應鏈族群輪動")

    def test_sector_strength_filters_generic_market_date_sources(self):
        bad_source = SourceItem(
            source_id="S001",
            title="Oil and Corn 2026-05-24 Weekly Market Structure",
            url="https://www.youtube.com/watch?v=example",
            source_level="Level 3",
            published_date="2026-05-24",
            snippet="Commodity market analysis.",
            provider="minimax_mcp_search",
            provider_detail="query=market 2026-05-24; task=族群強弱與法人資金",
        )
        good_source = SourceItem(
            source_id="S002",
            title="上市類股指數行情 - 台股",
            url="https://www.wantgoo.com/index/listed/industry",
            source_level="Level 2",
            published_date="2026-05-24",
            snippet="台股 類股 強弱",
            provider="minimax_mcp_search",
            provider_detail="query=台股 族群強弱 類股輪動; task=族群強弱與法人資金",
        )

        self.assertTrue(is_irrelevant_market_source(bad_source, "sector_strength"))
        self.assertFalse(is_irrelevant_market_source(good_source, "sector_strength"))

    def test_theme_market_commands_filter_non_taiwan_and_clickbait_sources(self):
        a_share = SourceItem(
            source_id="S001",
            title="大陸A股市場資訊整理：寒武纪與寧德時代熱門股票",
            url="https://www.threads.com/post/example",
            source_level="Level 4",
            snippet="A股市場與科創50主線。",
        )
        clickbait = SourceItem(
            source_id="S002",
            title="2026年五月必漲股？",
            url="https://www.youtube.com/watch?v=example",
            source_level="Level 3",
            snippet="熱門股票懶人包。",
        )
        taiwan = SourceItem(
            source_id="S003",
            title="台股電子零組件族群強弱分析",
            url="https://example.com/taiwan-sector",
            source_level="Level 2",
            snippet="台股上市櫃類股輪動。",
        )

        self.assertTrue(is_irrelevant_market_source(a_share, "sector_strength"))
        self.assertTrue(is_irrelevant_market_source(clickbait, "theme_radar"))
        self.assertFalse(is_irrelevant_market_source(taiwan, "theme_radar"))

    def test_news_related_topics_do_not_store_theme_dict_string(self):
        repo = _FakeNewsRepository()
        request = CommandRequest(command="theme_flow", raw_text="/theme_flow AI伺服器", target="AI伺服器")
        source = SourceItem(
            source_id="S001",
            title="AI server news",
            url="https://example.com/ai-server",
            source_level="Level 2",
            published_date="2026-05-24",
            snippet="AI server supply chain",
            provider="minimax_mcp_search",
        )
        data = {
            "theme": {
                "theme_id": "ai_server_odm_rack_scale",
                "theme_name": "AI伺服器ODM與整櫃系統",
                "keywords": ["AI伺服器"],
            }
        }

        status = persist_search_sources_to_news(request, data, [source], repository=repo)

        self.assertEqual(status["saved"], 1)
        self.assertIn("AI伺服器ODM與整櫃系統", repo.items[0].related_topics)
        self.assertNotIn("{", " ".join(repo.items[0].related_topics))


    def test_extract_structured_events_includes_news_events(self):
        events = extract_structured_events({
            "source_events": [{"event_type": "source", "title": "B"}],
            "news_events": [{"event_type": "news", "title": "A"}],
        })

        self.assertEqual([event["event_type"] for event in events], ["source", "news"])


class _FakeNewsRepository:
    def __init__(self):
        self.items = []

    def save_many(self, items):
        self.items.extend(items)
        return len(items), 0

    def query_by_symbol(self, symbol, hours=168):
        return [item for item in self.items if symbol in item.related_symbols]

    def query_by_topic(self, topic, hours=168):
        return [item for item in self.items if topic in item.related_topics]

    def query_all_recent(self, hours=168):
        return list(self.items)


if __name__ == "__main__":
    unittest.main()
