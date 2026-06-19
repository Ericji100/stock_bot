from __future__ import annotations

import json
import unittest
import shutil
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from research_center.ai_workflow_service import (
    AI_WORKFLOW_COVERAGE_SCHEMA_VERSION,
    HIGH_MODEL_INPUT_SCHEMA_VERSION,
    LOW_MODEL_DIGEST_SCHEMA_VERSION,
    _digest_fingerprint,
    _build_low_model_digest_payload,
    attach_low_model_digest,
    build_ai_workflow_coverage,
    build_high_model_input_package,
    build_low_model_digest_prompt,
    run_low_model_digest_for_payload,
    validate_low_model_digest,
)
from research_center.minimax_service import MiniMaxResult
from research_center.models import CommandRequest, SourceItem
from research_center.prompt_registry import build_prompt_from_request
from research_center.report_html_renderer import render_report_html


class FakeMiniMax:
    model = "MiniMax-M3"

    def __init__(self, markdown: str | None = None, error: Exception | None = None):
        self.markdown = markdown or "{}"
        self.error = error

    def is_configured(self) -> bool:
        return True

    def generate_json(self, prompt: str) -> MiniMaxResult:
        if self.error:
            raise self.error
        return MiniMaxResult(
            markdown=self.markdown,
            raw={"ok": True},
            diagnostics={"model": self.model, "usage": {"total_tokens": 10}},
        )


class CountingMiniMax(FakeMiniMax):
    def __init__(self):
        super().__init__(
            """
            {
              "schema_version": "low_model_digest_v1",
              "status": "success",
              "facts": [{"fact": "segment fact", "source_ids": ["S001"]}],
              "source_map": [{"source_id": "S001", "title": "segment source"}]
            }
            """
        )
        self.prompts: list[str] = []

    def generate_json(self, prompt: str) -> MiniMaxResult:
        self.prompts.append(prompt)
        return super().generate_json(prompt)


class FailingMiniMax(FakeMiniMax):
    def __init__(self):
        super().__init__(None)
        self.prompts: list[str] = []

    def generate_json(self, prompt: str) -> MiniMaxResult:
        self.prompts.append(prompt)
        raise RuntimeError("segment too large")


class QuotaFailingMiniMax(FakeMiniMax):
    def __init__(self):
        super().__init__(None)
        self.prompts: list[str] = []

    def generate_json(self, prompt: str) -> MiniMaxResult:
        self.prompts.append(prompt)
        raise RuntimeError("MiniMax API request failed: status=429 weekly usage limit exceeded; resets at 2099-06-08T00:00:00Z")


class RetryThenSuccessMiniMax(FakeMiniMax):
    def __init__(self):
        super().__init__(None)
        self.prompts: list[str] = []

    def generate_json(self, prompt: str) -> MiniMaxResult:
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return MiniMaxResult(
                markdown='{"schema_version": "low_model_digest_v1", "facts": [',
                raw={"ok": False},
                diagnostics={"model": self.model},
            )
        return MiniMaxResult(
            markdown="""
            {
              "schema_version": "low_model_digest_v1",
              "status": "success",
              "facts": [{"fact": "retry fact", "source_ids": ["S001"]}],
              "source_map": [{"source_id": "S001", "title": "retry source"}]
            }
            """,
            raw={"ok": True},
            diagnostics={"model": self.model, "usage": {"total_tokens": 8}},
        )


def _request() -> CommandRequest:
    return CommandRequest(command="research", raw_text="/research 2330", target="2330", mode="deep")


def _request_for(command: str) -> CommandRequest:
    return CommandRequest(command=command, raw_text=f"/{command}", target="market", mode="deep")


def _sources() -> list[SourceItem]:
    return [
        SourceItem(
            source_id="S001",
            title="台積電法說會",
            url="https://example.com/tsmc",
            source_level="Level 1",
            published_date="2026-05-01",
            snippet="公司說明 AI 需求與資本支出。",
        )
    ]


class LowModelDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._cache_root = Path(".cache") / "test_tmp" / f"ai_workflow_{self._testMethodName}_{uuid4().hex}"
        shutil.rmtree(self._cache_root, ignore_errors=True)
        self._cache_patch = patch("research_center.ai_workflow_service.LOW_MODEL_ARTIFACT_DIR", self._cache_root)
        self._cache_patch.start()

    def tearDown(self) -> None:
        self._cache_patch.stop()
        shutil.rmtree(self._cache_root, ignore_errors=True)

    def test_prompt_forbids_final_investment_judgment(self) -> None:
        prompt = build_low_model_digest_prompt(
            _request(),
            {
                "ai_prompt_context": {"target": "2330"},
                "ai_input_audit": {"source_coverage": {"official_sources": 1}},
                "report_confidence": {"confidence_score": 80},
            },
            _sources(),
        )
        self.assertIn("MiniMax M3", prompt)
        self.assertIn("facts", prompt)
        self.assertIn("json.loads()", prompt)
        self.assertIn("low_model_digest_v1", prompt)
        self.assertIn("source_map", prompt)
        self.assertTrue((Path("prompt") / "workflow" / "low_model_digest.md").exists())

    def test_attach_success_stores_digest(self) -> None:
        response = """
        {
          "schema_version": "low_model_digest_v1",
          "status": "success",
          "facts": [{"fact": "AI 需求強", "source_ids": ["S001"], "confidence": "medium"}],
          "source_map": [{"source_id": "S001", "title": "台積電法說會", "used_for": "需求驗證"}]
        }
        """
        data = {"ai_prompt_context": {"target": "2330"}}
        with patch("research_center.ai_workflow_service.write_prompt_log", return_value=Path("logs/ai_prompts/fake.json")):
            attach_low_model_digest(_request(), data, _sources(), minimax=FakeMiniMax(response), enabled=True)
        digest = data["low_model_digest"]
        self.assertEqual(digest["schema_version"], LOW_MODEL_DIGEST_SCHEMA_VERSION)
        self.assertEqual(digest["status"], "success")
        self.assertEqual(digest["model"], "MiniMax-M3")
        self.assertEqual(len(digest["facts"]), 1)
        self.assertTrue(str(data["low_model_prompt_path"]).replace("\\", "/").endswith("logs/ai_prompts/fake.json"))

    def test_attach_failure_does_not_raise(self) -> None:
        data = {"ai_prompt_context": {"target": "2330"}}
        with patch("research_center.ai_workflow_service.write_prompt_log", return_value=Path("logs/ai_prompts/fake.json")):
            attach_low_model_digest(
                _request(),
                data,
                _sources(),
                minimax=FakeMiniMax(error=RuntimeError("quota exhausted")),
                enabled=True,
            )
        self.assertEqual(data["low_model_digest"]["status"], "failed")
        self.assertIn("quota exhausted", data["low_model_digest"]["error"])

    def test_low_model_quota_error_enters_cooldown_and_next_run_skips_api_call(self) -> None:
        payload = {"command": "research", "items": [{"title": "AI"}]}
        failing = QuotaFailingMiniMax()
        with patch("research_center.ai_workflow_service.write_prompt_log", return_value=Path("logs/ai_prompts/quota.json")):
            first = run_low_model_digest_for_payload(
                _request(),
                payload,
                sources=_sources(),
                minimax=failing,
                enabled=True,
                purpose="unit_test_quota_cooldown",
            )
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["reason"], "low_model_quota_or_rate_limit")
        self.assertIn("cooldown_until", first)

        next_model = CountingMiniMax()
        second = run_low_model_digest_for_payload(
            _request(),
            payload,
            sources=_sources(),
            minimax=next_model,
            enabled=True,
            purpose="unit_test_quota_cooldown",
        )
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["reason"], "low_model_quota_cooldown")
        self.assertEqual(len(next_model.prompts), 0)

    def test_low_model_payload_excludes_large_structured_tables_but_keeps_text_evidence(self) -> None:
        stocks = [{"code": f"23{i:02d}", "name": f"Stock{i}", "trend_score": 80 + i} for i in range(80)]
        data = {
            "market_movers": {"top_gainers": stocks},
            "theme_rankings": [{"theme_name": "AI power", "representative_stocks": stocks}],
            "strong_stocks": stocks,
            "unified_evidence_pack": {
                "items": [
                    {
                        "source_id": "S001",
                        "title": "AI power news",
                        "summary": "AI server power demand is rising and supply chain companies benefit.",
                        "stance": "positive",
                    }
                ]
            },
            "data_gap_summary": {"missing": ["revenue mix needs verification"]},
        }
        payload = _build_low_model_digest_payload(_request_for("theme_radar"), data, _sources())
        payload_text = json.dumps(payload, ensure_ascii=False)
        skipped_sections = {row["section"] for row in payload["skipped_structured_sections"]}
        self.assertIn("AI server power demand is rising", payload_text)
        self.assertIn("low_model_input_policy", payload)
        self.assertIn("market_movers", skipped_sections)
        self.assertIn("theme_rankings", skipped_sections)
        self.assertIn("strong_stocks", skipped_sections)
        self.assertNotIn('"top_gainers"', payload_text)
        self.assertNotIn('"strong_stocks": [', payload_text)

    def test_high_model_package_still_receives_structured_tables_skipped_by_low_model(self) -> None:
        stocks = [{"code": f"23{i:02d}", "name": f"Stock{i}", "trend_score": 80 + i} for i in range(12)]
        data = {
            "theme_rankings": [{"theme_name": "AI power", "representative_stocks": stocks}],
            "strong_stocks": stocks,
            "market_movers": {"top_gainers": stocks},
            "unified_evidence_pack": {"items": [{"summary": "AI power evidence"}]},
        }
        payload = _build_low_model_digest_payload(_request_for("theme_radar"), data, _sources())
        data["low_model_input_policy"] = payload["low_model_input_policy"]
        data["low_model_text_evidence_count"] = len(payload["text_evidence"])
        data["low_model_skipped_structured_sections"] = payload["skipped_structured_sections"]
        package = build_high_model_input_package(
            _request_for("theme_radar"),
            data,
            _sources(),
            prompt_chars_estimate=400_000,
        )
        command_payload = package["command_specific_data"]["payload"]
        self.assertIn("theme_rankings", command_payload)
        self.assertIn("strong_stocks", command_payload)
        self.assertEqual(len(command_payload["stock_index"]), 12)
        self.assertEqual(len(command_payload["strong_stocks"]["stock_codes"]), 12)
        self.assertEqual(command_payload["theme_rankings"][0]["representative_stock_codes"][0], "2300")
        self.assertNotIn("representative_stocks", command_payload["theme_rankings"][0])
        self.assertNotIn("top_gainers", command_payload.get("market_movers", {}))
        self.assertEqual(command_payload["representation_policy"]["method"], "deduplicated_stock_index_with_relation_tables")
        self.assertEqual(package["low_model_text_evidence_count"], len(payload["text_evidence"]))
        self.assertIn("theme_rankings", {row["section"] for row in package["low_model_skipped_structured_sections"]})

    def test_run_low_model_digest_for_generic_payload(self) -> None:
        response = """
        {
          "schema_version": "low_model_digest_v1",
          "status": "success",
          "facts": [{"fact": "重大新聞需複核", "confidence": "medium"}],
          "warnings": ["只做資料整理"]
        }
        """
        with patch("research_center.ai_workflow_service.write_prompt_log", return_value=Path("logs/ai_prompts/generic.json")):
            digest = run_low_model_digest_for_payload(
                _request(),
                {"command": "news", "items": [{"title": "台股新聞"}]},
                sources=_sources(),
                minimax=FakeMiniMax(response),
                enabled=True,
                purpose="unit_test_generic_digest",
            )
        self.assertEqual(digest["status"], "success")
        self.assertEqual(digest["facts"][0]["fact"], "重大新聞需複核")
        self.assertTrue(str(digest["prompt_path"]).replace("\\", "/").endswith("logs/ai_prompts/generic.json"))

    def test_single_low_model_json_failure_retries_once(self) -> None:
        minimax = RetryThenSuccessMiniMax()
        with patch(
            "research_center.ai_workflow_service.write_prompt_log",
            side_effect=[Path("logs/ai_prompts/single.json"), Path("logs/ai_prompts/single_retry.json")],
        ):
            digest = run_low_model_digest_for_payload(
                _request(),
                {"command": "sector_strength", "items": [{"title": "AI 伺服器電源供應鏈"}]},
                sources=_sources(),
                minimax=minimax,
                enabled=True,
                purpose="unit_test_single_retry",
            )
        self.assertEqual(digest["status"], "success_after_retry")
        self.assertEqual(len(minimax.prompts), 2)
        self.assertEqual(digest["facts"][0]["fact"], "retry fact")
        self.assertTrue(str(digest["retry_prompt_path"]).replace("\\", "/").endswith("logs/ai_prompts/single_retry.json"))

    def test_large_low_model_payload_is_segmented(self) -> None:
        payload = {
            "command": "theme_radar",
            "target": "market",
            "items": [{"source_id": "S001", "text": "AI電源 " + ("x" * 5000)} for _ in range(80)],
        }
        minimax = CountingMiniMax()
        with patch("research_center.ai_workflow_service.LOW_MODEL_PROMPT_SOFT_LIMIT_CHARS", 1000), patch(
            "research_center.ai_workflow_service.write_prompt_log",
            return_value=Path("logs/ai_prompts/segmented.json"),
        ):
            digest = run_low_model_digest_for_payload(
                _request(),
                payload,
                sources=_sources(),
                minimax=minimax,
                enabled=True,
                purpose="unit_test_segmented_digest",
            )
        self.assertIn(digest["status"], {"success", "partial_success"})
        self.assertGreater(len(minimax.prompts), 1)
        self.assertEqual(digest["diagnostics"]["mode"], "segmented_low_model_digest")
        self.assertGreater(digest["estimated_tokens"], 0)

    def test_excessive_low_model_segments_are_skipped_without_ai_call(self) -> None:
        payload = {
            "command": "theme_radar",
            "target": "market",
            "items": [{"source_id": f"S{index:03d}", "text": "AI " + ("x" * 2000)} for index in range(70)],
        }
        minimax = CountingMiniMax()
        with patch("research_center.ai_workflow_service.LOW_MODEL_PROMPT_SOFT_LIMIT_CHARS", 1000), patch(
            "research_center.ai_workflow_service.LOW_MODEL_SEGMENT_TARGET_CHARS", 1000
        ):
            digest = run_low_model_digest_for_payload(
                _request(),
                payload,
                sources=_sources(),
                minimax=minimax,
                enabled=True,
                purpose="unit_test_excessive_segment_skip",
            )
        self.assertEqual(digest["status"], "skipped")
        self.assertEqual(digest["diagnostics"]["mode"], "skipped_low_model_digest")
        self.assertEqual(len(minimax.prompts), 0)
        self.assertEqual(digest["failed_segment_index"][0]["fallback_action"], "use_local_fidelity_package_for_final_model")

    def test_segment_failure_retries_and_records_failed_index(self) -> None:
        payload = {
            "command": "theme_radar",
            "target": "market",
            "items": [{"source_id": "S001", "text": "AI " + ("x" * 5000)} for _ in range(40)],
        }
        minimax = FailingMiniMax()
        with patch("research_center.ai_workflow_service.LOW_MODEL_PROMPT_SOFT_LIMIT_CHARS", 1000), patch(
            "research_center.ai_workflow_service.write_prompt_log",
            return_value=Path("logs/ai_prompts/segmented.json"),
        ):
            digest = run_low_model_digest_for_payload(
                _request(),
                payload,
                sources=_sources(),
                minimax=minimax,
                enabled=True,
                purpose="unit_test_segment_failure",
            )
        self.assertEqual(digest["status"], "failed")
        self.assertGreater(len(minimax.prompts), 1)
        self.assertGreater(len(digest["failed_segment_index"]), 0)
        self.assertEqual(digest["diagnostics"]["failed_count"], len(digest["failed_segment_index"]))
        self.assertEqual(digest["failed_segment_index"][0]["fallback_action"], "record_failed_segment_for_audit")

    def test_low_model_fingerprint_ignores_volatile_timestamps(self) -> None:
        first = {
            "command": "research",
            "items": [{"title": "AI"}],
            "generated_at": "2026-06-03T08:00:00+08:00",
        }
        second = {
            "command": "research",
            "items": [{"title": "AI"}],
            "generated_at": "2026-06-03T09:30:00+08:00",
        }
        self.assertEqual(
            _digest_fingerprint(_request(), first, purpose="stable"),
            _digest_fingerprint(_request(), second, purpose="stable"),
        )

    def test_html_contains_low_model_tab(self) -> None:
        html = render_report_html(
            {
                "report_title": "測試報告",
                "metadata": {
                    "analysis_model": "gemini",
                    "low_model_model": "MiniMax-M3",
                    "low_model_prompt_path": "logs/ai_prompts/fake.json",
                    "low_model_digest": {
                        "status": "success",
                        "facts": [{"fact": "AI 需求強", "source_ids": ["S001"], "confidence": "medium"}],
                    },
                },
                "sources": [],
            },
            "# 測試報告\n\n主要內容",
        )
        self.assertIn("資料整理底稿", html)
        self.assertIn("MiniMax-M3", html)
        self.assertIn("AI 需求強", html)


