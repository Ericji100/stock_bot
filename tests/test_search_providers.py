from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from research_center.models import CommandRequest, SourceItem
from research_center.orchestrator import (
    _gemini_discovery_source_count,
    _merge_sources,
    _source_quality_summary,
    _should_run_gemini_search_fallback,
)
from research_center.config import ResearchCenterConfig
from research_center.quota_guard import SearchProviderQuotaGuard
from research_center.source_rank import make_source_items


class SearchProviderTests(unittest.TestCase):
    def test_quota_guard_disables_until_next_month_and_recovers(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("search_providers/test_quota_guard_disables")
        try:
            guard = SearchProviderQuotaGuard(tmp / "quota.json")
            guard.mark_exhausted("tavily", "quota", today=date(2026, 5, 15))
            self.assertFalse(guard.is_available("tavily", today=date(2026, 5, 31)))
            self.assertTrue(guard.is_available("tavily", today=date(2026, 6, 1)))
        finally:
            safe_remove_test_cache("search_providers/test_quota_guard_disables")

    def test_quota_guard_monthly_limit_reserve(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("search_providers/test_quota_guard_monthly_limit")
        try:
            guard = SearchProviderQuotaGuard(tmp / "quota.json")
            guard.record_usage("tavily", 980, today=date(2026, 5, 15))
            self.assertFalse(guard.is_under_monthly_limit("tavily", 1000, reserve=20, today=date(2026, 5, 15)))
            self.assertTrue(guard.is_under_monthly_limit("tavily", 1000, reserve=20, today=date(2026, 6, 1)))
        finally:
            safe_remove_test_cache("search_providers/test_quota_guard_monthly_limit")

    def test_make_source_items_keeps_provider_fields(self):
        items = make_source_items([
            {
                "title": "source",
                "url": "https://example.com/a",
                "snippet": "ok",
                "provider": "tavily_extract",
                "provider_detail": "extract_depth=basic",
            }
        ])
        self.assertEqual(items[0].provider, "tavily_extract")
        self.assertEqual(items[0].provider_detail, "extract_depth=basic")

    def test_merge_sources_prefers_tavily_extract_over_search(self):
        base = [SourceItem("S001", "Search", "https://example.com/a", "Level 3", snippet="short", provider="tavily_search")]
        extra = [SourceItem("S999", "Extract", "https://example.com/a", "Level 3", snippet="full", provider="tavily_extract")]
        merged = _merge_sources(base, extra)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].title, "Extract")
        self.assertEqual(merged[0].provider, "tavily_extract")

    def test_source_quality_summary_counts_provider_and_risk(self):
        request = CommandRequest(command="research", raw_text="/research 2330 --deep", mode="deep")
        sources = [
            SourceItem("S001", "MOPS", "https://mops.twse.com.tw/a", "Level 1", provider="official_connector"),
            SourceItem("S002", "新聞 風險", "https://news.example/a", "Level 2", snippet="庫存風險", provider="tavily_extract"),
            SourceItem("S003", "產業", "https://industry.example/a", "Level 3", provider="tavily_search"),
        ]
        summary = _source_quality_summary(sources, request)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["level1"], 1)
        self.assertEqual(summary["level2_or_3"], 2)
        self.assertGreaterEqual(summary["risk_or_contradiction"], 1)
        self.assertEqual(summary["by_provider"]["tavily_extract"], 1)

    def test_should_run_gemini_fallback_when_sources_insufficient(self):
        request = CommandRequest(command="research", raw_text="/research 2330 --deep", mode="deep")
        config = ResearchCenterConfig(
            api_key=None,
            minimax_api_key=None,
            serper_api_key=None,
            jina_api_key=None,
            tavily_api_key=None,
            gemini_fallback_thresholds={
                "research_deep": {
                    "min_total_sources": 20,
                    "min_level1_sources": 1,
                    "min_level2_or_3_sources": 5,
                    "min_risk_or_contradiction_sources": 2,
                }
            },
        )
        sources = [SourceItem("S001", "MOPS", "https://mops.twse.com.tw/a", "Level 1", provider="official_connector")]
        self.assertTrue(_should_run_gemini_search_fallback(request, sources, config))

    def test_should_skip_gemini_fallback_when_sources_sufficient(self):
        request = CommandRequest(command="research", raw_text="/research 2330 --deep", mode="deep")
        config = ResearchCenterConfig(
            api_key=None,
            minimax_api_key=None,
            serper_api_key=None,
            jina_api_key=None,
            tavily_api_key=None,
            gemini_fallback_thresholds={
                "research_deep": {
                    "min_total_sources": 20,
                    "min_level1_sources": 1,
                    "min_level2_or_3_sources": 5,
                    "min_risk_or_contradiction_sources": 2,
                }
            },
        )
        sources = [SourceItem("S001", "MOPS", "https://mops.twse.com.tw/a", "Level 1", provider="official_connector")]
        for i in range(2, 22):
            title = "風險 新聞" if i in {2, 3} else "產業新聞"
            snippet = "庫存風險" if i in {2, 3} else "一般產業資料"
            sources.append(SourceItem(f"S{i:03d}", title, f"https://news.example/{i}", "Level 2", snippet=snippet, provider="tavily_extract"))
        self.assertFalse(_should_run_gemini_search_fallback(request, sources, config))

    def test_gemini_discovery_source_count_handles_skipped_and_success(self):
        skipped = {
            "gemini_search_discovery": {
                "enabled": False,
                "reason": "skipped_enough_non_gemini_sources",
                "source_count": 12,
            }
        }
        self.assertEqual(_gemini_discovery_source_count(skipped), 0)

        success = {
            "gemini_search_discovery": {
                "mode": "multi_stage",
                "source_count": 7,
            }
        }
        self.assertEqual(_gemini_discovery_source_count(success), 7)

        empty = {}
        self.assertEqual(_gemini_discovery_source_count(empty), 0)


