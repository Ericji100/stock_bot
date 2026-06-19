from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from research_center.models import CommandRequest, SourceItem
from tools.ai_command_live_audit import (
    _progress_collector,
    _summary_markdown,
    _write_codex_command_review,
    _write_codex_formal_output,
    audit_radar_command,
    audit_report_command,
)
from tests.test_cache_utils import ensure_test_cache_dir


class AiCommandLiveAuditSummaryTests(unittest.TestCase):
    def test_progress_collector_writes_incrementally(self) -> None:
        out_dir = ensure_test_cache_dir("live_audit_progress")
        log_path = out_dir / "progress.log"
        messages: list[str] = []
        progress = _progress_collector("unit", messages, log_path)

        progress("step one")

        self.assertEqual(len(messages), 1)
        self.assertTrue(log_path.exists())
        self.assertIn("step one", log_path.read_text(encoding="utf-8-sig"))

    def test_summary_includes_ai_workflow_coverage_columns(self) -> None:
        markdown = _summary_markdown(
            [
                {
                    "command_text": "/news refresh --model minimax",
                    "status": "success",
                    "elapsed_seconds": 1.2,
                    "source_count": 12,
                    "prompt_chars": 5000,
                    "rough_prompt_tokens_char4": 1250,
                    "input_mode": "maintenance",
                    "ai_workflow_coverage_status": "aligned",
                    "ai_workflow_missing_capabilities": [],
                    "ai_workflow_not_applicable": ["html_sections"],
                    "has_prompt_list_truncated": False,
                    "has_prompt_dict_truncated": False,
                }
            ],
            Path("logs/ai_command_audit/unit"),
        )

        self.assertIn("覆蓋度", markdown)
        self.assertIn("待補", markdown)
        self.assertIn("不適用", markdown)
        self.assertIn("aligned", markdown)
        self.assertIn("html_sections", markdown)
        self.assertIn("截斷標記", markdown)


    def test_include_low_model_calls_shared_low_model_digest(self) -> None:
        request = CommandRequest(command="research", raw_text="/research 2330", target="2330", ai_model="minimax")
        source = SourceItem("S001", "TWSE", "https://www.twse.com.tw/", "L1_official")
        center = SimpleNamespace(
            parse=lambda _: request,
            config=SimpleNamespace(
                enable_grounding=False,
                enable_low_model_digest=True,
                model="gemini",
                minimax_model="MiniMax-M3",
                opencode_model="deepseek",
            ),
            low_model_minimax=object(),
            _gemini_discovery_runner=SimpleNamespace(
                run_discovery_flow=lambda request, sources, structured_data, use_grounding, progress: (sources, False)
            ),
        )

        def fake_attach_low_model_digest(request, structured_data, sources, *, minimax, enabled, progress):
            structured_data["low_model_digest"] = {
                "schema_version": "low_model_digest_v1",
                "status": "success",
                "model": "MiniMax-M3",
                "facts": [{"fact": "test"}],
            }

        out_dir = Path(".cache/test_tmp/live_audit_low")
        with patch("tools.ai_command_live_audit.collect_structured_data", return_value=({"stock": {"code": "2330"}}, [source])), \
            patch("tools.ai_command_live_audit._select_sources_for_prompt", return_value=[source]), \
            patch("tools.ai_command_live_audit._enrich_sources_with_web_fetch"), \
            patch("tools.ai_command_live_audit.attach_low_model_digest", side_effect=fake_attach_low_model_digest) as attach_low, \
            patch("tools.ai_command_live_audit.write_prompt_log", return_value=Path("logs/ai_prompts/test.json")):
            report = audit_report_command(center, "/research 2330 --model minimax", out_dir, skip_low_model=False)

        self.assertEqual(report["status"], "success")
        attach_low.assert_called_once()
        structured_text = (out_dir / "structured_data.json").read_text(encoding="utf-8")
        self.assertIn('"status": "success"', structured_text)
        self.assertTrue((out_dir / "raw_core_snapshot.json").exists())
        self.assertTrue((out_dir / "raw_vs_high_model_input.json").exists())

    def test_radar_empty_cache_is_warning_not_success(self) -> None:
        out_dir = Path(".cache/test_tmp/live_audit_radar_empty")
        fake_result = SimpleNamespace(
            report_date=date(2026, 5, 22),
            candidates=[],
            diagnostics={"source": "unit"},
        )

        with patch("tools.ai_command_live_audit.load_radar_result", return_value=fake_result), \
            patch("tools.ai_command_live_audit._load_radar_records", return_value=[]):
            report = audit_radar_command("/radar --source technical --ai-top 5 --model minimax", out_dir)

        self.assertEqual(report["status"], "warning")
        self.assertIn("候選股為 0", report["error"])
        self.assertEqual(report["prompt_chars"], 0)
        structured_text = (out_dir / "structured_data.json").read_text(encoding="utf-8")
        self.assertIn('"candidate_count": 0', structured_text)

    def test_radar_empty_latest_falls_back_to_recent_non_empty_cache(self) -> None:
        out_dir = Path(".cache/test_tmp/live_audit_radar_fallback")
        empty_result = SimpleNamespace(
            report_date=date(2026, 5, 22),
            candidates=[],
            diagnostics={"source": "unit_empty"},
        )
        non_empty_result = SimpleNamespace(
            report_date=date(2026, 5, 21),
            candidates=[SimpleNamespace(code="2330", evidence_pack={})],
            diagnostics={"source": "unit_non_empty"},
        )

        with patch("tools.ai_command_live_audit.load_radar_result", return_value=empty_result), \
            patch("tools.ai_command_live_audit._load_radar_records", return_value=[{"radar_id": "old"}]), \
            patch("tools.ai_command_live_audit._record_to_result", return_value=non_empty_result), \
            patch("tools.ai_command_live_audit._select_ai_enrichment_codes", return_value=[]):
            report = audit_radar_command("/radar --source technical --ai-top 5 --model minimax", out_dir)

        self.assertEqual(report["status"], "success")
        structured_text = (out_dir / "structured_data.json").read_text(encoding="utf-8")
        self.assertIn("最近快取候選股為 0", structured_text)
        self.assertIn('"candidate_count": 1', structured_text)

    def test_radar_audit_writes_research_sources(self) -> None:
        out_dir = Path(".cache/test_tmp/live_audit_radar_sources")
        result = SimpleNamespace(
            report_date=date(2026, 6, 3),
            candidates=[SimpleNamespace(code=2330, evidence_pack={})],
            diagnostics={"source": "unit"},
        )
        source = SourceItem("S001", "TWSE", "https://www.twse.com.tw/", "Level 1")

        with patch("tools.ai_command_live_audit.load_radar_result", return_value=result), \
            patch("tools.ai_command_live_audit._select_ai_enrichment_codes", return_value=["2330"]), \
            patch("tools.ai_command_live_audit._load_or_build_radar_light_research", return_value=({"stock": {"code": "2330"}}, [source], "unit")), \
            patch("tools.ai_command_live_audit._build_ai_comment_prompt_jobs", return_value=[{"prompt": "test prompt", "codes": ["2330"]}]):
            report = audit_radar_command("/radar --source technical --ai-top 5 --model minimax", out_dir)

        self.assertEqual(report["source_count"], 1)
        sources_text = (out_dir / "sources.json").read_text(encoding="utf-8")
        self.assertIn("TWSE", sources_text)

    def test_radar_formal_output_uses_candidate_summaries(self) -> None:
        out_dir = Path(".cache/test_tmp/live_audit_radar_formal")
        candidate = SimpleNamespace(
            code="2330",
            name="台積電",
            industry="???",
            total_score=88.5,
            score_components={"momentum": 30, "chip": 20},
            strategy_codes=["technical_breakout"],
            technical_signal_summary="技術訊號偏強",
            evidence_pack={"summary": "foreign flow and price action improved", "external_source_count": 2},
        )
        result = SimpleNamespace(
            report_date=date(2026, 6, 3),
            candidates=[candidate],
            diagnostics={"source": "unit"},
        )
        source = SourceItem("S001", "TWSE", "https://www.twse.com.tw/", "Level 1")

        with patch("tools.ai_command_live_audit.load_radar_result", return_value=result), \
            patch("tools.ai_command_live_audit._select_ai_enrichment_codes", return_value=["2330"]), \
            patch("tools.ai_command_live_audit._load_or_build_radar_light_research", return_value=({"stock": {"code": "2330"}}, [source], "unit")), \
            patch("tools.ai_command_live_audit._ensure_radar_source_sufficiency"), \
            patch("tools.ai_command_live_audit._build_ai_comment_prompt_jobs", return_value=[{"prompt": "test prompt", "codes": ["2330"]}]):
            report = audit_radar_command("/radar --source technical --ai-top 1 --model minimax", out_dir)

        review = {"codex_review_status": "review ok", "warnings": [], "recommendations": []}
        _write_codex_formal_output(out_dir, report, review)
        structured_text = (out_dir / "structured_data.json").read_text(encoding="utf-8-sig")
        formal_text = (out_dir / "codex_formal_output.md").read_text(encoding="utf-8-sig")
        self.assertIn("radar_candidates", structured_text)
        self.assertIn("台積電", formal_text)
        self.assertIn("88.5", formal_text)
        self.assertIn("foreign flow and price action improved", formal_text)

    def test_codex_command_review_is_written(self) -> None:
        out_dir = Path(".cache/test_tmp/live_audit_codex_review")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "sources.json").write_text(
            '[{"source_id":"S001","title":"TWSE","level":"Level 1","source_type":"official"}]',
            encoding="utf-8",
        )
        (out_dir / "high_model_input_package.json").write_text(
            '{"input_quality_gate":{"warnings":[]},"command_specific_data":{"payload":{"candidates":[{"code":"2330"}]}}}',
            encoding="utf-8",
        )
        report = {
            "command_text": "/research 2330 --deep --model minimax",
            "status": "success",
            "source_count": 8,
            "prompt_chars": 50000,
            "rough_prompt_tokens_char4": 12500,
            "input_mode": "balanced",
            "ai_workflow_coverage_status": "aligned",
            "has_prompt_list_truncated": False,
            "has_prompt_dict_truncated": False,
            "core_counts": {"candidates": 1},
        }

        review = _write_codex_command_review(out_dir, report)

        self.assertTrue((out_dir / "codex_high_model_review.md").exists())
        self.assertTrue((out_dir / "codex_high_model_review.json").exists())
        self.assertEqual(review["codex_review_status"], "可產出但需標示風險")
        self.assertTrue(any("反證完整度" in warning for warning in review["warnings"]))
        markdown = (out_dir / "codex_high_model_review.md").read_text(encoding="utf-8-sig")
        self.assertIn("Codex 高階模型替代判讀", markdown)

    def test_codex_review_warns_when_prompt_is_too_large(self) -> None:
        out_dir = Path(".cache/test_tmp/live_audit_codex_review_large")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "sources.json").write_text(
            '[{"source_id":"S001","title":"source","level":"Level 1","source_type":"official"}]',
            encoding="utf-8",
        )
        (out_dir / "high_model_input_package.json").write_text("{}", encoding="utf-8")
        report = {
            "command_text": "/value_scan 精選選股 --deep --top 30 --model minimax",
            "status": "success",
            "source_count": 20,
            "prompt_chars": 1200000,
            "rough_prompt_tokens_char4": 300000,
            "has_prompt_list_truncated": False,
            "has_prompt_dict_truncated": False,
        }

        review = _write_codex_command_review(out_dir, report)

        self.assertEqual(review["codex_review_status"], "可產出但需標示風險")
        self.assertTrue(any("高於建議上限" in warning for warning in review["warnings"]))

    def test_codex_news_review_checks_classification_quality(self) -> None:
        out_dir = Path(".cache/test_tmp/live_audit_codex_news_review")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "sources.json").write_text(
            json.dumps(
                [
                    {"source_id": "S001", "title": "MOPS", "source_level": "L1_official", "source_type": "official"},
                    {"source_id": "S002", "title": "財經新聞", "source_level": "L2_media", "source_type": "news"},
                    {"source_id": "S003", "title": "市場新聞", "source_level": "Level 3", "source_type": "news"},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (out_dir / "high_model_input_package.json").write_text(
            json.dumps(
                {
                    "sample_payload": [
                        {"title": "法人進出 - Yahoo股市", "snippet": "工具頁"},
                        {"title": "台股重挫606點！三大法人賣超破千億", "snippet": "重大市場新聞"},
                        {"title": "AI與記憶體族群受關注", "snippet": "題材新聞"},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (out_dir / "structured_data.json").write_text(
            json.dumps(
                {
                    "existing_news_count": 100,
                    "search_query_log": {"task_count": 9},
                    "discovery_tasks": [{"label": "台股與大盤"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (out_dir / "prompt.md").write_text("請分類新聞，保留利多、利空、中性、反證、可信與資料不足。", encoding="utf-8")
        report = {
            "command_text": "/news refresh --model minimax",
            "status": "success",
            "source_count": 3,
            "prompt_chars": 5000,
            "has_prompt_list_truncated": False,
            "has_prompt_dict_truncated": False,
        }

        review = _write_codex_command_review(out_dir, report)

        self.assertTrue(any(item["name"] == "News refresh 專屬檢查" for item in review["dimensions"]))
        self.assertTrue(any("行情頁或工具頁" in rec for rec in review["recommendations"]))
        markdown = (out_dir / "codex_high_model_review.md").read_text(encoding="utf-8-sig")
        self.assertIn("News refresh 專屬檢查", markdown)

    def test_codex_topic_review_checks_library_gaps(self) -> None:
        out_dir = Path(".cache/test_tmp/live_audit_codex_topic_review")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "sources.json").write_text(
            json.dumps(
                [
                    {"source_id": f"S{i:03d}", "title": f"source {i}", "source_level": "L2_media", "source_type": "news"}
                    for i in range(12)
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (out_dir / "high_model_input_package.json").write_text(
            json.dumps({"topic_maintain_prompt_variables": ["structured_data_json", "discovery_sources_json"]}, ensure_ascii=False),
            encoding="utf-8",
        )
        (out_dir / "structured_data.json").write_text(
            json.dumps(
                {
                    "topic_library_gap_analysis": {
                        "profile_gap_count": 3,
                        "company_gap_count": 7,
                        "supply_chain_node_gap_count": 9,
                        "priority_company_gaps": [{"company_code": "2330", "missing": ["customers"]}],
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (out_dir / "prompt.md").write_text("請維護題材、供應鏈、證據、短期新聞、可信、反證與資料不足。", encoding="utf-8")
        report = {
            "command_text": "/topic_maintain --model minimax",
            "status": "success",
            "source_count": 12,
            "prompt_chars": 50000,
            "has_prompt_list_truncated": False,
            "has_prompt_dict_truncated": False,
        }

        review = _write_codex_command_review(out_dir, report)

        self.assertTrue(any(item["name"] == "Topic maintain 專屬檢查" for item in review["dimensions"]))
        self.assertTrue(any("供應鏈節點缺口" in rec for rec in review["recommendations"]))
        markdown = (out_dir / "codex_high_model_review.md").read_text(encoding="utf-8-sig")
        self.assertIn("Topic maintain 專屬檢查", markdown)

    def test_codex_formal_output_is_written(self) -> None:
        out_dir = Path(".cache/test_tmp/live_audit_codex_formal")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "sources.json").write_text(
            '[{"source_id":"S001","title":"TWSE","level":"Level 1","source_type":"official","url":"https://www.twse.com.tw/"}]',
            encoding="utf-8",
        )
        (out_dir / "high_model_input_package.json").write_text(
            '{"target":"2330","command_specific_data":{"payload":{"stock":{"code":"2330","name":"台積電","industry":"半導體"},"source_events":[{"title":"法說會","summary":"營運展望穩健"}]}}}',
            encoding="utf-8",
        )
        (out_dir / "structured_data.json").write_text(
            '{"stock":{"code":"2330","name":"台積電"},"data_gap_summary":{"warnings":["缺少最新法說會逐字稿"]}}',
            encoding="utf-8",
        )
        report = {
            "command_text": "/research 2330 --deep --model minimax",
            "status": "success",
            "source_count": 1,
            "prompt_chars": 50000,
        }
        review = {
            "codex_review_status": "可產出但需標示風險",
            "warnings": ["來源數偏少"],
            "recommendations": ["補官方法說會資料"],
        }

        output = _write_codex_formal_output(out_dir, report, review)

        self.assertTrue((out_dir / "codex_formal_output.md").exists())
        self.assertTrue((out_dir / "codex_formal_output.json").exists())
        self.assertEqual(output["command_name"], "research")
        markdown = (out_dir / "codex_formal_output.md").read_text(encoding="utf-8-sig")
        self.assertIn("核心結論", markdown)
        self.assertIn("可信度與資料缺口", markdown)
        self.assertIn("基於現實的推演", markdown)
        self.assertIn("主要來源", markdown)


if __name__ == "__main__":
    unittest.main()
