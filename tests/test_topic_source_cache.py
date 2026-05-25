from __future__ import annotations

import unittest

from research_center.topic_source_cache import load_json_cache, save_json_cache
from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache


class TopicSourceCacheTests(unittest.TestCase):
    def tearDown(self):
        safe_remove_test_cache("topic_source_cache")

    def test_load_json_cache_missing_returns_default(self):
        cache_dir = ensure_test_cache_dir("topic_source_cache/missing")
        path = cache_dir / "missing.json"
        default = {"source": "test", "items": []}

        self.assertEqual(load_json_cache(path, default), default)

    def test_load_json_cache_invalid_returns_default(self):
        cache_dir = ensure_test_cache_dir("topic_source_cache/invalid")
        path = cache_dir / "bad.json"
        path.write_text("not-json", encoding="utf-8")
        default = {"source": "test", "items": []}

        self.assertEqual(load_json_cache(path, default), default)

    def test_save_json_cache_adds_updated_at(self):
        cache_dir = ensure_test_cache_dir("topic_source_cache/save")
        path = cache_dir / "cache.json"

        save_json_cache(path, {"source": "test", "items": [{"name": "AI"}]})
        loaded = load_json_cache(path, {})

        self.assertEqual(loaded["source"], "test")
        self.assertEqual(loaded["items"][0]["name"], "AI")
        self.assertTrue(loaded.get("updated_at"))


if __name__ == "__main__":
    unittest.main()
