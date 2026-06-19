from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from research_center.command_runtime_service import CommandRuntimeService
from research_center.resource_guard_service import ResourceGuardService
from research_center.system_health_service import (
    SYSTEM_HEALTH_SCHEMA_VERSION,
    build_system_health_snapshot,
    format_system_health_snapshot,
)
from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache


class SystemHealthServiceTests(unittest.TestCase):
    def tearDown(self):
        safe_remove_test_cache("system_health_service")

    def test_build_snapshot_counts_runtime_and_artifacts(self):
        runtime = CommandRuntimeService()
        resource_guard = ResourceGuardService({"background_backfill": 1})
        runtime.start_task("t1", label="unit", task_type="test")
        root = ensure_test_cache_dir("system_health_service/registry")
        report_dir = root / "report_json"
        report_dir.mkdir(parents=True)
        (report_dir / "sample.json").write_text(json.dumps({"ok": True}), encoding="utf-8")

        with patch(
            "research_center.system_health_service._data_source_health",
            return_value={"sources": {}, "cooling_sources": [], "quota": {"finmind_hourly_remaining": 500, "fugle_historical_remaining": 60}},
        ), patch(
            "research_center.system_health_service.build_artifact_inventory",
            return_value={"records": []},
        ):
            snapshot = build_system_health_snapshot(runtime=runtime, resource_guard=resource_guard, artifact_registry_root=root)

        self.assertEqual(snapshot["schema_version"], SYSTEM_HEALTH_SCHEMA_VERSION)
        self.assertEqual(snapshot["runtime"]["active_task_count"], 1)
        self.assertEqual(snapshot["resources"]["pools"]["background_backfill"]["limit"], 1)
        self.assertEqual(snapshot["artifacts"]["record_count"], 1)
        self.assertEqual(snapshot["artifacts"]["by_type"]["report_json"], 1)
        self.assertIn("artifact_inventory", snapshot)

    def test_format_snapshot_includes_operational_summary(self):
        snapshot = {
            "runtime": {"active_task_count": 2},
            "resources": {"pools": {"background_backfill": {"active": 1, "limit": 1}}},
            "data_sources": {
                "cooling_sources": ["finmind"],
                "quota": {"finmind_hourly_remaining": 123, "fugle_historical_remaining": 45},
            },
            "artifacts": {"record_count": 3, "by_type": {"feature_pack": 2, "report_json": 1}},
            "artifact_inventory": {"target_count": 4, "usable_count": 3},
        }

        text = format_system_health_snapshot(snapshot)

        self.assertIn("系統健康狀態", text)
        self.assertIn("執行中任務：2", text)
        self.assertIn("資源池：background_backfill 1/1", text)
        self.assertIn("冷卻來源：finmind", text)
        self.assertIn("FinMind 安全剩餘額度：123", text)
        self.assertIn("本機資料盤點目標：4，可用：3", text)
        self.assertIn("feature_pack=2", text)


if __name__ == "__main__":
    unittest.main()
