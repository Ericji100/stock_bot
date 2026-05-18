"""Tests for finmind_client.py: FinMindClient with health and quota integration.

No real network calls. Uses unittest.mock to mock HTTP.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root))


class TestFinMindClient(unittest.TestCase):
    @patch("httpx.Client")
    def test_no_key_returns_empty_dict(self, mock_client_cls):
        """When no API key is available, request_dataset returns {} without sending HTTP."""
        from finmind_client import FinMindClient
        # Pass api_key=None and patch _load_api_key to return None
        with patch("finmind_client._load_api_key", return_value=None):
            client = FinMindClient(api_key=None)
            result = client.request_dataset("TaiwanStockInstitutionalInvestorsBuySell", {"stock_id": "2330"})
            self.assertEqual(result, {})
            mock_client_cls.assert_not_called()

    @patch("httpx.Client")
    def test_quota_exceeded_returns_empty(self, mock_client_cls):
        """When FinMindQuotaManager.can_use returns False, no HTTP request is sent."""
        from finmind_client import FinMindClient

        fake_quota = MagicMock()
        fake_quota.can_use.return_value = False

        client = FinMindClient(api_key="fake_key", health_manager=None, quota_manager=fake_quota)
        result = client.request_dataset("TaiwanStockInstitutionalInvestorsBuySell", {"stock_id": "2330"})
        self.assertEqual(result, {})
        mock_client_cls.assert_not_called()

    @patch("httpx.Client")
    def test_success_records_quota_and_health(self, mock_client_cls):
        """HTTP success → record_use and record_success are called."""
        from finmind_client import FinMindClient

        mock_response = MagicMock()
        mock_response.json.return_value = {"status": 200, "data": [{"date": "2026-05-15", "name": "Foreign_Investor", "buy": 1000, "sell": 500}]}
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__.return_value = mock_client
        mock_client_cls.return_value = mock_client

        fake_quota = MagicMock()
        fake_quota.can_use.return_value = True

        fake_health = MagicMock()
        fake_health.is_available.return_value = True

        client = FinMindClient(api_key="fake_key", health_manager=fake_health, quota_manager=fake_quota)
        result = client.request_dataset("TaiwanStockInstitutionalInvestorsBuySell", {"stock_id": "2330"})

        fake_quota.record_use.assert_called_once_with(cost=1, scope="default")
        fake_health.record_success.assert_called_once_with("finmind")

    @patch("httpx.Client")
    def test_http_failure_records_health(self, mock_client_cls):
        """HTTP exception → record_failure is called and exception re-raised."""
        from finmind_client import FinMindClient

        mock_client = MagicMock()
        mock_client.get.side_effect = Exception("network error")
        mock_client.__enter__.return_value = mock_client
        mock_client_cls.return_value = mock_client

        fake_quota = MagicMock()
        fake_quota.can_use.return_value = True

        fake_health = MagicMock()
        fake_health.is_available.return_value = True

        client = FinMindClient(api_key="fake_key", health_manager=fake_health, quota_manager=fake_quota)
        with self.assertRaises(Exception):
            client.request_dataset("TaiwanStockInstitutionalInvestorsBuySell", {"stock_id": "2330"})

        fake_health.record_failure.assert_called_once()
        call_args = fake_health.record_failure.call_args
        # Error string is passed as second positional arg (the error message)
        self.assertEqual(call_args[0][0], "finmind")
        # Verify the error message is the actual exception string, not "<class 'Exception'>"
        self.assertEqual(call_args[0][1], "network error")

    @patch("httpx.Client")
    def test_runtime_error_message_recorded(self, mock_client_cls):
        """RuntimeError → record_failure receives 'boom' not 'RuntimeError' or str(Exception)."""
        from finmind_client import FinMindClient

        mock_client = MagicMock()
        mock_client.get.side_effect = RuntimeError("boom")
        mock_client.__enter__.return_value = mock_client
        mock_client_cls.return_value = mock_client

        fake_health = MagicMock()
        fake_health.is_available.return_value = True

        client = FinMindClient(api_key="fake_key", health_manager=fake_health, quota_manager=None)
        with self.assertRaises(RuntimeError):
            client.request_dataset("TaiwanStockInstitutionalInvestorsBuySell", {"stock_id": "2330"})

        fake_health.record_failure.assert_called_once()
        call_args = fake_health.record_failure.call_args
        self.assertEqual(call_args[0][1], "boom")

    @patch("httpx.Client")
    def test_key_not_in_logs(self, mock_client_cls):
        """Ensure the API key does NOT appear in any logged output."""
        import io
        from finmind_client import FinMindClient

        real_key = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.test_signature"
        log_capture = io.StringIO()

        fake_quota = MagicMock()
        fake_quota.can_use.return_value = True

        fake_health = MagicMock()
        fake_health.is_available.return_value = True

        mock_response = MagicMock()
        mock_response.json.return_value = {"status": 200, "data": []}
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__.return_value = mock_client
        mock_client_cls.return_value = mock_client

        client = FinMindClient(api_key=real_key, health_manager=fake_health, quota_manager=fake_quota)
        with patch("sys.stdout", log_capture):
            client.request_dataset("TaiwanStockInstitutionalInvestorsBuySell", {"stock_id": "2330"})

        logged = log_capture.getvalue()
        self.assertNotIn(real_key, logged)
        self.assertNotIn("test_signature", logged)


if __name__ == "__main__":
    unittest.main()