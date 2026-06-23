from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from research_center.command_parser import parse_command_text
from research_center.config import ResearchCenterConfig, load_research_config
from research_center.gemini_service import GeminiResult, GeminiService
from research_center.minimax_search_service import MiniMaxSearchService, _normalize_search_item
from research_center.minimax_service import MiniMaxRequestError, MiniMaxResult, MiniMaxService, _extract_minimax_text
from research_center.models import ReportArtifacts, ResearchCenterResult, SourceItem
from research_center.orchestrator import (
    ResearchCenter,
    _build_minimax_research_retry_prompt,
    _should_retry_minimax_research,
)
from research_center.report_builder import write_report_artifacts


class MiniMaxIntegrationTests(unittest.TestCase):
    def test_deep_research_minimax_timeout_is_retryable(self):
        request = parse_command_text("/research 2241 --deep --model minimax")

        self.assertTrue(_should_retry_minimax_research(request, TimeoutError("ReadTimeout"), 220_000))
        self.assertFalse(_should_retry_minimax_research(request, TimeoutError("ReadTimeout"), 20_000))
        self.assertFalse(_should_retry_minimax_research(parse_command_text("/theme AI電源 --model minimax"), TimeoutError("ReadTimeout"), 220_000))

    def test_deep_research_retry_prompt_keeps_source_index_and_core_data(self):
        request = parse_command_text("/research 2241 --deep --model minimax")
        source = SourceItem(
            source_id="S001",
            title="2241 月營收公告",
            url="https://example.com/revenue",
            source_level="L1_official",
            snippet="營收 YoY 轉正",
        )

        prompt = _build_minimax_research_retry_prompt(
            request,
            {
                "revenue_data": [{"month": "2026-04", "yoy": 22}],
                "data_gap_summary": {"missing": ["財報尚未反映"]},
                "low_model_digest": {"facts": [{"fact": "營收轉強"}]},
            },
            [source],
            TimeoutError("ReadTimeout"),
            original_prompt_chars=220_000,
        )

        self.assertIn("保真重試資料包", prompt)
        self.assertIn("2241 月營收公告", prompt)
        self.assertIn("財報尚未反映", prompt)
        self.assertIn("不是 fallback 報告", prompt)

    def test_minimax_search_item_normalizes_relative_date_from_date_field(self):
        item = _normalize_search_item({
            "title": "台股半導體新聞",
            "link": "https://news.cnyes.com/news/id/123",
            "snippet": "台股半導體供應鏈新聞",
            "date": "7 hours ago",
        })
        self.assertRegex(item["published_date"], r"^20\d{2}-\d{2}-\d{2}$")

    def test_minimax_search_item_normalizes_date_from_snippet(self):
        item = _normalize_search_item({
            "title": "台股半導體新聞",
            "url": "https://news.cnyes.com/news/id/124",
            "snippet": "發布時間：2026/06/15 09:10 台股半導體供應鏈新聞",
        })
        self.assertEqual(item["published_date"], "2026-06-15")

    def test_config_loads_minimax_and_search_keys(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("minimax_integration/test_config_loads_minimax_and_search_keys")
        try:
            root = tmp
            (root / "config").mkdir(parents=True, exist_ok=True)
            (root / "config" / "research_center.json").write_text(
                json.dumps(
                    {
                        "enable_minimax_search": True,
                        "enable_minimax_comparison": True,
                        "minimax_model": "MiniMax-M3",
                    }
                ),
                encoding="utf-8",
            )
            (root / "config" / "secrets.json").write_text(
                json.dumps(
                    {
                        "gemini_api_key": "gemini",
                        "minimax_api_key": "mini",
                        "serper_api_key": "serper",
                        "jina_api_key": "jina",
                    }
                ),
                encoding="utf-8",
            )
            config = load_research_config(root)
        finally:
            safe_remove_test_cache("minimax_integration/test_config_loads_minimax_and_search_keys")
        self.assertEqual(config.minimax_model, "MiniMax-M3")
        self.assertEqual(config.minimax_api_key, "mini")
        self.assertEqual(config.serper_api_key, "serper")
        self.assertTrue(config.enable_minimax_search)
        self.assertTrue(config.enable_minimax_comparison)


    def test_config_defaults_gemini_31_pro_preview_with_35_flash_fallback(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("minimax_integration/test_config_defaults_gemini_31_pro_preview_with_35_flash_fallback")
        try:
            root = tmp
            (root / "config").mkdir(parents=True, exist_ok=True)
            (root / "config" / "research_center.json").write_text("{}", encoding="utf-8")
            (root / "config" / "secrets.json").write_text(json.dumps({"gemini_api_key": "gemini"}), encoding="utf-8")
            config = load_research_config(root)
        finally:
            safe_remove_test_cache("minimax_integration/test_config_defaults_gemini_31_pro_preview_with_35_flash_fallback")
        self.assertEqual(config.model, "gemini-3.1-pro-preview")
        self.assertEqual(config.fallback_models, ("gemini-3.5-flash",))
        self.assertEqual(config.gemini_discovery_model, "gemini-3.5-flash")
        self.assertEqual(config.minimax_model, "MiniMax-M3")
        self.assertEqual(config.minimax_low_model, "MiniMax-M3")

    def test_research_center_low_model_client_uses_configured_m3(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache

        tmp = ensure_test_cache_dir("minimax_integration/test_research_center_low_model_client_uses_configured_m3")
        center = None
        try:
            config = ResearchCenterConfig(
                api_key="gemini",
                minimax_api_key="mini",
                minimax_model="MiniMax-M3",
                minimax_low_model="MiniMax-M3",
                database_path=tmp / "research.db",
                report_root=tmp / "reports",
            )
            center = ResearchCenter(config)
            self.assertEqual(center.low_model_minimax.model, "MiniMax-M3")
        finally:
            if center is not None:
                center.database.close()
            safe_remove_test_cache("minimax_integration/test_research_center_low_model_client_uses_configured_m3")

    def test_gemini_service_keeps_configured_fallback_chain(self):
        service = GeminiService(
            "key",
            "gemini-3.1-pro-preview",
            fallback_models=("gemini-3.5-flash", "gemini-3.1-pro-preview", ""),
        )
        self.assertEqual(service.model, "gemini-3.1-pro-preview")
        self.assertEqual(service.fallback_models, ("gemini-3.5-flash",))
    def test_minimax_default_timeout_is_bounded_for_full_prompt_comparison(self):
        service = MiniMaxService("key")
        self.assertEqual(service.timeout_seconds, 600.0)

    def test_minimax_extract_text_strips_thinking_block(self):
        text = _extract_minimax_text({"choices": [{"message": {"content": "<think>hidden</think>\n# Report"}}]})
        self.assertEqual(text, "# Report")

    def test_minimax_http_400_error_keeps_provider_diagnostics(self):
        request = httpx.Request("POST", "https://api.minimax.io/v1/chat/completions")
        response = httpx.Response(
            400,
            request=request,
            json={"error": {"message": "prompt is too long"}},
        )

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, headers=None, json=None):
                return response

        service = MiniMaxService("key", model="MiniMax-M3", max_retries=0)
        with patch("research_center.minimax_service.httpx.Client", FakeClient):
            with self.assertRaises(MiniMaxRequestError) as ctx:
                service.generate_report("x" * 25)

        message = str(ctx.exception)
        self.assertIn("MiniMax API request failed", message)
        self.assertIn("status=400", message)
        self.assertIn("model=MiniMax-M3", message)
        self.assertIn("prompt_chars=", message)
        self.assertIn("payload_bytes=", message)
        self.assertIn("prompt is too long", message)
        self.assertEqual(ctx.exception.diagnostics["status_code"], 400)
        self.assertEqual(ctx.exception.diagnostics["provider"], "minimax")

    def test_minimax_timeout_error_keeps_provider_diagnostics(self):
        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, headers=None, json=None):
                raise httpx.ReadTimeout("timed out")

        service = MiniMaxService("key", model="MiniMax-M3", timeout_seconds=1.5, max_retries=0)
        with patch("research_center.minimax_service.httpx.Client", FakeClient):
            with self.assertRaises(MiniMaxRequestError) as ctx:
                service.generate_report("timeout test")

        self.assertIn("status=timeout", str(ctx.exception))
        self.assertEqual(ctx.exception.diagnostics["status_code"], "timeout")
        self.assertEqual(ctx.exception.diagnostics["timeout_seconds"], 1.5)
        self.assertEqual(ctx.exception.diagnostics["provider"], "minimax")
        self.assertGreater(ctx.exception.diagnostics["prompt_chars"], 0)

    def test_minimax_timeout_is_not_retried(self):
        calls = {"count": 0}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, headers=None, json=None):
                calls["count"] += 1
                raise httpx.ReadTimeout("timed out")

        service = MiniMaxService("key", model="MiniMax-M3", timeout_seconds=1.5, max_retries=1)
        with patch("research_center.minimax_service.httpx.Client", FakeClient):
            with self.assertRaises(MiniMaxRequestError) as ctx:
                service.generate_report("timeout test")

        self.assertEqual(calls["count"], 1)
        self.assertEqual(ctx.exception.diagnostics["retry_skipped_reason"], "timeout_not_retried")

    def test_minimax_search_discover_builds_sources_without_network(self):
        # Create a service with a mock MCP session
        service = MiniMaxSearchService("serper", "jina", minimax=None)
        # Mock _search_many: new signature returns (results, errors)
        def mock_search_many(queries, api_key=None, raw_response_samples=None, max_samples=3):
            return [
                {"title": "TWSE", "url": "https://www.twse.com.tw/test", "snippet": "official", "published_date": None, "query": queries[0] if queries else ""}
            ], []
        service._search_many = mock_search_many  # type: ignore[method-assign]
        request = parse_command_text("/research 2330")
        result = service.discover(request, [{"label": "official", "queries": ["2330 TWSE"], "objective": "official data"}])
        self.assertGreaterEqual(len(result.sources), 1)
        self.assertIn("L1", result.sources[0].source_level)
        self.assertIn("official", result.sources[0].snippet)
        self.assertGreaterEqual(result.diagnostics["source_count"], 1)

    def test_minimax_extract_search_items_supports_organic(self):
        from research_center.minimax_search_service import _extract_search_items
        raw = {"organic": [{"title": "A", "link": "https://a.com", "snippet": "s"}]}
        items = _extract_search_items(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://a.com")
        self.assertEqual(items[0]["snippet"], "s")

    def test_minimax_extract_search_items_supports_results(self):
        from research_center.minimax_search_service import _extract_search_items
        raw = {"results": [{"title": "B", "url": "https://b.com", "summary": "sum"}]}
        items = _extract_search_items(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://b.com")
        self.assertEqual(items[0]["snippet"], "sum")

    def test_minimax_extract_search_items_supports_sources(self):
        from research_center.minimax_search_service import _extract_search_items
        raw = {
            "sources": [
                {
                    "title": "Source A",
                    "url": "https://example.com/a",
                    "snippet": "source snippet",
                    "published_date": "2026-05-17"
                }
            ]
        }
        items = _extract_search_items(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://example.com/a")
        self.assertEqual(items[0]["snippet"], "source snippet")
        self.assertEqual(items[0]["published_date"], "2026-05-17")

    def test_minimax_discover_handles_mcp_failure_without_raise(self):
        service = MiniMaxSearchService("serper", "jina", minimax=None)
        def mock_search_many(queries, api_key=None, raw_response_samples=None, max_samples=3):
            raise RuntimeError("MCP subprocess failed")
        service._search_many = mock_search_many  # type: ignore[method-assign]
        request = parse_command_text("/research 2330")
        result = service.discover(request, [{"label": "official", "queries": ["2330"], "objective": "test"}])
        self.assertEqual(len(result.sources), 0)
        self.assertEqual(result.diagnostics["runs"][0]["status"], "failed")

    def test_minimax_discover_builds_provider_fields(self):
        service = MiniMaxSearchService("serper", "jina", minimax=None)
        def mock_search_many(queries, api_key=None, raw_response_samples=None, max_samples=3):
            return [{"title": "Test", "url": "https://test.com", "snippet": "test", "published_date": None, "query": "test"}], []
        service._search_many = mock_search_many  # type: ignore[method-assign]
        request = parse_command_text("/research 2330")
        result = service.discover(request, [{"label": "test", "queries": ["2330"], "objective": "test"}])
        self.assertGreaterEqual(len(result.sources), 1)
        self.assertEqual(result.sources[0].provider, "minimax_mcp_search")

    def test_comparison_report_filename_uses_minimax_variant(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("minimax_integration/test_comparison_report_filename_uses_minimax_variant")
        try:
            request = parse_command_text("/research 2330 --no-html --no-json")
            artifacts, report_json = write_report_artifacts(
                tmp,
                request,
                "# MiniMax Report\n\n## 摘要\n測試",
                "測試",
                [],
                True,
                None,
                {"analysis_model": "MiniMax-M3"},
                report_variant="minimax",
            )
        finally:
            safe_remove_test_cache("minimax_integration/test_comparison_report_filename_uses_minimax_variant")
        self.assertIn("_minimax_", artifacts.report_id)
        self.assertEqual(report_json["report_variant"], "minimax")
        self.assertEqual(report_json["metadata"]["analysis_model"], "MiniMax-M3")



    def test_minimax_mcp_env_sets_uv_dirs(self):
        """Verify _build_mcp_startup_params sets UV_CACHE_DIR and UV_TOOL_DIR."""
        from research_center.minimax_search_service import _build_mcp_startup_params
        import os

        # Use project test cache dir instead of tempfile (avoids Windows permission issues)
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("minimax_integration/test_mcp_env_sets_uv_dirs")
        try:
            cache_dir = str(tmp / "cache")
            tool_dir = str(tmp / "tools")
            old_cache = os.environ.pop("UV_CACHE_DIR", None)
            old_tool = os.environ.pop("UV_TOOL_DIR", None)
            old_minimax_cmd = os.environ.pop("MINIMAX_MCP_COMMAND", None)
            try:
                os.environ["UV_CACHE_DIR"] = cache_dir
                os.environ["UV_TOOL_DIR"] = tool_dir
                cmd, args_list, env = _build_mcp_startup_params("fake-key")
                self.assertEqual(env["UV_CACHE_DIR"], cache_dir)
                self.assertEqual(env["UV_TOOL_DIR"], tool_dir)
                # Dirs should be created
                self.assertTrue(os.path.exists(cache_dir))
                self.assertTrue(os.path.exists(tool_dir))
            finally:
                if old_cache:
                    os.environ["UV_CACHE_DIR"] = old_cache
                if old_tool:
                    os.environ["UV_TOOL_DIR"] = old_tool
                if old_minimax_cmd:
                    os.environ["MINIMAX_MCP_COMMAND"] = old_minimax_cmd
        finally:
            safe_remove_test_cache("minimax_integration/test_mcp_env_sets_uv_dirs")

    def test_minimax_mcp_prefers_project_runtime_exe_over_uvx(self):
        """Verify project .runtime MCP exe is preferred over uvx fallback."""
        from research_center.minimax_search_service import _build_mcp_startup_params, MCP_PACKAGE
        import os

        old_cmd = os.environ.pop("MINIMAX_MCP_COMMAND", None)
        old_cache = os.environ.pop("UV_CACHE_DIR", None)
        old_tool = os.environ.pop("UV_TOOL_DIR", None)
        try:
            cmd, args_list, env = _build_mcp_startup_params("fake-key")
            runtime_suffix = f".runtime/uv_tools/{MCP_PACKAGE}/Scripts/{MCP_PACKAGE}.exe"
            if Path(cmd).exists() and cmd.replace("\\", "/").endswith(runtime_suffix):
                self.assertEqual(args_list, [])
                self.assertTrue(env["UV_TOOL_DIR"].replace("\\", "/").endswith(".runtime/uv_tools"))
            else:
                self.assertNotIn("uvx.exe", cmd.replace("\\", "/"))
        finally:
            if old_cmd:
                os.environ["MINIMAX_MCP_COMMAND"] = old_cmd
            if old_cache:
                os.environ["UV_CACHE_DIR"] = old_cache
            if old_tool:
                os.environ["UV_TOOL_DIR"] = old_tool

    def test_minimax_mcp_command_override_skips_uv(self):
        """Verify MINIMAX_MCP_COMMAND bypasses uvx/uv and uses the given command directly."""
        from research_center.minimax_search_service import _build_mcp_startup_params
        import os

        old_cmd = os.environ.pop("MINIMAX_MCP_COMMAND", None)
        old_args = os.environ.pop("MINIMAX_MCP_ARGS", None)
        old_cache = os.environ.pop("UV_CACHE_DIR", None)
        old_tool = os.environ.pop("UV_TOOL_DIR", None)
        try:
            os.environ["MINIMAX_MCP_COMMAND"] = "C:\\mock\\mcp.exe"
            os.environ["MINIMAX_MCP_ARGS"] = "--stdio"
            cmd, args_list, env = _build_mcp_startup_params("fake-key")
            self.assertEqual(cmd, "C:\\mock\\mcp.exe")
            self.assertEqual(args_list, ["--stdio"])
            # Must not contain uvx or uv tool run
            self.assertNotIn("uvx", cmd)
            self.assertNotIn("uv.exe", cmd)
            self.assertNotIn("tool run", " ".join(args_list))
        finally:
            if old_cmd:
                os.environ["MINIMAX_MCP_COMMAND"] = old_cmd
            if old_args:
                os.environ["MINIMAX_MCP_ARGS"] = old_args
            if old_cache:
                os.environ["UV_CACHE_DIR"] = old_cache
            if old_tool:
                os.environ["UV_TOOL_DIR"] = old_tool

    def test_minimax_mcp_prefers_env_command(self):
        """Verify MINIMAX_MCP_COMMAND env override is used instead of uvx/uv."""
        from research_center.minimax_search_service import _build_mcp_startup_params
        import os
        old_cmd = os.environ.pop("MINIMAX_MCP_COMMAND", None)
        old_args = os.environ.pop("MINIMAX_MCP_ARGS", None)
        old_cache = os.environ.pop("UV_CACHE_DIR", None)
        old_tool = os.environ.pop("UV_TOOL_DIR", None)
        try:
            os.environ["MINIMAX_MCP_COMMAND"] = "C:\\custom\\mcp.exe"
            os.environ["MINIMAX_MCP_ARGS"] = "--verbose"
            cmd, args_list, env = _build_mcp_startup_params("fake-key")
            self.assertEqual(cmd, "C:\\custom\\mcp.exe")
            self.assertEqual(args_list, ["--verbose"])
        finally:
            if old_cmd:
                os.environ["MINIMAX_MCP_COMMAND"] = old_cmd
            if old_args:
                os.environ["MINIMAX_MCP_ARGS"] = old_args
            if old_cache:
                os.environ["UV_CACHE_DIR"] = old_cache

    def test_minimax_mcp_error_reason_permission_denied(self):
        """Verify permission denied errors are classified correctly."""
        from research_center.minimax_search_service import _classify_errors
        errors = ["WinError 5: Access is denied", "Permission denied: C:\\Users\\..."]
        reasons = _classify_errors(errors)
        self.assertIn("uv_permission_denied", reasons)

    def test_minimax_mcp_error_reason_pypi_failed(self):
        """Verify PyPI connection failures are classified correctly."""
        from research_center.minimax_search_service import _classify_errors
        errors = ["Failed to fetch https://pypi.org/simple/minimax/", "Connection refused"]
        reasons = _classify_errors(errors)
        self.assertIn("pypi_connection_failed", reasons)

    def test_minimax_mcp_error_reason_package_not_installed(self):
        """Verify missing package errors are classified correctly."""
        from research_center.minimax_search_service import _classify_errors
        errors = ["ENOENT: no such file or directory, open 'C:\\Users\\...\\minimax-coding-plan-mcp'"]
        reasons = _classify_errors(errors)
        self.assertIn("mcp_package_not_installed", reasons)

    def test_minimax_mcp_error_reason_api_key_missing(self):
        """Verify MINIMAX_API_KEY missing is classified correctly."""
        from research_center.minimax_search_service import _classify_errors
        errors = ["MINIMAX_API_KEY environment variable is required"]
        reasons = _classify_errors(errors)
        self.assertIn("minimax_api_key_missing", reasons)

    def test_minimax_mcp_error_reason_api_auth_failed(self):
        """Verify 401 Unauthorized is classified correctly."""
        from research_center.minimax_search_service import _classify_errors
        errors = ["401 Unauthorized", "Authentication failed"]
        for err in errors:
            reasons = _classify_errors([err])
            self.assertIn("minimax_api_auth_failed", reasons)

    def test_minimax_mcp_error_reason_quota_failed(self):
        """Verify quota/credit exceeded is classified correctly."""
        from research_center.minimax_search_service import _classify_errors
        errors = ["quota exceeded", "credit insufficient", "rate limit exceeded"]
        for err in errors:
            reasons = _classify_errors([err])
            self.assertIn("minimax_quota_or_credit_failed", reasons)

    def test_minimax_mcp_error_reason_empty_response(self):
        """Verify empty response is classified correctly."""
        from research_center.minimax_search_service import _classify_errors
        errors = ["empty response", "response is empty", "null response from server"]
        for err in errors:
            reasons = _classify_errors([err])
            self.assertIn("mcp_empty_response", reasons)

    def test_minimax_mcp_error_reason_protocol_errors_case_insensitive(self):
        """Verify TypeError/AttributeError/JSONDecodeError are classified as mcp_protocol_error."""
        from research_center.minimax_search_service import _classify_errors
        for err in ["TypeError: bad", "AttributeError: x", "JSONDecodeError: bad"]:
            reasons = _classify_errors([err])
            self.assertIn("mcp_protocol_error", reasons)

    def test_minimax_discover_partial_results_with_errors(self):
        """Verify discover() keeps valid sources when some queries fail."""
        from research_center.minimax_search_service import MiniMaxSearchService
        from research_center.command_parser import parse_command_text

        service = MiniMaxSearchService("serper", "jina", minimax=None)

        # Mock _search_many: returns 1 valid result + 1 error
        def mock_search_many(queries, api_key=None, raw_response_samples=None, max_samples=3):
            results = [
                {"title": "Valid Result", "url": "https://example.com/valid", "snippet": "found", "published_date": None, "query": "test query"}
            ]
            errors = ["some error for query2"]
            return results, errors

        service._search_many = mock_search_many  # type: ignore[method-assign]
        request = parse_command_text("/research 2330")
        result = service.discover(request, [{"label": "test", "queries": ["test1", "test2"], "objective": "test"}])

        self.assertEqual(len(result.sources), 1)
        self.assertEqual(result.diagnostics["source_count"], 1)
        self.assertEqual(result.diagnostics["runs"][0]["status"], "partial")
        self.assertIn("error_reasons", result.diagnostics["runs"][0])
        self.assertIn("error_count", result.diagnostics["runs"][0])
        self.assertEqual(result.diagnostics["runs"][0]["error_count"], 1)
        self.assertIn("error_samples", result.diagnostics["runs"][0])
        # error_samples uses "error" field, not "query"
        self.assertIn("error", result.diagnostics["runs"][0]["error_samples"][0])
        self.assertEqual(result.diagnostics["runs"][0]["error_samples"][0]["error"], "some error for query2")

    def test_minimax_discover_empty_results_sets_mcp_empty_results_flag(self):
        """Verify discover() sets mcp_empty_results=True when queries succeed but return no results."""
        from research_center.minimax_search_service import MiniMaxSearchService
        from research_center.command_parser import parse_command_text

        service = MiniMaxSearchService("serper", "jina", minimax=None)

        def mock_search_many(queries, api_key=None, raw_response_samples=None, max_samples=3):
            return [], []  # No results, no errors

        service._search_many = mock_search_many  # type: ignore[method-assign]
        request = parse_command_text("/research 2330")
        result = service.discover(request, [{"label": "empty", "queries": ["test"], "objective": "test"}])

        self.assertEqual(len(result.sources), 0)
        self.assertTrue(result.diagnostics["runs"][0].get("mcp_empty_results"))
        self.assertEqual(result.diagnostics["runs"][0]["status"], "ok")

    def test_minimax_raw_response_samples_include_new_fields(self):
        """Verify raw_response_samples include status, raw_keys, preview."""
        from research_center.minimax_search_service import MiniMaxSearchService
        from research_center.command_parser import parse_command_text

        service = MiniMaxSearchService("serper", "jina", minimax=None)

        class FakeRaw:
            def __init__(self):
                self.content = []
                self.other = "val"

            def __repr__(self):
                return "FakeRaw(...)"

        def mock_search_many(queries, api_key=None, raw_response_samples=None, max_samples=3):
            fr = FakeRaw()
            if raw_response_samples is not None:
                raw_response_samples.append({
                    "query": "test",
                    "status": "success",
                    "raw_type": "FakeRaw",
                    "raw_keys": ["content", "other"],
                    "item_count": 0,
                    "preview": "FakeRaw(...)",
                })
            return [], []

        service._search_many = mock_search_many  # type: ignore[method-assign]
        request = parse_command_text("/research 2330")
        result = service.discover(request, [{"label": "test", "queries": ["test"], "objective": "test"}])

        samples = result.diagnostics.get("raw_response_samples", [])
        self.assertGreaterEqual(len(samples), 1)
        s = samples[0]
        self.assertIn("status", s)
        self.assertIn("raw_keys", s)
        self.assertIn("preview", s)
        self.assertNotIn("raw_preview", s)

    def test_flatten_task_queries_handles_list_of_dicts_with_items(self):
        """Verify _flatten_task_queries handles dict items and excludes empty strings."""
        from research_center.minimax_search_service import _flatten_task_queries
        queries = [
            {"items": ["query1", "query2"]},
            "",
            "direct_query",
            {"items": []},
            "  ",
        ]
        result = _flatten_task_queries(queries)
        self.assertEqual(result, ["query1", "query2", "direct_query"])


    def test_classify_errors_recognizes_minimax_sensitive_block(self):
        from research_center.minimax_search_service import _classify_errors
        errors = ["MiniMax MCP returned text response: Failed to perform search: API Error: 1027-output new_sensitive Trace-Id: abc"]
        reasons = _classify_errors(errors)
        self.assertIn("minimax_sensitive_query_blocked", reasons)
        self.assertNotIn("mcp_parse_error", reasons)

    def test_extract_search_items_rejects_plain_text_error(self):
        from research_center.minimax_search_service import _McpParseError, _extract_search_items
        with self.assertRaises(_McpParseError) as ctx:
            _extract_search_items("Failed to perform search: API Error: 1027-output new_sensitive")
        self.assertIn("MiniMax MCP returned text response", str(ctx.exception))

    def test_classify_errors_recognizes_mcp_parse_error(self):
        """Verify parse errors from _McpParseError are classified as mcp_parse_error."""
        from research_center.minimax_search_service import _classify_errors
        for err in [
            "McpParseError: Failed to extract search items",
            "McpParseError: No recognized item keys in response: ['foo']",
            "McpParseError: Unexpected content list item type: str",
        ]:
            reasons = _classify_errors([err])
            self.assertIn("mcp_parse_error", reasons, f"Expected mcp_parse_error for: {err}")

    def test_classify_errors_recognizes_mcp_error_response(self):
        """Verify 'error response' style errors are classified as mcp_error_response."""
        from research_center.minimax_search_service import _classify_errors
        # Use error strings that don't match other categories
        for err in ["mcp_error: something went wrong", "mcp_error: connection refused"]:
            reasons = _classify_errors([err])
            self.assertIn("mcp_error_response", reasons, f"Expected mcp_error_response for: {err}")

    def test_classify_errors_recognizes_missing_mcp_module(self):
        """Verify missing MCP Python module is reported as package/dependency missing."""
        from research_center.minimax_search_service import _classify_errors
        reasons = _classify_errors(["No module named 'mcp'"])
        self.assertIn("mcp_package_not_installed", reasons)
        self.assertNotIn("mcp_unknown_error", reasons)

    def test_parallel_model_jobs_use_same_prompt_and_write_reports(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache

        class FakeGemini:
            def __init__(self):
                self.prompts = []

            def is_configured(self):
                return True

            def generate_report(self, prompt, enable_grounding=None):
                self.prompts.append(prompt)
                return GeminiResult("# Gemini\n\n## 摘要\nOK [S001]", [], {"raw": "g"}, {"finish_reason": "STOP"})

        class FakeMiniMax:
            def __init__(self):
                self.prompts = []

            def is_configured(self):
                return True

            def generate_report(self, prompt):
                self.prompts.append(prompt)
                return MiniMaxResult("# MiniMax\n\n## 摘要\nOK [S001]", {"raw": "m"}, {"finish_reason": "stop"})

        tmp = ensure_test_cache_dir("minimax_integration/test_parallel_model_jobs")
        try:
            root = tmp
            config = ResearchCenterConfig(
                api_key="gemini",
                minimax_api_key="mini",
                enable_minimax_comparison=True,
                enable_grounding=False,
                report_root=root / "reports",
                database_path=root / "research.db",
            )
            center = ResearchCenter(config)
            center.gemini = FakeGemini()  # type: ignore[assignment]
            center.minimax = FakeMiniMax()  # type: ignore[assignment]
            request = parse_command_text("/research 2330")
            sources = [SourceItem("S001", "TWSE", "https://www.twse.com.tw/", "Level 1")]
            pending = ResearchCenterResult(
                status="pending_models",
                request=request,
                summary="pending",
                markdown="pending",
                report_json={},
                sources=sources,
                artifacts=ReportArtifacts("pending", "research", Path("__no_md__"), Path("__no_html__"), Path("__no_json__"), Path("__no_sources__")),
                ai_used=False,
                runtime_context={
                    "parallel_model_jobs": {
                        "prompt": "SAME FULL PROMPT",
                        "prompt_path": "prompt.json",
                        "sources": sources,
                        "structured_data": {"prompt_policy": {}},
                        "use_grounding": False,
                    }
                },
            )
            gemini_entry = center.run_parallel_model_job(pending, "gemini")
            minimax_entry = center.run_parallel_model_job(pending, "minimax")
            self.assertEqual(gemini_entry["status"], "success")
            self.assertEqual(minimax_entry["status"], "success")
            self.assertTrue(Path(gemini_entry["markdown_path"]).exists())
            self.assertTrue(Path(minimax_entry["markdown_path"]).exists())
            self.assertEqual(center.gemini.prompts, ["SAME FULL PROMPT"])
            self.assertEqual(center.minimax.prompts, ["SAME FULL PROMPT"])
        finally:
            safe_remove_test_cache("minimax_integration/test_parallel_model_jobs")

    def test_ensure_minimax_mcp_build_uv_env(self):
        from tools.ensure_minimax_mcp import build_uv_env, project_runtime_mcp_exe
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache

        tmp = ensure_test_cache_dir("minimax_integration/test_ensure_minimax_mcp_build_uv_env")
        try:
            env = build_uv_env(tmp)
            self.assertIn("UV_CACHE_DIR", env)
            self.assertIn("UV_TOOL_DIR", env)
            self.assertTrue(env["UV_CACHE_DIR"].replace("\\", "/").endswith(".runtime/uv_cache"))
            self.assertTrue(env["UV_TOOL_DIR"].replace("\\", "/").endswith(".runtime/uv_tools"))
            self.assertTrue(project_runtime_mcp_exe(tmp).replace("\\", "/").endswith(".runtime/uv_tools/minimax-coding-plan-mcp/Scripts/minimax-coding-plan-mcp.exe"))
        finally:
            safe_remove_test_cache("minimax_integration/test_ensure_minimax_mcp_build_uv_env")

    def test_ensure_minimax_mcp_find_exe(self):
        from tools.ensure_minimax_mcp import find_minimax_mcp_exe
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache

        # Test finding when it exists in project .runtime first.
        tmp = ensure_test_cache_dir("minimax_integration/test_ensure_minimax_mcp_find_exe")
        try:
            exe_dir = tmp / ".runtime" / "uv_tools" / "minimax-coding-plan-mcp" / "Scripts"
            exe_dir.mkdir(parents=True, exist_ok=True)
            exe_file = exe_dir / "minimax-coding-plan-mcp.exe"
            exe_file.write_text("dummy", encoding="utf-8")

            found = find_minimax_mcp_exe(root=tmp, temp_dir="X:/does/not/exist")
            self.assertIsNotNone(found)
            self.assertTrue(found.replace("\\", "/").endswith("minimax-coding-plan-mcp.exe"))

            # Test not found when temp_dir has no exe (use different base dir)
            empty_tmp = ensure_test_cache_dir("minimax_integration/test_ensure_minimax_mcp_find_exe_empty")
            try:
                not_found = find_minimax_mcp_exe(root=empty_tmp, temp_dir="X:/does/not/exist", appdata_dir="X:/also/not", localappdata_dir="X:/local/not")
                self.assertIsNone(not_found)
            finally:
                safe_remove_test_cache("minimax_integration/test_ensure_minimax_mcp_find_exe_empty")
        finally:
            safe_remove_test_cache("minimax_integration/test_ensure_minimax_mcp_find_exe")

    def test_ensure_minimax_mcp_build_install_command(self):
        from tools.ensure_minimax_mcp import build_install_command
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache

        tmp = ensure_test_cache_dir("minimax_integration/test_ensure_minimax_mcp_build_install_command")
        try:
            # When venv uv.exe exists - create file (no removal needed, safe_remove_test_cache cleans up)
            venv_dir = tmp / ".venv"
            uv_dir = venv_dir / "Scripts"
            uv_dir.mkdir(parents=True, exist_ok=True)
            uv_file = uv_dir / "uv.exe"
            uv_file.write_text("dummy", encoding="utf-8")

            cmd = build_install_command(str(venv_dir))
            self.assertTrue(cmd[0].replace("\\", "/").endswith("uv.exe"))
            self.assertEqual(cmd[1:], ["tool", "install", "--force", "minimax-coding-plan-mcp"])

            # When venv uv.exe does not exist - use a different venv dir without the file
            venv_no_uv = ensure_test_cache_dir("minimax_integration/test_ensure_minimax_mcp_build_install_command_no_uv")
            try:
                cmd_fallback = build_install_command(str(venv_no_uv))
                self.assertEqual(cmd_fallback, ["uv", "tool", "install", "--force", "minimax-coding-plan-mcp"])
            finally:
                safe_remove_test_cache("minimax_integration/test_ensure_minimax_mcp_build_install_command_no_uv")
        finally:
            safe_remove_test_cache("minimax_integration/test_ensure_minimax_mcp_build_install_command")

    def test_ensure_minimax_mcp_find_exe_from_appdata(self):
        from tools.ensure_minimax_mcp import find_minimax_mcp_exe
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache

        # Create a mock APPDATA dir with the exe
        mock_appdata = ensure_test_cache_dir("minimax_integration/test_ensure_mcp_appdata")
        try:
            exe_dir = mock_appdata / "uv" / "tools" / "minimax-coding-plan-mcp" / "Scripts"
            exe_dir.mkdir(parents=True, exist_ok=True)
            exe_file = exe_dir / "minimax-coding-plan-mcp.exe"
            exe_file.write_text("dummy", encoding="utf-8")

            # Pass empty temp_dir so it won't find in TEMP; should find in appdata_dir
            found = find_minimax_mcp_exe(root="X:/root/not", temp_dir="X:/does/not/exist", appdata_dir=str(mock_appdata))
            self.assertIsNotNone(found)
            self.assertTrue("minimax-coding-plan-mcp.exe" in found)
        finally:
            safe_remove_test_cache("minimax_integration/test_ensure_mcp_appdata")

    def test_ensure_minimax_mcp_find_exe_from_localappdata(self):
        from tools.ensure_minimax_mcp import find_minimax_mcp_exe
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache

        # Create a mock LOCALAPPDATA dir with the exe
        mock_localappdata = ensure_test_cache_dir("minimax_integration/test_ensure_mcp_localappdata")
        try:
            exe_dir = mock_localappdata / "uv" / "tools" / "minimax-coding-plan-mcp" / "Scripts"
            exe_dir.mkdir(parents=True, exist_ok=True)
            exe_file = exe_dir / "minimax-coding-plan-mcp.exe"
            exe_file.write_text("dummy", encoding="utf-8")

            # Pass empty temp_dir and appdata_dir so it falls through to localappdata_dir
            found = find_minimax_mcp_exe(root="X:/root/not", temp_dir="X:/does/not/exist", appdata_dir="X:/also/not", localappdata_dir=str(mock_localappdata))
            self.assertIsNotNone(found)
            self.assertTrue("minimax-coding-plan-mcp.exe" in found)
        finally:
            safe_remove_test_cache("minimax_integration/test_ensure_mcp_localappdata")

    def test_ensure_minimax_mcp_format_success_output(self):
        from tools.ensure_minimax_mcp import format_success_output
        lines = format_success_output("C:\\test\\minimax-coding-plan-mcp.exe")
        self.assertEqual(len(lines), 2)
        self.assertIn("MINIMAX_MCP_READY=1", lines[0])
        self.assertIn("MINIMAX_MCP_COMMAND=C:\\test\\minimax-coding-plan-mcp.exe", lines[1])

    def test_ensure_minimax_mcp_format_failure_output(self):
        from tools.ensure_minimax_mcp import format_failure_output
        lines = format_failure_output("not installed")
        self.assertEqual(len(lines), 2)
        self.assertIn("MINIMAX_MCP_READY=0", lines[0])
        self.assertIn("MINIMAX_MCP_ERROR=not installed", lines[1])

    def test_start_bat_minimax_block_has_single_main_entry(self):
        """Verify 啟動機器人.bat has exactly one main.py, one pause, one ensure_minimax_mcp.py call."""
        import os
        from pathlib import Path

        # Find the bat file at project root (not in tests/)
        project_root = Path(__file__).resolve().parent.parent
        bat_files = list(project_root.glob("啟動機器人.bat"))
        self.assertEqual(len(bat_files), 1, "Expected exactly one 啟動機器人.bat in project root")
        bat_path = bat_files[0]

        text = bat_path.read_text(encoding="utf-8-sig")

        # Verify single main.py and pause
        self.assertEqual(text.count("main.py"), 1, "bat should contain exactly one main.py call")
        self.assertEqual(text.lower().count("pause"), 1, "bat should contain exactly one pause")

        # Verify ensure_minimax_mcp.py appears exactly once
        self.assertEqual(
            text.count("ensure_minimax_mcp.py"), 1,
            "bat should contain exactly one ensure_minimax_mcp.py call"
        )

        # Verify for /f dynamic parsing exists
        self.assertIn("for /f", text.lower(), "bat should contain for /f dynamic parsing")

        # Verify MINIMAX_MCP_COMMAND environment variable is set
        self.assertIn("MINIMAX_MCP_COMMAND", text, "bat should set MINIMAX_MCP_COMMAND")
        self.assertIn("%CD%\\.runtime\\uv_cache", text, "bat should use project-local UV cache")
        self.assertIn("%CD%\\.runtime\\uv_tools", text, "bat should use project-local UV tool dir")

        # Verify no hardcoded username path
        self.assertNotIn("紀成達", text, "bat should not contain hardcoded username")

    def test_health_check_disabled_and_no_api_key(self):
        # API key not present, search disabled
        from research_center.minimax_search_service import MiniMaxSearchService
        from research_center.config import ResearchCenterConfig
        from unittest.mock import patch
        
        config = ResearchCenterConfig(
            api_key="gemini",
            minimax_api_key="",
            enable_minimax_search=False,
        )
        service = MiniMaxSearchService(minimax=None)
        service._config = config
        
        with patch("research_center.minimax_search_service._get_api_key", return_value=""):
            res = service.health_check(run_smoke=False)
            self.assertFalse(res["enabled"])
            self.assertFalse(res["configured"])
            self.assertFalse(res["api_key_present"])
            self.assertEqual(res["status"], "failed")
            self.assertIn("disabled_by_config", res["error_reasons"])

    def test_health_check_api_key_missing_but_enabled(self):
        # API key not present, search enabled
        from research_center.minimax_search_service import MiniMaxSearchService
        from research_center.config import ResearchCenterConfig
        from unittest.mock import patch
        
        config = ResearchCenterConfig(
            api_key="gemini",
            minimax_api_key="",
            enable_minimax_search=True,
        )
        service = MiniMaxSearchService(minimax=None)
        service._config = config
        
        with patch("research_center.minimax_search_service._get_api_key", return_value=""):
            res = service.health_check(run_smoke=False)
            self.assertTrue(res["enabled"])
            self.assertFalse(res["configured"])
            self.assertFalse(res["api_key_present"])
            self.assertEqual(res["status"], "failed")
            self.assertIn("minimax_api_key_missing", res["error_reasons"])

    def test_health_check_mcp_command_not_exists(self):
        # API key present, search enabled, but command missing
        from research_center.minimax_search_service import MiniMaxSearchService
        from research_center.config import ResearchCenterConfig
        from unittest.mock import patch
        
        config = ResearchCenterConfig(
            api_key="gemini",
            minimax_api_key="key",
            enable_minimax_search=True,
        )
        service = MiniMaxSearchService(minimax=None)
        service._config = config
        
        with patch("research_center.minimax_search_service._get_api_key", return_value="key"):
            with patch("research_center.minimax_search_service._build_mcp_startup_params", return_value=("/nonexistent/mcp.exe", [], {})):
                res = service.health_check(run_smoke=False)
                self.assertTrue(res["enabled"])
                self.assertTrue(res["configured"])
                self.assertFalse(res["mcp_command_exists"])
                self.assertEqual(res["status"], "failed")
                self.assertIn("mcp_package_not_installed", res["error_reasons"])

    def test_health_check_smoke_success(self):
        # All ok, run smoke test success
        from research_center.minimax_search_service import MiniMaxSearchService
        from research_center.config import ResearchCenterConfig
        from unittest.mock import patch
        
        config = ResearchCenterConfig(
            api_key="gemini",
            minimax_api_key="key",
            enable_minimax_search=True,
        )
        service = MiniMaxSearchService(minimax=None)
        service._config = config
        
        def mock_search_many(queries, api_key, raw_response_samples):
            return [{"title": "t", "url": "http://u", "snippet": "s", "published_date": ""}], []
            
        service._search_many = mock_search_many
        
        with patch("research_center.minimax_search_service._get_api_key", return_value="key"):
            with patch("research_center.minimax_search_service._build_mcp_startup_params", return_value=("python", [], {})):
                with patch("shutil.which", return_value="python"):
                    res = service.health_check(run_smoke=True)
                    self.assertTrue(res["enabled"])
                    self.assertTrue(res["configured"])
                    self.assertTrue(res["mcp_command_exists"])
                    self.assertEqual(res["status"], "ok")
                    self.assertGreater(res["source_count"], 0)
                    self.assertEqual(res["error_reasons"], [])

    def test_health_check_smoke_failed(self):
        # All ok, run smoke test failed due to empty response
        from research_center.minimax_search_service import MiniMaxSearchService
        from research_center.config import ResearchCenterConfig
        from unittest.mock import patch
        
        config = ResearchCenterConfig(
            api_key="gemini",
            minimax_api_key="key",
            enable_minimax_search=True,
        )
        service = MiniMaxSearchService(minimax=None)
        service._config = config
        
        def mock_search_many(queries, api_key, raw_response_samples):
            return [], ["Some error occurred"]
            
        service._search_many = mock_search_many
        
        with patch("research_center.minimax_search_service._get_api_key", return_value="key"):
            with patch("research_center.minimax_search_service._build_mcp_startup_params", return_value=("python", [], {})):
                with patch("shutil.which", return_value="python"):
                    res = service.health_check(run_smoke=True)
                    self.assertTrue(res["enabled"])
                    self.assertTrue(res["configured"])
                    self.assertTrue(res["mcp_command_exists"])
                    self.assertEqual(res["status"], "failed")
                    self.assertEqual(res["source_count"], 0)
                    self.assertIn("mcp_unknown_error", res["error_reasons"])


if __name__ == "__main__":
    unittest.main()
