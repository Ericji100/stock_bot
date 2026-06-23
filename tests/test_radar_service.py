from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

import radar_service as radar
from research_center.models import SourceItem


class RadarDataMaximizationTests(unittest.TestCase):
    def test_normalise_ai_sources_preserves_all_sources_and_fetch_details_unittest(self):
        sources = [
            SourceItem(
                source_id=f"S{i:03d}",
                title=f"title {i}",
                url=f"https://example.com/{i}",
                source_level="Level 3",
                provider="minimax",
                provider_detail="web_search",
                fetch_provider="requests",
                fetch_status="ok",
                snippet=f"snippet {i}",
            )
            for i in range(12)
        ]
        structured_data = {
            "web_fetched_sources": [
                {
                    "title": "full text",
                    "url": "https://example.com/full",
                    "provider": "web_fetch",
                    "fetch_provider": "beautifulsoup",
                    "fetch_status": "ok",
                    "content": "complete article body",
                }
            ]
        }

        items = radar._normalise_ai_sources(sources, structured_data)

        self.assertEqual(len(items), 13)
        self.assertEqual(items[0]["provider_detail"], "web_search")
        self.assertEqual(items[0]["fetch_provider"], "requests")
        self.assertEqual(items[-1]["content"], "complete article body")

    def test_radar_evidence_pack_has_three_layer_context_and_sufficiency(self):
        candidate = radar.RadarCandidate(
            code="2330",
            name="TSMC",
            ai_sources=[
                {"title": f"source {i}", "url": f"https://example.com/{i}", "provider": "minimax"}
                for i in range(3)
            ],
            score_components={"technical": 20},
            total_score=20,
        )
        candidate.data_coverage = radar._build_radar_data_coverage(candidate)

        pack = radar._build_radar_evidence_pack(candidate, date(2026, 5, 22))

        self.assertIn("raw_sources", pack)
        self.assertIn("final_context", pack)
        self.assertIn("three_layer_context", pack)
        self.assertEqual(pack["three_layer_context"]["schema_version"], "three_layer_context_v1")
        self.assertEqual(pack["three_layer_context"]["source_sufficiency"]["source_count"], 6)
        self.assertFalse(pack["three_layer_context"]["source_sufficiency"]["sufficient"])
        self.assertEqual(candidate.data_coverage["checks"]["source_sufficiency"], "insufficient")

    def test_ensure_radar_source_sufficiency_appends_without_dropping_existing_sources(self):
        candidate = radar.RadarCandidate(
            code="2330",
            name="TSMC",
            ai_sources=[{"title": "existing", "url": "https://example.com/existing"}],
        )

        def fake_attach(candidates, ai_codes, analysis_date, progress):
            self.assertEqual(ai_codes, ["2330"])
            candidates[0].web_sources.extend(
                [
                    {"title": f"extra {i}", "url": f"https://example.com/extra-{i}"}
                    for i in range(8)
                ]
            )

        with patch.object(radar, "_attach_web_sources", side_effect=fake_attach):
            radar._ensure_radar_source_sufficiency([candidate], ["2330"], date(2026, 5, 22), None)

        self.assertEqual(candidate.ai_sources[0]["title"], "existing")
        self.assertEqual(radar._candidate_external_source_count(candidate), 9)

    def test_attach_ai_comments_splits_to_single_stock_jobs_when_prompt_is_large_unittest(self):
        candidate = radar.RadarCandidate(
            code="2330",
            name="TSMC",
            score_components={"technical": 20, "revenue": 10, "chip": 5, "theme": 5, "market": 5},
            total_score=45,
            ai_sources=[
                {"title": f"source {i}", "url": f"https://example.com/{i}", "snippet": "x" * 400}
                for i in range(8)
            ],
            evidence_pack={
                "research_structured_data": {
                    "financial_data": [{"Quarter": "2026Q1", "note": "y" * 800}],
                    "topic_context": {"summary": "z" * 800},
                },
                "research_sources": [
                    {"title": f"research {i}", "url": f"https://research.example.com/{i}", "snippet": "r" * 300}
                    for i in range(5)
                ],
            },
        )
        calls = []

        def fake_call(model, prompt):
            calls.append(prompt)
            return json.dumps(
                {
                    "comments": [
                        {
                            "code": "2330",
                            "priority": "高",
                            "confidence": "中",
                            "reason": "compact evidence reviewed",
                            "risk": "data quality",
                            "watch": "revenue",
                        }
                    ]
                }
            )

        with patch.object(radar, "RADAR_AI_PROMPT_MAX_CHARS", 7000), \
             patch.object(radar, "_attach_radar_low_model_digest", return_value={}), \
             patch.object(radar, "_call_ai_comment_model", side_effect=fake_call):
            meta = radar._attach_ai_comments([candidate], ["2330"], "deepseek", date(2026, 5, 22), None)

        self.assertEqual(candidate.ai_comment["status"], "ok")
        self.assertEqual(candidate.ai_comment["reason"], "compact evidence reviewed")
        self.assertEqual(meta["chunks"][0]["status"], "ok")
        self.assertEqual(meta["chunks"][0]["jobs"][0]["profile"], "minimal")
        self.assertEqual(meta["ai_workflow_coverage"]["schema_version"], "ai_workflow_coverage_v1")
        self.assertEqual(meta["ai_workflow_coverage"]["status"], "aligned")
        self.assertEqual(len(calls), 1)


