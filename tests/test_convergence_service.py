from __future__ import annotations

import json
from datetime import date
import unittest
from unittest.mock import patch

from research_center.command_parser import parse_command_text
from research_center.convergence_service import attach_convergence_fields, candidate_snapshot_from_row
from research_center.models import SourceItem
from research_center.report_builder import build_report_json
from research_center import recent_scans
import radar_service


class ConvergenceServiceTests(unittest.TestCase):
    def test_candidate_snapshot_from_value_scan_row_preserves_early_stage(self) -> None:
        snapshot = candidate_snapshot_from_row(
            {
                "code": "2330",
                "name": "台積電",
                "symbol": "2330.TW",
                "rerating_score": 82,
                "verification_score": 61,
                "early_signal_priority": 80,
                "rerating_evidence": ["營收轉強"],
                "counter_evidence": ["題材證據仍需驗證"],
            },
            source_command="value_scan",
            source_pool="精選選股",
            data_date="2026-06-19",
        )

        self.assertEqual(snapshot["schema_version"], "candidate_snapshot_v1")
        self.assertEqual(snapshot["code"], "2330")
        self.assertEqual(snapshot["source_command"], "value_scan")
        self.assertEqual(snapshot["source_pool"], "精選選股")
        self.assertEqual(snapshot["stage"], "early_single_signal")
        self.assertIn("early_signal_priority", snapshot["early_stage_flags"])
        self.assertEqual(snapshot["local_scores"]["rerating_score"], 82)

    def test_attach_convergence_fields_adds_ai_input_layers(self) -> None:
        request = parse_command_text("/theme_radar --source market")
        data = {
            "report_date": "2026-06-19",
            "market_data_date": "2026-06-18",
            "strong_stock_policy": {
                "source": "market",
                "status": "market_movers",
                "candidate_count": 1,
            },
            "strong_stocks": [
                {
                    "code": "2308",
                    "name": "台達電",
                    "theme_matches": [{"theme_id": "ai_power", "theme_name": "AI 電源"}],
                    "theme_score": 88,
                }
            ],
        }

        attach_convergence_fields(request, data)

        self.assertEqual(data["candidate_snapshot"][0]["code"], "2308")
        self.assertEqual(data["candidate_snapshot"][0]["source_command"], "theme_radar")
        self.assertEqual(data["data_source_summary"][0]["data_type"], "theme_radar_candidates")
        self.assertEqual(data["report_metadata"]["command"], "theme_radar")

    def test_report_json_contains_convergence_fields_for_main_commands(self) -> None:
        cases = [
            (
                "/research 2330",
                {
                    "stock": {"code": "2330", "name": "台積電", "symbol": "2330.TW"},
                    "market_data_date": "2026-06-19",
                    "local_scoring": {"scores": []},
                },
            ),
            (
                "/value_scan 精選選股 --deep",
                {
                    "candidate_pool": "精選選股",
                    "report_date": "2026-06-19",
                    "candidate_source_policy": {"source": "精選選股", "status": "ok", "candidate_count": 1},
                    "ai_candidates": [{"code": "2330", "name": "台積電", "rerating_score": 80}],
                    "local_scoring": {"scores": []},
                },
            ),
            (
                "/theme_radar --source market",
                {
                    "report_date": "2026-06-19",
                    "strong_stock_policy": {"source": "market", "status": "market_movers", "candidate_count": 1},
                    "strong_stocks": [{"code": "2308", "name": "台達電", "theme_score": 88}],
                    "local_scoring": {"scores": []},
                },
            ),
        ]

        for raw_command, structured_data in cases:
            with self.subTest(raw_command=raw_command):
                request = parse_command_text(raw_command)
                report_json = build_report_json(
                    request,
                    "# Test\n\n## 資料來源\n- [S001] Example",
                    "summary",
                    [SourceItem("S001", "Example", "https://example.com", "Level 2", provider="unit_test")],
                    True,
                    None,
                    structured_data,
                    report_id="unit_report",
                )

                self.assertEqual(report_json["schema_version"], "report_json_v2")
                self.assertEqual(report_json["report_metadata"]["report_id"], "unit_report")
                self.assertIsInstance(report_json["data_source_summary"], list)
                self.assertIsInstance(report_json["candidate_snapshot"], list)
                self.assertGreaterEqual(len(report_json["candidate_snapshot"]), 1)

    def test_recent_scan_result_saves_candidate_snapshot(self) -> None:
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache

        tmp = ensure_test_cache_dir("convergence_service/recent_scans")
        try:
            path = tmp / "recent_scan_results.json"
            with patch.object(recent_scans, "RECENT_SCAN_PATH", path):
                record = recent_scans.save_recent_scan_result(
                    "精選選股",
                    date(2026, 6, 19),
                    "2330 台積電",
                    ["2330"],
                )
                saved = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(record["candidate_snapshot"][0]["code"], "2330")
            self.assertEqual(saved[0]["candidate_snapshot"][0]["source_command"], "scan")
        finally:
            safe_remove_test_cache("convergence_service/recent_scans")

    def test_radar_candidate_dict_includes_candidate_snapshot(self) -> None:
        candidate = radar_service.RadarCandidate(
            code="2308",
            name="台達電",
            symbol="2308.TW",
            source_labels=["技術面選股", "精選選股"],
            strategy_codes={"A1_close_breakout"},
            total_score=77,
        )

        payload = radar_service._candidate_to_dict(candidate)

        self.assertEqual(payload["candidate_snapshot"]["code"], "2308")
        self.assertEqual(payload["candidate_snapshot"]["source_command"], "radar")
        self.assertEqual(payload["candidate_snapshot"]["local_scores"]["total_score"], 77)


if __name__ == "__main__":
    unittest.main()
