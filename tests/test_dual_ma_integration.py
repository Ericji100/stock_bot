from datetime import date
from types import SimpleNamespace

import pandas as pd

import stock_ai_bot.selection.curated_scan_service as curated
import stock_ai_bot.monitoring.radar_service as radar
import stock_ai_bot.scanning.technical_scanner as scanner
from stock_ai_bot.telegram.telegram_stock_formatting import strip_stock_markers


REPORT_DATE = date(2026, 5, 20)


def _dual_signal(code="2222"):
    return {
        "stock_id": code,
        "stock_name": "雙均線股",
        "signal_date": REPORT_DATE.isoformat(),
        "close": 20.0,
        "primary_group": "ma5_ma21",
        "matched_target_mas": [5],
        "triggers": {"MA5": ["突破"]},
        "aligned_long_mas": [],
        "long_position": "below",
        "long_relation": "低於 MA105、低於 MA144",
        "industry": "電子業",
    }


def _technical_result():
    return scanner.TechnicalScanResult(
        report_date=REPORT_DATE,
        total_symbols=3,
        hard_filter_passed=3,
        matched_symbols=3,
        bullish={},
        bearish={},
        sources={"本機快取"},
        strategy_signals={
            "A": [{"stock_id": "1111", "stock_name": "策略股", "strategy_code": "A", "sub_signal_type": "A1_direct_ma21_breakout", "signal_date": REPORT_DATE.isoformat(), "close": 20.0, "features": {}}],
            "B": [], "C": [], "D": [],
        },
        dual_ma_signals=[_dual_signal(), _dual_signal("3333")],
    )


def test_curated_accepts_a_to_d_and_dual_ma_with_existing_cross_hit_gate(monkeypatch):
    financial = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                code=code,
                name=name,
                symbol=f"{code}.TW",
                industry="電子業",
                price=20.0,
                avg_volume_20d=1000.0,
                latest_monthly_revenue=50_000_000.0,
                revenue_group="group_1",
                gross_margin_rating="B",
                revenue_history=[SimpleNamespace(month="2026-04", revenue=50_000_000, yoy=10.0)],
            )
            for code, name in (("1111", "策略股"), ("2222", "雙均線股"), ("3333", "單一命中"))
        ]
    )
    chip = SimpleNamespace(candidates=pd.DataFrame(columns=["code"]), scan_settings={})
    monkeypatch.setattr(curated, "build_chip_grade_maps", lambda *_: {"chip_1": {"1111": "A", "2222": "B"}})

    result = curated.build_curated_scan_result(
        {}, REPORT_DATE, financial_report=financial, chip_context=chip,
        technical_result=_technical_result(), include_scoring=False,
    )

    assert set(result.selected_codes) == {"1111", "2222"}
    assert "3333" not in result.selected_codes
    report = strip_stock_markers(result.report_text)
    assert report.count("1111 策略股 |") == 1
    assert report.count("2222 雙均線股 |") == 1
    assert "策略 A｜A1｜直接突破型" in report
    assert "雙均線｜MA5 高於 MA21" in report


def test_curated_passes_real_a_to_d_and_separate_dual_ma_to_shared_scorer(monkeypatch):
    captured = {}

    def fake_score(candidates, *_args, **_kwargs):
        captured.update({item.code: item for item in candidates})
        for item in candidates:
            item.total_score = 50
            item.score_components = {"technical": 10, "revenue": 10, "financial": 10, "chip": 10, "theme": 5, "sector": 5}
        return candidates

    monkeypatch.setattr(radar, "prepare_radar_scoring_data", lambda *args, **kwargs: None)
    monkeypatch.setattr(radar, "score_radar_candidates", fake_score)
    curated._score_curated_candidates(
        target_date=REPORT_DATE,
        selected_codes=["1111", "2222"],
        selected_by_signal={"策略 A｜A1｜直接突破型": ["1111"], scanner.dual_ma_signal_label(_dual_signal()): ["2222"]},
        stock_info={code: {"name": code, "industry": "電子業", "price": 20.0} for code in ("1111", "2222")},
        hits={"1111": ["營收", "籌碼"], "2222": ["營收", "籌碼"]},
        chip_grade_maps={},
        technical_result=_technical_result(),
    )

    assert captured["1111"].strategy_codes == {"A"}
    assert captured["1111"].technical_signals[0]["sub_signal_type"] == "A1_direct_ma21_breakout"
    assert captured["2222"].strategy_codes == set()
    assert captured["2222"].technical_signals == []
    assert captured["2222"].dual_ma_signals[0]["stock_id"] == "2222"


