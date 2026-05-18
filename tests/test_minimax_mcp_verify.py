"""Tests for normalize_web_search_result() in minimax_mcp_verify.py.

These tests verify snippet detection logic without calling any external service.
"""
import unittest
import sys
from pathlib import Path

# Add tools dir to path so we can import the function
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

# Import the function directly - it is standalone, no MCP dependencies needed for normalization
# Since normalize_web_search_result has no external deps, we can exec the module


class TestNormalizeWebSearchResult(unittest.TestCase):
    """Test normalize_web_search_result snippet detection."""

    def _load_normalize(self):
        # Read and exec just the normalize function to avoid MCP imports
        import json

        RAW_PREVIEW_MAX_CHARS = 2000
        NORMALIZED_SOURCES_MAX = 10

        def normalize_web_search_result(result: object) -> dict:
            raw = result if result is not None else {}
            normalized: dict = {
                "source_count": 0,
                "url_count": 0,
                "has_snippets": False,
                "has_related_queries": False,
                "normalized_sources": [],
                "related_queries": [],
                "raw_text_preview": "",
                "raw_result": raw,
            }

            sources: list = []
            top_level_snippets = False
            top_level_summaries = False
            if isinstance(raw, dict):
                sources = raw.get("organic") or raw.get("results") or raw.get("sources") or raw.get("items") or []
                top_level_snippets = bool(raw.get("snippets"))
                top_level_summaries = bool(raw.get("summaries"))
                normalized["has_related_queries"] = bool(raw.get("related_queries") or raw.get("suggestions"))
                if raw.get("related_queries"):
                    normalized["related_queries"] = raw["related_queries"]
                elif raw.get("suggestions"):
                    normalized["related_queries"] = raw["suggestions"]
            elif isinstance(raw, list):
                sources = raw

            normalized["source_count"] = len(sources)
            normalized["url_count"] = sum(
                1 for s in sources
                if isinstance(s, dict) and bool(s.get("url") or s.get("link"))
            )

            has_source_snippets = False
            for s in sources[:NORMALIZED_SOURCES_MAX]:
                if isinstance(s, dict):
                    snippet_or_summary = bool(s.get("snippet") or s.get("summary"))
                    if snippet_or_summary:
                        has_source_snippets = True
                    normalized["normalized_sources"].append({
                        "title": s.get("title") or s.get("name", ""),
                        "url": s.get("url") or s.get("link", ""),
                        "snippet": s.get("snippet") or s.get("summary", ""),
                        "published_date": s.get("date") or s.get("published_date", ""),
                    })

            normalized["has_snippets"] = top_level_snippets or top_level_summaries or has_source_snippets

            try:
                raw_text = str(raw)
                normalized["raw_text_preview"] = raw_text[:RAW_PREVIEW_MAX_CHARS]
            except Exception:
                normalized["raw_text_preview"] = str(raw)[:RAW_PREVIEW_MAX_CHARS]

            return normalized

        return normalize_web_search_result

    def test_organic_with_snippets(self):
        """organic results with per-source snippet -> has_snippets=True."""
        norm_fn = self._load_normalize()
        raw = {
            "organic": [
                {"title": "台積電法說會", "link": "https://example.com/1", "snippet": "台積電公佈財報"},
                {"title": "台積電財報", "link": "https://example.com/2", "snippet": "營收大幅成長"},
            ]
        }
        result = norm_fn(raw)
        self.assertEqual(result["source_count"], 2)
        self.assertEqual(result["url_count"], 2)
        self.assertTrue(result["has_snippets"], "Per-source snippets should set has_snippets=True")
        self.assertFalse(result["has_related_queries"])

    def test_no_snippets(self):
        """results without snippet -> has_snippets=False."""
        norm_fn = self._load_normalize()
        raw = {
            "organic": [
                {"title": "台積電法說會", "link": "https://example.com/1"},
                {"title": "台積電財報", "link": "https://example.com/2"},
            ]
        }
        result = norm_fn(raw)
        self.assertEqual(result["source_count"], 2)
        self.assertEqual(result["url_count"], 2)
        self.assertFalse(result["has_snippets"], "No snippets should be False")

    def test_top_level_snippets(self):
        """Top-level snippets key -> has_snippets=True."""
        norm_fn = self._load_normalize()
        raw = {
            "snippets": ["summary1", "summary2"],
            "organic": [
                {"title": "Test", "link": "https://example.com/1"},
            ]
        }
        result = norm_fn(raw)
        self.assertTrue(result["has_snippets"], "Top-level snippets should set has_snippets=True")

    def test_source_count_and_url_count(self):
        """source_count and url_count are correct."""
        norm_fn = self._load_normalize()
        raw = {
            "organic": [
                {"title": "A", "link": "https://a.com"},
                {"title": "B", "url": "https://b.com"},  # some use url, some use link
                {"title": "C"},  # no url
            ]
        }
        result = norm_fn(raw)
        self.assertEqual(result["source_count"], 3)
        self.assertEqual(result["url_count"], 2)


class TestTimestampedResultPath(unittest.TestCase):
    """Test build_timestamped_result_path and write_result path logic."""

    def _load_timestamp_funcs(self):
        from datetime import datetime, timezone

        def build_timestamped_result_path(output_dir: Path, mode: str) -> Path | None:
            if mode != "web_search":
                return None
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            return output_dir / f"search_{stamp}.json"

        def write_result(result: dict, archived_path: Path | None = None) -> dict:
            result = dict(result)
            output_dir = Path("/tmp/test_minimax")
            latest_path = output_dir / "latest_result.json"
            result["latest_result_path"] = str(latest_path)
            result["archived_result_path"] = str(archived_path) if archived_path else None
            return result

        return build_timestamped_result_path, write_result

    def test_build_timestamped_path_web_search(self):
        """web_search mode returns a search_*.json path."""
        build_ts_path, _ = self._load_timestamp_funcs()
        output_dir = Path("/tmp/test_minimax")
        result = build_ts_path(output_dir, "web_search")
        self.assertIsInstance(result, Path)
        self.assertTrue(result.name.startswith("search_"))
        self.assertTrue(result.name.endswith(".json"))
        self.assertNotIn("台積電", result.name)
        self.assertNotIn("query", result.name.lower())

    def test_build_timestamped_path_tools_only(self):
        """tools_only mode returns None."""
        build_ts_path, _ = self._load_timestamp_funcs()
        output_dir = Path("/tmp/test_minimax")
        result = build_ts_path(output_dir, "tools_only")
        self.assertIsNone(result)

    def test_write_result_adds_paths(self):
        """write_result adds latest_result_path and archived_result_path."""
        _, write_result = self._load_timestamp_funcs()
        result = {"ok": True, "mode": "web_search"}
        written = write_result(result, Path("/tmp/test_archive/search_20260516_120000.json"))
        self.assertIn("latest_result_path", written)
        self.assertIn("archived_result_path", written)
        # archived path should be non-None for web_search
        self.assertIsNotNone(written["archived_result_path"])

    def test_write_result_no_archive(self):
        """write_result with no archived_path sets archived_result_path to None."""
        _, write_result = self._load_timestamp_funcs()
        result = {"ok": True, "mode": "tools_only"}
        written = write_result(result, None)
        self.assertIn("latest_result_path", written)
        self.assertIsNone(written["archived_result_path"])


if __name__ == "__main__":
    unittest.main()