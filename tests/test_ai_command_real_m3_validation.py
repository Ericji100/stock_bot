from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools.ai_command_real_m3_validation import (
    _augment_special_artifacts,
    _parse_parameter_case,
    _quality_review,
    _run_parameter_matrix,
)
from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache


class AiCommandRealM3ParameterMatrixTests(unittest.TestCase):
    def test_parameter_matrix_passes_and_covers_all_ai_entries(self) -> None:
        matrix = _run_parameter_matrix()

        self.assertEqual(matrix["status"], "success")
        self.assertEqual(matrix["failed_count"], 0)
        for command in [
            "research",
            "value_scan",
            "macro",
            "theme",
            "theme_flow",
            "theme_radar",
            "sector_strength",
            "radar",
            "news",
            "topic_maintain",
        ]:
            self.assertIn(command, matrix["command_coverage"])

    def test_research_rejects_top_parameter(self) -> None:
        result = _parse_parameter_case(
            {
                "name": "unit_research_reject_top",
                "command": "/research 2330 --top 5 --model minimax",
                "parser": "research_center",
                "parameters": ["--top"],
                "expected_error_contains": "不支援 --top",
            }
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["status"], "expected_error")

    def test_research_default_deep_parameter_case(self) -> None:
        result = _parse_parameter_case(
            {
                "name": "unit_research_default_deep",
                "command": "/research 2330 --model minimax",
                "parser": "research_center",
                "parameters": ["--model"],
                "expected": {"command": "research", "mode": "deep", "ai_model": "minimax"},
            }
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["parsed"]["mode"], "deep")

    def test_radar_parameter_case_parses_ai_top_and_no_ai(self) -> None:
        result = _parse_parameter_case(
            {
                "name": "unit_radar_no_ai",
                "command": "/radar --source chip --ai-top 3 --no-ai-comment",
                "parser": "radar",
                "parameters": ["--source", "--ai-top", "--no-ai-comment"],
                "expected": {"source": "chip", "ai_top": 3, "model": "minimax", "ai_comment_enabled": False},
            }
        )

        self.assertTrue(result["pass"])
        self.assertFalse(result["parsed"]["ai_comment_enabled"])

    def test_radar_prompt_chars_are_counted_from_progress_log(self) -> None:
        record = {
            "command": "/radar --source technical --ai-top 2 --model minimax",
            "status": "success",
            "summary": "雷達短評完成，包含風險、後續觀察與雷達候選。",
            "elapsed_seconds": 10,
            "source_count": 3,
            "prompt_chars": 0,
        }
        joined = "\n".join([
            "MiniMax M3 低階整理開始：model=MiniMax-M3 prompt=1000 chars est_tokens=250 sources=0",
            "Radar AI 短評 chunk 1.1 開始，1 檔，profile=normal，prompt=2000 chars",
            "Radar AI 短評 chunk 1.2 開始，1 檔，profile=normal，prompt=3000 chars",
        ])

        _augment_special_artifacts(record, joined)

        self.assertEqual(record["prompt_chars"], 6000)
        self.assertEqual(record["quality_review"]["metrics"]["rough_prompt_tokens"], 1500)

    def test_quality_review_rejects_coverage_pct_internal_labels(self) -> None:
        review = _quality_review(
            command="/theme_radar --model minimax",
            output_text="財務摘要仍有 financial validation coverage pct = 0%，需要修正。",
            source_count=10,
            prompt_chars=1000,
            elapsed_seconds=10,
            status="success",
            error=None,
        )

        self.assertFalse(review["checks"]["無內部欄位外露"])
        self.assertFalse(review["pass"])

    def test_topic_raw_quality_probe_detects_risk_and_follow_up(self) -> None:
        root = ensure_test_cache_dir("ai_command_real_m3_validation/topic_raw")
        progress_path = root / "progress.log"
        raw_dir = Path("logs/topic_ai_raw")
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / "change_20990101_010101.json"
        try:
            raw_path.write_text(
                json.dumps(
                    {
                        "stages": [
                            {
                                "stage": "detail_expand",
                                "raw": json.dumps(
                                    {
                                        "actions": [
                                            {
                                                "theme_name": "測試題材",
                                                "risk_notes": ["需求不確定"],
                                                "counter_evidence": ["接單尚未驗證"],
                                                "missing_data": ["客戶資料不足"],
                                                "next_validation": ["後續追蹤月營收"],
                                            }
                                        ]
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            record = {
                "command": "/topic_maintain --model minimax",
                "status": "success",
                "summary": "題材庫更新維護：產生可審核變更。",
                "elapsed_seconds": 10,
                "source_count": 0,
                "prompt_chars": 0,
                "stdout_path": str(root / "worker_stdout.log"),
                "report_paths": {},
            }
            progress_path.write_text(
                "\n".join([
                    "Discovery來源：12 筆",
                    "MiniMax M3 分段資料整理開始：segments=1 original_prompt=9000 chars",
                    "MiniMax M3 分段資料整理 1/1：test prompt=3000 chars est_tokens=750 sources=12",
                    "Raw response 已保存：change_20990101_010101",
                ]),
                encoding="utf-8",
            )
            joined = "\n".join([
                "Discovery來源：12 筆",
                "MiniMax M3 分段資料整理開始：segments=1 original_prompt=9000 chars",
                "MiniMax M3 分段資料整理 1/1：test prompt=3000 chars est_tokens=750 sources=12",
                "Raw response 已保存：change_20990101_010101",
            ])

            _augment_special_artifacts(record, joined)

            checks = record["quality_review"]["checks"]
            self.assertTrue(checks["有風險或反證"])
            self.assertTrue(checks["有後續推演"])
            self.assertEqual(record["source_count"], 12)
            self.assertEqual(record["prompt_chars"], 3000)
        finally:
            try:
                raw_path.unlink()
            except FileNotFoundError:
                pass
            safe_remove_test_cache("ai_command_real_m3_validation/topic_raw")


if __name__ == "__main__":
    unittest.main()
