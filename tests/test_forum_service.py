"""Tests for forum_service changes: Serper fallback removal and unified backup message."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from research_center.forum_service import (
    ForumFetchResult,
    _collect_cmoney,
    _collect_dcard,
    _collect_mobile01,
    _collect_ptt_stock,
    fetch_forum_sources,
)


def _make_mock(name, side_effect=None, return_value=None):
    """Create a mock function with a __name__ attribute."""
    if side_effect is not None:
        mock = MagicMock(side_effect=side_effect)
    else:
        mock = MagicMock(return_value=return_value if return_value is not None else [])
    mock.__name__ = name
    return mock


class TestForumSerperFallbackRemoved(unittest.TestCase):
    """Verify that collect_forum_sources no longer calls Serper fallback
    and produces the correct unified backup message when all collectors fail."""

    def test_all_collectors_fail_produces_unified_message(self):
        """All four collectors fail → unified Serper/Jina stopped message."""
        collectors = (
            _make_mock("_collect_ptt_stock", side_effect=Exception("WinError 10054")),
            _make_mock("_collect_dcard", side_effect=Exception("Connection refused")),
            _make_mock("_collect_mobile01", side_effect=Exception("403 Forbidden")),
            _make_mock("_collect_cmoney", side_effect=Exception("403 Forbidden")),
        )
        with patch("research_center.forum_service._collect_ptt_stock", collectors[0]), \
             patch("research_center.forum_service._collect_dcard", collectors[1]), \
             patch("research_center.forum_service._collect_mobile01", collectors[2]), \
             patch("research_center.forum_service._collect_cmoney", collectors[3]):
            result = fetch_forum_sources("5425 台半", deep=True, progress=None)

        # Check no sources were returned
        self.assertEqual(len(result.sources), 0)

        # Check failure count
        self.assertEqual(result.failure_count, 4)

        # Check that messages do NOT contain the old Serper fallback text
        joined = "\n".join(result.notes)
        self.assertNotIn("Serper API Key 未設定", joined)
        self.assertNotIn("搜尋 fallback 略過", joined)

        # Check that messages DO contain the new unified backup message
        self.assertIn("Serper/Jina 已停用", joined)
        self.assertIn("Tavily Search", joined)
        self.assertIn("Gemini Search fallback", joined)

    def test_partial_failure_produces_partial_message(self):
        """Some collectors succeed, some fail → partial failure message."""
        collectors = (
            _make_mock("_collect_ptt_stock", return_value=[]),
            _make_mock("_collect_dcard", return_value=[{"title": "Dcard post", "url": "https://dcard.tw/p/123", "snippet": "test"}]),
            _make_mock("_collect_mobile01", side_effect=Exception("403")),
            _make_mock("_collect_cmoney", side_effect=Exception("SSL error")),
        )
        with patch("research_center.forum_service._collect_ptt_stock", collectors[0]), \
             patch("research_center.forum_service._collect_dcard", collectors[1]), \
             patch("research_center.forum_service._collect_mobile01", collectors[2]), \
             patch("research_center.forum_service._collect_cmoney", collectors[3]):
            result = fetch_forum_sources("5425 台半", deep=False, progress=None)

        # Dcard succeeds, PTT succeeds (0 results is not failure), Mobile01 and CMoney fail
        self.assertGreaterEqual(len(result.sources), 1)
        self.assertEqual(result.failure_count, 2)

        joined = "\n".join(result.notes)
        # Should have partial failure message
        self.assertIn("部分論壇直連失敗", joined)

    def test_all_succeed_no_failure_message(self):
        """All collectors succeed (even with 0 results) → no backup message."""
        collectors = (
            _make_mock("_collect_ptt_stock", return_value=[]),
            _make_mock("_collect_dcard", return_value=[]),
            _make_mock("_collect_mobile01", return_value=[]),
            _make_mock("_collect_cmoney", return_value=[]),
        )
        with patch("research_center.forum_service._collect_ptt_stock", collectors[0]), \
             patch("research_center.forum_service._collect_dcard", collectors[1]), \
             patch("research_center.forum_service._collect_mobile01", collectors[2]), \
             patch("research_center.forum_service._collect_cmoney", collectors[3]):
            result = fetch_forum_sources("5425 台半", deep=False, progress=None)

        # All collectors return empty but don't raise exceptions
        self.assertEqual(result.failure_count, 0)

        joined = "\n".join(result.notes)
        # No failure message should appear
        self.assertNotIn("Serper/Jina 已停用", joined)
        self.assertNotIn("部分論壇直連失敗", joined)

    def test_progress_messages_include_failure_count(self):
        """Progress callback should receive completion message with failure count."""
        collectors = (
            _make_mock("_collect_ptt_stock", side_effect=Exception("timeout")),
            _make_mock("_collect_dcard", side_effect=Exception("timeout")),
            _make_mock("_collect_mobile01", side_effect=Exception("timeout")),
            _make_mock("_collect_cmoney", side_effect=Exception("timeout")),
        )
        progress_messages = []
        def capture_progress(msg):
            progress_messages.append(msg)

        with patch("research_center.forum_service._collect_ptt_stock", collectors[0]), \
             patch("research_center.forum_service._collect_dcard", collectors[1]), \
             patch("research_center.forum_service._collect_mobile01", collectors[2]), \
             patch("research_center.forum_service._collect_cmoney", collectors[3]):
            result = fetch_forum_sources("2330 台積電", deep=True, progress=capture_progress)

        # Should see completion message with failure count
        completion_msgs = [m for m in progress_messages if "論壇來源搜尋完成" in m]
        self.assertTrue(any("失敗 4 筆" in m for m in completion_msgs),
                        f"Expected failure count in progress messages, got: {completion_msgs}")


class TestForumFetchResultDataclass(unittest.TestCase):
    """Test that ForumFetchResult has failure_count field with default."""

    def test_default_failure_count(self):
        result = ForumFetchResult(sources=[], notes=["test"])
        self.assertEqual(result.failure_count, 0)

    def test_explicit_failure_count(self):
        result = ForumFetchResult(sources=[], notes=["test"], failure_count=3)
        self.assertEqual(result.failure_count, 3)


if __name__ == "__main__":
    unittest.main()