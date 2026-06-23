from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools.ai_command_real_m3_validation import (
    _augment_record_with_runtime_issues,
    _augment_special_artifacts,
    _artifact_status_for_command,
    _looks_like_command_failure_output,
    _parse_parameter_case,
    _quality_review,
    _run_parameter_matrix,
    _summary_markdown,
    _sync_quality_review_file,
    _topic_quality_probe_text,
    _topic_raw_indicates_ai_fallback,
    _write_json,
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
        self.assertEqual(record["max_prompt_chars"], 3000)
        self.assertEqual(record["quality_review"]["metrics"]["rough_prompt_tokens"], 1500)
        self.assertEqual(record["quality_review"]["metrics"]["max_prompt_chars"], 3000)

    def test_segmented_theme_uses_max_prompt_for_runaway_check(self) -> None:
        record = {
            "command": "/theme_radar --model minimax",
            "status": "success",
            "summary": "題材雷達完成，包含題材、族群、風險、反證與後續觀察。",
            "elapsed_seconds": 100,
            "source_count": 50,
            "prompt_chars": 0,
        }
        joined = "\n".join(
            [
                f"分段 AI 呼叫 {index}/8：核心資料 prompt=150000 chars est_tokens=37500 sources=20 timeout=900s"
                for index in range(1, 9)
            ]
        )

        _augment_special_artifacts(record, joined)

        self.assertEqual(record["prompt_chars"], 1_200_000)
        self.assertEqual(record["max_prompt_chars"], 150_000)
        self.assertTrue(record["quality_review"]["checks"]["Prompt 未失控"])
        self.assertNotIn("Prompt 未失控", record["quality_review"]["issues"])

    def test_segmented_theme_prompt_chars_are_counted_from_progress_log(self) -> None:
        record = {
            "command": "/theme_radar --model minimax",
            "status": "success",
            "summary": "市場題材雷達分析完成，含風險、反證、後續驗證與來源。",
            "elapsed_seconds": 10,
            "source_count": 3,
            "prompt_chars": 0,
        }
        joined = "\n".join([
            "分段 AI 呼叫 1/2：本地核心資料包 prompt=1000 chars est_tokens=250 sources=0 timeout=900s",
            "分段 AI 呼叫 2/2：證據與反證 prompt=2000 chars est_tokens=500 sources=3 timeout=900s",
            "分段 AI 最終整合開始：prompt=3000 chars est_tokens=750 sources=3 timeout=900s",
        ])

        _augment_special_artifacts(record, joined)

        self.assertEqual(record["prompt_chars"], 6000)
        self.assertEqual(record["quality_review"]["metrics"]["rough_prompt_tokens"], 1500)

    def test_sync_quality_review_file_prefers_record_review(self) -> None:
        root = ensure_test_cache_dir("ai_command_real_m3_validation/sync_quality")
        try:
            stale_file_review = {
                "score": 10,
                "total": 10,
                "pass": True,
                "issues": [],
                "metrics": {"prompt_chars": 6000},
            }
            (root / "quality_review.json").write_text(
                json.dumps(stale_file_review, ensure_ascii=False),
                encoding="utf-8",
            )
            record = {
                "command": "/theme_radar --model minimax",
                "quality_review": {
                    "score": 7,
                    "total": 10,
                    "pass": False,
                    "issues": ["old issue"],
                },
            }

            _sync_quality_review_file(record, root)

            self.assertFalse(record["quality_review"]["pass"])
            self.assertEqual(record["quality_review"]["score"], 7)
            self.assertEqual(record["quality_review"]["issues"], ["old issue"])
            saved = json.loads((root / "quality_review.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["score"], 7)
        finally:
            safe_remove_test_cache("ai_command_real_m3_validation/sync_quality")

    def test_write_json_removes_control_characters(self) -> None:
        root = ensure_test_cache_dir("ai_command_real_m3_validation/json_safe")
        try:
            path = root / "partial_results.json"
            _write_json(path, {"message": "ok\x00bad\x1f\nkeep-tab\t"})

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["message"], "okbad\nkeep-tab\t")
        finally:
            safe_remove_test_cache("ai_command_real_m3_validation/json_safe")

    def test_topic_quality_probe_includes_date_from_change_id(self) -> None:
        root = ensure_test_cache_dir("ai_command_real_m3_validation/topic_probe")
        try:
            path = root / "change_20990101_010101.json"
            path.write_text(
                json.dumps({"stages": [{"raw": "risk_notes counter_evidence missing_data 後續驗證"}]}, ensure_ascii=False),
                encoding="utf-8",
            )

            text = _topic_quality_probe_text(path)

            self.assertIn("資料日期：2099-01-01", text)
        finally:
            safe_remove_test_cache("ai_command_real_m3_validation/topic_probe")

    def test_topic_raw_recovered_timeout_is_not_fallback(self) -> None:
        root = ensure_test_cache_dir("ai_command_real_m3_validation/topic_raw_recovered")
        try:
            path = root / "change_20990101_010101.json"
            path.write_text(
                json.dumps(
                    {
                        "stages": [
                            {
                                "stage": "candidate_extract",
                                "error": "MiniMax API request failed; status=timeout; reason=ReadTimeout",
                            },
                            {"stage": "candidate_extract_retry", "raw": "{\"candidates\": []}"},
                            {
                                "stage": "detail_expand_1",
                                "error": "MiniMax API request failed; status=timeout; reason=ReadTimeout",
                            },
                            {"stage": "detail_expand_1_retry", "raw": "{\"actions\": []}"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            self.assertFalse(_topic_raw_indicates_ai_fallback(path))
        finally:
            safe_remove_test_cache("ai_command_real_m3_validation/topic_raw_recovered")

    def test_topic_raw_unrecovered_timeout_is_fallback(self) -> None:
        root = ensure_test_cache_dir("ai_command_real_m3_validation/topic_raw_unrecovered")
        try:
            path = root / "change_20990101_010101.json"
            path.write_text(
                json.dumps(
                    {
                        "stages": [
                            {
                                "stage": "detail_expand_1",
                                "error": "MiniMax API request failed; status=timeout; reason=ReadTimeout",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            self.assertTrue(_topic_raw_indicates_ai_fallback(path))
        finally:
            safe_remove_test_cache("ai_command_real_m3_validation/topic_raw_unrecovered")

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

    def test_quality_review_rejects_research_fallback_success(self) -> None:
        review = _quality_review(
            command="/research 2241 --deep --model minimax",
            output_text="這不是正式 AI 完成報告，而是本地資料 fallback 報告。風險與後續觀察如下。",
            source_count=120,
            prompt_chars=240000,
            elapsed_seconds=300,
            status="fallback_success",
            error="MiniMax-M3 ReadTimeout",
        )

        self.assertFalse(review["checks"]["非 fallback 報告"])
        self.assertFalse(review["pass"])

    def test_quality_review_rejects_model_reasoning_and_unreadable_text(self) -> None:
        review = _quality_review(
            command="/topic_maintain --model minimax",
            output_text=(
                "<think>The user wants me to reason internally.</think>\n"
                "\u984c\u6750\u66f4\u65b0\u5305\uff1aAI \uf5fb\uf5fb\uf5fb\uff0c"
                "\u5f8c\u7e8c\u9a57\u8b49\u8207\u98a8\u96aa\u53cd\u8b49\u90fd\u9700\u8981\u88dc\u5f37\u3002"
            ),
            source_count=12,
            prompt_chars=3000,
            elapsed_seconds=10,
            status="success",
            error=None,
        )

        self.assertFalse(review["checks"]["\u7121\u6a21\u578b\u601d\u8003\u5916\u9732"])
        self.assertFalse(review["checks"]["\u7121\u4e82\u78bc\u6216\u4e0d\u53ef\u8b80\u6587\u5b57"])
        self.assertFalse(review["pass"])

    def test_summary_lists_partial_quality_as_issue(self) -> None:
        root = ensure_test_cache_dir("ai_command_real_m3_validation/summary_partial")
        try:
            text = _summary_markdown(
                root,
                [
                    {
                        "command": "/topic_maintain --model minimax",
                        "status": "success",
                        "elapsed_seconds": 10,
                        "source_count": 12,
                        "prompt_chars": 3000,
                        "quality_review": {
                            "score": 9,
                            "total": 13,
                            "pass": False,
                            "issues": ["partial quality"],
                        },
                        "report_paths": {},
                    }
                ],
            )

            self.assertIn("/topic_maintain --model minimax", text)
            self.assertIn("partial quality", text)
            self.assertNotIn("- \u7121\u3002", text)
        finally:
            safe_remove_test_cache("ai_command_real_m3_validation/summary_partial")

    def test_summary_distinguishes_fallback_artifact_from_formal_ai_success(self) -> None:
        root = ensure_test_cache_dir("ai_command_real_m3_validation/summary_fallback_status")
        try:
            text = _summary_markdown(
                root,
                [
                    {
                        "command": "/research 2241 --deep --model minimax",
                        "status": "fallback_success",
                        "ai_status": "fallback_success",
                        "artifact_status": "complete",
                        "formal_ai_success": False,
                        "elapsed_seconds": 300,
                        "source_count": 120,
                        "prompt_chars": 240000,
                        "fallback_reason": "MiniMax-M3 ReadTimeout",
                        "quality_review": {
                            "score": 10,
                            "total": 13,
                            "pass": False,
                            "issues": ["非 fallback 報告"],
                        },
                        "report_paths": {"html": "fallback.html"},
                    }
                ],
            )

            self.assertIn("| /research 2241 --deep --model minimax | fallback_success | fallback_success | complete |", text)
            self.assertIn("AI 失敗但 fallback 報告已產出", text)
            self.assertIn("不能視為正式 AI 分析成功", text)
            self.assertNotIn("- \u7121\u3002", text)
        finally:
            safe_remove_test_cache("ai_command_real_m3_validation/summary_fallback_status")

    def test_special_command_artifact_status_uses_command_specific_outputs(self) -> None:
        self.assertEqual(
            _artifact_status_for_command("/news refresh --model minimax", {"telegram": True}),
            "complete",
        )
        self.assertEqual(
            _artifact_status_for_command("/radar --ai-top 5 --model minimax", {"summary": True}),
            "complete",
        )
        self.assertEqual(
            _artifact_status_for_command("/topic_maintain --model minimax", {"output": True}),
            "partial",
        )
        self.assertEqual(
            _artifact_status_for_command("/topic_maintain --model minimax", {"json": True}),
            "complete",
        )
        self.assertEqual(
            _artifact_status_for_command("/research 2330 --model minimax", {"html": True}),
            "partial",
        )

    def test_command_failure_output_is_detected(self) -> None:
        self.assertTrue(_looks_like_command_failure_output("❌ 執行失敗：Object of type TopicConfidence is not JSON serializable"))
        self.assertFalse(_looks_like_command_failure_output("題材庫維護完成，已產生可審核變更包。"))
        self.assertFalse(
            _looks_like_command_failure_output(
                "### 14.4 失敗條件\n"
                "- 仲琦：若 6–7 月月營收仍維持雙位數年減，AI 重估敘事必須宣告失敗。\n"
            )
        )
        self.assertTrue(_looks_like_command_failure_output("❌ AI 投研任務失敗：MiniMax timeout"))

    def test_news_recovered_lightweight_retry_is_not_runtime_failure(self) -> None:
        root = ensure_test_cache_dir("ai_command_real_m3_validation/news_retry")
        try:
            (root / "progress.log").write_text(
                "\n".join([
                    "AI 分類 1/2 prompt=3000 chars est_tokens=750 items=3",
                    "AI 分類 1/2 failed: MiniMax API request failed; status=timeout; retrying with lightweight payload",
                    "AI 分類 1/2 retry_prompt=2000 chars est_tokens=500 items=3",
                    "AI 分類 1/2 lightweight retry completed",
                ]),
                encoding="utf-8",
            )
            record = {
                "command": "/news refresh --model minimax",
                "status": "success",
                "source_count": 10,
                "prompt_chars": 0,
                "elapsed_seconds": 20,
                "stdout_path": str(root / "worker_stdout.log"),
                "quality_review": {"pass": True, "issues": [], "metrics": {}},
            }

            updated = _augment_record_with_runtime_issues(record, root)

            self.assertNotIn("runtime_issues", updated)
            self.assertTrue(updated["quality_review"]["pass"])
        finally:
            safe_remove_test_cache("ai_command_real_m3_validation/news_retry")

    def test_news_local_fallback_marks_partial_success(self) -> None:
        root = ensure_test_cache_dir("ai_command_real_m3_validation/news_local_fallback")
        try:
            (root / "progress.log").write_text(
                "\n".join([
                    "AI 分類 1/2 prompt=3000 chars est_tokens=750 items=3",
                    "AI 分類 1/2 failed: MiniMax API request failed; status=timeout; retrying with lightweight payload",
                    "AI 分類 1/2 retry_prompt=2000 chars est_tokens=500 items=3",
                    "AI 分類 1/2 fallback to local rules: MiniMax API request failed; status=timeout",
                    "AI 分類 timeout; fallback remaining 3 items to local rules",
                    "AI 分類 fallback：6/6 則改用本地規則",
                ]),
                encoding="utf-8",
            )
            record = {
                "command": "/news refresh --model minimax",
                "status": "success",
                "ai_status": "ai_success",
                "source_count": 10,
                "prompt_chars": 0,
                "elapsed_seconds": 20,
                "stdout_path": str(root / "worker_stdout.log"),
                "quality_review": {"pass": True, "issues": [], "metrics": {}},
            }

            updated = _augment_record_with_runtime_issues(record, root)

            self.assertEqual(updated["ai_status"], "partial_success")
            self.assertFalse(updated["formal_ai_success"])
            self.assertEqual(updated["local_fallback_count"], 6)
            self.assertFalse(updated["quality_review"]["pass"])
        finally:
            safe_remove_test_cache("ai_command_real_m3_validation/news_local_fallback")

    def test_news_runtime_issue_without_new_marker_marks_partial_success(self) -> None:
        root = ensure_test_cache_dir("ai_command_real_m3_validation/news_runtime_issue")
        try:
            (root / "progress.log").write_text(
                "\n".join([
                    "AI 分類 1/6 prompt=5015 chars est_tokens=1253 items=3",
                    "AI 分類 1/6 failed: MiniMax API request failed; status=timeout; retrying with lightweight payload",
                    "AI 分類 1/6 fallback to local rules: MiniMax API request failed; status=timeout",
                    "AI 分類 timeout; fallback remaining 15 items to local rules",
                ]),
                encoding="utf-8",
            )
            record = {
                "command": "/news refresh --model minimax",
                "status": "success",
                "ai_status": "ai_success",
                "source_count": 462,
                "prompt_chars": 0,
                "elapsed_seconds": 398.82,
                "stdout_path": str(root / "worker_stdout.log"),
                "quality_review": {"pass": True, "issues": [], "metrics": {}},
            }

            updated = _augment_record_with_runtime_issues(record, root)

            self.assertEqual(updated["ai_status"], "partial_success")
            self.assertFalse(updated["formal_ai_success"])
            self.assertFalse(updated["quality_review"]["pass"])
            self.assertIn("MiniMax API", " ".join(updated["runtime_issues"]))
        finally:
            safe_remove_test_cache("ai_command_real_m3_validation/news_runtime_issue")

    def test_low_model_recovered_retry_is_not_runtime_failure(self) -> None:
        root = ensure_test_cache_dir("ai_command_real_m3_validation/low_model_retry")
        try:
            (root / "progress.log").write_text(
                "\n".join([
                    "MiniMax M3 低階整理失敗，改用精簡重試：JSON parse error",
                    "MiniMax M3 低階整理重試成功：facts=25 sources=29",
                ]),
                encoding="utf-8",
            )
            record = {
                "command": "/topic_maintain --model minimax",
                "status": "success",
                "source_count": 10,
                "prompt_chars": 1000,
                "elapsed_seconds": 20,
                "stdout_path": str(root / "worker_stdout.log"),
                "quality_review": {"pass": True, "issues": [], "metrics": {}},
            }

            updated = _augment_record_with_runtime_issues(record, root)

            self.assertNotIn("runtime_issues", updated)
            self.assertNotIn("低階模型整理失敗", updated["quality_review"].get("issues", []))
        finally:
            safe_remove_test_cache("ai_command_real_m3_validation/low_model_retry")

    def test_segmented_low_model_recovered_retry_is_not_runtime_failure(self) -> None:
        root = ensure_test_cache_dir("ai_command_real_m3_validation/segmented_low_model_retry")
        try:
            (root / "progress.log").write_text(
                "\n".join(
                    [
                        "MiniMax M3 分段資料整理失敗 5/9，改用精簡重試：JSON parse error",
                        "MiniMax M3 分段資料整理重試成功 5/9：facts=21 sources=29",
                        "MiniMax M3 分段資料整理結束：success=9/9 failed=0 facts=167 sources=194",
                        "部分 MiniMax M3 分段整理失敗；失敗段來源已列入 failed_segment_index。",
                    ]
                ),
                encoding="utf-8",
            )
            record = {
                "command": "/value_scan 我的持股 --deep --top 30 --model minimax",
                "status": "success",
                "source_count": 10,
                "prompt_chars": 1000,
                "elapsed_seconds": 20,
                "stdout_path": str(root / "worker_stdout.log"),
                "runtime_issues": ["低階模型整理失敗"],
                "quality_review": {
                    "pass": False,
                    "issues": ["低階模型整理失敗"],
                    "runtime_issues": ["低階模型整理失敗"],
                    "metrics": {},
                },
            }

            updated = _augment_record_with_runtime_issues(record, root)

            self.assertNotIn("runtime_issues", updated)
            self.assertNotIn("低階模型整理失敗", updated["quality_review"]["issues"])
        finally:
            safe_remove_test_cache("ai_command_real_m3_validation/segmented_low_model_retry")

    def test_segmented_low_model_api_error_recovered_retry_is_not_runtime_failure(self) -> None:
        root = ensure_test_cache_dir("ai_command_real_m3_validation/segmented_low_model_api_retry")
        try:
            (root / "progress.log").write_text(
                "\n".join(
                    [
                        "MiniMax M3 分段資料整理失敗 9/9，改用精簡重試：MiniMax API request failed; status=529",
                        "MiniMax M3 分段資料整理重試成功 9/9：facts=18 sources=20",
                        "MiniMax M3 分段資料整理結束：success=9/9 failed=0 facts=167 sources=194",
                    ]
                ),
                encoding="utf-8",
            )
            record = {
                "command": "/value_scan 我的持股 --deep --top 30 --model minimax",
                "status": "success",
                "source_count": 10,
                "prompt_chars": 1000,
                "elapsed_seconds": 20,
                "stdout_path": str(root / "worker_stdout.log"),
                "quality_review": {
                    "pass": True,
                    "issues": [],
                    "metrics": {},
                },
            }

            updated = _augment_record_with_runtime_issues(record, root)

            self.assertNotIn("runtime_issues", updated)
            self.assertNotIn("MiniMax API 呼叫失敗", updated["quality_review"].get("issues", []))
        finally:
            safe_remove_test_cache("ai_command_real_m3_validation/segmented_low_model_api_retry")

    def test_quality_review_accepts_analysis_date_as_data_date(self) -> None:
        review = _quality_review(
            command="/theme_radar --model minimax",
            output_text=(
                "# 台股主題雷達分析報告\n\n"
                "**分析日期：2026 年 6 月 22 日 ｜ 範圍：全市場主題雷達**\n\n"
                "本報告含題材、族群、代表股、風險、反證、後續觀察與來源說明。"
            ),
            source_count=10,
            prompt_chars=1000,
            elapsed_seconds=10,
            status="success",
            error=None,
        )

        self.assertTrue(review["checks"]["有資料日期或基準日"])

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
