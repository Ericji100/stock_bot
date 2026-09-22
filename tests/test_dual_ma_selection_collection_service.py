from __future__ import annotations

from datetime import date, datetime, timezone
import json
from types import SimpleNamespace

import pandas as pd

import stock_ai_bot.selection.dual_ma_selection_collection_service as service
import stock_ai_bot.selection.historical_universe_service as universe_service


def _candidate(code: str, **extra):
    values = {
        "code": code,
        "symbol": f"{code}.TW",
        "market": "TWSE",
        "name": f"stock-{code}",
        "industry": "半導體業",
        "revenue_group": "group_1",
        "gross_margin_rating": "A",
        "setup_type": "breakout",
        "total_score": 70,
        "reasons": ["relative strength"],
        "risks": [],
    }
    values.update(extra)
    return SimpleNamespace(**values)


def _wire_successful_selectors(monkeypatch):
    financial = SimpleNamespace(
        total_symbols=100,
        hard_filter_passed=3,
        candidates=[_candidate("2330"), _candidate("2317")],
    )
    chip_context = SimpleNamespace(
        total_symbols=100,
        latest_trading_date=date(2023, 1, 3),
        candidates=pd.DataFrame(
            [
                {"code": "2330", "symbol": "2330.TW", "market": "TWSE", "name": "TSMC", "industry": "半導體業"},
                {"code": "2454", "symbol": "2454.TW", "market": "TWSE", "name": "MTK", "industry": "半導體業"},
            ]
        ),
    )
    technical = SimpleNamespace(
        total_symbols=100,
        hard_filter_passed=50,
        matched_symbols=2,
        sources={"cache"},
        bullish={"KD 黃金交叉": {"半導體業": ["2454 MTK (100.0)"]}},
        bearish={"MACD 死亡交叉": {"半導體業": ["2317 HonHai (100.0)"]}},
        strategy_signals={"A": [], "B": [], "C": [], "D": []},
        dual_ma_signals=[],
        kd_ma_signals=[],
    )
    curated = SimpleNamespace(
        selected_codes=["2330"],
        stock_info={"2330": {"name": "TSMC"}},
        hits={"2330": ["財報", "籌碼"]},
        scores={"2330": {"total": 80}},
        selected_by_signal={"KD 黃金交叉": ["2330"]},
    )
    laoxiao_candidate = _candidate("2603", market="TWSE", industry="航運業")
    laoxiao = SimpleNamespace(
        selected_codes=["2603"],
        candidates=[laoxiao_candidate],
        diagnostics={"selected_count": 1},
    )

    monkeypatch.setattr(service, "scan_tw_market", lambda *_args, **_kwargs: financial)
    monkeypatch.setattr(service, "build_market_context", lambda *_args, **_kwargs: chip_context)
    monkeypatch.setattr(
        service,
        "build_chip_grade_maps",
        lambda *_args, **_kwargs: {
            "chip_1": {"2330": "S"},
            "chip_2": {"2454": "A"},
            "chip_3": {},
            "chip_4": {"2330": "B"},
        },
    )
    monkeypatch.setattr(service.technical_scanner, "run_technical_scan", lambda *_args, **_kwargs: technical)
    monkeypatch.setattr(
        service.technical_scanner,
        "collect_technical_selected_codes",
        lambda _result: ["2454", "2317"],
    )
    monkeypatch.setattr(
        service.curated_scan_service,
        "build_curated_scan_result",
        lambda *_args, **_kwargs: curated,
    )
    monkeypatch.setattr(
        service.laoxiao_scan_service,
        "build_laoxiao_scan_result",
        lambda *_args, **_kwargs: laoxiao,
    )


def test_collects_existing_program_groups_and_preserves_memberships(monkeypatch):
    _wire_successful_selectors(monkeypatch)

    manifest = service.collect_daily_selection(
        date(2023, 1, 3),
        historical_replay=True,
        now=datetime(2023, 1, 3, 21, 0, tzinfo=timezone.utc),
    )

    assert manifest.overall_status == "COMPLETE"
    assert list(manifest.programs) == list(service.PROGRAM_ORDER)
    assert manifest.union_codes == ["2317", "2330", "2454", "2603"]
    by_code = {row["code"]: row for row in manifest.candidate_memberships()}
    assert by_code["2330"]["programs"] == ["financial", "chip_1", "chip_4", "curated"]
    assert by_code["2454"]["programs"] == ["chip_2", "technical"]
    signals = manifest.programs["technical"].candidate_details["2317"]["signals"]
    assert signals[0]["direction"] == "bearish"
    assert manifest.freeze_payload()["universe_notice"].startswith("historical membership")
    assert manifest.provenance["universe"]["status"] == "UNVERIFIED_AS_OF_MEMBERSHIP"
    assert manifest.provenance["universe"]["formal_historical_backtest_ready"] is False
    assert set(manifest.provenance["selector_source_sha256"]) == {
        "financial",
        "chip",
        "technical",
        "curated",
        "laoxiao",
    }


def test_failed_chip_collection_degrades_without_substituting_other_sources(monkeypatch):
    _wire_successful_selectors(monkeypatch)
    monkeypatch.setattr(
        service,
        "build_market_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("chip cache unavailable")),
    )

    manifest = service.collect_daily_selection(date(2023, 1, 3), historical_replay=True)

    assert manifest.overall_status == "DEGRADED"
    for key in ("chip_1", "chip_2", "chip_3", "chip_4"):
        assert manifest.programs[key].status == service.STATUS_FAILED
        assert manifest.programs[key].codes == ()
    assert manifest.programs["curated"].status == service.STATUS_MISSING_DEPENDENCY
    assert manifest.programs["financial"].status == service.STATUS_SUCCESS
    assert manifest.programs["technical"].status == service.STATUS_SUCCESS


