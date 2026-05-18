"""Tests for fugle_data.py: Fugle single-count enforcement.

Verifies that calling fetch_fugle_history records exactly one Fugle historical
usage (not double-counting between fugle_data.py and caller).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import importlib
import data_source_manager

from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache


class _TempCache:
    """Route all cache writes to .cache/test_tmp/fugle_data/ to avoid Windows permission issues."""

    @classmethod
    def setup(cls):
        cls._test_cache_dir = ensure_test_cache_dir("fugle_data")
        # Patch paths BEFORE reload
        data_source_manager._CACHE_DIR = cls._test_cache_dir
        data_source_manager._SOURCE_HEALTH_PATH = cls._test_cache_dir / "source_health.json"
        data_source_manager._FINMIND_QUOTA_PATH = cls._test_cache_dir / "finmind_quota.json"
        data_source_manager._FUGLE_QUOTA_PATH = cls._test_cache_dir / "fugle_quota.json"
        importlib.reload(data_source_manager)
        data_source_manager.FugleRateLimiter._data = {}
        data_source_manager.SourceHealthManager._data = {}

    @classmethod
    def teardown(cls):
        # Clear _data first to release Windows file handles
        data_source_manager.FugleRateLimiter._data = {}
        data_source_manager.SourceHealthManager._data = {}
        # Restore original paths
        data_source_manager._CACHE_DIR = Path(".cache")
        data_source_manager._SOURCE_HEALTH_PATH = Path(".cache") / "source_health.json"
        data_source_manager._FINMIND_QUOTA_PATH = Path(".cache") / "finmind_quota.json"
        data_source_manager._FUGLE_QUOTA_PATH = Path(".cache") / "fugle_quota.json"
        importlib.reload(data_source_manager)
        # Safe cleanup of test cache dir
        safe_remove_test_cache("fugle_data")


class TestFugleSingleCount(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _TempCache.setup()

    @classmethod
    def tearDownClass(cls):
        _TempCache.teardown()

    @patch("httpx.Client")
    def test_fetch_fugle_history_records_once(self, mock_client_cls):
        """One successful fetch_fugle_history call must increase historical count by exactly 1."""
        import fugle_data
        importlib.reload(fugle_data)
        # Reset the module-level limiter/health AND block file persistence
        fugle_data._FUGLE_LIMITER._data = {}
        fugle_data._FUGLE_HEALTH._data = {}
        fugle_data._FUGLE_LIMITER._persist = lambda: None
        fugle_data._FUGLE_HEALTH._persist = lambda: None

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {"date": "2026-05-15", "open": 100, "high": 105, "low": 99, "close": 103, "volume": 1000}
            ]
        }
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__.return_value = mock_client
        mock_client_cls.return_value = mock_client

        with patch.object(fugle_data, "get_fugle_api_key", return_value="fake_key"):
            from datetime import date
            result = fugle_data.fetch_fugle_history(
                "2330", date(2026, 5, 10), date(2026, 5, 15), "1d"
            )

        self.assertFalse(result.empty)
        self.assertEqual(
            fugle_data._FUGLE_LIMITER._data.get("minute_historical", 0),
            1,
            "Historical count must increase by exactly 1 per fetch",
        )

    @patch("httpx.Client")
    def test_caller_must_not_double_record(self, mock_client_cls):
        """Verify the old pattern (caller ALSO records) would double-count."""
        import fugle_data
        importlib.reload(fugle_data)
        fugle_data._FUGLE_LIMITER._data = {}
        fugle_data._FUGLE_HEALTH._data = {}
        fugle_data._FUGLE_LIMITER._persist = lambda: None
        fugle_data._FUGLE_HEALTH._persist = lambda: None

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {"date": "2026-05-15", "open": 100, "high": 105, "low": 99, "close": 103, "volume": 1000}
            ]
        }
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__.return_value = mock_client
        mock_client_cls.return_value = mock_client

        with patch.object(fugle_data, "get_fugle_api_key", return_value="fake_key"):
            from datetime import date
            fugle_data.fetch_fugle_history("2330", date(2026, 5, 10), date(2026, 5, 15), "1d")
            # If caller mistakenly ALSO records, count would be 2
            fugle_data._FUGLE_LIMITER.record_use("historical")

        self.assertEqual(
            fugle_data._FUGLE_LIMITER._data.get("minute_historical", 0),
            2,
            "Double record_use would cause count=2",
        )


if __name__ == "__main__":
    unittest.main()