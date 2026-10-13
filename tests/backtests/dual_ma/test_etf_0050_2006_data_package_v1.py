"""Phase-1 QA for the 0050 2005--2026 causal research package."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "backtests" / "dual_ma" / "etf_0050_2006_data_package_v1.py"


def _load():
    spec = importlib.util.spec_from_file_location("etf_0050_2006_data_v1", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pkg = _load()


def _json(relative: str):
    return json.loads((pkg.OUT / relative).read_text(encoding="utf-8"))


def test_contract_and_adopted_package_pins_are_exact():
    assert pkg.file_sha256(pkg.CONTRACT) == pkg.CONTRACT_SHA256
    assert pkg.file_sha256(pkg.ADOPTED / "READY_FOR_02.json") == pkg.ADOPTED_READY_SHA256
    assert pkg.file_sha256(pkg.ADOPTED / "artifact_manifest.json") == pkg.ADOPTED_MANIFEST_SHA256


def test_validate_only_is_network_free_and_passes():
    result = pkg.validate_only()
    assert result["status"] == "PASS"
    assert result["network_used"] is False
    assert result["writes_performed"] is False
    assert result["recomputed"]["artifact_hashes_verified"] is True
    assert result["recomputed"]["source_hashes_verified"] is True


def test_gate_stops_before_performance_and_has_no_named_hold():
    gate = _json("STOP_WAIT_00_DATA_QA.json")
    assert gate["status"] == "STOP_WAIT_00_DATA_QA"
    assert gate["data_complete_candidate"] is True
    assert gate["ready_for_performance"] is False
    assert gate["performance_read_or_computed"] is False
    assert gate["named_gaps"] == []
    assert all(gate["checks"].values())


def test_daily_calendar_and_source_boundary_are_exact():
    payload = _json("data/0050/daily_raw.json")
    rows = payload["rows"]
    assert len(rows) == 5340
    assert rows[0]["date"] == "2005-01-03"
    assert rows[-1]["date"] == "2026-09-30"
    assert payload["formal"]["from"] == "2006-01-02"
    assert next(row["date"] for row in rows if row["date"] >= "2006-01-01") == "2006-01-02"
    assert all(row["source"] == "FinMind_TaiwanStockPrice" for row in rows if row["date"] < "2010-01-01")
    assert all(row["source"] == "TWSE_STOCK_DAY" for row in rows if row["date"] >= "2010-01-01")
    assert len({row["date"] for row in rows}) == len(rows)


def test_2005_is_warmup_only():
    coverage = _json("coverage.json")
    assert coverage["warmup"] == {
        "from": "2005-01-03",
        "through": "2005-12-30",
        "trading_days": 247,
        "performance_allowed": False,
    }
    fixture = _json("fixtures/warmup_to_formal_2005_2006.json")
    assert fixture["performance_or_transactions_before_formal_from"] == "PROHIBITED"
    assert fixture["rows"][-1]["date"] >= "2006-01-02"


def test_early_finmind_daily_matches_first_party_twse_monthly_totals():
    validation = _json("validation/twse_fmsrfk_2005_2009_validation.json")
    assert validation["months_checked"] == 60
    assert validation["mismatch_count"] == 0
    assert validation["mismatches"] == []
    assert all(not row["differences"] for row in validation["comparisons"])


def test_full_finmind_raw_cross_check_and_adjusted_limit_are_explicit():
    raw = _json("validation/finmind_price_cross_validation.json")
    assert raw["twse_rows"] == raw["finmind_rows"] == raw["intersection_rows"] == 5340
    assert raw["mismatch_count"] == 0
    assert raw["twse_only_dates"] == raw["finmind_only_dates"] == []
    adjusted = _json("validation/finmind_adjusted_cross_validation.json")
    assert adjusted["rows"] == 2612
    assert adjusted["from"] == "2016-01-04"
    assert "2005-2015" in adjusted["coverage_limitation"]
    assert adjusted["policy"] == "CROSS_VALIDATION_ONLY_NOT_EXECUTION_AND_NOT_UNVERIFIED_SIGNAL_INPUT"


def test_adopted_2016_2026_overlap_is_exact_and_pins_stay_unchanged():
    overlap = _json("validation/adopted_2016_2026_overlap.json")
    assert overlap["price_rows_checked"] == 2612
    assert overlap["price_mismatch_count"] == 0
    assert overlap["event_rows_checked"] == 22
    assert overlap["event_mismatch_count"] == 0
    assert pkg.file_sha256(pkg.ADOPTED / "READY_FOR_02.json") == pkg.ADOPTED_READY_SHA256
    assert pkg.file_sha256(pkg.ADOPTED / "artifact_manifest.json") == pkg.ADOPTED_MANIFEST_SHA256


def test_dividends_and_split_have_first_party_evidence():
    dividends = _json("events/0050_dividends.json")
    assert dividends["twse_event_count"] == 32
    assert dividends["finmind_dividend_rows"] == 32
    assert dividends["finmind_dividend_result_rows"] == 32
    assert dividends["events"][0]["ex_date"] == "2005-05-19"
    assert dividends["events"][-1]["ex_date"] == "2026-07-21"
    split = _json("events/0050_split.json")
    assert split["status"] == "FIRST_PARTY_VERIFIED"
    assert split["ratio"] == 4.0
    assert len(split["finmind_cross_validation_rows"]) == 1


def test_causal_formal_snapshot_excludes_future_events_and_anchors_raw():
    raw = {row["date"]: row for row in _json("data/0050/daily_raw.json")["rows"]}
    snapshot = _json("technical/as_of_2006-01-02.json")
    assert snapshot["rows"][-1]["trade_date"] == "2006-01-02"
    assert snapshot["rows"][-1]["technical_close"] == raw["2006-01-02"]["close"]
    assert all(row["as_of_date"] == "2006-01-02" for row in snapshot["rows"])
    assert all("2025-06-18" not in event for row in snapshot["rows"] for event in row["factor_event_ids"])


def test_split_fixture_conserves_book_cost_without_a_trade():
    fixture = _json("fixtures/split_crossing_2025.json")
    assert fixture["transaction_count_on_event_date"] == 0
    assert fixture["position_after"]["shares"] == fixture["position_before"]["shares"] * 4
    for field in ("unit_cost", "stop", "addon", "r"):
        assert fixture["position_after"][field] == fixture["position_before"][field] / 4
    assert fixture["position_after"]["book_cost"] == fixture["position_before"]["book_cost"]


def test_source_receipts_have_hashes_and_no_secrets():
    table = _json("sources/source_master_table.json")
    assert table["entry_count"] >= 300
    for receipt in (pkg.OUT / "sources").rglob("*.json"):
        text = receipt.read_text(encoding="utf-8")
        assert "Authorization" not in text
        assert "finmind_api_key" not in text
        assert "access_token" not in text


def test_archive_request_is_idempotent_and_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(pkg, "OUT", tmp_path)
    body = b'{"status":200,"data":[]}'
    calls = []

    def fake_get(url, params):
        calls.append((url, params))
        return 200, body, url + "?x=1"

    monkeypatch.setattr(pkg, "http_get", fake_get)
    first = pkg.archive_request("sample", "one", "https://example.test", {"x": 1})
    second = pkg.archive_request("sample", "one", "https://example.test", {"x": 1})
    assert first == second
    assert len(calls) == 1
    (tmp_path / "sources/sample/raw/one.raw").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SOURCE_RECEIPT_INVALID"):
        pkg.archive_request("sample", "one", "https://example.test", {"x": 1})


@pytest.mark.parametrize("status", [403, 429])
def test_archive_request_preserves_failure_receipt(tmp_path, monkeypatch, status):
    monkeypatch.setattr(pkg, "OUT", tmp_path)
    monkeypatch.setattr(pkg, "http_get", lambda *args, **kwargs: (status, b"blocked", "https://example.test"))
    with pytest.raises(RuntimeError, match="HTTP_" + str(status)):
        pkg.archive_request("sample", "blocked", "https://example.test", {})
    assert not (tmp_path / "sources/sample/raw/blocked.raw").exists()
    receipt = json.loads((tmp_path / "sources/sample/receipts/blocked.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "FAIL_CLOSED"


def test_cp950_import():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp950"
    env["PYTHONUTF8"] = "0"
    code = (
        "import importlib.util,sys;"
        "s=importlib.util.spec_from_file_location('m',r'" + str(SCRIPT) + "');"
        "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m);print('CP950_OK')"
    )
    completed = subprocess.run([sys.executable, "-X", "utf8=0", "-c", code], cwd=str(ROOT), env=env, capture_output=True)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    assert b"CP950_OK" in completed.stdout