class TavilySearchTests(unittest.TestCase):
    def test_tavily_search_discover_does_not_call_extract(self):
        from research_center.tavily_search_service import TavilySearchService
        service = TavilySearchService(api_key="fake_key")
        extract_called = False

        def mock_search_many(queries, task_label):
            return [{"title": "A", "url": "https://a.com", "snippet": "s"}]
        def mock_extract(search_results, task_label, progress=None):
            nonlocal extract_called
            extract_called = True
            raise RuntimeError("_extract_top_results should not be called")
        service._search_many = mock_search_many
        service._extract_top_results = mock_extract

        from research_center.command_parser import parse_command_text
        request = parse_command_text("/research 2330")
        result = service.discover(request, [{"label": "test", "queries": ["2330 news"], "objective": "test"}])

        self.assertFalse(extract_called)
        self.assertEqual(len(result.sources), 1)
        self.assertEqual(result.diagnostics["runs"][0]["extracted_url_count"], 0)

    def test_tavily_search_progress_uses_extracted_0(self):
        from research_center.tavily_search_service import TavilySearchService
        service = TavilySearchService(api_key="fake_key")

        def mock_search_many(queries, task_label):
            return [{"title": "A", "url": "https://a.com", "snippet": "s"}]
        service._search_many = mock_search_many

        progress_messages = []
        def capture_progress(msg):
            progress_messages.append(msg)
        service._extract_top_results = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("should not be called"))

        from research_center.command_parser import parse_command_text
        request = parse_command_text("/research 2330")
        result = service.discover(request, [{"label": "test", "queries": ["2330"], "objective": "test"}], progress=capture_progress)

        completed = [m for m in progress_messages if "completed" in m]
        self.assertTrue(any("extracted=0" in m for m in completed), f"extracted=0 not in {completed}")


class MiniMaxMCPFallbackTests(unittest.TestCase):
    def test_minimax_failure_does_not_block_tavily(self):
        from research_center.command_parser import parse_command_text
        from research_center.config import ResearchCenterConfig
        from research_center.orchestrator import _GeminiDiscoveryRunner

        config = ResearchCenterConfig(api_key=None, minimax_api_key="fake", tavily_api_key="fake",
                                       serper_api_key=None, jina_api_key=None)
        # Enable search providers in config
        object.__setattr__(config, 'enable_tavily_search', True)
        object.__setattr__(config, 'enable_minimax_search', True)
        object.__setattr__(config, 'tavily_monthly_credit_limit', 1000)
        object.__setattr__(config, 'tavily_credit_reserve', 100)

        # Mock MiniMax MCP failure
        class FailingMiniMax:
            def is_configured(self):
                return True
            def discover(self, request, tasks, progress=None):
                from research_center.minimax_search_service import MiniMaxSearchResult
                return MiniMaxSearchResult([], {"enabled": True, "reason": "failed", "runs": [{"label": "test", "status": "failed", "error": "MCP crash"}]})

        # Mock Tavily success
        class WorkingTavily:
            def is_configured(self):
                return True
            def discover(self, request, tasks, progress=None):
                from research_center.tavily_search_service import TavilySearchResult
                src = make_source_items([{"title": "T", "url": "https://t.com", "snippet": "s"}])
                return TavilySearchResult(src, {"enabled": True, "runs": [{"label": "test", "status": "ok", "source_count": 1}]})

        # Mock center with the required services
        class MockCenter:
            pass

        mock_center = MockCenter()
        mock_center.config = config
        mock_center.minimax_search = FailingMiniMax()
        mock_center.tavily_search = WorkingTavily()
        mock_center.official_connector = None
        mock_center.gemini = None
        mock_center.minimax = None
        mock_center.quota_guard = type('QuotaGuard', (), {
            'is_available': lambda self, provider: True,
            'is_under_monthly_limit': lambda self, provider, limit, reserve=0, today=None: True,
            'record_usage': lambda self, provider, units: None,
            'mark_exhausted': lambda self, provider, reason: None,
        })()

        runner = _GeminiDiscoveryRunner(mock_center)

        request = parse_command_text("/research 2330 --deep")
        sources = []
        structured_data = {}

        # Run discovery flow - should not raise even though MiniMax fails
        try:
            runner.run_discovery_flow(request, sources, structured_data, True, progress=None)
        except Exception:
            self.fail("run_discovery_flow raised exception when minimax failed")

        # Tavily should have been called (at least attempted)
        tavily_diag = structured_data.get("tavily_search_discovery", {})
        self.assertTrue(tavily_diag.get("enabled", False), f"Tavily not enabled: {tavily_diag}")
        # source_count is in the runs list
        runs = tavily_diag.get("runs", [])
        self.assertTrue(len(runs) > 0, f"No Tavily runs: {tavily_diag}")
        self.assertEqual(runs[0].get("status"), "ok", f"Tavily run failed: {runs[0]}")


if __name__ == "__main__":
    unittest.main()