class RadarAiBudgetAndStabilityTests(unittest.TestCase):
    def test_ai_enrichment_respects_total_ai_top_budget_unittest(self):
        a1 = radar.RadarCandidate(code="1111", strategy_codes={"A"}, total_score=80)
        a2 = radar.RadarCandidate(code="2222", strategy_codes={"A"}, total_score=70)
        c1 = radar.RadarCandidate(code="3333", strategy_codes={"C"}, total_score=90)
        self.assertEqual(radar._select_ai_enrichment_codes([a1, a2, c1], 1), ["1111"])

    def test_ai_enrichment_keeps_strategy_diversity_within_budget_unittest(self):
        a1 = radar.RadarCandidate(code="1111", strategy_codes={"A"}, total_score=80)
        a2 = radar.RadarCandidate(code="2222", strategy_codes={"A"}, total_score=120)
        b1 = radar.RadarCandidate(code="3333", strategy_codes={"B"}, total_score=70)
        c1 = radar.RadarCandidate(code="4444", strategy_codes={"C"}, total_score=60)
        self.assertEqual(radar._select_ai_enrichment_codes([a1, a2, b1, c1], 3), ["2222", "3333", "4444"])

    def test_attach_chip_scores_uses_lightweight_radar_context_unittest(self):
        radar._RADAR_CHIP_GRADE_CACHE.clear()
        candidate = radar.RadarCandidate(code="2330")
        captured: dict[str, object] = {}

        def fake_build_market_context(*args, **kwargs):
            captured.update(kwargs)
            return object()

        with patch.object(radar, "build_market_context", side_effect=fake_build_market_context), patch.object(
            radar,
            "build_chip_grade_maps",
            return_value={"chip_1": {}, "chip_2": {}, "chip_3": {}, "chip_4": {}},
        ):
            radar._attach_chip_scores([candidate], date(2026, 5, 20), None)

        self.assertEqual(captured["target_trading_days"], 5)
        self.assertIs(captured["include_foreign_ratio"], False)
        self.assertEqual(captured["scope"], "radar")

    def test_chip_grade_maps_reused_between_candidates_and_scoring_unittest(self):
        radar._RADAR_CHIP_GRADE_CACHE.clear()
        calls = {"count": 0}

        def fake_build_market_context(*args, **kwargs):
            calls["count"] += 1
            return object()

        with patch.object(radar, "build_market_context", side_effect=fake_build_market_context), \
             patch.object(
                 radar,
                 "build_chip_grade_maps",
                 return_value={"chip_1": {"2330": "A"}, "chip_2": {}, "chip_3": {}, "chip_4": {}},
             ), \
             patch.object(radar, "_stock_meta_by_code", return_value={}):
            candidates, policy = radar._chip_candidates(date(2026, 5, 20))
            radar._attach_chip_scores(candidates, date(2026, 5, 20), None)

        self.assertEqual(calls["count"], 1)
        self.assertEqual(candidates[0].code, "2330")
        self.assertEqual(policy["candidate_count"], 1)

    def test_save_radar_result_uses_slim_cache_and_artifact_candidates_unittest(self):
        cache_path = Path(".cache") / "radar_unittest_slim_cache.json"
        report_dir = Path(".cache") / "radar_unittest_slim_reports"
        result = radar.RadarResult(
            request=radar.RadarRequest(source="technical", report_date=date(2026, 5, 20), ai_top=5),
            report_date=date(2026, 5, 20),
            candidates=[
                radar.RadarCandidate(
                    code="2330",
                    name="台積電",
                    technical_signals=[{"signal_date": pd.Timestamp("2026-05-20")}],
                    strategy_codes={"A"},
                    total_score=80,
                )
            ],
            ai_enriched_codes=[],
            diagnostics={},
        )

        with patch.object(radar, "RADAR_CACHE_PATH", cache_path), patch.object(radar, "RADAR_REPORT_DIR", report_dir):
            radar.save_radar_result(result)
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            loaded = radar.load_radar_result(date(2026, 5, 20))

        self.assertNotIn("candidates", payload[0])
        self.assertTrue(Path(payload[0]["artifact_paths"]["candidates"]).exists())
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.candidates[0].code, "2330")
        self.assertEqual(loaded.candidates[0].technical_signals[0]["signal_date"], "2026-05-20T00:00:00")

    def test_large_radar_cache_rebuilds_from_artifact_index_unittest(self):
        cache_path = Path(".cache") / "radar_unittest_large_cache.json"
        report_dir = Path(".cache") / "radar_unittest_large_reports"
        radar_dir = report_dir / "2026-05-20" / "radar_20260520_120000"
        radar_dir.mkdir(parents=True, exist_ok=True)
        (radar_dir / "radar_candidates.json").write_text(
            json.dumps([{"code": "2330", "name": "台積電", "total_score": 88}], ensure_ascii=False),
            encoding="utf-8",
        )
        for name in ("radar_summary.md", "evidence_pack.json", "ai_analysis.json", "sources.json"):
            (radar_dir / name).write_text("{}" if name.endswith(".json") else "summary", encoding="utf-8")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("x" * 200, encoding="utf-8")

        with patch.object(radar, "RADAR_CACHE_PATH", cache_path), \
             patch.object(radar, "RADAR_REPORT_DIR", report_dir), \
             patch.object(radar, "RADAR_CACHE_MAX_BYTES", 10):
            records = radar._load_radar_records(limit=5)
            result = radar._record_to_result(records[0])

        self.assertEqual(records[0]["schema_version"], "radar_cache_index_v2")
        self.assertTrue(Path(records[0]["artifact_paths"]["candidates"]).exists())
        self.assertEqual(result.candidates[0].code, "2330")
        self.assertIn("radar_cache_index_v2", cache_path.read_text(encoding="utf-8"))

    def test_research_center_sources_skip_fallback_when_minimax_has_enough_sources_unittest(self):
        candidate = radar.RadarCandidate(code="2330", name="台積電")

        class FakeRunner:
            def __init__(self):
                self.tavily_called = False
                self.gemini_called = False

            def _run_minimax_mcp(self, request, tasks, sources, structured_data, progress):
                for idx in range(8):
                    sources.append(
                        SourceItem(
                            source_id=f"S{idx}",
                            title=f"source {idx}",
                            url=f"https://example.com/{idx}",
                            source_level="web",
                            provider="minimax",
                            published_date="2026-05-20",
                        )
                    )

            def _run_tavily(self, *args, **kwargs):
                self.tavily_called = True

            def _should_run_gemini(self, *args, **kwargs):
                return True

            def _run_gemini(self, *args, **kwargs):
                self.gemini_called = True

        runner = FakeRunner()

        class FakeCenter:
            _gemini_discovery_runner = runner

        with patch.object(radar, "ResearchCenter", return_value=FakeCenter()), patch.object(
            radar, "_enrich_sources_with_web_fetch", return_value=None
        ):
            radar._attach_research_center_sources([candidate], ["2330"], date(2026, 5, 20), None)

        self.assertFalse(runner.tavily_called)
        self.assertFalse(runner.gemini_called)
        self.assertEqual(len(candidate.ai_sources), 8)

    def test_call_ai_comment_model_retries_temporary_overload_unittest(self):
        calls = {"count": 0}

        def fake_call_once(model, prompt):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("MiniMax API request failed; status=529; overloaded")
            return '{"comments":[]}'

        with patch.object(radar, "_call_ai_comment_model_once", side_effect=fake_call_once), patch.object(
            radar.time, "sleep", return_value=None
        ):
            self.assertEqual(radar._call_ai_comment_model("minimax", "prompt"), '{"comments":[]}')
        self.assertEqual(calls["count"], 2)

    def test_ai_comment_payload_uses_compact_pack_not_full_evidence_unittest(self):
        candidate = radar.RadarCandidate(
            code="2330",
            name="台積電",
            ai_sources=[
                {"title": f"source {i}", "url": f"https://example.com/{i}", "snippet": "x" * 500}
                for i in range(20)
            ],
            evidence_pack={
                "research_structured_data": {
                    "financial_data": [{"Quarter": f"2026Q{i}", "note": "y" * 800} for i in range(8)],
                },
                "research_sources": [
                    {"title": f"research {i}", "url": f"https://research.example.com/{i}", "snippet": "r" * 500}
                    for i in range(20)
                ],
            },
        )

        payload = radar._build_ai_comment_payload(candidate, date(2026, 5, 22))

        self.assertNotIn("evidence_pack", payload)
        self.assertIn("ai_compact_pack", payload)
        compact_payload = payload["ai_compact_pack"]["payload"]
        self.assertLessEqual(len(compact_payload["news"]["ai_sources"]), radar.RADAR_AI_COMPACT_SOURCE_LIMIT)
        self.assertLessEqual(len(compact_payload["research_summary"]["financial_data"]), 4)

    def test_ai_comment_payload_replaces_internal_truncation_markers_unittest(self):
        candidate = radar.RadarCandidate(
            code="2330",
            name="台積電",
            technical_signals=[
                {
                    "stock_id": "2330",
                    "ma_context": {"ma5": 1, "ma10": 2, "ma20": 3, "ma60": 4, "extra": {"deep": "x"}},
                    "features": {"nested": {"deep": {"value": "x"}}},
                }
            ],
        )

        payload = radar._build_ai_comment_payload(candidate, date(2026, 5, 22), compact_profile="minimal")
        payload_text = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("<dict truncated>", payload_text)
        self.assertNotIn("<list truncated>", payload_text)
        self.assertIn("深層欄位未放入 AI 短評 prompt", payload_text)

    def test_ai_prompt_requires_traditional_chinese_unittest(self):
        candidate = radar.RadarCandidate(code="2330", name="台積電")

        prompt = radar._build_ai_comment_prompt([candidate], date(2026, 5, 22))

        self.assertIn("繁體中文", prompt)
        self.assertIn("ai_compact_pack", prompt)
        self.assertIn("MiniMax M3 批次資料整理底稿", prompt)
        self.assertIn("禁止整段英文分析", prompt)
        self.assertIn("不得新增候選股票", prompt)
        self.assertIn("不得改變本地分數", prompt)
        self.assertIn("priority=高", prompt)
        self.assertIn("confidence=高", prompt)
        self.assertIn("資料覆蓋為 insufficient", prompt)

    def test_ai_prompt_includes_low_model_digest_for_selected_codes_unittest(self):
        candidate = radar.RadarCandidate(code="2330", name="台積電")
        digest = {
            "status": "success",
            "facts": [
                {"fact": "2330 來源證據完整", "source_ids": ["S001"]},
                {"fact": "2454 其他候選資料", "source_ids": ["S002"]},
            ],
        }

        prompt = radar._build_ai_comment_prompt([candidate], date(2026, 5, 22), low_model_digest=digest)

        self.assertIn("2330 來源證據完整", prompt)
        self.assertNotIn("2454 其他候選資料", prompt)

    def test_radar_report_truncates_ai_comment_lines_unittest(self):
        candidate = radar.RadarCandidate(
            code="2330",
            name="台積電",
            ai_comment={
                "status": "ok",
                "reason": "理由" * 120,
                "risk": "風險" * 120,
                "watch": "觀察" * 120,
            },
        )

        lines = radar._ai_comment_lines(candidate)

        self.assertEqual(len(lines), 3)
        self.assertTrue(all(len(line) < 190 for line in lines))
        self.assertTrue(all(line.endswith("...") for line in lines))


