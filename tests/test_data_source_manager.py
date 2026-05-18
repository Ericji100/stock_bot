"""Tests for data_source_manager: SourceHealthManager, FinMindQuotaManager, FugleRateLimiter.

No real network calls. Uses workspace-based test cache under .cache/test_tmp/.
Each test class uses its own isolated test dir via setUpClass/tearDownClass.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import data_source_manager
import importlib

from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache


class _PerClassCache:
    """Routes all cache writes to a workspace test dir under .cache/test_tmp/."""

    @classmethod
    def setup(cls, test_class_name: str):
        # Use a unique subdir per test class so they never interfere
        subdir = f"data_source_manager/{test_class_name}"
        cls._test_cache_dir = ensure_test_cache_dir(subdir)
        # Patch paths BEFORE reload
        data_source_manager._CACHE_DIR = cls._test_cache_dir
        data_source_manager._SOURCE_HEALTH_PATH = cls._test_cache_dir / "source_health.json"
        data_source_manager._FINMIND_QUOTA_PATH = cls._test_cache_dir / "finmind_quota.json"
        data_source_manager._FUGLE_QUOTA_PATH = cls._test_cache_dir / "fugle_quota.json"
        importlib.reload(data_source_manager)
        # Clear class-level _data so fresh instances start clean
        data_source_manager.SourceHealthManager._data = {}
        data_source_manager.FinMindQuotaManager._data = {}
        data_source_manager.FugleRateLimiter._data = {}

    @classmethod
    def teardown(cls):
        # Clear _data first to release Windows file handles
        data_source_manager.SourceHealthManager._data = {}
        data_source_manager.FinMindQuotaManager._data = {}
        data_source_manager.FugleRateLimiter._data = {}
        # Restore original paths
        data_source_manager._CACHE_DIR = Path(".cache")
        data_source_manager._SOURCE_HEALTH_PATH = Path(".cache") / "source_health.json"
        data_source_manager._FINMIND_QUOTA_PATH = Path(".cache") / "finmind_quota.json"
        data_source_manager._FUGLE_QUOTA_PATH = Path(".cache") / "fugle_quota.json"
        importlib.reload(data_source_manager)
        # Safe cleanup of the test cache dir
        subdir = f"data_source_manager/{cls.__name__}"
        safe_remove_test_cache(subdir)


def _fresh(cls):
    """Return a fresh instance with a clean _data dict."""
    importlib.reload(data_source_manager)
    reloaded_cls = getattr(data_source_manager, cls.__name__)
    instance = reloaded_cls()
    instance._data = {}
    # Remove any stale state files in the current test cache dir
    for p in [data_source_manager._SOURCE_HEALTH_PATH,
              data_source_manager._FINMIND_QUOTA_PATH,
              data_source_manager._FUGLE_QUOTA_PATH]:
        if isinstance(p, Path) and p.exists():
            try:
                p.unlink()
            except Exception:
                pass
    return instance


# ---------------------------------------------------------------------------
# SourceHealthManager tests
# ---------------------------------------------------------------------------

class TestSourceHealthManager(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _PerClassCache.setup(cls.__name__)

    @classmethod
    def tearDownClass(cls):
        _PerClassCache.teardown()

    def test_initial_state_is_available(self):
        sh = _fresh(data_source_manager.SourceHealthManager)
        self.assertTrue(sh.is_available("yahoo"))
        self.assertTrue(sh.is_available("fugle"))
        self.assertTrue(sh.is_available("finmind"))
        self.assertEqual(sh.get_cooling_sources(), [])

    def test_single_failure_no_cooldown(self):
        sh = _fresh(data_source_manager.SourceHealthManager)
        sh.record_failure("yahoo", "timeout")
        status = sh.get_status("yahoo")
        self.assertEqual(status["failure_count"], 1)
        self.assertTrue(sh.is_available("yahoo"))

    def test_second_failure_triggers_cooldown(self):
        sh = _fresh(data_source_manager.SourceHealthManager)
        sh.record_failure("fugle", "error1")
        sh.record_failure("fugle", "error2")
        self.assertFalse(sh.is_available("fugle"))
        self.assertIn("fugle", sh.get_cooling_sources())

    def test_success_clears_cooldown(self):
        sh = _fresh(data_source_manager.SourceHealthManager)
        sh.record_failure("finmind", "err")
        sh.record_failure("finmind", "err")
        self.assertFalse(sh.is_available("finmind"))
        sh.record_success("finmind")
        self.assertTrue(sh.is_available("finmind"))
        self.assertEqual(sh.get_status("finmind")["failure_count"], 0)

    def test_fourth_failure_max_cooldown(self):
        sh = _fresh(data_source_manager.SourceHealthManager)
        for i in range(4):
            sh.record_failure("twse_t86", f"err{i}")
        self.assertFalse(sh.is_available("twse_t86"))
        status = sh.get_status("twse_t86")
        self.assertEqual(status["failure_count"], 4)
        self.assertIsNotNone(status["cooldown_until"])


# ---------------------------------------------------------------------------
# FinMindQuotaManager tests
# ---------------------------------------------------------------------------

class TestFinMindQuotaManager(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _PerClassCache.setup(cls.__name__)

    @classmethod
    def tearDownClass(cls):
        _PerClassCache.teardown()

    def test_under_limit_is_allowed(self):
        fm = _fresh(data_source_manager.FinMindQuotaManager)
        for _ in range(450):
            self.assertTrue(fm.can_use(cost=1))
            fm.record_use(cost=1)
        self.assertTrue(fm.can_use(cost=1))

    def test_at_500_is_allowed(self):
        fm = _fresh(data_source_manager.FinMindQuotaManager)
        for _ in range(499):
            fm.record_use(cost=1)
        self.assertTrue(fm.can_use(cost=1))

    def test_over_500_is_rejected(self):
        fm = _fresh(data_source_manager.FinMindQuotaManager)
        for _ in range(500):
            fm.record_use(cost=1)
        self.assertFalse(fm.can_use(cost=1))

    def test_scope_independent(self):
        fm = _fresh(data_source_manager.FinMindQuotaManager)
        for _ in range(300):
            fm.record_use(cost=1, scope="backfill")
        # "default" scope is independent in the data dict key,
        # but global 500/hour limit is shared across all scopes.
        # 300 (backfill) + 200 (default) = 500 → at limit, allowed
        self.assertTrue(fm.can_use(cost=200, scope="default"))
        # 300 (backfill) + 201 (default) = 501 > 500 → rejected
        self.assertFalse(fm.can_use(cost=201, scope="default"))

    def test_hourly_remaining(self):
        fm = _fresh(data_source_manager.FinMindQuotaManager)
        fm.record_use(cost=100)
        self.assertEqual(fm.hourly_remaining(), 400)
        fm.record_use(cost=400)
        self.assertEqual(fm.hourly_remaining(), 0)
        self.assertFalse(fm.can_use(cost=1))

    def test_scope_reset_on_new_hour(self):
        """Scope counters must reset when the hour changes."""
        fm = _fresh(data_source_manager.FinMindQuotaManager)
        # Record 300 uses in backfill scope
        for _ in range(300):
            fm.record_use(cost=1, scope="backfill")
        # Exhaust backfill scope (300 + 200 = 500)
        for _ in range(200):
            fm.record_use(cost=1, scope="backfill")
        self.assertFalse(fm.can_use(cost=1, scope="backfill"))
        # Simulate hour change: set _hour_key to a different (future) hour
        next_hour = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H")
        fm._data["_hour_key"] = next_hour
        fm._persist()
        # Trigger clean by calling can_use (which calls _clean_expired_hour)
        result = fm.can_use(cost=1, scope="backfill")
        self.assertTrue(result)  # Should work because scope counter was reset


# ---------------------------------------------------------------------------
# FugleRateLimiter tests
# ---------------------------------------------------------------------------

class TestFugleRateLimiter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _PerClassCache.setup(cls.__name__)

    @classmethod
    def tearDownClass(cls):
        _PerClassCache.teardown()

    def test_historical_under_limit(self):
        fl = _fresh(data_source_manager.FugleRateLimiter)
        for _ in range(60):
            self.assertTrue(fl.can_use("historical"))
            fl.record_use("historical")

    def test_historical_at_limit(self):
        fl = _fresh(data_source_manager.FugleRateLimiter)
        for _ in range(60):
            fl.record_use("historical")
        self.assertFalse(fl.can_use("historical"))

    def test_historical_over_limit(self):
        fl = _fresh(data_source_manager.FugleRateLimiter)
        for _ in range(60):
            fl.record_use("historical")
        self.assertFalse(fl.can_use("historical", cost=1))

    def test_intraday_independent(self):
        fl = _fresh(data_source_manager.FugleRateLimiter)
        for _ in range(60):
            fl.record_use("historical")
        self.assertFalse(fl.can_use("historical"))
        self.assertTrue(fl.can_use("intraday"))
        for _ in range(60):
            fl.record_use("intraday")
        self.assertFalse(fl.can_use("intraday"))

    def test_websocket_connection_limit(self):
        fl = _fresh(data_source_manager.FugleRateLimiter)
        self.assertTrue(fl.can_use("websocket_connection"))
        fl.record_use("websocket_connection")
        self.assertFalse(fl.can_use("websocket_connection"))

    def test_websocket_subscription_limit(self):
        fl = _fresh(data_source_manager.FugleRateLimiter)
        for _ in range(5):
            self.assertTrue(fl.can_use("websocket_subscription"))
            fl.record_use("websocket_subscription")
        self.assertFalse(fl.can_use("websocket_subscription"))

    def test_remaining_quota(self):
        fl = _fresh(data_source_manager.FugleRateLimiter)
        self.assertEqual(fl.remaining_quota("historical"), 60)
        fl.record_use("historical", cost=10)
        self.assertEqual(fl.remaining_quota("historical"), 50)


if __name__ == "__main__":
    unittest.main()