def test_historical_chip_collection_is_cache_only(monkeypatch):
    captured = {}
    context = SimpleNamespace(
        total_symbols=1,
        latest_trading_date=date(2023, 1, 3),
        candidates=pd.DataFrame([{"code": "2330"}]),
    )

    def build_context(*_args, **kwargs):
        captured.update(kwargs)
        return context

    monkeypatch.setattr(service, "build_market_context", build_context)
    monkeypatch.setattr(
        service,
        "build_chip_grade_maps",
        lambda *_args, **_kwargs: {key: {} for key in ("chip_1", "chip_2", "chip_3", "chip_4")},
    )

    service._collect_chip({}, date(2023, 1, 3), historical_replay=True)

    assert captured["cached_only"] is True
    assert captured["historical_replay"] is True
    assert captured["include_foreign_ratio"] is False
    assert isinstance(captured["trading_calendar"], list)


def test_empty_historical_chip_sources_are_not_recorded_as_success(monkeypatch):
    _wire_successful_selectors(monkeypatch)
    context = SimpleNamespace(
        total_symbols=100,
        latest_trading_date=None,
        candidates=pd.DataFrame([{"code": "2330"}]),
        daily_data=pd.DataFrame(),
        weekly_data=pd.DataFrame(),
    )
    monkeypatch.setattr(service, "build_market_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(
        service,
        "build_chip_grade_maps",
        lambda *_args, **_kwargs: {key: {} for key in ("chip_1", "chip_2", "chip_3", "chip_4")},
    )

    manifest = service.collect_daily_selection(date(2023, 1, 3), historical_replay=True)

    for key in ("chip_1", "chip_2", "chip_3"):
        assert manifest.programs[key].status == service.STATUS_MISSING_SOURCE
        assert manifest.programs[key].diagnostics["missing_source"] == "institutional_daily_history"
    assert manifest.programs["chip_4"].status == service.STATUS_MISSING_SOURCE
    assert manifest.programs["chip_4"].diagnostics["missing_source"] == "tdcc_weekly_history"
    assert manifest.programs["curated"].status == service.STATUS_MISSING_DEPENDENCY


def test_freeze_is_content_addressed_idempotent_and_never_overwrites(monkeypatch, tmp_path):
    _wire_successful_selectors(monkeypatch)
    first = service.collect_daily_selection(
        date(2023, 1, 3),
        historical_replay=True,
        now=datetime(2023, 1, 3, 13, 0, tzinfo=timezone.utc),
    )
    second = service.collect_daily_selection(
        date(2023, 1, 3),
        historical_replay=True,
        now=datetime(2023, 1, 4, 13, 0, tzinfo=timezone.utc),
    )

    first_path = service.freeze_daily_selection_manifest(first, tmp_path)
    second_path = service.freeze_daily_selection_manifest(second, tmp_path)

    assert first.manifest_id == second.manifest_id
    assert first_path == second_path
    payload = json.loads(first_path.read_text(encoding="utf-8"))
    assert payload["generated_at"] == first.generated_at
    assert payload["content_sha256"] == first.content_sha256

    changed_programs = dict(first.programs)
    changed_programs["financial"] = service.ProgramSelection.success("financial", ["1101"])
    changed = service.DailySelectionManifest(
        report_date=first.report_date,
        historical_replay=True,
        programs=changed_programs,
        generated_at=first.generated_at,
    )
    changed_path = service.freeze_daily_selection_manifest(changed, tmp_path)

    assert changed_path != first_path
    assert first_path.exists() and changed_path.exists()


def test_verified_historical_universe_is_injected_and_restored(monkeypatch, tmp_path):
    _wire_successful_selectors(monkeypatch)
    sources = universe_service.OfficialUniverseSources(
        collected_at=datetime(2026, 9, 21, tzinfo=timezone.utc).isoformat(),
        coverage_start=date(2022, 1, 1),
        coverage_end=date(2025, 12, 31),
        twse_current=(
            {
                "公司代號": "2330",
                "公司簡稱": "台積電",
                "產業別": "24",
                "上市日期": "19940905",
            },
        ),
        tpex_current=(),
        twse_delisted=(),
        tpex_delisted=(),
        twse_new_listings=(),
        tpex_new_listings=(),
    )
    universe = universe_service.build_historical_universe(sources)
    original_stock_loader = service.stock_scanner.load_stock_universe
    original_technical_loader = service.technical_scanner.load_stock_universe
    original_chip_cache_dir = service.chip_strategies.DAILY_CHIP_CACHE_DIR
    original_tdcc_cache_dir = service.chip_strategies.TDCC_CACHE_DIR

    manifest, snapshot_path = service.collect_historical_daily_selection(
        date(2023, 1, 3),
        universe,
        universe_snapshot_root=tmp_path / "universe",
        now=datetime(2023, 1, 3, 13, 0, tzinfo=timezone.utc),
    )

    assert snapshot_path.exists()
    assert manifest.provenance["universe"]["status"] == "VERIFIED_AS_OF_MEMBERSHIP"
    assert manifest.provenance["universe"]["member_count"] == 1
    assert manifest.freeze_payload()["universe_notice"].startswith("historical membership supplied")
    assert service.stock_scanner.load_stock_universe is original_stock_loader
    assert service.technical_scanner.load_stock_universe is original_technical_loader
    assert service.chip_strategies.DAILY_CHIP_CACHE_DIR == original_chip_cache_dir
    assert service.chip_strategies.TDCC_CACHE_DIR == original_tdcc_cache_dir
