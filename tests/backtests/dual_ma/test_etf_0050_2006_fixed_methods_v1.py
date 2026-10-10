from __future__ import annotations

import csv
import json
from datetime import date
from decimal import Decimal

import pytest

from backtests.dual_ma import etf_0050_2006_fixed_methods_v1 as study
from backtests.dual_ma import etf_0050_ma21_fixed_stop_v1 as fixed


def test_all_locked_inputs_are_exact() -> None:
    study.validate_inputs()
    assert study.file_sha256(study.CONTRACT) == study.EXPECTED["contract"]
    assert study.file_sha256(study.PACKAGE_ROOT / "artifact_manifest.json") == study.EXPECTED["package_manifest"]
    assert study.file_sha256(study.ACCEPTED_RUN_ROOT / "manifest.json") == study.EXPECTED["accepted_run_manifest"]
    assert study.file_sha256(study.ACCEPTED_REPORT_ROOT / "artifact-manifest.json") == study.EXPECTED["accepted_report_manifest"]


def test_full_and_compatibility_package_boundaries() -> None:
    package, rows, events = study.load_package()
    assert len(rows) == 5340
    assert len(events) == 33
    assert len(package.dividends) == 32
    assert package.sessions[0] == date(2005, 1, 3)
    assert package.sessions[-1] == study.CUTOFF

    compat, compat_rows, compat_events = study.load_package(slice_from=study.COMPAT_WARMUP_START)
    assert len(compat_rows) == 2612
    assert len(compat_events) == 22
    assert len(compat.dividends) == 21
    assert compat.sessions[0] == study.COMPAT_WARMUP_START


def test_phase1_source_limit_is_preserved() -> None:
    capability = json.loads((study.PACKAGE_ROOT / "sources/source_capability_probe.json").read_text(encoding="utf-8"))
    assert capability["paid_tier_used"] is False
    assert capability["semantic_switches"][0]["execution_source"] == "FinMind_TaiwanStockPrice"
    assert capability["semantic_switches"][0]["official_validation"] == "TWSE_FMSRFK_MONTHLY"


def test_causal_maps_cover_warmup_and_formal_start_without_future_factor() -> None:
    package, rows, events = study.load_package()
    gates, sma21, audit = study.build_causal_maps(raw_rows=rows, events=events, official_sessions=package.sessions)
    assert len(gates) == len(sma21) == len(audit) == 5340
    assert audit[0]["as_of"] == date(2005, 1, 3)
    assert audit[-1]["as_of"] == study.CUTOFF
    assert audit[-1]["same_day_technical_close"] == Decimal("112.05")
    assert gates[study.FORMAL_START].state in {"PASS", "FAIL", "UNDETERMINED"}
    assert sma21[study.FORMAL_START] is not None


def test_period_configuration_restores_imported_accepted_modules() -> None:
    package, _rows, _events = study.load_package(slice_from=study.COMPAT_WARMUP_START)
    original_fixed_start = fixed.FORMAL_START
    original_builder = fixed.build_ma21_macd_run
    with study.configured_period(study.COMPAT_START, package, "TEST"):
        assert fixed.FORMAL_START == study.COMPAT_START
        assert fixed.build_ma21_macd_run is not original_builder
    assert fixed.FORMAL_START == original_fixed_start
    assert fixed.build_ma21_macd_run is original_builder


def test_isolated_2017_compatibility_replay_is_exact() -> None:
    package, rows, events = study.load_package(slice_from=study.COMPAT_WARMUP_START)
    output, _opportunities, _macd, _causal = study.run_methods(
        package=package, raw_rows=rows, events=events,
        git_commit="TEST", start=study.COMPAT_START,
    )
    result = study.compatibility_check(output)
    assert result["status"] == "PASS_EXACT"
    assert len(result["checks"]) == 10
    assert all(item["exact"] for item in result["checks"].values())


def test_final_output_if_present_is_self_verified() -> None:
    if not study.OUTPUT_ROOT_DEFAULT.exists() or not study.REPORT_ROOT_DEFAULT.exists():
        pytest.skip("formal output not built yet")
    result = study.validate_only()
    assert result["status"] == "PASS"
    assert result["broken_links"] == []
    with (study.OUTPUT_ROOT_DEFAULT / "details/all_scenarios_metrics.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 15
    assert {int(row["slippage_bps"]) for row in rows} == {0, 10, 20}
    with (study.REPORT_ROOT_DEFAULT / "calendar-year-summary.csv").open(encoding="utf-8-sig", newline="") as handle:
        annual = list(csv.DictReader(handle))
    assert len(annual) == 5 * len(study.YEARS)
    assert {int(row["year"]) for row in annual} == set(study.YEARS)


def test_report_only_errata_is_complete_and_preserves_v1(tmp_path) -> None:
    legacy_manifest = study.file_sha256(study.LEGACY_REPORT_ROOT / "artifact-manifest.json")
    output = tmp_path / "report-v1-1"
    result = study._report_build_errata(study.OUTPUT_ROOT_DEFAULT, output)
    assert result["status"] == "STOP_WAIT_00_QA"
    assert result["all_scenario_rows"] == 15
    assert result["legacy_report_unchanged"] is True
    assert study.file_sha256(study.LEGACY_REPORT_ROOT / "artifact-manifest.json") == legacy_manifest
    for name in (
        "all-scenarios-summary.csv",
        "all-scenarios-trade-details.csv",
        "all-scenarios-transactions.csv",
        "all-scenarios-daily-equity.csv",
        "all-scenarios-corporate-actions.csv",
        "macd-event-audit.csv",
    ):
        assert (output / name).is_file()
        assert f"]({name})" in (output / "README.md").read_text(encoding="utf-8")
        assert f"]({name})" in (output / "report.md").read_text(encoding="utf-8")
    with (output / "all-scenarios-summary.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 15
    assert {int(row["slippage_bps"]) for row in rows} == {0, 10, 20}
    risk = (output / "benchmark-risk-report.md").read_text(encoding="utf-8")
    assert "五法全期風險與資金使用對照" in risk
    assert "報酬／回落" in risk
    assert "平均資金閒置" in risk
    readback = json.loads((output / "readback.json").read_text(encoding="utf-8"))
    assert readback == {"mismatches": [], "status": "PASS"}
