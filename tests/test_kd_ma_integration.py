from datetime import date
from types import SimpleNamespace

import pandas as pd

import curated_scan_service as curated
import radar_service as radar
import technical_scanner as scanner
from stock_ai_bot.telegram.telegram_stock_formatting import strip_stock_markers


REPORT_DATE = date(2026, 8, 20)


def _kd_signal(code="4444"):
    return {
        "stock_id": code,
        "stock_name": "KD策略股",
        "signal_date": REPORT_DATE.isoformat(),
        "close": 30.0,
        "primary_group": "K1",
        "matched_groups": ["K1"],
        "matched_target_mas": [5],
        "triggers": {"MA5": ["突破"]},
        "recent_golden_cross_days": 3,
        "k": 25.0,
        "d": 35.0,
        "industry": "電子業",
    }


def _technical_result(*signals):
    return scanner.TechnicalScanResult(
        report_date=REPORT_DATE,
        total_symbols=len(signals),
        hard_filter_passed=len(signals),
        matched_symbols=len(signals),
        bullish={},
        bearish={},
        sources={"本機快取"},
        strategy_signals={"A": [], "B": [], "C": [], "D": []},
        kd_ma_signals=list(signals),
    )


def test_curated_uses_kd_strategy_as_technical_entry_but_keeps_cross_hit_gate(monkeypatch):
    financial = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                code=code,
                name="KD策略股",
                symbol=f"{code}.TW",
                industry="電子業",
                price=30.0,
                avg_volume_20d=1000.0,
                latest_monthly_revenue=50_000_000.0,
                revenue_group="group_1",
                gross_margin_rating="B",
                revenue_history=[SimpleNamespace(month="2026-07", revenue=50_000_000, yoy=10.0)],
            )
            for code in ("4444", "5555")
        ]
    )
    chip = SimpleNamespace(candidates=pd.DataFrame(columns=["code"]), scan_settings={})
    monkeypatch.setattr(curated, "build_chip_grade_maps", lambda *_: {"chip_1": {"4444": "A"}})

    result = curated.build_curated_scan_result(
        {},
        REPORT_DATE,
        financial_report=financial,
        chip_context=chip,
        technical_result=_technical_result(_kd_signal(), _kd_signal("5555")),
        include_scoring=False,
    )

    assert result.selected_codes == ["4444"]
    report = strip_stock_markers(result.report_text)
    assert "K1 KD 金叉後突破或收復 MA5" in report
    assert "4444 KD策略股 |" in report
    assert "5555" in report
    assert "早期單點異動觀察" in report


def test_curated_passes_kd_evidence_separately_to_radar_scorer(monkeypatch):
    captured = {}

    def fake_score(candidates, *_args, **_kwargs):
        captured.update({item.code: item for item in candidates})
        return candidates

    monkeypatch.setattr(radar, "prepare_radar_scoring_data", lambda *args, **kwargs: None)
    monkeypatch.setattr(radar, "score_radar_candidates", fake_score)
    curated._score_curated_candidates(
        target_date=REPORT_DATE,
        selected_codes=["4444"],
        selected_by_signal={scanner.kd_ma_signal_label(_kd_signal()): ["4444"]},
        stock_info={"4444": {"name": "KD策略股", "industry": "電子業", "price": 30.0}},
        hits={"4444": ["營收", "籌碼"]},
        chip_grade_maps={},
        technical_result=_technical_result(_kd_signal()),
    )

    assert captured["4444"].technical_signals == []
    assert captured["4444"].dual_ma_signals == []
    assert captured["4444"].kd_ma_signals == [_kd_signal()]


def test_radar_generated_candidate_keeps_kd_evidence_without_fixed_extra_score(monkeypatch):
    result = _technical_result(_kd_signal())
    saved = {}
    monkeypatch.setattr(radar, "_find_technical_scan_cache", lambda *_: None)
    monkeypatch.setattr(radar.ts, "run_technical_scan", lambda *_: result)
    monkeypatch.setattr(radar, "save_recent_scan_result", lambda *args, **kwargs: saved.update(kwargs))
    monkeypatch.setattr(radar, "_stock_meta_by_code", lambda: {})

    candidates, policy = radar._technical_candidates_for_radar(REPORT_DATE, {}, None)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.code == "4444"
    assert candidate.kd_ma_signals == [_kd_signal()]
    assert candidate.technical_signals == []
    assert policy["kd_ma_signal_count"] == 1
    assert saved["metadata"]["kd_ma_signals"] == [_kd_signal()]
    assert "KD" in radar._technical_signal_line(candidate)
    assert radar._build_radar_evidence_pack(candidate, REPORT_DATE)["technical"]["kd_ma_signals"] == [_kd_signal()]
    snapshot = {"technical": {"above_ma": {"ma5": True, "ma21": True}}}
    plain = radar.RadarCandidate(code="4444")
    assert radar._score_technical_detail(candidate, snapshot)["score"] == radar._score_technical_detail(plain, snapshot)["score"]


def test_radar_cached_candidate_restores_kd_only_stock(monkeypatch):
    record = {
        "scan_type": "技術面選股",
        "report_date": REPORT_DATE.isoformat(),
        "selected_codes": [],
        "strategy_signals": {"A": [], "B": [], "C": [], "D": []},
        "dual_ma_signals": [],
        "kd_ma_signals": [_kd_signal()],
    }
    monkeypatch.setattr(radar, "load_recent_scan_results", lambda **kwargs: [record])
    monkeypatch.setattr(radar, "_stock_meta_by_code", lambda: {})
    monkeypatch.setattr(radar.ts, "run_technical_scan", lambda *_: (_ for _ in ()).throw(AssertionError("must reuse complete cache")))

    candidates, policy = radar._technical_candidates_for_radar(REPORT_DATE, {}, None)

    assert [item.code for item in candidates] == ["4444"]
    assert policy["kd_ma_signal_count"] == 1


def test_radar_candidate_round_trip_preserves_kd_evidence():
    candidate = radar.RadarCandidate(code="4444", kd_ma_signals=[_kd_signal()])
    record = {
        "source": "technical",
        "report_date": REPORT_DATE.isoformat(),
        "candidates": [radar._candidate_to_dict(candidate)],
    }

    loaded = radar._record_to_result(record)

    assert loaded.candidates[0].kd_ma_signals == [_kd_signal()]
