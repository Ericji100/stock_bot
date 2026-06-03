from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from research_center.models import CommandRequest, SourceItem
from research_center.orchestrator import (
    _append_search_provider_log,
    _build_search_query_log,
    _gemini_discovery_source_count,
    _merge_sources,
    _source_quality_summary,
    _should_run_gemini_search_fallback,
)
from research_center.config import ResearchCenterConfig
from research_center.quota_guard import SearchProviderQuotaGuard, provider_key_fingerprint
from research_center.source_rank import make_source_items, select_theme_sources_for_prompt


class SearchProviderTests(unittest.TestCase):
    def test_load_research_config_normalizes_tavily_api_keys(self):
        import json
        from research_center.config import load_research_config
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache

        tmp = ensure_test_cache_dir("search_providers/test_tavily_api_keys")
        try:
            config_dir = tmp / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "research_center.json").write_text("{}", encoding="utf-8")
            (config_dir / "secrets.json").write_text(
                json.dumps(
                    {
                        "tavily_api_key": "key_a",
                        "tavily_api_keys": ["key_b", "key_a", "", " key_c "],
                    }
                ),
                encoding="utf-8",
            )

            config = load_research_config(tmp)

            self.assertEqual(config.tavily_api_key, "key_a")
            self.assertEqual(config.tavily_api_keys, ("key_a", "key_b", "key_c"))
        finally:
            safe_remove_test_cache("search_providers/test_tavily_api_keys")

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

    def test_quota_guard_recovers_when_api_key_changes(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("search_providers/test_quota_guard_key_change")
        try:
            guard = SearchProviderQuotaGuard(tmp / "quota.json")
            old_fp = provider_key_fingerprint("old_tavily_key")
            new_fp = provider_key_fingerprint("new_tavily_key")
            guard.mark_exhausted("tavily", "quota", today=date(2026, 5, 15), key_fingerprint=old_fp)

            self.assertFalse(guard.is_available("tavily", today=date(2026, 5, 20), key_fingerprint=old_fp))
            self.assertTrue(guard.is_available("tavily", today=date(2026, 5, 20), key_fingerprint=new_fp))
            self.assertTrue(guard.is_available("tavily", today=date(2026, 5, 20), key_fingerprint=new_fp))
        finally:
            safe_remove_test_cache("search_providers/test_quota_guard_key_change")

    def test_provider_key_fingerprint_does_not_store_raw_key(self):
        raw_key = "tvly-dev-secret-value"
        fingerprint = provider_key_fingerprint(raw_key)
        self.assertIsNotNone(fingerprint)
        self.assertNotIn(raw_key, fingerprint or "")
        self.assertEqual(len(fingerprint or ""), 12)

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

    def test_theme_gemini_fallback_when_sources_many_but_not_relevant(self):
        request = CommandRequest(command="theme", raw_text="/theme AI電源 --deep", theme_scope="AI電源", mode="deep")
        config = ResearchCenterConfig(
            api_key=None,
            gemini_fallback_thresholds={
                "theme_deep": {
                    "min_total_sources": 20,
                    "min_level2_or_3_sources": 7,
                    "min_theme_relevant_sources": 12,
                    "min_theme_high_quality_relevant_sources": 4,
                }
            },
        )
        sources = [
            SourceItem(f"S{i:03d}", "general AI geopolitics news", f"https://example.com/{i}", "Level 3", provider="minimax_mcp_search")
            for i in range(1, 40)
        ]
        self.assertTrue(_should_run_gemini_search_fallback(request, sources, config))

    def test_theme_source_selection_prefers_relevant_taiwan_sources(self):
        sources = [
            SourceItem("S001", "AI geopolitics weekly", "https://substack.example/a", "Level 3", snippet="general AI news", provider="minimax_mcp_search"),
            SourceItem("S002", "台達電 AI伺服器電源 BBU", "https://money.udn.com/a", "L2_media", snippet="AI電源 800VDC 台股", provider="minimax_mcp_search"),
            SourceItem("S003", "光寶科 伺服器電源 法說會", "https://mops.twse.com.tw/a", "L1_official", snippet="PSU power supply", provider="official_connector"),
        ]
        selected, diagnostics = select_theme_sources_for_prompt(
            sources,
            theme="AI電源",
            keywords=["伺服器電源", "BBU"],
            companies=[{"code": "2308", "name": "台達電"}, {"code": "2301", "name": "光寶科"}],
            max_sources=2,
        )
        self.assertEqual(len(selected), 2)
        titles = " ".join(item.title for item in selected)
        self.assertIn("光寶科", titles)
        self.assertIn("台達電", titles)
        self.assertEqual(diagnostics["input_count"], 3)
        self.assertEqual(diagnostics["selected_count"], 2)

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

    def test_search_query_log_records_tasks_and_provider_summary(self):
        structured_data: dict = {}
        tasks = [
            {"label": "官方公告", "objective": "找官方資料", "queries": ["2330 公開資訊觀測站", "2330 法說會"]},
            {"label": "風險反證", "objective": "找反證", "queries": ["2330 風險"]},
        ]
        structured_data["search_query_log"] = _build_search_query_log(tasks)
        _append_search_provider_log(
            structured_data,
            provider="minimax_mcp_search",
            source_count=5,
            diagnostics={"runs": [{"query_count": 2}, {"query_count": 1}], "error_reasons": []},
        )

        log = structured_data["search_query_log"]
        self.assertEqual(log["schema_version"], "search_tasks_v1")
        self.assertEqual(log["task_count"], 2)
        self.assertEqual(log["total_query_count"], 3)
        self.assertEqual(log["providers"][0]["provider"], "minimax_mcp_search")
        self.assertEqual(log["providers"][0]["source_count"], 5)
        self.assertEqual(log["providers"][0]["query_count"], 3)


class TavilySearchTests(unittest.TestCase):
    def test_tavily_usage_parses_key_remaining(self):
        from unittest.mock import patch
        from research_center.tavily_search_service import TavilySearchService

        class FakeResponse:
            def raise_for_status(self):
                return None
            def json(self):
                return {
                    "key": {"usage": 450, "limit": 1000, "search_usage": 300},
                    "account": {"current_plan": "Researcher", "plan_usage": 450, "plan_limit": 1000},
                }

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.headers = None
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def get(self, url, headers=None):
                self.headers = headers
                return FakeResponse()

        service = TavilySearchService(api_key="fake_key")
        with patch("research_center.tavily_search_service.httpx.Client", FakeClient):
            usage = service.get_usage()

        self.assertTrue(usage["available"])
        self.assertEqual(usage["key_usage"], 450)
        self.assertEqual(usage["key_limit"], 1000)
        self.assertEqual(usage["remaining"], 550)

    def test_tavily_usage_availability_respects_reserve(self):
        from research_center.tavily_search_service import TavilySearchService

        service = TavilySearchService(api_key="fake_key")
        service.get_usage = lambda: {"available": True, "remaining": 15}

        available, diagnostics = service.has_available_usage(reserve=20)

        self.assertFalse(available)
        self.assertEqual(diagnostics["remaining"], 15)

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

    def test_tavily_search_switches_to_second_key_on_quota_error(self):
        from research_center.quota_guard import provider_key_fingerprint
        from research_center.tavily_search_service import TavilyQuotaError, TavilySearchService

        service = TavilySearchService(api_key=None, api_keys=("key_one", "key_two"))
        calls: list[str] = []

        def fake_search_with_key(query, api_key):
            calls.append(api_key)
            if api_key == "key_one":
                raise TavilyQuotaError("quota exceeded")
            return [{"title": "B", "url": "https://b.com", "snippet": "ok"}]

        service._search_with_key = fake_search_with_key
        from research_center.command_parser import parse_command_text
        request = parse_command_text("/research 2330")
        result = service.discover(request, [{"label": "test", "queries": ["2330"], "objective": "test"}])

        fp_one = provider_key_fingerprint("key_one")
        fp_two = provider_key_fingerprint("key_two")
        self.assertEqual(calls, ["key_one", "key_two"])
        self.assertEqual(len(result.sources), 1)
        self.assertIn(fp_one, result.diagnostics["quota_exhausted_key_fingerprints"])
        self.assertEqual(result.diagnostics["query_count_by_key_fingerprint"][fp_two], 1)


class MiniMaxMCPFallbackTests(unittest.TestCase):
    def test_tavily_official_usage_overrides_local_exhausted_marker(self):
        from research_center.command_parser import parse_command_text
        from research_center.config import ResearchCenterConfig
        from research_center.orchestrator import _GeminiDiscoveryRunner
        from research_center.tavily_search_service import TavilySearchResult

        config = ResearchCenterConfig(api_key=None, tavily_api_key="fake")
        object.__setattr__(config, 'enable_tavily_search', True)
        object.__setattr__(config, 'tavily_credit_reserve', 20)

        class WorkingTavily:
            def is_configured(self):
                return True
            def has_available_usage(self, reserve=0):
                return True, {
                    "available": True,
                    "remaining": 550,
                    "selected_key_fingerprint": provider_key_fingerprint("fake"),
                }
            def discover(self, request, tasks, progress=None):
                src = make_source_items([{"title": "T", "url": "https://t.com", "snippet": "s"}])
                return TavilySearchResult(src, {"enabled": True, "runs": [{"label": "test", "status": "ok"}]})

        class LocalExhaustedGuard:
            def __init__(self):
                self.cleared = False
            def is_available(self, provider, **kwargs):
                return False
            def is_under_monthly_limit(self, provider, limit, reserve=0, today=None):
                return False
            def clear(self, provider):
                self.cleared = provider.startswith("tavily:")
            def record_usage(self, provider, units):
                return None
            def mark_exhausted(self, provider, reason, **kwargs):
                return None

        class MockCenter:
            pass

        mock_center = MockCenter()
        mock_center.config = config
        mock_center.tavily_search = WorkingTavily()
        mock_center.quota_guard = LocalExhaustedGuard()
        runner = _GeminiDiscoveryRunner(mock_center)
        request = parse_command_text("/research 2330 --deep")
        sources = []
        structured_data = {}

        runner._run_tavily(request, [{"label": "test", "queries": ["2330"], "objective": "test"}], sources, structured_data, None)

        self.assertTrue(mock_center.quota_guard.cleared)
        self.assertEqual(len(sources), 1)
        self.assertEqual(structured_data["tavily_search_discovery"]["official_usage"]["remaining"], 550)

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
            'is_available': lambda self, provider, **kwargs: True,
            'is_under_monthly_limit': lambda self, provider, limit, reserve=0, today=None: True,
            'record_usage': lambda self, provider, units: None,
            'mark_exhausted': lambda self, provider, reason, **kwargs: None,
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