class HighModelInputPackageTests(unittest.TestCase):
    def test_high_model_input_package_switches_to_compact_mode(self) -> None:
        data = {
            "ai_data_center": {"source_selection": {"selected_sources": []}},
            "ai_prompt_context": {"target": "2330", "items": ["x" * 1000]},
            "low_model_digest": {
                "schema_version": LOW_MODEL_DIGEST_SCHEMA_VERSION,
                "status": "success",
                "facts": [{"fact": "AI demand", "source_ids": ["S001"]}],
                "source_map": [{"source_id": "S001", "title": "news"}],
            },
            "local_scoring": {"scores": [{"name": "revenue", "score": 80}]},
        }
        package = build_high_model_input_package(
            _request(),
            data,
            _sources(),
            prompt_chars_estimate=400_000,
        )
        self.assertEqual(package["schema_version"], HIGH_MODEL_INPUT_SCHEMA_VERSION)
        self.assertEqual(package["input_mode"], "compact")
        self.assertEqual(package["low_model_validation"]["status"], "success")
        self.assertIn("完整資料保留", package["workflow_policy"])
        self.assertNotIn("agent_retrieval", package)
        self.assertNotIn("final_prompt_budget_guard", package)

    def test_value_scan_quality_gate_warns_when_sources_too_sparse_for_candidates(self) -> None:
        data = {
            "ai_candidates": [{"code": f"{2300 + index}", "name": f"候選{index}"} for index in range(30)],
            "ai_candidate_evidence_pack": [{"code": "2308", "evidence": "本地重估底稿"}],
            "local_ranking": [{"code": "2308", "score": 80}],
        }

        package = build_high_model_input_package(
            _request_for("value_scan"),
            data,
            _sources(),
            prompt_chars_estimate=1_800_000,
        )

        gate = package["input_quality_gate"]
        self.assertEqual(gate["status"], "warning")
        self.assertEqual(gate["source_count"], 1)
        self.assertEqual(gate["candidate_count"], 30)
        self.assertTrue(any("價值重估外部來源覆蓋不足" in warning for warning in gate["warnings"]))
        self.assertTrue(any("資料量極大" in warning for warning in gate["warnings"]))
        self.assertIn("input_quality_gate", package["ai_workflow_coverage"]["diagnostics"])

    def test_value_scan_quality_gate_records_candidate_source_coverage(self) -> None:
        data = {
            "ai_candidates": [
                {"code": "2308", "name": "台達電", "source_events": [{"type": "mops"}]},
                {"code": "6282", "name": "康舒"},
            ],
            "ai_candidate_evidence_pack": [
                {"code": "2308", "source_events": [{"type": "chip"}]},
                {"code": "6282", "source_events": []},
            ],
        }
        sources = [
            SourceItem("S001", "台達電 AI 電源新聞", "https://example.com/2308", "Level 2", snippet="2308 台達電"),
        ]

        package = build_high_model_input_package(
            _request_for("value_scan"),
            data,
            sources,
            prompt_chars_estimate=120_000,
        )

        coverage = package["input_quality_gate"]["candidate_source_coverage"]
        self.assertEqual(coverage["candidate_count"], 2)
        self.assertEqual(coverage["with_external_source_count"], 1)
        self.assertEqual(coverage["zero_external_source_candidates"][0]["code"], "6282")
        self.assertTrue(any("缺少可對應的外部搜尋來源" in warning for warning in package["input_quality_gate"]["warnings"]))

    def test_value_scan_high_payload_uses_candidate_summary_not_raw_chip_data(self) -> None:
        data = {
            "candidate_pool": "selected",
            "ai_candidates": [
                {
                    "code": "2330",
                    "name": "TSMC",
                    "industry": "semi",
                    "rerating_score": 80,
                    "verification_score": 70,
                    "chip_backup_data": {"very_large_raw_holder_table": ["x"] * 100},
                    "source_events": [{"source_id": "L1", "event": "local"}],
                }
            ],
            "ai_candidate_evidence_pack": [{"code": "2330", "chip_backup_summary": {"holder_count": 10}}],
            "unified_evidence_pack": {
                "schema_version": "unified_evidence_pack_v1",
                "items": [{"source_id": f"S{index}", "payload": {"long": "x" * 1000}} for index in range(50)],
            },
        }

        package = build_high_model_input_package(
            _request_for("value_scan"),
            data,
            [],
            prompt_chars_estimate=1_000_000,
        )
        candidate = package["command_specific_data"]["payload"]["ai_candidates"][0]

        self.assertIn("source_event_summary", candidate)
        self.assertNotIn("chip_backup_data", candidate)
        self.assertEqual(candidate["raw_candidate_location"], "structured_data.json.ai_candidates")
        evidence_pack = package["command_specific_data"]["payload"]["unified_evidence_pack"]
        self.assertLess(len(str(evidence_pack)), 10000)
        self.assertEqual(evidence_pack["item_count"], 50)

    def test_value_scan_high_payload_uses_evidence_summary_not_full_raw_pack(self) -> None:
        long_note = "long raw detail " * 500
        data = {
            "ai_candidates": [
                {
                    "code": "2308",
                    "name": "台達電",
                    "rerating_score": 91,
                    "verification_score": 76,
                    "rerating_reason": "AI 電源需求重估",
                    "rerating_evidence": [{"source_id": "S001", "summary": "訂單能見度提高"}],
                    "counter_evidence": [{"source_id": "S002", "summary": "毛利率仍待驗證"}],
                    "failure_conditions": ["AI 客戶拉貨不如預期"],
                    "financial_detail": {
                        "revenue_yoy": 12.5,
                        "operating_margin": 9.8,
                        "raw_notes": long_note,
                    },
                    "company_knowledge": {
                        "products": ["AI 電源", "散熱"],
                        "raw_customer_notes": long_note,
                    },
                    "source_events": [{"source_id": "S001", "title": "AI 電源成長", "raw": long_note}],
                }
            ],
            "ai_candidate_evidence_pack": [
                {
                    "code": "2308",
                    "name": "台達電",
                    "rerating_score": 91,
                    "verification_score": 76,
                    "rerating_reason": "AI 電源需求重估",
                    "rerating_evidence": [{"source_id": "S001", "summary": "訂單能見度提高"}],
                    "counter_evidence": [{"source_id": "S002", "summary": "毛利率仍待驗證"}],
                    "failure_conditions": ["AI 客戶拉貨不如預期"],
                    "financial_detail": {
                        "revenue_yoy": 12.5,
                        "operating_margin": 9.8,
                        "raw_notes": long_note,
                    },
                    "cross_validation": {
                        "status": "partial",
                        "verified_points": ["新聞與營收方向一致"],
                        "raw_validation_notes": long_note,
                    },
                    "source_events": [{"source_id": "S001", "title": "AI 電源成長", "raw": long_note}],
                    "raw_blob": long_note,
                }
            ],
        }

        package = build_high_model_input_package(
            _request_for("value_scan"),
            data,
            _sources(),
            prompt_chars_estimate=1_600_000,
        )
        payload = package["command_specific_data"]["payload"]
        candidate = payload["ai_candidates"][0]
        evidence_summary = payload["ai_candidate_evidence_summary"]
        payload_text = json.dumps(payload, ensure_ascii=False)

        self.assertIn("financial_key_metrics", candidate)
        self.assertEqual(candidate["financial_key_metrics"]["revenue_yoy"], 12.5)
        self.assertIn("company_knowledge_summary", candidate)
        self.assertIn("raw_candidate_location", candidate)
        self.assertNotIn("ai_candidate_evidence_pack", payload)
        self.assertEqual(evidence_summary["schema_version"], "value_scan_evidence_summary_v1")
        self.assertEqual(evidence_summary["candidate_count"], 1)
        self.assertEqual(evidence_summary["candidates"][0]["cross_validation"]["status"], "partial")
        self.assertEqual(evidence_summary["candidates"][0]["rerating_reason"], "AI 電源需求重估")
        self.assertIn("supporting_evidence", evidence_summary["candidates"][0])
        self.assertIn("counter_evidence", evidence_summary["candidates"][0])
        self.assertIn("failure_conditions", evidence_summary["candidates"][0])
        self.assertIn("S001", evidence_summary["candidates"][0]["source_ids"])
        self.assertEqual(evidence_summary["candidates"][0]["local_score_summary"]["rerating_score"], 91)
        self.assertIn("raw_evidence_location", evidence_summary["candidates"][0])
        self.assertNotIn("raw_blob", payload_text)
        self.assertNotIn(long_note, payload_text)
        self.assertLess(len(payload_text), 12000)

    def test_input_quality_gate_ok_when_research_has_enough_sources(self) -> None:
        sources = [
            SourceItem(f"S{index:03d}", f"來源{index}", f"https://example.com/{index}", "Level 2")
            for index in range(1, 9)
        ]

        package = build_high_model_input_package(
            _request(),
            {"stock": {"code": "2330", "name": "台積電"}},
            sources,
            prompt_chars_estimate=100_000,
        )

        self.assertEqual(package["input_quality_gate"]["status"], "ok")
        self.assertEqual(package["input_quality_gate"]["source_count"], 8)
        self.assertEqual(package["input_quality_gate"]["warnings"], [])

    def test_full_mode_excludes_pretruncated_context_from_high_model_package(self) -> None:
        data = {
            "ai_data_center": {
                "source_selection": {"selected_sources": []},
                "nested_snapshot": {"raw": "<dict truncated>"},
            },
            "ai_prompt_context": {"target": "台股"},
            "quantitative_market": {"twse": {"score": 70}},
            "volatility": {"vix_proxy": 20},
            "industry_flow": [{"industry": "半導體", "flow": 1}],
            "fear_greed": {"score": 55},
            "market_score": {"total": 70},
            "global_public_macro": [{"event": "Fed"}],
            "unified_evidence_pack": {"items": [{"source_id": "S001", "summary": "macro evidence"}]},
            "low_model_digest": {
                "schema_version": LOW_MODEL_DIGEST_SCHEMA_VERSION,
                "status": "success",
                "facts": [{"fact": "macro fact", "source_ids": ["S001"]}],
                "source_map": [{"source_id": "S001", "title": "news"}],
            },
        }

        package = build_high_model_input_package(
            _request_for("macro"),
            data,
            _sources(),
            prompt_chars_estimate=120_000,
        )

        self.assertEqual(package["input_mode"], "full")
        self.assertIsNone(package["ai_data_center"])
        self.assertIsNotNone(package["ai_data_center_summary"])
        package_text = json.dumps(package, ensure_ascii=False)
        self.assertNotIn("<dict truncated>", package_text)
        self.assertNotIn("<list truncated>", package_text)

    def test_all_ai_commands_keep_core_sections_in_high_model_package(self) -> None:
        commands = [
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
        ]
        base_data = {
            "stock": {"code": "2330", "name": "台積電"},
            "price_data": {"close": 100},
            "technical_data": {"trend_score": 80},
            "institutional_data": [{"date": "2026-06-01", "net_buy": 1}],
            "margin_data": [{"date": "2026-06-01", "margin_balance": 1}],
            "revenue_data": [{"month": "2026-05", "yoy": 10}],
            "financial_data": [{"quarter": "2026Q1", "eps": 1}],
            "local_scoring": {"scores": [{"name": "revenue", "score": 80}]},
            "local_ranking": [{"code": "2330", "score": 80}],
            "topic_context": {"matched_topics": [{"topic": "AI"}]},
            "unified_evidence_pack": {"items": [{"type": "news", "summary": "AI demand"}]},
            "ai_candidates": [{"code": "2330", "name": "台積電", "rerating_score": 80}],
            "ai_candidate_evidence_pack": [{"code": "2330", "evidence": "AI"}],
            "quantitative_market": {"twse": {"score": 70}},
            "volatility": {"vix_proxy": 20},
            "industry_flow": [{"industry": "半導體", "flow": 1}],
            "fear_greed": {"score": 55},
            "market_score": {"total": 70},
            "global_public_macro": [{"event": "Fed"}],
            "theme": {"name": "AI電源"},
            "matched_companies": [{"code": "2308", "name": "台達電"}],
            "supply_chain_profile": {"layers": [{"name": "電源", "companies": ["台達電"]}]},
            "theme_rankings": [{"theme_id": "ai_power", "theme_name": "AI電源", "representative_stocks": [{"code": "2308", "name": "台達電"}]}],
            "sector_rankings": [{"sector": "電子零組件", "representative_stocks": [{"code": "2308", "name": "台達電"}]}],
            "subsector_rankings": [{"subsector": "電源", "strong_samples": [{"code": "2308", "name": "台達電"}]}],
            "strong_stocks": [{"code": "2308", "name": "台達電", "trend_score": 88}],
            "related_stocks": [{"code": "2308", "name": "台達電"}],
            "layers": [{"layer": 1, "representative_stocks": [{"code": "2308", "name": "台達電"}]}],
            "layer_market_validation": [{"layer": 1, "status": "validated"}],
            "next_layer_candidates": [{"code": "6282", "name": "康舒"}],
            "market_movers": {"top_gainers": [{"code": "2308", "name": "台達電"}]},
            "candidates": [{"code": "2308", "name": "台達電", "reason": "radar"}],
            "evidence_pack": {"items": [{"summary": "radar evidence"}]},
            "ai_compact_pack": {"candidates": [{"code": "2308"}]},
            "feature_pack": {"scope": "unit"},
            "data_coverage": {"status": "complete"},
            "news_batch": [{"title": "AI news", "source": "news"}],
            "news_context": {"items": [{"title": "AI news"}]},
            "sources": [{"title": "news", "url": "https://example.com"}],
            "existing_profiles": [{"topic": "AI電源"}],
            "source_candidates": [{"topic": "AI電源", "source": "news"}],
            "candidate_topics": [{"topic": "AI電源"}],
            "candidate_companies": [{"code": "2308", "name": "台達電"}],
            "change_pack": {"updates": [{"topic": "AI電源"}]},
        }
        for command in commands:
            with self.subTest(command=command):
                package = build_high_model_input_package(
                    _request_for(command),
                    dict(base_data),
                    _sources(),
                    prompt_chars_estimate=400_000,
                )
                command_data = package["command_specific_data"]
                self.assertEqual(command_data["schema_version"], "complete_segment_context_v1")
                self.assertEqual(command_data["legacy_schema_version"], "semantic_command_context_v1")
                self.assertIn("完整分段", command_data["policy"])
                self.assertTrue(command_data["payload"])
                sections = command_data["core_input_audit"]["sections"]
                self.assertTrue(sections)
                self.assertGreater(command_data["core_input_audit"]["status_counts"]["direct"], 0)
                payload_text = __import__("json").dumps(command_data["payload"], ensure_ascii=False)
                self.assertNotIn("<list truncated>", payload_text)
                self.assertNotIn("<dict truncated>", payload_text)
                coverage = package["ai_workflow_coverage"]
                self.assertEqual(coverage["schema_version"], AI_WORKFLOW_COVERAGE_SCHEMA_VERSION)
                self.assertEqual(coverage["command"], command)
                self.assertEqual(coverage["status"], "aligned")
                for key in coverage["standard_capabilities"]:
                    self.assertTrue(coverage["checks"][key], f"{command} missing {key}")

    def test_ai_workflow_coverage_reports_partial_when_capability_missing(self) -> None:
        coverage = build_ai_workflow_coverage(
            "news",
            local_data_package=True,
            low_model_digest={"schema_version": LOW_MODEL_DIGEST_SCHEMA_VERSION, "status": "skipped"},
            high_model_input_package=True,
            dedupe_strategy="news_batch_deduped_classification",
            source_index=True,
            input_audit=True,
            html_sections=False,
            diagnostics={"prompt_chars": 1200},
        )
        self.assertEqual(coverage["schema_version"], AI_WORKFLOW_COVERAGE_SCHEMA_VERSION)
        self.assertEqual(coverage["status"], "partial")
        self.assertIn("html_sections", coverage["missing_capabilities"])

    def test_ai_workflow_coverage_allows_not_applicable_capability(self) -> None:
        coverage = build_ai_workflow_coverage(
            "news",
            local_data_package=True,
            low_model_digest={"schema_version": LOW_MODEL_DIGEST_SCHEMA_VERSION, "status": "skipped"},
            high_model_input_package=True,
            dedupe_strategy="news_batch_deduped_classification",
            source_index=True,
            input_audit=True,
            html_sections=False,
            diagnostics={"prompt_chars": 1200},
            not_applicable=["html_sections"],
        )
        self.assertEqual(coverage["status"], "aligned")
        self.assertIn("html_sections", coverage["not_applicable"])
        self.assertNotIn("html_sections", coverage["missing_capabilities"])

    def test_theme_rankings_are_not_required_for_single_theme_report(self) -> None:
        data = {
            "theme": {"name": "功率半導體"},
            "matched_companies": [{"code": "2481", "name": "強茂"}],
            "topic_context": {"matched_topics": [{"topic": "功率半導體"}]},
            "supply_chain_profile": {"layers": [{"name": "功率元件"}]},
            "unified_evidence_pack": {"items": [{"summary": "漲價證據"}]},
        }

        package = build_high_model_input_package(
            _request_for("theme"),
            data,
            _sources(),
            prompt_chars_estimate=400_000,
        )
        rows = {
            item["section"]: item
            for item in package["command_specific_data"]["core_input_audit"]["sections"]
        }

        self.assertEqual(rows["theme_rankings"]["status"], "not_required")
        self.assertEqual(rows["sector_rankings"]["status"], "not_required")
        self.assertIn("本指令不是全市場排行型任務", rows["theme_rankings"]["note"])

    def test_theme_radar_rankings_remain_required_when_missing(self) -> None:
        data = {
            "theme": {"name": "市場題材雷達"},
            "matched_companies": [{"code": "2481", "name": "強茂"}],
            "topic_context": {"matched_topics": [{"topic": "功率半導體"}]},
            "supply_chain_profile": {"layers": [{"name": "功率元件"}]},
            "unified_evidence_pack": {"items": [{"summary": "漲價證據"}]},
        }

        package = build_high_model_input_package(
            _request_for("theme_radar"),
            data,
            _sources(),
            prompt_chars_estimate=400_000,
        )
        rows = {
            item["section"]: item
            for item in package["command_specific_data"]["core_input_audit"]["sections"]
        }

        self.assertEqual(rows["theme_rankings"]["status"], "source_missing")
        self.assertEqual(rows["sector_rankings"]["status"], "source_missing")

    def test_theme_radar_compact_package_preserves_core_topic_company_sector_data(self) -> None:
        stocks = [
            {
                "code": f"23{i:02d}",
                "name": f"測試股{i}",
                "industry": "電子零組件",
                "primary_subsector": "電源",
                "trend_score": 80 + i,
                "change_pct": i,
                "theme_matches": [{"theme_name": "AI電源", "relation_score": 90}],
            }
            for i in range(12)
        ]
        data = {
            "theme": {"name": "AI電源"},
            "matched_companies": stocks,
            "topic_context": {"definition": "AI資料中心電源需求", "catalysts": ["資料中心擴建"]},
            "supply_chain_profile": {"layers": [{"name": "電源供應器", "companies": ["台達電", "康舒"]}]},
            "theme_rankings": [
                {
                    "theme_id": "ai_power",
                    "theme_name": "AI電源",
                    "theme_strength_score": 91,
                    "representative_stocks": stocks[:6],
                    "candidate_stocks": stocks[6:12],
                    "main_risks": ["缺少官方營收占比"],
                }
            ],
            "sector_rankings": [{"sector": "電子零組件", "representative_stocks": stocks[:4]}],
            "subsector_rankings": [{"subsector": "電源", "strong_samples": stocks[:5]}],
            "strong_stocks": stocks,
            "news_theme_stats": [{"theme_name": "AI電源", "news_count": 3}],
            "unified_evidence_pack": {"items": [{"summary": "AI電源證據"}]},
        }
        package = build_high_model_input_package(
            _request_for("theme_radar"),
            data,
            _sources(),
            prompt_chars_estimate=4_600_000,
        )
        payload = package["command_specific_data"]["payload"]
        self.assertEqual(package["input_mode"], "compact")
        self.assertIn("theme_rankings", payload)
        self.assertIn("matched_companies", payload)
        self.assertIn("topic_context", payload)
        self.assertIn("supply_chain_profile", payload)
        self.assertIn("sector_rankings", payload)
        self.assertIn("subsector_rankings", payload)
        self.assertEqual(payload["theme_rankings"][0]["representative_stock_codes"][0], "2300")
        self.assertEqual(payload["theme_rankings"][0]["candidate_stock_codes"][0], "2306")
        self.assertEqual(len(payload["theme_rankings"][0]["representative_stock_codes"]), 6)
        self.assertEqual(len(payload["theme_rankings"][0]["candidate_stock_codes"]), 6)
        self.assertEqual(len(payload["strong_stocks"]["stock_codes"]), 12)
        self.assertEqual(len(payload["stock_index"]), 12)
        self.assertNotIn("omitted_counts", payload["theme_rankings"][0])
        self.assertIsNone(package["ai_data_center"])
        self.assertIsNone(package["ai_prompt_context"])
        self.assertIn("ai_data_center_summary", package)
        self.assertIn("ai_prompt_context_summary", package)
        payload_text = __import__("json").dumps(payload, ensure_ascii=False)
        self.assertNotIn("<list truncated>", payload_text)
        self.assertNotIn("<dict truncated>", payload_text)

    def test_sector_strength_compact_package_uses_stock_index_relations(self) -> None:
        stocks = [
            {
                "code": f"31{i:02d}",
                "name": f"類股股{i}",
                "industry": "電子零組件",
                "change_pct": 9 - i * 0.1,
                "volume_ratio": 2.5,
                "trend_score": 80 + i,
            }
            for i in range(16)
        ]
        data = {
            "market_movers": {
                "market_data_date": "2026-06-05",
                "top_gainers": stocks[:10],
                "top_volume_surge": stocks[4:14],
                "sector_mover_rankings": [
                    {"sector": "電子零組件", "sector_score": 95, "top_gainers": stocks[:6]},
                ],
            },
            "sector_rankings": [
                {
                    "sector": "電子零組件",
                    "sector_score": 92,
                    "sector_strong_samples": stocks[:8],
                    "representative_stocks": stocks[:3],
                    "candidate_stocks": stocks[3:8],
                }
            ],
            "subsector_rankings": [
                {"subsector": "被動元件", "subsector_score": 88, "strong_samples": stocks[8:16]},
            ],
            "strong_stocks": stocks,
            "theme_rankings": [
                {"theme_id": "passive", "theme_name": "被動元件", "representative_stocks": stocks[:4]},
            ],
            "unified_evidence_pack": {"items": [{"summary": "族群強勢證據", "source_id": "S001"}]},
        }

        package = build_high_model_input_package(
            _request_for("sector_strength"),
            data,
            _sources(),
            prompt_chars_estimate=4_600_000,
        )

        payload = package["command_specific_data"]["payload"]
        self.assertEqual(payload["schema_version"], "sector_strength_relation_payload_v1")
        self.assertEqual(len(payload["stock_index"]), 16)
        self.assertEqual(payload["market_movers"]["top_gainers_codes"][0], "3100")
        self.assertEqual(payload["sector_rankings"][0]["sector_strong_codes"][0], "3100")
        self.assertEqual(payload["sector_rankings"][0]["representative_stock_codes"], ["3100", "3101", "3102"])
        self.assertEqual(payload["subsector_rankings"][0]["strong_stock_codes"][0], "3108")
        self.assertIn("representation_policy", payload)
        payload_text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('"sector_strong_samples"', payload_text)
        self.assertNotIn('"top_gainers": [', payload_text)
        self.assertNotIn("<list truncated>", payload_text)
        self.assertNotIn("<dict truncated>", payload_text)

    def test_unified_evidence_pack_is_summarized_in_command_payload(self) -> None:
        data = {
            "stock": {"code": "2330", "name": "台積電"},
            "unified_evidence_pack": {
                "items": [
                    {
                        "source_id": f"S{i:03d}",
                        "title": f"來源{i}",
                        "summary": "AI demand " + ("x" * 2000),
                    }
                    for i in range(60)
                ]
            },
        }

        package = build_high_model_input_package(
            _request_for("research"),
            data,
            _sources(),
            prompt_chars_estimate=4_600_000,
        )
        evidence = package["command_specific_data"]["payload"]["unified_evidence_pack"]
        self.assertEqual(evidence["item_count"], 60)
        self.assertLessEqual(len(evidence["items"]), 30)
        evidence_text = json.dumps(evidence, ensure_ascii=False)
        self.assertNotIn("x" * 1000, evidence_text)

    def test_theme_radar_flow_layers_use_stock_code_refs_not_full_stock_rows(self) -> None:
        stock = {
            "code": "2330",
            "name": "TSMC",
            "price": 1000,
            "theme_matches": [
                {
                    "theme_id": "ai",
                    "theme_name": "AI",
                    "relation_score": 90,
                    "evidence": [{"content": "very long evidence" * 50}],
                    "risk_notes": ["risk"],
                    "missing_data": ["missing"],
                }
            ],
        }
        data = {
            "theme_rankings": [{"theme_id": "ai", "theme_name": "AI", "representative_stocks": [stock]}],
            "strong_stocks": [stock],
            "theme_flow_summaries": [
                {
                    "theme_query": "AI",
                    "related_stocks": [stock],
                    "layers": [
                        {
                            "layer": 1,
                            "name": "core",
                            "display_stock_groups": {"verified_representatives": [stock]},
                        }
                    ],
                }
            ],
        }
        package = build_high_model_input_package(
            _request_for("theme_radar"),
            data,
            _sources(),
            prompt_chars_estimate=4_600_000,
        )
        flow = package["command_specific_data"]["payload"]["theme_flow_summaries"][0]
        self.assertEqual(flow["related_stock_codes"], ["2330"])
        self.assertEqual(
            flow["layers"][0]["display_stock_group_codes"]["verified_representatives_codes"],
            ["2330"],
        )
        flow_text = __import__("json").dumps(flow, ensure_ascii=False)
        self.assertNotIn("very long evidence", flow_text)

    def test_prompt_registry_uses_high_model_input_package_for_balanced_mode(self) -> None:
        data = {
            "analysis_model": "gemini",
            "high_model_input_mode": "balanced",
            "high_model_input_package": {
                "schema_version": HIGH_MODEL_INPUT_SCHEMA_VERSION,
                "input_mode": "balanced",
                "target": "2330",
                "low_model_digest": {"facts": [{"fact": "AI demand"}]},
            },
            "financial_data": {"raw": "x" * 1000},
        }
        prompt = build_prompt_from_request(_request(), data, _sources())
        self.assertIn("high_model_input_package", prompt)
        self.assertIn("high_model_input_package", prompt)
        self.assertNotIn('"financial_data"', prompt)

    def test_prompt_registry_uses_high_model_input_package_for_full_mode(self) -> None:
        data = {
            "analysis_model": "minimax",
            "high_model_input_mode": "full",
            "high_model_input_package": {
                "schema_version": HIGH_MODEL_INPUT_SCHEMA_VERSION,
                "input_mode": "full",
                "target": "台股",
                "command_specific_data": {
                    "payload": {
                        "market_scope": "台股",
                        "macro_core": {"summary": "market breadth"},
                    }
                },
            },
            "ai_prompt_context": {
                "legacy_raw_dump": "<dict truncated>",
            },
            "quantitative_market": {
                "raw": "x" * 1000,
            },
        }
        prompt = build_prompt_from_request(_request_for("macro"), data, _sources())
        self.assertIn("high_model_input_package", prompt)
        self.assertIn("market breadth", prompt)
        self.assertNotIn("<dict truncated>", prompt)
        self.assertNotIn('"quantitative_market"', prompt)

    def test_low_model_digest_saves_artifacts_and_reuses_cache(self) -> None:
        root = Path(".cache") / "test_tmp" / f"ai_workflow_low_model_{uuid4().hex}"
        shutil.rmtree(root, ignore_errors=True)
        response = """
        {
          "schema_version": "low_model_digest_v1",
          "status": "success",
          "facts": [{"fact": "AI demand", "source_ids": ["S001"]}],
          "source_map": [{"source_id": "S001", "title": "news"}]
        }
        """
        try:
            with patch("research_center.ai_workflow_service.LOW_MODEL_ARTIFACT_DIR", root), patch(
                "research_center.ai_workflow_service.write_prompt_log",
                return_value=Path("logs/ai_prompts/generic.json"),
            ):
                first = run_low_model_digest_for_payload(
                    _request(),
                    {"command": "research", "items": [{"title": "AI"}]},
                    sources=_sources(),
                    minimax=FakeMiniMax(response),
                    enabled=True,
                    purpose="unit_test_cache",
                )
                second = run_low_model_digest_for_payload(
                    _request(),
                    {"command": "research", "items": [{"title": "AI"}]},
                    sources=_sources(),
                    minimax=FakeMiniMax(error=RuntimeError("should not run")),
                    enabled=True,
                    purpose="unit_test_cache",
                )
            self.assertEqual(first["status"], "success")
            self.assertTrue(Path(first["artifact_paths"]["json_path"]).exists())
            self.assertEqual(second["status"], "cached")
            self.assertTrue(second["cache_hit"])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_low_model_validation_flags_missing_digest(self) -> None:
        validation = validate_low_model_digest({})
        self.assertFalse(validation["valid"])
        self.assertTrue(validation["warnings"])


if __name__ == "__main__":
    unittest.main()
