from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

import radar_service as radar
from research_center.models import SourceItem


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


def test_attach_ai_comments_failure_keeps_local_result(monkeypatch):
    candidate = radar.RadarCandidate(code="2330", name="台積電")
    monkeypatch.setattr(radar, "_call_ai_comment_model", lambda model, prompt: (_ for _ in ()).throw(RuntimeError("boom")))

    radar._attach_ai_comments([candidate], ["2330"], "deepseek", date(2026, 5, 22), None)

    assert candidate.ai_comment["status"] == "failed"
    assert "boom" in candidate.ai_comment["error"]


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
