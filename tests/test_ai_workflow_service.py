from __future__ import annotations

import unittest
import shutil
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from research_center.ai_workflow_service import (
    HIGH_MODEL_INPUT_SCHEMA_VERSION,
    LOW_MODEL_DIGEST_SCHEMA_VERSION,
    _digest_fingerprint,
    attach_low_model_digest,
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
    model = "MiniMax-M2.7"

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


def _request() -> CommandRequest:
    return CommandRequest(command="research", raw_text="/research 2330", target="2330", mode="deep")


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
        self.assertIn("嚴禁產出最終投資結論", prompt)
        self.assertIn("只輸出可被 json.loads()", prompt)
        self.assertIn("low_model_digest_v1", prompt)
        self.assertIn("低階資料整理模型", prompt)
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
        self.assertEqual(digest["model"], "MiniMax-M2.7")
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
                    "low_model_model": "MiniMax-M2.7",
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
        self.assertIn("MiniMax-M2.7", html)
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
        self.assertIn("完整資料保留", package["workflow_policy"])
        self.assertEqual(package["low_model_validation"]["status"], "success")

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
        self.assertIn("完整原始資料仍保存在報告 JSON", prompt)
        self.assertNotIn('"financial_data"', prompt)

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
        self.assertIn("未取得低階模型資料包。", validation["warnings"])


if __name__ == "__main__":
    unittest.main()
