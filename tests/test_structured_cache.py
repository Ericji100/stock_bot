"""Tests for research_center/structured_cache.py save/load functionality."""
from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from research_center.structured_cache import (
    CACHE_DIR,
    load_latest_research_structured_cache,
    load_research_structured_cache,
    save_research_structured_cache,
)


class TestSaveAndLoadCache(unittest.TestCase):
    def setUp(self):
        from tests.test_cache_utils import ensure_test_cache_dir
        self._test_cache_dir = ensure_test_cache_dir("structured_cache/test_save_and_load")
        self.cache_dir = self._test_cache_dir / "research_structured"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        from tests.test_cache_utils import safe_remove_test_cache
        safe_remove_test_cache("structured_cache/test_save_and_load")

    def test_save_and_load_roundtrip(self):
        stock_code = "2330"
        report_date = date(2026, 5, 15)
        data = {
            "stock": {"code": "2330", "name": "台積電"},
            "price_data": [{"Date": "2026-05-14", "Close": 800.0}],
            "notes": [],
        }

        with patch("research_center.structured_cache.CACHE_DIR", self.cache_dir):
            path = save_research_structured_cache(stock_code, report_date, data)
            self.assertTrue(path.exists())

            loaded = load_research_structured_cache(stock_code, report_date)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["stock"]["code"], "2330")
            self.assertEqual(loaded["stock"]["name"], "台積電")

    def test_load_nonexistent_returns_none(self):
        with patch("research_center.structured_cache.CACHE_DIR", self.cache_dir):
            result = load_research_structured_cache("9999", date(2026, 5, 15))
            self.assertIsNone(result)

    def test_load_expired_returns_none(self):
        stock_code = "2330"
        report_date = date(2026, 5, 15)
        data = {"stock": {"code": "2330"}}

        with patch("research_center.structured_cache.CACHE_DIR", self.cache_dir):
            # Save, then modify the timestamp to be old
            save_research_structured_cache(stock_code, report_date, data)
            path = self.cache_dir / report_date.strftime("%Y%m%d") / f"{stock_code}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["generated_at"] = (datetime.now() - timedelta(hours=25)).isoformat()
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            result = load_research_structured_cache(stock_code, report_date, max_age_hours=24)
            self.assertIsNone(result)

    def test_load_within_ttl_returns_data(self):
        stock_code = "2330"
        report_date = date(2026, 5, 15)
        data = {"stock": {"code": "2330"}}

        with patch("research_center.structured_cache.CACHE_DIR", self.cache_dir):
            save_research_structured_cache(stock_code, report_date, data)
            result = load_research_structured_cache(stock_code, report_date, max_age_hours=24)
            self.assertIsNotNone(result)
            self.assertEqual(result["stock"]["code"], "2330")

    def test_different_date_separate_cache(self):
        with patch("research_center.structured_cache.CACHE_DIR", self.cache_dir):
            data1 = {"stock": {"code": "2330", "name": "v1"}}
            data2 = {"stock": {"code": "2330", "name": "v2"}}

            save_research_structured_cache("2330", date(2026, 5, 14), data1)
            save_research_structured_cache("2330", date(2026, 5, 15), data2)

            loaded1 = load_research_structured_cache("2330", date(2026, 5, 14))
            loaded2 = load_research_structured_cache("2330", date(2026, 5, 15))

            self.assertEqual(loaded1["stock"]["name"], "v1")
            self.assertEqual(loaded2["stock"]["name"], "v2")

    def test_load_latest_research_structured_cache(self):
        with patch("research_center.structured_cache.CACHE_DIR", self.cache_dir):
            data1 = {"stock": {"code": "2330"}, "report_date": "2026-05-20"}
            data2 = {"stock": {"code": "2330"}, "report_date": "2026-05-22"}

            save_research_structured_cache("2330", date(2026, 5, 20), data1)
            save_research_structured_cache("2330", date(2026, 5, 22), data2)

            result = load_latest_research_structured_cache("2330", before_or_on=date(2026, 5, 25), max_age_days=7)

            self.assertIsNotNone(result)
            loaded, cache_date = result
            self.assertEqual(cache_date, date(2026, 5, 22))
            self.assertEqual(loaded["report_date"], "2026-05-22")


if __name__ == "__main__":
    unittest.main()
