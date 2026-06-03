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
        self.assertEqual(pack["three_layer_context"]["source_sufficiency"]["source_count"], 3)
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
        self.assertEqual(len(calls), 1)

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

    def test_ai_prompt_requires_traditional_chinese_unittest(self):
        candidate = radar.RadarCandidate(code="2330", name="台積電")

        prompt = radar._build_ai_comment_prompt([candidate], date(2026, 5, 22))

        self.assertIn("繁體中文", prompt)
        self.assertIn("ai_compact_pack", prompt)
        self.assertIn("MiniMax M2.7 批次資料整理底稿", prompt)
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


def test_parse_radar_args_defaults_to_technical_top5():
    request = radar.parse_radar_args([])
    assert request.source == "technical"
    assert request.report_date is None
    assert request.ai_top == 5


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


def test_ai_enrichment_selects_top_per_strategy():
    a1 = radar.RadarCandidate(code="1111", strategy_codes={"A"}, total_score=80)
    a2 = radar.RadarCandidate(code="2222", strategy_codes={"A"}, total_score=70)
    c1 = radar.RadarCandidate(code="3333", strategy_codes={"C"}, total_score=90)
    assert radar._select_ai_enrichment_codes([a1, a2, c1], 1) == ["1111", "3333"]


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


def test_attach_ai_comments_runs_in_chunks(monkeypatch):
    candidates = [radar.RadarCandidate(code=f"23{i:02d}", name=f"Stock{i}") for i in range(12)]
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
    assert meta["comment_count"] == 12
    assert all(item.ai_comment["status"] == "ok" for item in candidates)


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


def test_attach_chip_scores_adds_existing_grade_maps(monkeypatch):
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


def test_format_radar_more_reads_saved_cache(monkeypatch):
    cache_path = Path(".cache") / "radar_test_results.json"
    monkeypatch.setattr(radar, "RADAR_CACHE_PATH", cache_path)
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
    monkeypatch.setattr(radar, "RADAR_CACHE_PATH", cache_path)
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
    signal = payload[0]["candidates"][0]["technical_signals"][0]

    assert signal["ma_context"]["ma105_support"] is True
    assert signal["features"]["score"] == 1.5
    assert signal["features"]["missing"] is None
    assert signal["signal_date"] == "2026-05-20T00:00:00"