def test_parse_radar_args_defaults_to_combined_top15():
    request = radar.parse_radar_args([])
    assert request.source == "combined"
    assert request.report_date is None
    assert request.ai_top == 15
    assert request.model == "minimax"


def test_parse_radar_args_accepts_source_date_ai_top():
    request = radar.parse_radar_args(["--source", "curated", "--date", "2026-05-20", "--ai-top", "3"])
    assert request.source == "curated"
    assert request.report_date == date(2026, 5, 20)
    assert request.ai_top == 3


def test_parse_radar_args_accepts_model_and_no_ai_comment():
    request = radar.parse_radar_args(["--model", "deepseek", "--no-ai-comment"])
    assert request.model == "deepseek"
    assert request.ai_comment_enabled is False


def test_run_radar_accepts_raw_arg_list(monkeypatch):
    captured = {}

    def fake_load_candidates(source, target_date, scan_settings, config, progress):
        captured["source"] = source
        captured["target_date"] = target_date
        return [], {"source": source}

    monkeypatch.setattr(radar, "_load_candidates", fake_load_candidates)

    result = radar.run_radar(["--source", "technical", "--date", "2026-05-20", "--ai-top", "5"])

    assert result.request.source == "technical"
    assert result.request.report_date == date(2026, 5, 20)
    assert result.report_date == date(2026, 5, 20)
    assert captured == {"source": "technical", "target_date": date(2026, 5, 20)}


def test_combined_candidates_merges_technical_chip_and_financial(monkeypatch):
    tech = radar.RadarCandidate(code="2330", name="台積電", source_labels=["技術"])
    chip = radar.RadarCandidate(code="2241", name="艾姆勒", chip_grades={"chip_1": "B"}, source_labels=["籌碼"])
    financial = radar.RadarCandidate(
        code="2241",
        name="艾姆勒",
        revenue_history=[{"month": "2026-04-01", "yoy": 22}],
        source_labels=["財報營收"],
    )

    monkeypatch.setattr(radar, "_technical_candidates_for_radar", lambda *args, **kwargs: ([tech], {"source": "technical"}))
    monkeypatch.setattr(radar, "_chip_candidates", lambda *args, **kwargs: ([chip], {"source": "chip"}))
    monkeypatch.setattr(radar, "_financial_candidates", lambda *args, **kwargs: ([financial], {"source": "financial"}))
    monkeypatch.setattr(radar, "_curated_candidates", lambda *args, **kwargs: ([], {"source": "curated"}))

    candidates, policy = radar._load_candidates("combined", date(2026, 5, 22), {}, {}, None)
    by_code = {item.code: item for item in candidates}

    assert policy["status"] == "combined"
    assert set(by_code) == {"2330", "2241"}
    assert by_code["2241"].chip_grades["chip_1"] == "B"
    assert by_code["2241"].revenue_history
    assert any("跨來源" in label for label in by_code["2241"].source_labels)


def test_resolve_radar_report_date_uses_previous_trading_day_on_holiday(monkeypatch):
    monkeypatch.setattr(radar, "get_tw_today", lambda: date(2026, 5, 24))
    monkeypatch.setattr(radar, "is_possible_trading_day", lambda value: value == date(2026, 5, 22))

    resolved, note = radar.resolve_radar_report_date(None)

    assert resolved == date(2026, 5, 22)
    assert "今天 2026-05-24 不是交易日" in note


def test_run_radar_without_date_passes_latest_trading_day(monkeypatch):
    captured = {}
    monkeypatch.setattr(radar, "get_tw_today", lambda: date(2026, 5, 24))
    monkeypatch.setattr(radar, "is_possible_trading_day", lambda value: value == date(2026, 5, 22))

    def fake_load_candidates(source, target_date, scan_settings, config, progress):
        captured["target_date"] = target_date
        return [], {"source": source}

    monkeypatch.setattr(radar, "_load_candidates", fake_load_candidates)

    result = radar.run_radar(radar.RadarRequest(source="technical", ai_top=0, ai_comment_enabled=False))

    assert captured["target_date"] == date(2026, 5, 22)
    assert result.report_date == date(2026, 5, 22)
    assert "不是交易日" in result.diagnostics["date_note"]


def test_research_evidence_pack_timeout_marks_candidate_and_continues(monkeypatch):
    candidate = radar.RadarCandidate(code="2330", name="台積電")
    messages = []

    def fake_collect(*args, **kwargs):
        raise TimeoutError("單檔 Evidence Pack 超過 1 秒")

    monkeypatch.setattr(radar, "_collect_structured_data_with_timeout", fake_collect)

    radar._attach_research_evidence_packs(
        [candidate],
        ["2330"],
        date(2026, 5, 22),
        messages.append,
    )

    assert candidate.evidence_pack["research_structured_timeout"] is True
    assert "超過 1 秒" in candidate.evidence_pack["research_structured_error"]
    assert any("逾時跳過" in message for message in messages)


