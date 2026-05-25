"""Tests for topic_formatters.py."""
import unittest
from unittest.mock import patch

from research_center.topic_models import (
    TopicActionType,
    TopicChangeAction,
    TopicChangeMode,
    TopicChangePack,
    TopicChangeStatus,
    TopicConfidence,
)
from research_center import topic_formatters as formatters


class TestTopicFormatters(unittest.TestCase):
    def test_format_change_pack_list_empty(self):
        result = formatters.format_change_pack_list([])
        self.assertIn("沒有變更包", result)

    def test_format_change_pack_list_with_packs(self):
        packs = [
            TopicChangePack(
                change_id="change_001",
                parent_change_id=None,
                mode=TopicChangeMode.INITIAL,
                status=TopicChangeStatus.PENDING,
                model="gemini",
                created_at="2026-01-01T10:00:00+0800",
                updated_at="2026-01-01T10:00:00+0800",
                summary="Test summary",
                confidence="high",
                actions=[],
            )
        ]
        result = formatters.format_change_pack_list(packs)
        self.assertIn("change_001", result)
        self.assertIn("⏳", result)

    def test_format_change_pack_detail(self):
        pack = TopicChangePack(
            change_id="change_detail_test",
            parent_change_id=None,
            mode=TopicChangeMode.UPDATE,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="Detailed test",
            confidence="medium",
            actions=[
                TopicChangeAction(
                    action_type=TopicActionType.CREATE_THEME,
                    theme_id="ai_server",
                    theme_name="AI伺服器",
                    keywords=["AI"],
                    industries=["半導體"],
                    supply_chain_role="核心",
                    confidence=TopicConfidence.HIGH,
                    reason="需求爆發",
                    evidence=[],
                )
            ],
            warnings=["Coverage low"],
        )
        result = formatters.format_change_pack_detail(pack)
        self.assertIn("change_detail_test", result)
        self.assertIn("AI伺服器", result)
        self.assertIn("⚠️ 警告", result)
        self.assertIn("下一步", result)

    def test_format_change_pack_detail_with_adjustment_check(self):
        pack = TopicChangePack(
            change_id="change_adjust_test",
            parent_change_id="change_parent",
            mode=TopicChangeMode.ADJUST,
            status=TopicChangeStatus.PENDING,
            model="minimax",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="Adjusted themes",
            confidence="medium",
            actions=[],
            adjustment_check={
                "user_request_summary": "請增加更多題材",
                "changes_made": ["新增了5個題材"],
                "not_fully_satisfied": ["資料不足"],
                "satisfaction": "partial",
            },
        )
        result = formatters.format_change_pack_detail(pack)
        self.assertIn("🧭 調整意見檢查", result)
        self.assertIn("使用者要求", result)
        self.assertIn("請增加更多題材", result)
        self.assertIn("已完成", result)
        self.assertIn("未完成", result)
        self.assertIn("partial", result)

    def test_format_change_pack_detail_with_satisfied_check(self):
        pack = TopicChangePack(
            change_id="change_satisfied",
            parent_change_id="change_parent",
            mode=TopicChangeMode.ADJUST,
            status=TopicChangeStatus.PENDING,
            model="gemini",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="Fully adjusted",
            confidence="high",
            actions=[],
            adjustment_check={
                "user_request_summary": "請修正議題名稱",
                "changes_made": ["已修正所有名稱"],
                "not_fully_satisfied": [],
                "satisfaction": "satisfied",
            },
        )
        result = formatters.format_change_pack_detail(pack)
        self.assertIn("satisfied", result)
        self.assertIn("✅ satisfied", result)

    def test_format_apply_result_success(self):
        from research_center.topic_models import TopicApplyResult

        result = TopicApplyResult(
            change_id="change_apply_001",
            success=True,
            created=3,
            updated=1,
            merged=0,
            skipped=2,
            failed=0,
        )
        output = formatters.format_apply_result(result)
        self.assertIn("✅", output)
        self.assertIn("3", output)

    def test_format_apply_result_failure(self):
        from research_center.topic_models import TopicApplyResult

        result = TopicApplyResult(
            change_id="change_apply_fail",
            success=False,
            created=1,
            updated=0,
            merged=0,
            skipped=0,
            failed=2,
            errors=["action failed"],
        )
        output = formatters.format_apply_result(result)
        self.assertIn("❌", output)
        self.assertIn("action failed", output)

    def test_format_topic_profiles_empty(self):
        with patch("research_center.topic_formatters.load_topic_profiles", return_value=[]):
            result = formatters.format_topic_profiles()
            self.assertIn("沒有題材", result)
            self.assertIn("/topic_maintain", result)

    def test_format_topic_profiles_with_data(self):
        with patch("research_center.topic_formatters.load_topic_profiles", return_value=[]):
            result = formatters.format_topic_profiles()
        # Check empty path
        self.assertIn("沒有題材", result)

    def test_format_next_steps_pending(self):
        result = formatters.format_next_steps("change_xxx", "pending")
        self.assertIn("/topic_confirm", result)
        self.assertIn("/topic_reject", result)
        self.assertNotIn("/topic_adjust", result)

    def test_format_next_steps_failed(self):
        result = formatters.format_next_steps("change_xxx", "failed")
        self.assertIn("/topic_reject", result)
        self.assertIn("/topic_maintain", result)
        self.assertNotIn("raw_response_path", result)
        self.assertNotIn("prompt_log_path", result)

    def test_format_next_steps_non_pending(self):
        result = formatters.format_next_steps("change_xxx", "confirmed")
        self.assertIn("已 confirmed", result)

    def test_format_change_pack_detail_failed_no_logs(self):
        """Failed pack should NOT show raw_response_path or prompt_log_path to users."""
        pack = TopicChangePack(
            change_id="change_failed_test",
            parent_change_id=None,
            mode=TopicChangeMode.UPDATE,
            status=TopicChangeStatus.FAILED,
            model="deepseek",
            created_at="2026-01-01T10:00:00+0800",
            updated_at="2026-01-01T10:00:00+0800",
            summary="AI returned empty actions",
            confidence="low",
            actions=[],
            warnings=["AI 未產生可套用的題材變更，請拒絕此變更包或重新執行 /topic_maintain。"],
            raw_response_path="/logs/topic_ai_raw/change_failed_test.json",
            prompt_log_path="/logs/ai_prompts/change_failed_test.json",
        )
        result = formatters.format_change_pack_detail(pack)
        self.assertIn("⚠️", result)
        self.assertIn("failed", result)
        # Raw/prompt paths should NOT appear in Telegram display
        self.assertNotIn("/logs/topic_ai_raw/change_failed_test.json", result)
        self.assertNotIn("/logs/ai_prompts/change_failed_test.json", result)
        self.assertNotIn("Raw Response", result)
        self.assertNotIn("Prompt Log", result)
        self.assertNotIn("請查看 raw", result)
        # Next steps should still appear
        self.assertIn("/topic_reject", result)
        self.assertIn("/topic_maintain", result)


if __name__ == "__main__":
    unittest.main()