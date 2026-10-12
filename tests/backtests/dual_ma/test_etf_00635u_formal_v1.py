import csv
import json
import re
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtests.dual_ma import etf_00635u_formal_v1 as formal
from backtests.dual_ma import etf_00635u_prep_v1 as prep


ETF_FIELDS = (
    "date", "open", "high", "low", "close", "volume",
    "official_session", "tradable", "tradability_reason",
    "price_rebase_factor", "share_factor", "cash_dividend_per_share",
    "dividend_event_id", "dividend_stage",
)


def _write_formal_snapshot(
    root,
    *,
    count=460,
    include_taiex=False,
    gold_delay=0,
    unstaged_dividend=False,
):
    root.mkdir(parents=True, exist_ok=True)
    etf = root / "etf.csv"
    gold = root / "gold.csv"
    taiex = root / "taiex.csv"
    start = date(2015, 4, 15)
    dates = [start + timedelta(days=index * 7) for index in range(count)]
    with etf.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ETF_FIELDS)
        writer.writeheader()
        for index, day in enumerate(dates):
            phase = index % 80
            close = Decimal("15") + Decimal(phase if phase <= 40 else 80 - phase) / Decimal("2")
            writer.writerow({
                "date": day.isoformat(), "open": close + Decimal("0.1"),
                "high": close + 1, "low": close - 1, "close": close,
                "volume": 100000, "official_session": True, "tradable": True,
                "tradability_reason": "", "price_rebase_factor": 1,
                "share_factor": 1,
                "cash_dividend_per_share": (
                    1 if unstaged_dividend and index == min(200, count // 2) else 0
                ),
                "dividend_event_id": "", "dividend_stage": "",
            })
    with gold.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=(
            "source_date", "available_date", "value", "source_timezone",
        ))
        writer.writeheader()
        for index, day in enumerate(dates[gold_delay:], start=gold_delay):
            writer.writerow({
                "source_date": day.isoformat(), "available_date": day.isoformat(),
                "value": Decimal("100") + Decimal(index) / 5,
                "source_timezone": "America/New_York",
            })
    files = [
        {"role": "etf_daily_causal", "path": etf.name, "sha256": prep.sha256_file(etf)},
        {"role": "gold_er_official", "path": gold.name, "sha256": prep.sha256_file(gold)},
    ]
    if include_taiex:
        with taiex.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=(
                "source_date", "available_date", "value", "source_timezone",
            ))
            writer.writeheader()
            for index, day in enumerate(dates):
                writer.writerow({
                    "source_date": day.isoformat(), "available_date": day.isoformat(),
                    "value": 10000 + index, "source_timezone": "Asia/Taipei",
                })
        files.append({
            "role": "taiex_total_return", "path": taiex.name,
            "sha256": prep.sha256_file(taiex),
        })
    manifest = {
        "schema": prep.MANIFEST_SCHEMA, "symbol": "00635U",
        "listing_date": "2015-04-15", "immutable": True,
        "data_classification": "SYNTHETIC_TEST_ONLY", "files": files,
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def test_manifest_role_adapter_and_formal_analysis_are_data_agnostic(tmp_path):
    etf = tmp_path / "etf.csv"
    gold = tmp_path / "gold.csv"
    start = date(2015, 4, 15)
    with etf.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=(
            "date", "open", "high", "low", "close", "volume",
            "official_session", "tradable", "tradability_reason",
            "price_rebase_factor", "share_factor", "cash_dividend_per_share",
            "dividend_event_id", "dividend_stage",
        ))
        writer.writeheader()
        for index in range(170):
            close = Decimal("20") + Decimal(index % 30) / Decimal("10")
            writer.writerow({
                "date": (start + timedelta(days=index)).isoformat(),
                "open": close, "high": close + 1, "low": close - 1, "close": close,
                "volume": 1000, "official_session": True, "tradable": True,
                "price_rebase_factor": 1, "share_factor": 1,
            })
    with gold.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=(
            "source_date", "available_date", "value", "source_timezone",
        ))
        writer.writeheader()
        for index in range(170):
            day = start + timedelta(days=index)
            writer.writerow({
                "source_date": day.isoformat(), "available_date": day.isoformat(),
                "value": 100 + index, "source_timezone": "America/New_York",
            })
    manifest = {
        "schema": prep.MANIFEST_SCHEMA,
        "symbol": "00635U",
        "listing_date": "2015-04-15",
        "immutable": True,
        "files": [
            {"role": "etf_daily_causal", "path": etf.name, "sha256": prep.sha256_file(etf)},
            {"role": "gold_er_official", "path": gold.name, "sha256": prep.sha256_file(gold)},
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    loaded = formal.load_formal_snapshot(manifest_path)
    assert len(loaded.indicator_rows) == 170
    assert loaded.indicator_rows[143].sma144 is not None
    analysis = formal.run_formal_analysis(loaded)
    assert len(analysis["lines"]) == 7
    assert len(analysis["active"]) == 4
    assert len(analysis["cost_sensitivity"]) == 12
    assert analysis["taiex_status"] == "HOLD_MISSING_TAIEX_TOTAL_RETURN_ROLE"
    assert analysis["taiex_supplement"] is None


def test_formal_rejects_unstaged_dividend_and_late_gold_start(tmp_path):
    unstaged = _write_formal_snapshot(
        tmp_path / "unstaged", count=180, unstaged_dividend=True
    )
    with pytest.raises(ValueError, match="legacy unstaged cash dividend"):
        formal.load_formal_snapshot(unstaged)

    late = _write_formal_snapshot(tmp_path / "late", count=180, gold_delay=1)
    data = formal.load_formal_snapshot(late)
    with pytest.raises(ValueError, match="first ETF session"):
        formal.run_formal_analysis(data)


def test_active_metrics_use_trades_and_taiex_is_supplement_only(tmp_path):
    manifest = _write_formal_snapshot(tmp_path / "snapshot", include_taiex=True)
    analysis = formal.run_formal_analysis(formal.load_formal_snapshot(manifest))
    assert len(analysis["lines"]) == 7
    assert all("TAIEX" not in line.line_id for line in analysis["lines"])
    assert analysis["taiex_status"] == "AVAILABLE_SUPPLEMENT_ONLY"
    assert analysis["taiex_supplement"].line_id == "TAIEX_TOTAL_RETURN_SUPPLEMENT"
    active_metrics = [
        analysis["line_metrics"][line_id] for line_id in analysis["active"]
    ]
    assert any(row["closed_positions"] > 0 for row in active_metrics)
    assert any(row["maximum_single_entry_cash"] > 0 for row in active_metrics)
    starts = {line.audit["comparison_start"] for line in analysis["lines"]}
    ends = {line.audit["comparison_end"] for line in analysis["lines"]}
    counts = {line.audit["comparison_observations"] for line in analysis["lines"]}
    assert len(starts) == len(ends) == len(counts) == 1
    assert all(len(benchmarks) == 3 for benchmarks in analysis["relative_benchmarks"].values())


def test_formal_pack_revalidates_gate_and_replays_identically_in_two_roots(tmp_path):
    snapshot = _write_formal_snapshot(tmp_path / "snapshot", include_taiex=True)
    sha = prep.sha256_file(snapshot)
    with pytest.raises(prep.FormalSnapshotNotAccepted, match="manifest SHA mismatch"):
        formal.run_formal_pack(
            snapshot, tmp_path / "rejected", accepted_manifest_sha256="0" * 64
        )
    assert not (tmp_path / "rejected").exists()

    left = tmp_path / "left"
    right = tmp_path / "right"
    one = formal.run_formal_pack(snapshot, left, accepted_manifest_sha256=sha)
    two = formal.run_formal_pack(snapshot, right, accepted_manifest_sha256=sha)
    assert one["manifest_sha256"] == two["manifest_sha256"]
    assert one["status"] == formal.STOP_CODE
    assert prep.validate_report_tree(left, report_name=formal.REPORT_NAME) == []
    assert prep.validate_report_schema(left, report_name=formal.REPORT_NAME) == []
    assert (left / formal.REPORT_NAME).read_bytes() == (right / formal.REPORT_NAME).read_bytes()
    assert (left / formal.TRADE_REPORT_NAME).read_bytes() == (right / formal.TRADE_REPORT_NAME).read_bytes()
    assert not (left / prep.REPORT_NAME).exists()

    report = (left / formal.REPORT_NAME).read_text(encoding="utf-8")
    assert "SYNTHETIC TEST ONLY" in report
    assert "<a id=" not in report
    headings = re.findall(r"^## ((?:0[1-9]|1[0-4])｜[^\n]+)$", report, flags=re.MULTILINE)
    assert len(headings) == 14
    assert all(f"](#{prep._chapter_anchor(title)})" in report for title in headings)
    observations = re.findall(
        r"^### 觀察結果\s*$\n\s*([^\n]+)", report, flags=re.MULTILINE
    )
    assert len(observations) == len(set(observations)) == 14
    assert "[繁體中文逐筆交易明細](trade-details.md)" in report
    assert "[逐筆交易明細 CSV](trade-details.csv)" in report
    assert (left / formal.TRADE_REPORT_NAME).is_file()

    # User-facing Markdown is rounded for reading while CSV retains raw values.
    assert re.search(r"\d{1,3}(?:,\d{3})*\.\d{2} 元", report)
    assert re.search(r"[+-]?\d+\.\d{2}%", report)
    assert "百分點" in report
    assert not re.search(r"(?<![\d-])-?\d+\.\d{3,}", report)
    assert "策略 A（次一交易日開盤成交）" in report
    assert "策略 B（次一交易日開盤成交）" in report
    assert "A_FIXED_STOP_MACD_21_55_55" not in report
    assert "B_FIXED_STOP_2R_KD_9_9_55" not in report
    for line_id in formal.LINE_LABELS:
        if line_id != "TAIEX_TOTAL_RETURN_SUPPLEMENT":
            assert report.count(line_id) == 1
    assert "期末資產" in observations[0]
    assert "總報酬" in observations[0]
    assert "年化報酬率" in observations[0]
    assert "最大回落" in observations[0]
    for phrase in (
        "MA21", "MA144", "MACD(21,55,55)", "高價格區間", "低價格區間",
        "固定初始防守", "策略 A", "策略 B", "淨成本 2R", "KD(9,9,55)",
        "固定防守優先", "訊號日收盤成交", "次一個可成交交易日開盤成交",
    ):
        assert phrase in report
    assert "命令列" in report
    assert "fail closed" not in report

    readable_trades = (left / formal.TRADE_REPORT_NAME).read_text(encoding="utf-8")
    assert "SYNTHETIC TEST ONLY" in readable_trades
    assert "進場來源／出場理由" in readable_trades
    assert not re.search(r"(?<![\d-])-?\d+\.\d{3,}", readable_trades)
    raw_summary = (left / "formal-summary.csv").read_text(encoding="utf-8-sig")
    assert re.search(r"-?\d+\.\d{6,}", raw_summary)

    with (left / "trade-details.csv").open(encoding="utf-8-sig", newline="") as handle:
        trades = list(csv.DictReader(handle))
    assert {
        "signal_date", "fill_date", "timing", "entry_type", "position_id",
        "shares", "reference_price", "fill_price", "commission", "tax",
        "initial_stop", "initial_r", "kd_2r_arm_date", "exit_reason", "net_pnl",
    } <= set(trades[0])

    with (left / "fixed-periods.csv").open(encoding="utf-8-sig", newline="") as handle:
        fixed = list(csv.DictReader(handle))
    assert {"cagr", "max_drawdown"} <= set(fixed[0])
    with (left / "cost-sensitivity.csv").open(encoding="utf-8-sig", newline="") as handle:
        costs = list(csv.DictReader(handle))
    assert {"cagr", "max_drawdown", "completed_rounds"} <= set(costs[0])
    with (left / "parameter-grid.csv").open(encoding="utf-8-sig", newline="") as handle:
        grid = list(csv.DictReader(handle))
    assert len(grid) == 486
    assert {
        "total_return", "cagr", "max_drawdown", "return_drawdown_ratio",
        "rolling_5y_worst_cagr",
    } <= set(grid[0])
    with (left / "core-allocation-summary.csv").open(encoding="utf-8-sig", newline="") as handle:
        core = list(csv.DictReader(handle))
    assert {
        "cagr", "max_drawdown", "core_ending_equity",
        "tactical_ending_equity", "maximum_reconstruction_error",
        "transaction_count", "total_commission", "total_tax",
    } <= set(core[0])

    chart_kinds = set()
    chart_hashes = set()
    for chart in left.glob("*-chart.svg"):
        svg = chart.read_text(encoding="utf-8")
        chart_kinds.add(re.search(r'data-chart-kind="([^"]+)"', svg).group(1))
        chart_hashes.add(re.search(r'data-source-sha256="([0-9a-f]{64})"', svg).group(1))
        assert "STRATEGY_" not in svg
        assert "策略 A" in svg or "策略 B" in svg
    assert len(chart_kinds) == 9
    assert len(chart_hashes) >= 7

    output_manifest = json.loads((left / "manifest.json").read_text(encoding="utf-8"))
    output_names = {item["path"] for item in output_manifest["files"]}
    assert {formal.REPORT_NAME, formal.TRADE_REPORT_NAME} <= output_names
    assert output_manifest["input_data_classification"] == "SYNTHETIC_TEST_ONLY"
    readback = json.loads((left / "readback.json").read_text(encoding="utf-8"))
    assert readback["stop_code"] == formal.STOP_CODE