def test_radar_generated_candidates_keep_dual_ma_evidence_without_extra_technical_points(monkeypatch):
    result = _technical_result()
    saved = {}
    monkeypatch.setattr(radar, "_find_technical_scan_cache", lambda *_: None)
    monkeypatch.setattr(radar.ts, "run_technical_scan", lambda *_: result)
    monkeypatch.setattr(radar, "save_recent_scan_result", lambda *args, **kwargs: saved.update({"args": args, "kwargs": kwargs}))
    monkeypatch.setattr(radar, "_stock_meta_by_code", lambda: {})

    candidates, policy = radar._technical_candidates_for_radar(REPORT_DATE, {}, None)
    by_code = {item.code: item for item in candidates}

    assert set(by_code) == {"1111", "2222", "3333"}
    assert policy["dual_ma_signal_count"] == 2
    assert saved["kwargs"]["metadata"]["dual_ma_signals"] == result.dual_ma_signals
    assert "2222" in saved["kwargs"]["metadata"]["radar_candidate_codes"]
    dual = by_code["2222"]
    assert dual.strategy_codes == set()
    assert dual.technical_signals == []
    assert dual.dual_ma_signals == [_dual_signal()]
    assert "雙均線" in radar._technical_signal_line(dual)
    assert radar._build_radar_evidence_pack(dual, REPORT_DATE)["technical"]["dual_ma_signals"] == [_dual_signal()]
    snapshot = {"technical": {"above_ma": {"ma5": True, "ma21": True}}}
    plain = radar.RadarCandidate(code="2222")
    assert radar._score_technical_detail(dual, snapshot)["score"] == radar._score_technical_detail(plain, snapshot)["score"]


def test_radar_cached_candidates_restore_dual_ma_only_stock(monkeypatch):
    record = {
        "scan_type": "技術面選股",
        "report_date": REPORT_DATE.isoformat(),
        "selected_codes": ["1111"],
        "strategy_signals": {"A": [], "B": [], "C": [], "D": []},
        "dual_ma_signals": [_dual_signal()],
        "kd_ma_signals": [],
    }
    monkeypatch.setattr(radar, "load_recent_scan_results", lambda **kwargs: [record])
    monkeypatch.setattr(radar, "_stock_meta_by_code", lambda: {})
    monkeypatch.setattr(radar.ts, "run_technical_scan", lambda *_: (_ for _ in ()).throw(AssertionError("must reuse complete cache")))

    candidates, policy = radar._technical_candidates_for_radar(REPORT_DATE, {}, None)
    assert set(item.code for item in candidates) == {"1111", "2222"}
    assert policy["dual_ma_signal_count"] == 1


def test_radar_old_cache_without_dual_ma_signals_is_recalculated(monkeypatch):
    record = {
        "scan_type": "技術面選股",
        "report_date": REPORT_DATE.isoformat(),
        "selected_codes": ["1111"],
        "strategy_signals": {"A": [], "B": [], "C": [], "D": []},
    }
    calls = []
    saved = {}
    monkeypatch.setattr(radar, "load_recent_scan_results", lambda **kwargs: [record])
    monkeypatch.setattr(radar, "_stock_meta_by_code", lambda: {})
    monkeypatch.setattr(radar.ts, "run_technical_scan", lambda *_: calls.append(1) or _technical_result())
    monkeypatch.setattr(radar, "save_recent_scan_result", lambda *args, **kwargs: saved.update(kwargs))

    candidates, _ = radar._technical_candidates_for_radar(REPORT_DATE, {}, None)
    assert calls == [1]
    assert "2222" in {item.code for item in candidates}
    assert "2222" in saved["metadata"]["radar_candidate_codes"]


def test_radar_candidate_round_trip_preserves_dual_ma_evidence():
    candidate = radar.RadarCandidate(code="2222", dual_ma_signals=[_dual_signal()])
    record = {
        "source": "technical",
        "report_date": REPORT_DATE.isoformat(),
        "candidates": [radar._candidate_to_dict(candidate)],
    }

    loaded = radar._record_to_result(record)
    assert loaded.candidates[0].dual_ma_signals == [_dual_signal()]
