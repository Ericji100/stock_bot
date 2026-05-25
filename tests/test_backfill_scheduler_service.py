from __future__ import annotations

import unittest
from datetime import date

from research_center.backfill_scheduler_service import build_backfill_priority_plan


class BackfillSchedulerServiceTests(unittest.TestCase):
    def test_priority_plan_builds_tasks_from_health(self):
        plan = build_backfill_priority_plan(
            date(2026, 5, 20),
            health={
                "technical": {"coverage_pct": 0.7, "missing_count": 10, "missing_codes": ["2330"]},
                "chip": {"coverage_pct": 0.96, "missing_count": 0},
            },
        )

        self.assertEqual(plan["schema_version"], "backfill_priority_v1")
        self.assertEqual(plan["task_count"], 1)
        self.assertEqual(plan["tasks"][0]["task"], "warmup_technical_cache")

    def test_priority_plan_adds_data_gap_tasks(self):
        plan = build_backfill_priority_plan(
            date(2026, 5, 20),
            gap_summary={"priority_gaps": [{"field": "financial_data", "priority": "high", "recommended_action": "backfill_financial_cache"}]},
        )

        self.assertEqual(plan["tasks"][0]["task"], "backfill_financial_cache")


if __name__ == "__main__":
    unittest.main()
