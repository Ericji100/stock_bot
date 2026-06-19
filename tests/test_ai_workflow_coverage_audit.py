from __future__ import annotations

import unittest

from tools.ai_workflow_coverage_audit import AI_COMMANDS, audit_command


class AiWorkflowCoverageAuditTests(unittest.TestCase):
    def test_all_ai_commands_are_aligned_in_offline_coverage_gate(self) -> None:
        self.assertEqual(
            AI_COMMANDS,
            [
                "research",
                "value_scan",
                "macro",
                "theme",
                "theme_radar",
                "theme_flow",
                "sector_strength",
                "radar",
                "news",
                "topic_maintain",
            ],
        )
        for command in AI_COMMANDS:
            with self.subTest(command=command):
                row = audit_command(command)
                self.assertEqual(row["status"], "aligned")
                self.assertEqual(row["missing_capabilities"], [])
                self.assertTrue(row["dedupe_strategy"])

    def test_data_maintenance_flows_mark_html_as_not_applicable(self) -> None:
        for command in ("news", "topic_maintain"):
            with self.subTest(command=command):
                row = audit_command(command)
                self.assertIn("html_sections", row["not_applicable"])
                self.assertFalse(row["checks"]["html_sections"])


if __name__ == "__main__":
    unittest.main()
