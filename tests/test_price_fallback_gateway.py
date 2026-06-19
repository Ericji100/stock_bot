from __future__ import annotations

import unittest
from dataclasses import dataclass
from unittest.mock import patch

from research_center.data_source_gateway import build_data_source_gateway_snapshot, format_data_source_gateway_snapshot
from research_center.price_fallbacks import load_price_metrics_with_fallback


@dataclass
class _Entry:
    code: str
    name: str
    symbol: str
    market: str = "TWSE"
    industry: str = ""


class PriceFallbackGatewayTests(unittest.TestCase):
    def test_primary_policy_contains_gateway_attempts(self):
        universe = [_Entry(code="2330", name="台積電", symbol="2330.TW")]
        with patch("research_center.price_fallbacks.load_price_metrics", return_value={"2330.TW": {"price": 100}}):
            metrics, policy = load_price_metrics_with_fallback(universe)

        self.assertIn("2330.TW", metrics)
        self.assertEqual(policy["status"], "primary_complete")
        self.assertEqual(policy["gateway_attempts"][0]["provider"], "stock_scanner.load_price_metrics")
        self.assertEqual(policy["gateway_attempts"][0]["status"], "success")

    def test_primary_failure_is_classified_in_gateway_attempts(self):
        universe = [_Entry(code="2330", name="台積電", symbol="2330.TW")]
        with patch("research_center.price_fallbacks.load_price_metrics", side_effect=RuntimeError("HTTP 429 quota exceeded")):
            metrics, policy = load_price_metrics_with_fallback(universe, fallback_limit=0)

        self.assertEqual(metrics, {})
        self.assertEqual(policy["gateway_attempts"][0]["status"], "failed")
        self.assertEqual(policy["gateway_attempts"][0]["error"]["error_type"], "quota_exhausted")


class DataSourceGatewaySnapshotTests(unittest.TestCase):
    def test_gateway_snapshot_collects_source_health_and_quota(self):
        snapshot = build_data_source_gateway_snapshot(
            source_names=("yahoo", "finmind"),
            health_manager=_FakeHealthManager(),
            finmind_quota=_FakeFinMindQuota(),
            fugle_limiter=_FakeFugleLimiter(),
        )

        self.assertEqual(snapshot["schema_version"], "data_source_gateway_v1")
        self.assertEqual(snapshot["source_count"], 2)
        self.assertEqual(snapshot["sources"]["yahoo"]["available"], True)
        self.assertEqual(snapshot["sources"]["finmind"]["available"], False)
        self.assertEqual(snapshot["cooling_sources"], ["finmind"])
        self.assertEqual(snapshot["available_sources"], ["yahoo"])
        self.assertEqual(snapshot["quota"]["finmind_hourly_remaining"], 321)
        self.assertEqual(snapshot["quota"]["fugle_historical_remaining"], 45)
        self.assertEqual(snapshot["quota"]["fugle_intraday_remaining"], 55)

    def test_format_gateway_snapshot_reports_summary(self):
        snapshot = build_data_source_gateway_snapshot(
            source_names=("yahoo",),
            health_manager=_FakeHealthManager(),
            finmind_quota=_FakeFinMindQuota(),
            fugle_limiter=_FakeFugleLimiter(),
        )

        text = format_data_source_gateway_snapshot(snapshot)

        self.assertIn("資料來源閘道", text)
        self.assertIn("來源數：1", text)
        self.assertIn("可用來源：yahoo", text)
        self.assertIn("FinMind 每小時剩餘：321", text)


class _FakeHealthManager:
    def get_status(self, source):
        return {
            "failure_count": 2 if source == "finmind" else 0,
            "cooldown_until": "2099-01-01T00:00:00" if source == "finmind" else None,
        }

    def is_available(self, source):
        return source != "finmind"

    def get_cooling_sources(self):
        return ["finmind"]


class _FakeFinMindQuota:
    def hourly_remaining(self):
        return 321

    def remaining_safe_quota(self, scope="default"):
        return {"default": 300, "backfill": 200, "scan": 80, "research": 20}.get(scope, 0)


class _FakeFugleLimiter:
    def remaining_quota(self, endpoint_type="historical"):
        return {"historical": 45, "intraday": 55}.get(endpoint_type, 0)


if __name__ == "__main__":
    unittest.main()