def test_research_evidence_pack_timeout_marks_candidate_and_continues(monkeypatch):
    candidate = radar.RadarCandidate(code="2330", name="Test")
    messages = []
    called = False

    def fake_collect(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Radar should not run full research collection")

    monkeypatch.setattr(radar, "_collect_structured_data_with_timeout", fake_collect)
    monkeypatch.setattr(radar, "load_research_structured_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(radar, "load_latest_research_structured_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(radar, "_load_radar_light_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(radar, "_save_radar_light_cache", lambda *args, **kwargs: None)

    radar._attach_research_evidence_packs([candidate], ["2330"], date(2026, 5, 22), messages.append)

    assert called is False
    assert candidate.evidence_pack["research_pack_mode"] == "light_generated"
    assert candidate.evidence_pack["research_structured_data"]["radar_research_mode"] == "light_generated"
    assert any("輕量" in message for message in messages)


def test_research_evidence_pack_uses_recent_full_cache(monkeypatch):
    candidate = radar.RadarCandidate(code="2330", name="Test")
    cached = {"stock": {"code": "2330"}, "financial_data": [{"eps": 1.2}]}

    monkeypatch.setattr(radar, "load_research_structured_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(radar, "load_latest_research_structured_cache", lambda *args, **kwargs: (cached, date(2026, 5, 20)))

    radar._attach_research_evidence_packs([candidate], ["2330"], date(2026, 5, 22), None)

    assert candidate.evidence_pack["research_pack_mode"] == "recent_cache"
    assert candidate.evidence_pack["research_structured_data"]["radar_research_data_date"] == "2026-05-20"


def test_ai_enrichment_respects_total_ai_top_budget():
    a1 = radar.RadarCandidate(code="1111", strategy_codes={"A"}, total_score=80)
    a2 = radar.RadarCandidate(code="2222", strategy_codes={"A"}, total_score=70)
    c1 = radar.RadarCandidate(code="3333", strategy_codes={"C"}, total_score=90)
    assert radar._select_ai_enrichment_codes([a1, a2, c1], 1) == ["1111"]


def test_ai_enrichment_keeps_strategy_diversity_within_budget():
    a1 = radar.RadarCandidate(code="1111", strategy_codes={"A"}, total_score=80)
    a2 = radar.RadarCandidate(code="2222", strategy_codes={"A"}, total_score=120)
    b1 = radar.RadarCandidate(code="3333", strategy_codes={"B"}, total_score=70)
    c1 = radar.RadarCandidate(code="4444", strategy_codes={"C"}, total_score=60)

    assert radar._select_ai_enrichment_codes([a1, a2, b1, c1], 3) == ["2222", "3333", "4444"]


def test_attach_ai_comments_writes_comment(monkeypatch):
    candidate = radar.RadarCandidate(
        code="2330",
        name="台積電",
        score_components={"technical": 20, "revenue": 15, "chip": 5, "theme": 10, "market": 4},
        total_score=54,
    )
    monkeypatch.setattr(
        radar,
        "_call_ai_comment_model",
        lambda model, prompt: '{"comments":[{"code":"2330","priority":"高","confidence":"中","reason":"題材與技術同向。","risk":"量縮需留意。","watch":"觀察量能。"}]}',
    )

    radar._attach_ai_comments([candidate], ["2330"], "deepseek", date(2026, 5, 22), None)

    assert candidate.ai_comment["status"] == "ok"
    assert candidate.ai_comment["priority"] == "高"
    assert candidate.ai_comment["reason"] == "題材與技術同向。"


def test_call_ai_comment_model_retries_temporary_overload(monkeypatch):
    calls = {"count": 0}

    def fake_call_once(model, prompt):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("MiniMax API request failed; status=529; overloaded")
        return '{"comments":[]}'

    monkeypatch.setattr(radar, "_call_ai_comment_model_once", fake_call_once)
    monkeypatch.setattr(radar.time, "sleep", lambda seconds: None)

    assert radar._call_ai_comment_model("minimax", "prompt") == '{"comments":[]}'
    assert calls["count"] == 2


def test_attach_ai_comments_runs_top15_in_three_chunks(monkeypatch):
    candidates = [radar.RadarCandidate(code=f"23{i:02d}", name=f"Stock{i}") for i in range(15)]
    calls = []

    def fake_call(model, prompt):
        calls.append(prompt)
        payload = json.loads(prompt.split("候選股資料：\n", 1)[1])
        return json.dumps(
            {
                "comments": [
                    {
                        "code": item["code"],
                        "priority": "中",
                        "confidence": "中",
                        "reason": "資料可用。",
                        "risk": "觀察風險。",
                        "watch": "觀察量能。",
                    }
                    for item in payload
                ]
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(radar, "_call_ai_comment_model", fake_call)

    meta = radar._attach_ai_comments(candidates, [item.code for item in candidates], "deepseek", date(2026, 5, 22), None)

    assert len(calls) == 3
    assert meta["chunk_count"] == 3
    assert meta["comment_count"] == 15
    assert all(item.ai_comment["status"] == "ok" for item in candidates)


def test_score_candidates_uses_early_wave_components(monkeypatch):
    candidate = radar.RadarCandidate(
        code="3550",
        name="聯穎",
        industry="電子零組件業",
        strategy_codes={"D"},
        technical_signals=[{"strategy_code": "D", "sub_signal_type": "D4_hammer_candle_reclaim"}],
        chip_grades={"chip_1": "B", "chip_2": "A"},
        news_items=[{"title": "AI高速傳輸 題材升溫"}],
    )
    snapshot = {
        "technical": {
            "above_ma": {"ma5": True, "ma10": True, "ma20": True, "ma21": True, "ma60": True},
            "reclaim_ma": {"ma20": True, "ma21": True, "ma60": True},
            "ma_slopes": {"ma5": "up", "ma10": "up", "ma20": "up"},
            "volume_ratio": 2.2,
            "volume": 2200,
            "volume_avg20": 1000,
            "volume_avg60": 1200,
            "price_up_volume_up": True,
            "recent_break_low_recover": True,
            "long_lower_shadow": True,
            "breakout_20d": True,
            "breakout_60d": False,
            "distance_from_60d_low_pct": 12,
            "distance_from_120d_low_pct": 25,
            "ma20_deviation_pct": 8,
            "price_metrics": {},
        },
        "revenue": {
            "history": [
                {"month": "2026-02-01", "revenue": 100, "yoy": -5, "MoM%": -1},
                {"month": "2026-03-01", "revenue": 120, "yoy": 8, "MoM%": 20},
                {"month": "2026-04-01", "revenue": 150, "yoy": 18, "MoM%": 25},
                {"month": "2026-05-01", "revenue": 190, "yoy": 32, "MoM%": 26},
            ]
        },
        "financial": {
            "financial_data": [
                {"Quarter": "2025Q4", "EPS": -0.2, "Gross_Margin": 14, "Operating_Margin": -2, "Net_Income": -100, "Operating_Cash_Flow": 10},
                {"Quarter": "2026Q1", "EPS": 0.1, "Gross_Margin": 16, "Operating_Margin": 1, "Net_Income": 20, "Operating_Cash_Flow": 30, "Free_Cash_Flow": 5},
            ],
            "valuation_data": {"latest": {"pb_ratio": 1.4}},
        },
        "chip": {
            "grades": {"chip_1": "B", "chip_2": "A"},
            "institutional_data": [
                {"Date": "2026-05-18", "Foreign_Net_Lots": -10, "Investment_Trust_Net_Lots": 0, "Dealer_Net_Lots": 0},
                {"Date": "2026-05-19", "Foreign_Net_Lots": 20, "Investment_Trust_Net_Lots": 5, "Dealer_Net_Lots": 1},
                {"Date": "2026-05-20", "Foreign_Net_Lots": 30, "Investment_Trust_Net_Lots": 5, "Dealer_Net_Lots": 1},
                {"Date": "2026-05-21", "Foreign_Net_Lots": 40, "Investment_Trust_Net_Lots": 5, "Dealer_Net_Lots": 1},
                {"Date": "2026-05-22", "Foreign_Net_Lots": 50, "Investment_Trust_Net_Lots": 5, "Dealer_Net_Lots": 1},
            ],
            "margin_data": [{"Date": "2026-05-22", "Financing_Net_Change_Lots": -5, "Short_Margin_Ratio": 40}],
            "tdcc_data": {"large_holder_pct": 65, "retail_holder_pct": 30},
        },
        "theme_news": {
            "local_news": [{"title": "AI高速傳輸需求升溫"}],
            "web_sources": [{"title": "供應鏈題材"}],
            "ai_sources": [],
            "topic_context": {"matched_topics": ["AI高速傳輸"], "company_topic_relations": {"direct_matches": 1}},
        },
        "sector": {"industry": "電子零組件業", "industry_candidate_count": 3, "same_industry_codes": ["3550", "1111", "2222"]},
    }
    monkeypatch.setattr(radar, "_build_radar_feature_snapshot", lambda *args, **kwargs: snapshot)

    radar._score_candidates([candidate], date(2026, 5, 22))

    assert set(candidate.score_components) == {"technical", "revenue", "financial", "chip", "theme", "sector"}
    assert candidate.score_components["technical"] <= 30
    assert candidate.score_components["revenue"] <= 20
    assert candidate.score_components["financial"] <= 15
    assert candidate.score_components["chip"] <= 15
    assert candidate.score_components["theme"] <= 15
    assert candidate.score_components["sector"] <= 5
    assert candidate.total_score <= 100
    assert candidate.score_details["financial"]["score"] > 0
    assert candidate.key_reasons
    assert candidate.radar_feature_snapshot is snapshot


def test_score_candidates_preserves_early_turnaround_when_financial_missing(monkeypatch):
    candidate = radar.RadarCandidate(
        code="2241",
        name="艾姆勒",
        industry="汽車零組件業",
        strategy_codes={"B"},
        technical_signals=[{"strategy_code": "B", "sub_signal_type": "B1_reversal"}],
        chip_grades={"chip_1": "B"},
    )
    snapshot = {
        "technical": {
            "status": "ok",
            "above_ma": {"ma5": True, "ma10": True},
            "reclaim_ma": {"ma20": True},
            "ma_slopes": {"ma5": "up"},
            "volume_ratio": 1.5,
            "price_up_volume_up": True,
            "distance_from_60d_low_pct": 16,
            "ma20_deviation_pct": 5,
            "price_metrics": {"change_pct_20d": 8},
        },
        "revenue": {
            "history": [
                {"month": "2026-01-01", "revenue": 90, "yoy": -10, "MoM%": -5},
                {"month": "2026-02-01", "revenue": 95, "yoy": -8, "MoM%": 5},
                {"month": "2026-03-01", "revenue": 110, "yoy": 6, "MoM%": 15},
                {"month": "2026-04-01", "revenue": 130, "yoy": 22, "MoM%": 18},
            ]
        },
        "financial": {"financial_data": []},
        "chip": {"grades": {"chip_1": "B"}, "institutional_data": [], "margin_data": [], "tdcc_data": {}},
        "theme_news": {"local_news": [], "web_sources": [], "ai_sources": [], "topic_context": {}},
        "sector": {"industry": "汽車零組件業", "industry_candidate_count": 2, "same_industry_codes": ["2241", "1111"]},
    }
    monkeypatch.setattr(radar, "_build_radar_feature_snapshot", lambda *args, **kwargs: snapshot)

    radar._score_candidates([candidate], date(2026, 5, 22))

    assert candidate.score_components["financial"] > 0
    assert candidate.score_components["revenue"] >= 8
    assert candidate.total_score >= 45
    assert any("早期" in reason or "尚未反映" in reason for reason in candidate.key_reasons)
    assert any("重估" in reason for reason in candidate.key_reasons)
    assert any("財報資料缺漏" in risk for risk in candidate.risk_flags)


def test_early_turnaround_candidate_survives_top15_among_technical_only_pool(monkeypatch):
    early = radar.RadarCandidate(
        code="2241",
        name="艾姆勒",
        industry="汽車零組件業",
        strategy_codes={"B"},
        technical_signals=[{"strategy_code": "B", "sub_signal_type": "B1_reversal"}],
        chip_grades={"chip_1": "B"},
        source_labels=["籌碼", "財報營收"],
    )
    ordinary = [
        radar.RadarCandidate(
            code=f"9{index:03d}",
            name=f"技術股{index}",
            industry="電子零組件業",
            strategy_codes={"A"},
            technical_signals=[{"strategy_code": "A"}],
        )
        for index in range(40)
    ]
    candidates = [*ordinary, early]

    def fake_snapshot(item, *_args, **_kwargs):
        if item.code == "2241":
            return {
                "technical": {
                    "status": "ok",
                    "above_ma": {"ma5": True, "ma10": True},
                    "reclaim_ma": {"ma20": True},
                    "ma_slopes": {"ma5": "up"},
                    "volume_ratio": 1.5,
                    "price_up_volume_up": True,
                    "distance_from_60d_low_pct": 16,
                    "ma20_deviation_pct": 5,
                    "price_metrics": {"change_pct_20d": 8},
                },
                "revenue": {
                    "history": [
                        {"month": "2026-01-01", "revenue": 90, "yoy": -10, "MoM%": -5},
                        {"month": "2026-02-01", "revenue": 95, "yoy": -8, "MoM%": 5},
                        {"month": "2026-03-01", "revenue": 110, "yoy": 6, "MoM%": 15},
                        {"month": "2026-04-01", "revenue": 130, "yoy": 22, "MoM%": 18},
                    ]
                },
                "financial": {"financial_data": []},
                "chip": {
                    "grades": {"chip_1": "B"},
                    "institutional_data": [
                        {"Date": "2026-05-20", "Foreign_Net_Lots": 10, "Investment_Trust_Net_Lots": 0, "Dealer_Net_Lots": 0},
                        {"Date": "2026-05-21", "Foreign_Net_Lots": 15, "Investment_Trust_Net_Lots": 0, "Dealer_Net_Lots": 1},
                        {"Date": "2026-05-22", "Foreign_Net_Lots": 20, "Investment_Trust_Net_Lots": 0, "Dealer_Net_Lots": 1},
                    ],
                    "margin_data": [],
                    "tdcc_data": {},
                },
                "theme_news": {"local_news": [], "web_sources": [], "ai_sources": [], "topic_context": {}},
                "sector": {"industry": "汽車零組件業", "industry_candidate_count": 1, "same_industry_codes": ["2241"]},
            }
        return {
            "technical": {
                "status": "ok",
                "above_ma": {"ma5": True, "ma10": True, "ma20": True},
                "reclaim_ma": {},
                "ma_slopes": {"ma5": "up"},
                "volume_ratio": 1.1,
                "price_up_volume_up": True,
                "distance_from_60d_low_pct": 35,
                "ma20_deviation_pct": 3,
                "price_metrics": {"change_pct_20d": 5},
            },
            "revenue": {"history": []},
            "financial": {"financial_data": []},
            "chip": {"grades": {}, "institutional_data": [], "margin_data": [], "tdcc_data": {}},
            "theme_news": {"local_news": [], "web_sources": [], "ai_sources": [], "topic_context": {}},
            "sector": {"industry": item.industry, "industry_candidate_count": len(ordinary), "same_industry_codes": [row.code for row in ordinary[:20]]},
        }

    monkeypatch.setattr(radar, "_build_radar_feature_snapshot", fake_snapshot)

    radar._score_candidates(candidates, date(2026, 5, 22))
    ranked = sorted(candidates, key=lambda item: (item.total_score, len(item.strategy_codes), item.code), reverse=True)
    top15_codes = {item.code for item in ranked[:15]}

    assert "2241" in top15_codes
    assert early.total_score > max(item.total_score for item in ordinary)
    assert any("早期" in reason or "尚未反映" in reason for reason in early.key_reasons)
    assert any("財報資料缺漏" in risk for risk in early.risk_flags)


def test_normalise_ai_sources_preserves_all_sources_and_fetch_details():
    sources = [
        SourceItem(
            source_id=f"S{i:03d}",
            title=f"title {i}",
            url=f"https://example.com/{i}",
            source_level="Level 3",
            provider="minimax",
            provider_detail="web_search",
            fetch_provider="requests",
            fetch_status="ok",
            snippet=f"snippet {i}",
        )
        for i in range(12)
    ]
    structured_data = {
        "web_fetched_sources": [
            {
                "title": "full text",
                "url": "https://example.com/full",
                "provider": "web_fetch",
                "fetch_provider": "beautifulsoup",
                "fetch_status": "ok",
                "content": "complete article body",
            }
        ]
    }

    items = radar._normalise_ai_sources(sources, structured_data)

    assert len(items) == 13
    assert items[0]["provider_detail"] == "web_search"
    assert items[0]["fetch_provider"] == "requests"
    assert items[-1]["content"] == "complete article body"


def test_attach_ai_comments_uses_compact_single_stock_job_when_prompt_is_large(monkeypatch):
    candidate = radar.RadarCandidate(
        code="2330",
        name="TSMC",
        score_components={"technical": 20, "revenue": 10, "chip": 5, "theme": 5, "market": 5},
        total_score=45,
        ai_sources=[
            {"title": f"source {i}", "url": f"https://example.com/{i}", "snippet": "x" * 400}
            for i in range(8)
        ],
        evidence_pack={
            "research_structured_data": {
                "financial_data": [{"Quarter": "2026Q1", "note": "y" * 800}],
                "topic_context": {"summary": "z" * 800},
            },
            "research_sources": [
                {"title": f"research {i}", "url": f"https://research.example.com/{i}", "snippet": "r" * 300}
                for i in range(5)
            ],
        },
    )
    calls = []

    def fake_call(model, prompt):
        calls.append(prompt)
        return json.dumps(
            {
                "comments": [
                    {
                        "code": "2330",
                        "priority": "高",
                        "confidence": "中",
                        "reason": "compact evidence reviewed",
                        "risk": "data quality",
                        "watch": "revenue",
                    }
                ]
            }
        )

    monkeypatch.setattr(radar, "RADAR_AI_PROMPT_MAX_CHARS", 7000)
    monkeypatch.setattr(radar, "_attach_radar_low_model_digest", lambda *args, **kwargs: {})
    monkeypatch.setattr(radar, "_call_ai_comment_model", fake_call)

    meta = radar._attach_ai_comments([candidate], ["2330"], "deepseek", date(2026, 5, 22), None)

    assert candidate.ai_comment["status"] == "ok"
    assert candidate.ai_comment["reason"] == "compact evidence reviewed"
    assert meta["chunks"][0]["status"] == "ok"
    assert meta["chunks"][0]["jobs"][0]["profile"] == "minimal"
    assert len(calls) == 1


def test_attach_ai_comments_failure_keeps_local_result(monkeypatch):
    candidate = radar.RadarCandidate(code="2330", name="台積電")
    monkeypatch.setattr(radar, "_call_ai_comment_model", lambda model, prompt: (_ for _ in ()).throw(RuntimeError("boom")))

    radar._attach_ai_comments([candidate], ["2330"], "deepseek", date(2026, 5, 22), None)

    assert candidate.ai_comment["status"] == "failed"
    assert "boom" in candidate.ai_comment["error"]


def test_build_radar_evidence_pack_includes_full_layers():
    candidate = radar.RadarCandidate(
        code="2330",
        name="台積電",
        industry="半導體",
        strategy_codes={"A"},
        technical_signals=[{"strategy_code": "A", "technical_setup_score": 8}],
        revenue_history=[{"month": "2026-04-01", "revenue": 100, "yoy": 20}],
        chip_grades={"chip_1": "A"},
        news_items=[{"title": "news"}],
        ai_sources=[{"title": "source"}],
        score_components={"technical": 24, "revenue": 10, "chip": 5, "theme": 3, "market": 2},
        total_score=44,
    )
    structured = {
        "financial_data": [{"Quarter": "2026Q1"}],
        "margin_data": [{"date": "2026-05-22"}],
        "institutional_data": [{"date": "2026-05-22"}],
        "feature_pack": {"scope": "single_stock"},
        "unified_evidence_pack": {"items": []},
    }

    candidate.data_coverage = radar._build_radar_data_coverage(candidate, structured)
    pack = radar._build_radar_evidence_pack(candidate, date(2026, 5, 22), structured)

    assert pack["schema_version"] == "radar_evidence_pack_v1"
    assert pack["technical"]["signals"]
    assert pack["revenue"]["history"][0]["yoy"] == 20
    assert pack["research_structured_data"]["feature_pack"]["scope"] == "single_stock"
    assert candidate.data_coverage["checks"]["financial"] == "ok"

    candidate.evidence_pack = {"research_structured_data": structured, "research_sources": [{"title": "source"}]}
    radar._attach_base_evidence_packs([candidate], date(2026, 5, 22))
    assert candidate.evidence_pack["data_coverage"]["checks"]["financial"] == "ok"
    assert candidate.evidence_pack["research_sources"][0]["title"] == "source"


def test_radar_evidence_pack_adds_official_basis_sources():
    candidate = radar.RadarCandidate(
        code="2330",
        name="台積電",
        technical_signals=[{"strategy_code": "A"}],
        revenue_history=[{"month": "2026-04-01", "revenue": 100}],
        chip_grades={"chip_1": "A"},
        score_components={"technical": 1, "revenue": 1, "chip": 1},
        total_score=3,
    )

    pack = radar._build_radar_evidence_pack(candidate, date(2026, 5, 22), {})
    sources = pack["raw_sources"]

    assert any(source.get("source_level") == "L1_official" for source in sources)
    assert any(source.get("source_id") == "RADAR_OFFICIAL_PRICE_VOLUME" for source in sources)
    candidate.evidence_pack = pack
    merged = radar._research_sources_from_item(candidate, date(2026, 5, 22))
    assert any(source.get("source_id") == "RADAR_OFFICIAL_PRICE_VOLUME" for source in merged)


def test_save_radar_artifacts_writes_evidence_pack(monkeypatch):
    output_dir = Path("reports") / "_test_radar_artifacts"
    monkeypatch.setattr(radar, "RADAR_REPORT_DIR", output_dir)
    candidate = radar.RadarCandidate(
        code="2330",
        name="台積電",
        evidence_pack={"schema_version": "radar_evidence_pack_v1", "candidate": {"code": "2330"}},
        data_coverage={"checks": {"technical": "ok"}},
        score_components={"technical": 1},
        total_score=1,
    )
    result = radar.RadarResult(
        request=radar.RadarRequest(source="technical", report_date=date(2026, 5, 22), ai_top=5),
        report_date=date(2026, 5, 22),
        candidates=[candidate],
        ai_enriched_codes=["2330"],
        diagnostics={"ai_analysis": {"chunk_count": 1}},
    )

    paths = radar._save_radar_artifacts(result, {"radar_id": "radar_test"})

    assert Path(paths["summary"]).exists()
    evidence = json.loads(Path(paths["evidence_pack"]).read_text(encoding="utf-8"))
    assert evidence[0]["candidate"]["code"] == "2330"


def test_format_radar_report_uses_chinese_signal_and_chip_labels():
    candidate = radar.RadarCandidate(
        code="2436",
        name="偉詮電",
        industry="半導體業",
        source_labels=[
            "技術面選股快取",
            "策略B/B2_short_reclaim_after_break_ma",
            "策略D/D1_reclaim_ma_after_break",
        ],
        strategy_codes={"B", "D"},
        technical_signals=[
            {"strategy_code": "B", "sub_signal_type": "B2_short_reclaim_after_break_ma"},
            {"strategy_code": "D", "sub_signal_type": "D1_reclaim_ma_after_break"},
        ],
        chip_grades={"chip_1": "B", "chip_2": "A", "chip_3": "B"},
        news_items=[{"title": "news"}],
        web_sources=[{"title": "web"}],
        score_components={"technical": 22, "revenue": 15, "chip": 13, "theme": 20, "market": 10},
        total_score=80,
    )
    result = radar.RadarResult(
        request=radar.RadarRequest(source="technical", report_date=date(2026, 5, 22), ai_top=5),
        report_date=date(2026, 5, 22),
        candidates=[candidate],
        ai_enriched_codes=["2436"],
        diagnostics={},
    )

    text = radar.format_radar_report(result)

    assert "B2_short_reclaim_after_break_ma" not in text
    assert "D1_reclaim_ma_after_break" not in text
    assert "chip_1" not in text
    assert "B 強勢紅柱回測突破：B2 跌破後收復短均線" in text
    assert "D 強勢股急跌收復：D1 跌破後收復均線" in text
    assert "籌碼：60日法人動態 B級、投信認養 A級、法人持股比例增加 B級" in text
    assert "外部來源 1 則" in text


def test_format_radar_report_shows_ai_comment():
    candidate = radar.RadarCandidate(
        code="2330",
        name="台積電",
        strategy_codes={"A"},
        score_components={"technical": 30, "revenue": 15, "chip": 10, "theme": 10, "market": 5},
        total_score=70,
        ai_comment={"status": "ok", "priority": "高", "reason": "技術與題材同步。", "risk": "追高風險。", "watch": "觀察量能。"},
    )
    result = radar.RadarResult(
        request=radar.RadarRequest(source="technical", report_date=date(2026, 5, 22), ai_top=5, model="deepseek"),
        report_date=date(2026, 5, 22),
        candidates=[candidate],
        ai_enriched_codes=["2330"],
        diagnostics={},
    )

    text = radar.format_radar_report(result)

    assert "AI 高" in text
    assert "AI短評：技術與題材同步。" in text
    assert "風險：追高風險。" in text
    assert "觀察：觀察量能。" in text


def test_radar_ai_comment_display_cleans_internal_english_labels():
    dirty_reason = (
        "[verified_fact+market_hypothesis] 題材發酵，"
        "volume_quality=false，row_count=0，chip/institutional/margin 待補。"
    )
    dirty_risk = "(verified_fact) risk uses market_hypothesis and data_coverage."
    dirty_watch = "reasoned_inference：setup score 改善，但 score_components 仍受 limited_by_light_research 影響。"
    candidate = radar.RadarCandidate(
        code="2330",
        name="台積電",
        strategy_codes={"A"},
        score_components={"technical": 30, "revenue": 15, "financial": 10, "chip": 10, "theme": 10, "sector": 5},
        total_score=80,
        key_reasons=["技術轉強"],
        ai_comment={"status": "ok", "priority": "高", "reason": dirty_reason, "risk": dirty_risk, "watch": dirty_watch},
    )
    result = radar.RadarResult(
        request=radar.RadarRequest(source="technical", report_date=date(2026, 5, 22), ai_top=15, model="minimax"),
        report_date=date(2026, 5, 22),
        candidates=[candidate],
        ai_enriched_codes=["2330"],
        diagnostics={},
    )

    full_text = radar.format_radar_report(result)
    push_text = radar.format_radar_push_summary(result, limit=15)
    combined = f"{full_text}\n{push_text}"

    for forbidden in ["verified_fact", "market_hypothesis", "volume_quality", "row_count", "chip/institutional/margin"]:
        assert forbidden not in combined
    assert "量能未配合" in combined
    assert "資料缺漏" in combined
    assert "籌碼、法人與融資券" in combined
    assert "推論" in combined
    assert "技術型態分數" in combined


def test_format_radar_report_defaults_to_top15():
    candidates = [
        radar.RadarCandidate(
            code=f"{2300 + index}",
            name=f"Stock{index}",
            score_components={"technical": 1, "revenue": 0, "financial": 0, "chip": 0, "theme": 0, "sector": 0},
            total_score=20 - index,
        )
        for index in range(16)
    ]
    result = radar.RadarResult(
        request=radar.RadarRequest(source="technical", report_date=date(2026, 5, 22), ai_top=15),
        report_date=date(2026, 5, 22),
        candidates=candidates,
        ai_enriched_codes=[],
        diagnostics={},
    )

    text = radar.format_radar_report(result)

    assert "15. 2314 Stock14" in text
    assert "16. 2315 Stock15" not in text
    assert "完整名單共 16 檔" in text


def test_format_radar_push_summary_is_concise_and_keeps_full_report_separate():
    candidates = [
        radar.RadarCandidate(
            code=f"{2300 + index}",
            name=f"Stock{index}",
            score_components={"technical": 10, "revenue": 3, "financial": 2, "chip": 1, "theme": 4, "sector": 5},
            total_score=80 - index,
            key_reasons=["技術轉強", "題材升溫", "法人回補"],
            risk_flags=["短線過熱"],
            ai_comment={"status": "ok", "priority": "中", "reason": "AI 短評" * 80, "risk": "風險" * 80},
        )
        for index in range(12)
    ]
    result = radar.RadarResult(
        request=radar.RadarRequest(source="technical", report_date=date(2026, 5, 22), ai_top=15, model="minimax"),
        report_date=date(2026, 5, 22),
        candidates=candidates,
        ai_enriched_codes=[],
        diagnostics={},
    )

    push_text = radar.format_radar_push_summary(result, limit=5)
    full_text = radar.format_radar_report(result)

    assert "5. 2304 Stock4" in push_text
    assert "6. 2305 Stock5" not in push_text
    assert "完整名單共 12 檔" in push_text
    assert len(push_text) < len(full_text)


def test_technical_candidates_from_cache_falls_back_to_scan_signals(monkeypatch):
    monkeypatch.setattr(
        radar,
        "load_recent_scan_results",
        lambda limit=30: [
            {
                "scan_type": "技術面選股",
                "report_date": "2026-05-20",
                "selected_codes": ["2330", "2317"],
            }
        ],
    )
    monkeypatch.setattr(
        radar,
        "_stock_meta_by_code",
        lambda: {
            "2330": SimpleNamespace(code="2330", name="TSMC", symbol="2330.TW", industry="semi"),
            "2317": SimpleNamespace(code="2317", name="Hon Hai", symbol="2317.TW", industry="electronic"),
        },
    )
    monkeypatch.setattr(
        radar.ts,
        "run_technical_scan",
        lambda scan_settings, target_date: SimpleNamespace(
            strategy_signals={
                "A": [
                    {
                        "stock_id": "2330",
                        "stock_name": "TSMC",
                        "strategy_code": "A",
                        "technical_setup_score": 8,
                        "features": {},
                    }
                ],
                "B": [],
                "C": [],
                "D": [],
            }
        ),
    )

    candidates, policy = radar._technical_candidates_for_radar(date(2026, 5, 20), {}, None)
    by_code = {item.code: item for item in candidates}

    assert policy["status"] == "cached"
    assert by_code["2330"].strategy_codes == {"A"}
    assert by_code["2330"].technical_signals
    assert by_code["2317"].strategy_codes == set()


class RadarTechnicalCacheFreshnessTests(unittest.TestCase):
    def _stock_meta(self):
        return {
            "2330": SimpleNamespace(code="2330", name="TSMC", symbol="2330.TW", industry="semi"),
            "2241": SimpleNamespace(code="2241", name="艾姆勒", symbol="2241.TW", industry="auto"),
        }

    def _scan_result(self):
        return SimpleNamespace(
            strategy_signals={
                "A": [],
                "B": [
                    {
                        "stock_id": "2241",
                        "stock_name": "艾姆勒",
                        "strategy_code": "B",
                        "sub_signal_type": "B2_short_reclaim_after_break_ma",
                        "technical_setup_score": 8,
                        "features": {},
                    }
                ],
                "C": [],
                "D": [],
            }
        )

    def test_technical_cache_before_close_is_ignored_and_scan_regenerated(self):
        records = [
            {
                "scan_type": "技術面選股",
                "report_date": "2026-05-20",
                "created_at": "2026-05-20T01:16:39+08:00",
                "selected_codes": ["2330"],
                "strategy_signals": {
                    "A": [{"stock_id": "2330", "strategy_code": "A", "technical_setup_score": 8}],
                    "B": [],
                    "C": [],
                    "D": [],
                },
            }
        ]
        progress_messages: list[str] = []

        with patch.object(radar, "load_recent_scan_results", return_value=records), patch.object(
            radar, "_stock_meta_by_code", return_value=self._stock_meta()
        ), patch.object(radar.ts, "run_technical_scan", return_value=self._scan_result()) as run_scan, patch.object(
            radar.ts, "format_technical_report", return_value="technical report"
        ), patch.object(
            radar, "save_recent_scan_result"
        ):
            candidates, policy = radar._technical_candidates_for_radar(
                date(2026, 5, 20),
                {},
                progress_messages.append,
            )

        self.assertEqual(policy["status"], "generated")
        self.assertTrue(run_scan.called)
        self.assertIn("2241", {item.code for item in candidates})
        self.assertTrue(any("略過收盤前技術面選股快取" in message for message in progress_messages))

    def test_technical_cache_after_close_is_reused(self):
        records = [
            {
                "scan_type": "技術面選股",
                "report_date": "2026-05-20",
                "created_at": "2026-05-20T15:01:00+08:00",
                "selected_codes": ["2330"],
                "strategy_signals": {
                    "A": [{"stock_id": "2330", "strategy_code": "A", "technical_setup_score": 8}],
                    "B": [],
                    "C": [],
                    "D": [],
                },
            }
        ]

        with patch.object(radar, "load_recent_scan_results", return_value=records), patch.object(
            radar, "_stock_meta_by_code", return_value=self._stock_meta()
        ), patch.object(radar.ts, "run_technical_scan", side_effect=AssertionError("should use cache")):
            candidates, policy = radar._technical_candidates_for_radar(date(2026, 5, 20), {}, None)

        self.assertEqual(policy["status"], "cached")
        self.assertEqual([item.code for item in candidates], ["2330"])

    def test_technical_cache_created_next_day_is_reused(self):
        records = [
            {
                "scan_type": "技術面選股",
                "report_date": "2026-05-20",
                "created_at": "2026-05-21T06:30:00+08:00",
                "selected_codes": ["2330"],
                "strategy_signals": {
                    "A": [{"stock_id": "2330", "strategy_code": "A", "technical_setup_score": 8}],
                    "B": [],
                    "C": [],
                    "D": [],
                },
            }
        ]

        with patch.object(radar, "load_recent_scan_results", return_value=records), patch.object(
            radar, "_stock_meta_by_code", return_value=self._stock_meta()
        ), patch.object(radar.ts, "run_technical_scan", side_effect=AssertionError("should use cache")):
            candidates, policy = radar._technical_candidates_for_radar(date(2026, 5, 20), {}, None)

        self.assertEqual(policy["status"], "cached")
        self.assertEqual([item.code for item in candidates], ["2330"])


def test_attach_chip_scores_adds_existing_grade_maps(monkeypatch):
    radar._RADAR_CHIP_GRADE_CACHE.clear()
    candidate = radar.RadarCandidate(code="2330")
    monkeypatch.setattr(radar, "build_market_context", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        radar,
        "build_chip_grade_maps",
        lambda context, keys: {"chip_1": {"2330": "A"}, "chip_2": {}, "chip_3": {}, "chip_4": {"2330": "B"}},
    )

    radar._attach_chip_scores([candidate], date(2026, 5, 20), None)

    assert candidate.chip_grades == {"chip_1": "A", "chip_4": "B"}
    assert radar._score_chip(candidate) == 10


def test_attach_chip_scores_uses_lightweight_radar_context(monkeypatch):
    radar._RADAR_CHIP_GRADE_CACHE.clear()
    candidate = radar.RadarCandidate(code="2330")
    captured: dict[str, object] = {}

    def fake_build_market_context(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(radar, "build_market_context", fake_build_market_context)
    monkeypatch.setattr(
        radar,
        "build_chip_grade_maps",
        lambda context, keys: {"chip_1": {}, "chip_2": {}, "chip_3": {}, "chip_4": {}},
    )

    radar._attach_chip_scores([candidate], date(2026, 5, 20), None)

    assert captured["target_trading_days"] == 5
    assert captured["include_foreign_ratio"] is False
    assert captured["scope"] == "radar"


def test_chip_candidates_uses_lightweight_radar_context(monkeypatch):
    radar._RADAR_CHIP_GRADE_CACHE.clear()
    captured: dict[str, object] = {}

    def fake_build_market_context(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(radar, "build_market_context", fake_build_market_context)
    monkeypatch.setattr(
        radar,
        "build_chip_grade_maps",
        lambda context, keys: {"chip_1": {"2330": "A"}, "chip_2": {}, "chip_3": {}, "chip_4": {}},
    )
    monkeypatch.setattr(radar, "_stock_meta_by_code", lambda: {})

    candidates, policy = radar._chip_candidates(date(2026, 5, 20))

    assert captured["target_trading_days"] == 5
    assert captured["include_foreign_ratio"] is False
    assert captured["scope"] == "radar"
    assert candidates[0].code == "2330"
    assert policy["candidate_count"] == 1


def test_radar_chip_grade_maps_reused_between_candidates_and_scoring(monkeypatch):
    radar._RADAR_CHIP_GRADE_CACHE.clear()
    calls = {"count": 0}

    def fake_build_market_context(*args, **kwargs):
        calls["count"] += 1
        return object()

    monkeypatch.setattr(radar, "build_market_context", fake_build_market_context)
    monkeypatch.setattr(
        radar,
        "build_chip_grade_maps",
        lambda context, keys: {"chip_1": {"2330": "A"}, "chip_2": {}, "chip_3": {}, "chip_4": {}},
    )
    monkeypatch.setattr(radar, "_stock_meta_by_code", lambda: {})

    candidates, _ = radar._chip_candidates(date(2026, 5, 20))
    radar._attach_chip_scores(candidates, date(2026, 5, 20), None)

    assert calls["count"] == 1


def test_attach_web_sources_filters_with_request_argument_order(monkeypatch):
    candidate = radar.RadarCandidate(code="2330", name="TSMC")

    class FakeService:
        def __init__(self, *args, **kwargs):
            pass

        def is_configured(self):
            return True

        def discover(self, request, tasks, progress=None):
            return SimpleNamespace(
                sources=[
                    SourceItem(
                        source_id="S001",
                        title="before",
                        url="https://example.com/before",
                        source_level="web",
                        provider="test",
                        published_date="2026-05-20",
                    ),
                    SourceItem(
                        source_id="S002",
                        title="future",
                        url="https://example.com/future",
                        source_level="web",
                        provider="test",
                        published_date="2026-05-21",
                    ),
                ]
            )

    monkeypatch.setattr(radar, "TavilySearchService", FakeService)
    monkeypatch.setattr(
        radar,
        "load_research_config",
        lambda: SimpleNamespace(
            tavily_api_key="test",
            enable_tavily_search=True,
            tavily_search_depth="basic",
            tavily_max_results_per_query=3,
        ),
    )
    monkeypatch.setattr(radar, "augment_discovery_tasks_with_date_context", lambda request, data, tasks: tasks)

    radar._attach_web_sources([candidate], ["2330"], date(2026, 5, 20), None)

    assert [source["title"] for source in candidate.web_sources] == ["before"]


def test_research_center_sources_skip_fallback_when_minimax_has_enough_sources(monkeypatch):
    candidate = radar.RadarCandidate(code="2330", name="台積電")

    class FakeRunner:
        def __init__(self):
            self.tavily_called = False
            self.gemini_called = False

        def _run_minimax_mcp(self, request, tasks, sources, structured_data, progress):
            for idx in range(8):
                sources.append(
                    SourceItem(
                        source_id=f"S{idx}",
                        title=f"source {idx}",
                        url=f"https://example.com/{idx}",
                        source_level="web",
                        provider="minimax",
                        published_date="2026-05-20",
                    )
                )

        def _run_tavily(self, *args, **kwargs):
            self.tavily_called = True

        def _should_run_gemini(self, *args, **kwargs):
            return True

        def _run_gemini(self, *args, **kwargs):
            self.gemini_called = True

    runner = FakeRunner()

    class FakeCenter:
        _gemini_discovery_runner = runner

    monkeypatch.setattr(radar, "ResearchCenter", lambda: FakeCenter())
    monkeypatch.setattr(radar, "_enrich_sources_with_web_fetch", lambda *args, **kwargs: None)

    radar._attach_research_center_sources([candidate], ["2330"], date(2026, 5, 20), None)

    assert runner.tavily_called is False
    assert runner.gemini_called is False
    assert len(candidate.ai_sources) == 8


def test_format_radar_more_reads_saved_cache(monkeypatch):
    cache_path = Path(".cache") / "radar_test_results.json"
    report_dir = Path(".cache") / "radar_test_reports"
    monkeypatch.setattr(radar, "RADAR_CACHE_PATH", cache_path)
    monkeypatch.setattr(radar, "RADAR_REPORT_DIR", report_dir)
    result = radar.RadarResult(
        request=radar.RadarRequest(source="technical", report_date=date(2026, 5, 20), ai_top=5),
        report_date=date(2026, 5, 20),
        candidates=[
            radar.RadarCandidate(
                code="2330",
                name="台積電",
                industry="半導體",
                strategy_codes={"A"},
                score_components={"technical": 30, "revenue": 10, "chip": 0, "theme": 4, "market": 2},
                total_score=46,
            )
        ],
        ai_enriched_codes=["2330"],
        diagnostics={},
    )
    radar.save_radar_result(result)
    text = radar.format_radar_more(date(2026, 5, 20))
    assert "2330" in text
    assert "2026-05-20" in text


def test_save_radar_result_serializes_numpy_and_pandas_signal_values(monkeypatch):
    cache_path = Path(".cache") / "radar_json_safe_test_results.json"
    report_dir = Path(".cache") / "radar_json_safe_reports"
    monkeypatch.setattr(radar, "RADAR_CACHE_PATH", cache_path)
    monkeypatch.setattr(radar, "RADAR_REPORT_DIR", report_dir)
    result = radar.RadarResult(
        request=radar.RadarRequest(source="technical", report_date=date(2026, 5, 20), ai_top=5),
        report_date=date(2026, 5, 20),
        candidates=[
            radar.RadarCandidate(
                code="2330",
                technical_signals=[
                    {
                        "strategy_code": "A",
                        "signal_date": pd.Timestamp("2026-05-20"),
                        "ma_context": {"ma105_support": np.bool_(True)},
                        "features": {"score": np.float64(1.5), "missing": np.nan},
                    }
                ],
                strategy_codes={"A"},
            )
        ],
        ai_enriched_codes=[],
        diagnostics={},
    )

    radar.save_radar_result(result)
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "candidates" not in payload[0]
    candidates_path = Path(payload[0]["artifact_paths"]["candidates"])
    signal = json.loads(candidates_path.read_text(encoding="utf-8"))[0]["technical_signals"][0]

    assert signal["ma_context"]["ma105_support"] is True
    assert signal["features"]["score"] == 1.5
    assert signal["features"]["missing"] is None
    assert signal["signal_date"] == "2026-05-20T00:00:00"


def test_load_radar_records_rebuilds_large_cache_from_artifacts(monkeypatch):
    cache_path = Path(".cache") / "radar_large_cache_test.json"
    report_dir = Path(".cache") / "radar_large_cache_reports"
    radar_dir = report_dir / "2026-05-20" / "radar_20260520_120000"
    radar_dir.mkdir(parents=True, exist_ok=True)
    (radar_dir / "radar_candidates.json").write_text(
        json.dumps([{"code": "2330", "name": "台積電", "total_score": 88}], ensure_ascii=False),
        encoding="utf-8",
    )
    for name in ("radar_summary.md", "evidence_pack.json", "ai_analysis.json", "sources.json"):
        (radar_dir / name).write_text("{}" if name.endswith(".json") else "summary", encoding="utf-8")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("x" * 200, encoding="utf-8")

    monkeypatch.setattr(radar, "RADAR_CACHE_PATH", cache_path)
    monkeypatch.setattr(radar, "RADAR_REPORT_DIR", report_dir)
    monkeypatch.setattr(radar, "RADAR_CACHE_MAX_BYTES", 10)

    records = radar._load_radar_records(limit=5)
    result = radar._record_to_result(records[0])

    assert records[0]["schema_version"] == "radar_cache_index_v2"
    assert Path(records[0]["artifact_paths"]["candidates"]).exists()
    assert result.candidates[0].code == "2330"
    assert cache_path.stat().st_size < 200
