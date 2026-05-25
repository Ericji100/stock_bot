"""Tests for recent /theme report context persistence."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from research_center.models import CommandRequest, ReportArtifacts, SourceItem
from research_center.theme_report_context import (
    load_recent_theme_report_context,
    save_theme_report_context,
)
from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache


class ThemeReportContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cache_name = "theme_report_context"
        self.cache_dir = ensure_test_cache_dir(self.cache_name)
        self.context_path = self.cache_dir / "recent_theme_reports.json"
        self.path_patcher = patch("research_center.theme_report_context.RECENT_THEME_REPORTS_PATH", self.context_path)
        self.path_patcher.start()

    def tearDown(self) -> None:
        self.path_patcher.stop()
        safe_remove_test_cache(self.cache_name)

    def _theme_request(self) -> CommandRequest:
        return CommandRequest(
            command="theme",
            raw_text="/theme AI伺服器",
            target="AI伺服器",
            theme_scope="AI伺服器",
            target_type="theme",
            ai_model="gemini",
        )

    def _artifacts(self, report_id: str = "theme_test") -> ReportArtifacts:
        return ReportArtifacts(
            report_id=report_id,
            report_type="theme",
            markdown_path=Path("reports/theme_test.md"),
            html_path=Path("reports/theme_test.html"),
            json_path=Path("reports/theme_test.json"),
            sources_path=Path("reports/theme_test.sources.json"),
        )

    def test_save_and_load_theme_report_context(self):
        sources = [
            SourceItem(
                source_id="s1",
                title="AI伺服器液冷供應鏈升溫",
                url="https://example.com/ai-server",
                source_level="L2_media",
                snippet="液冷、電源、伺服器代工需求增加",
                provider="test",
            )
        ]
        structured_data = {
            "topic_context": {
                "matched_topics": [
                    {"theme_id": "ai_server", "theme_name": "AI伺服器", "keywords": ["液冷", "電源"]}
                ]
            }
        }

        result = save_theme_report_context(self._theme_request(), "AI伺服器摘要", sources, structured_data, self._artifacts())
        loaded = load_recent_theme_report_context("AI伺服器")

        self.assertTrue(result["saved"])
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["theme"], "AI伺服器")
        self.assertIn("液冷", loaded[0]["suggested_search_terms"])
        self.assertEqual(loaded[0]["sources"][0]["title"], "AI伺服器液冷供應鏈升溫")

    def test_non_theme_command_is_not_saved(self):
        request = CommandRequest(command="research", raw_text="/research 2330", target="2330")
        result = save_theme_report_context(request, "summary", [], {}, self._artifacts())

        self.assertFalse(result["saved"])
        self.assertFalse(self.context_path.exists())

    def test_focus_filter_falls_back_to_recent_records(self):
        save_theme_report_context(self._theme_request(), "AI摘要", [], {}, self._artifacts("r1"))
        other = CommandRequest(
            command="theme",
            raw_text="/theme 金融股",
            target="金融股",
            theme_scope="金融股",
            target_type="theme",
        )
        save_theme_report_context(other, "金融摘要", [], {}, self._artifacts("r2"))

        loaded = load_recent_theme_report_context("不存在的題材", limit=2)

        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["theme"], "金融股")


if __name__ == "__main__":
    unittest.main()
