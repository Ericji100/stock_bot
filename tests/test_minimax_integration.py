from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_center.command_parser import parse_command_text
from research_center.config import ResearchCenterConfig, load_research_config
from research_center.gemini_service import GeminiResult, GeminiService
from research_center.minimax_search_service import MiniMaxSearchService
from research_center.minimax_service import MiniMaxResult, MiniMaxService, _extract_minimax_text
from research_center.models import ReportArtifacts, ResearchCenterResult, SourceItem
from research_center.orchestrator import ResearchCenter
from research_center.report_builder import write_report_artifacts


class MiniMaxIntegrationTests(unittest.TestCase):
    def test_config_loads_minimax_and_search_keys(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "research_center.json").write_text(
                json.dumps(
                    {
                        "enable_minimax_search": True,
                        "enable_minimax_comparison": True,
                        "minimax_model": "MiniMax-M2.7",
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
        self.assertEqual(config.minimax_model, "MiniMax-M2.7")
        self.assertEqual(config.minimax_api_key, "mini")
        self.assertEqual(config.serper_api_key, "serper")
        self.assertTrue(config.enable_minimax_search)
        self.assertTrue(config.enable_minimax_comparison)


    def test_config_defaults_gemini_pro_with_flash_fallback(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "research_center.json").write_text("{}", encoding="utf-8")
            (root / "config" / "secrets.json").write_text(json.dumps({"gemini_api_key": "gemini"}), encoding="utf-8")
            config = load_research_config(root)
        self.assertEqual(config.model, "gemini-3-pro-preview")
        self.assertEqual(config.fallback_models, ("gemini-3-flash-preview",))

    def test_gemini_service_keeps_flash_fallback_chain(self):
        service = GeminiService(
            "key",
            "gemini-3-pro-preview",
            fallback_models=("gemini-3-flash-preview", "gemini-3-pro-preview", ""),
        )
        self.assertEqual(service.model, "gemini-3-pro-preview")
        self.assertEqual(service.fallback_models, ("gemini-3-flash-preview",))
    def test_minimax_default_timeout_is_long_for_full_prompt_comparison(self):
        service = MiniMaxService("key")
        self.assertEqual(service.timeout_seconds, 1200.0)

    def test_minimax_extract_text_strips_thinking_block(self):
        text = _extract_minimax_text({"choices": [{"message": {"content": "<think>hidden</think>\n# Report"}}]})
        self.assertEqual(text, "# Report")

    def test_minimax_search_discover_builds_sources_without_network(self):
        service = MiniMaxSearchService("serper", "jina", minimax=None)
        service._serper_search = lambda query: [  # type: ignore[method-assign]
            {"title": "TWSE", "url": "https://www.twse.com.tw/test", "snippet": "official", "published_date": None}
        ]
        service._read_with_jina = lambda url: "official content"  # type: ignore[method-assign]
        request = parse_command_text("/research 2330")
        result = service.discover(request, [{"label": "official", "queries": ["2330 TWSE"], "objective": "official data"}])
        self.assertGreaterEqual(len(result.sources), 1)
        self.assertEqual(result.sources[0].source_level, "Level 1")
        self.assertIn("MiniMax Search", result.sources[0].snippet)
        self.assertGreaterEqual(result.diagnostics["source_count"], 1)

    def test_comparison_report_filename_uses_minimax_variant(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            request = parse_command_text("/research 2330 --no-html --no-json")
            artifacts, report_json = write_report_artifacts(
                Path(tmp),
                request,
                "# MiniMax Report\n\n## 摘要\n測試",
                "測試",
                [],
                True,
                None,
                {"analysis_model": "MiniMax-M2.7"},
                report_variant="minimax",
            )
        self.assertIn("_minimax_", artifacts.report_id)
        self.assertEqual(report_json["report_variant"], "minimax")
        self.assertEqual(report_json["metadata"]["analysis_model"], "MiniMax-M2.7")



    def test_parallel_model_jobs_use_same_prompt_and_write_reports(self):
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

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
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


if __name__ == "__main__":
    unittest.main()





