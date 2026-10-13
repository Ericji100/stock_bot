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
    include_gold=True,
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
    ]
    if include_gold:
        files.append({
            "role": "gold_er_official", "path": gold.name,
            "sha256": prep.sha256_file(gold),
        })
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


def _table_data_rows(markdown, heading, next_heading):
    section = markdown.split(heading, 1)[1].split(next_heading, 1)[0]
    return [
        line for line in section.splitlines()
        if line.startswith("|") and not line.startswith("|---")
    ][1:]


def test_trade_report_uses_role_specific_chinese_reason():
    common = {
        "line_id": "STRATEGY_A_NEXT_SESSION_OPEN",
        "signal_date": "2024-01-02",
        "fill_date": "2024-01-03",
        "timing": "NEXT_SESSION_OPEN",
        "entry_type": "LOW_REGIME_MACD_GREEN_TO_RED",
        "position_id": "P-1",
        "shares": 1000,
        "reference_price": "20",
        "fill_price": "20.1",
        "commission": "28.64",
        "tax": "0",
        "initial_stop": "18",
        "initial_r": "2.1",
        "kd_2r_arm_date": None,
        "net_pnl": None,
    }
    report = formal._trade_report(
        [
            {**common, "role": "ENTRY", "exit_reason": "MACD_RED_TO_GREEN"},
            {
                **common,
                "role": "EXIT",
                "exit_reason": "MACD_RED_TO_GREEN",
                "tax": "60.3",
                "net_pnl": "1234.56",
            },
        ],
        synthetic=True,
    )
    assert report.startswith("# 00635U 因果回測｜合成測試逐筆明細（非正式績效）")
    assert "合成測試資料／不是正式績效" in report
    assert "交易筆數刻意縮小，只驗證管線與規則" in report
    assert "不得解讀為 00635U 上市以來的正式交易紀錄" in report
    assert "1 條策略線 ×（1 次進場（ENTRY）＋1 次出場（EXIT））＝2 列事件" in report
    assert "這不是 2 筆獨立交易" in report
    assert "低價格區間 MACD 綠翻紅進場、MACD 紅翻綠出場" in report
    entry_line, exit_line = [
        line for line in report.splitlines()
        if line.startswith("| 策略 A（次一交易日開盤成交）")
    ]
    assert "低價格區間 MACD 綠翻紅（`LOW_REGIME_MACD_GREEN_TO_RED`）" in entry_line
    assert "MACD 紅翻綠（`MACD_RED_TO_GREEN`）" in exit_line
    assert "低價格區間 MACD 綠翻紅" not in exit_line
    assert not re.search(
        r"\| (?:LOW_REGIME_MACD_GREEN_TO_RED|MACD_RED_TO_GREEN) \|",
        report,
    )


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


def test_formal_analysis_omits_missing_gold_without_proxy(tmp_path):
    manifest = _write_formal_snapshot(
        tmp_path / "snapshot", include_taiex=True, include_gold=False
    )
    data = formal.load_formal_snapshot(manifest)
    assert data.gold_er is None
    analysis = formal.run_formal_analysis(data)
    assert len(analysis["lines"]) == 6
    assert analysis["gold_status"] == "OMITTED_NO_SP_GSCI_GOLD_ER_OFFICIAL_HISTORY"
    assert all(
        "GOLD_ER_OFFICIAL_TRACKED_INDEX" not in benchmarks
        and len(benchmarks) == 2
        for benchmarks in analysis["relative_benchmarks"].values()
    )


def test_strict_high_analysis_keeps_original_lines_and_audits_exclusions(tmp_path):
    manifest = _write_formal_snapshot(
        tmp_path / "snapshot", include_taiex=True, include_gold=False
    )
    data = formal.load_formal_snapshot(manifest)
    analysis = formal.run_formal_analysis(data, include_strict_high_variants=True)
    assert len(analysis["lines"]) == 10
    assert len(analysis["active"]) == 8
    audit = formal.low_regime_entry_exclusion_audit(data, analysis)
    assert audit
    assert all(row["excluded_from_strict_variant"] for row in audit)
    assert all(Decimal(row["ma21"]) <= Decimal(row["ma144"]) for row in audit)
    assert all(row["causal_fields"] == "SIGNAL_DATE_MA21_MA144_ONLY" for row in audit)
    output = tmp_path / "strict-high-pack"
    result = formal.run_formal_pack(
        manifest,
        output,
        accepted_manifest_sha256=prep.sha256_file(manifest),
        high_regime_study=True,
    )
    assert result["status"] == formal.HIGH_REGIME_STOP_CODE
    assert (output / "low-regime-entry-exclusion-audit.csv").is_file()
    report = (output / formal.REPORT_NAME).read_text(encoding="utf-8")
    assert "MA21 > MA144 高檔限定進場隔離研究" in report
    assert "舊版基準" in report and "本次高檔限定進場" in report
    assert formal.HIGH_REGIME_STOP_CODE in report
    relative_chapter = report.split("## 03｜", 1)[1].split("## 04｜", 1)[0]
    taiex_block = relative_chapter.split("#### 臺灣加權股價報酬指數機會成本補充", 1)[1]
    assert "高檔限定策略 A（次一交易日開盤成交）" in taiex_block
    assert "高檔限定策略 B（次一交易日開盤成交）" in taiex_block
    assert "-0.00 百分點" not in report
    assert "此 gate" not in report
    for phrase in (
        "進場門檻", "低價格區間進場排除稽核", "不是投資建議",
        "不支持將它採納為替代主策略",
    ):
        assert phrase in report
    rules_observation = re.search(
        r"## 04｜[^\n]+[\s\S]*?### 觀察結果\s*\n\s*([^\n]+)", report
    ).group(1)
    assert rules_observation.endswith("。")
    high_manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert high_manifest["publication_eligible"] is False
    assert high_manifest["intended_use"] == "PIPELINE_AND_RULE_VALIDATION_ONLY"


def test_percentage_points_normalizes_negative_zero():
    assert formal._percentage_points(Decimal("-0.000001")) == "0.00 百分點"
    assert formal._readable_decimal(Decimal("0.0000000000000001")) == "1.000000E-16"


def test_macd_only_pack_keeps_original_lines_and_publishes_causal_audit(tmp_path):
    manifest = _write_formal_snapshot(tmp_path / "snapshot", include_taiex=True)
    data = formal.load_formal_snapshot(manifest)
    analysis = formal.run_formal_analysis(data, include_macd_only_variants=True)
    assert len(analysis["lines"]) == 10
    assert len(analysis["active"]) == 8
    assert all(
        line_id in analysis["active"]
        for line_id in (
            "MACD_ONLY_STRATEGY_A_SIGNAL_CLOSE",
            "MACD_ONLY_STRATEGY_A_NEXT_SESSION_OPEN",
            "MACD_ONLY_STRATEGY_B_SIGNAL_CLOSE",
            "MACD_ONLY_STRATEGY_B_NEXT_SESSION_OPEN",
        )
    )
    output = tmp_path / "macd-only-pack"
    result = formal.run_formal_pack(
        manifest,
        output,
        accepted_manifest_sha256=prep.sha256_file(manifest),
        macd_only_study=True,
    )
    assert result["status"] == formal.MACD_ONLY_STOP_CODE
    assert (output / "macd-only-audit.csv").is_file()
    with (output / "macd-only-account-daily.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        account_daily = list(csv.DictReader(handle))
    assert len(account_daily) == 4 * len(data.indicator_rows)
    assert all(Decimal(row["cash"]) >= 0 for row in account_daily)
    assert all(
        Decimal(row["equity"])
        == Decimal(row["cash"]) + Decimal(row["liquidation_value"])
        for row in account_daily
    )
    assert (output / "strategy-rule-contract.md").read_bytes() == formal.MACD_ONLY_CONTRACT.read_bytes()
    report = (output / formal.REPORT_NAME).read_text(encoding="utf-8")
    assert "純 MACD 進出場隔離研究" in report
    assert "MA21／MA144 只留作診斷" in report
    assert "純 MACD 策略 A（次一交易日開盤成交）" in report
    assert "純 MACD 策略 B（次一交易日開盤成交）" in report
    assert "[純 MACD 每日主帳](macd-only-account-daily.csv)" in report
    assert "純 MACD策略" not in report
    assert "一致。。" not in report
    assert formal.MACD_ONLY_STOP_CODE in report
    assert len(re.findall(r"^## ((?:0[1-9]|1[0-4])｜[^\n]+)$", report, flags=re.MULTILINE)) == 14
    taiex_block = report.split("#### 臺灣加權股價報酬指數機會成本補充", 1)[1]
    assert "純 MACD 策略 A（次一交易日開盤成交）" in taiex_block
    assert "純 MACD 策略 B（次一交易日開盤成交）" in taiex_block
    with (output / "parameter-grid.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        grid_rows = list(csv.DictReader(handle))
    effective_summaries, effective_rows = formal._macd_effective_grid_analysis(
        grid_rows
    )
    assert len(grid_rows) == 486
    assert len(effective_rows) == 54
    assert all(row["attachment_rows"] == 243 for row in effective_summaries)
    assert all(row["effective_macd_settings"] == 27 for row in effective_summaries)
    assert all(
        row["diagnostic_ma_repeats_per_setting"] == 9
        for row in effective_summaries
    )
    for phrase in (
        "每策略保留 243 列",
        "策略 A、B 各有 243 列、27 種有效 MACD 設定",
        "每種因 MA 診斷組合重複 9 次",
        "不得把 243 列解讀為 243 個獨立規則",
        "有效 MACD 設定數",
        "27 設定正報酬比例",
    ):
        assert phrase in report
    grid_chart = (output / "parameter-grid-chart.svg").read_text(encoding="utf-8")
    assert "27 種有效 MACD 設定（附件保留 243 列）" in grid_chart
    assert "243 格參數分布" not in grid_chart

    warmup = formal._macd_warmup_analysis(data, analysis)
    assert (
        f"首筆純 MACD 進場訊號為 {warmup['first_macd_signal']}，"
        f"是第 {warmup['first_macd_observation']} 筆觀測"
    ) in report
    assert (
        f"MA144 首次可用為 {warmup['ma144_ready']}，"
        f"是第 {warmup['ma144_ready_observation']} 筆觀測"
    ) in report
    assert f"首筆原策略進場訊號為 {warmup['first_original_signal']}" in report
    assert "不補掛牌前合成 ETF 價格" in report
    assert "不事後加入暖機門檻" in report

    trade_report = (output / formal.TRADE_REPORT_NAME).read_text(encoding="utf-8")
    causal_table = trade_report.split("## 純 MACD 逐部位因果欄位", 1)[1]
    assert "完整原始精度另見 [純 MACD 因果稽核]" in causal_table
    assert "INITIAL_PROTECTIVE_STOP" not in causal_table
    assert "MACD_RED_TO_GREEN" not in causal_table
    assert "KD_2R_DEATH_CROSS_EXIT" not in causal_table
    assert not re.search(r"(?<![\d-])-?\d+\.\d{7,}", causal_table)
    strategy_a_rows = [
        line for line in causal_table.splitlines()
        if line.startswith("| 純 MACD 策略 A")
    ]
    assert strategy_a_rows and all(line.endswith("| — |") for line in strategy_a_rows)
    manifest_payload = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_payload["schema"] == "00635U_SYNTHETIC_FIXTURE_RUN_V1"
    assert manifest_payload["artifact_classification"] == "SYNTHETIC_TEST_PACK"
    assert manifest_payload["publication_eligible"] is False


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
    assert report.startswith(
        "# 00635U 黃金期貨 ETF 因果回測｜合成測試報告（非正式績效）"
    )
    assert "SYNTHETIC TEST ONLY" in report
    assert "合成測試資料／不是正式績效" in report
    assert "交易筆數刻意縮小，只驗證管線與規則" in report
    assert "不得解讀為 00635U 上市以來的正式結果" in report
    fixture_scope_phrases = (
        "此合成夾具每條策略線只建立 4 個完成回合",
        "4 條策略線 ×（4 次進場（ENTRY）＋4 次出場（EXIT））＝32 列事件",
        "這不是 32 筆獨立交易，也不是 00635U 正式上市歷史交易",
        "每條策略線的 4 個回合全屬低價格區間 MACD 綠翻紅進場、MACD 紅翻綠出場",
        "因此策略 A 與 B 在這份合成夾具中必然相同",
        "這不是正式資料得出的 A／B 相同結論",
        "高價格區間、固定初始防守、淨成本 2R 啟動、KD(9,9,55) 新死叉",
        "未在這 32 列中展示",
    )
    for phrase in fixture_scope_phrases:
        assert phrase in report
    assert "![合成測試資產曲線](equity-chart.svg)" in report
    assert "![正式資產曲線](equity-chart.svg)" not in report
    assert "現金保留與分配再投入兩線在本輪完全重合" in report
    assert "並非未比較" in report
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
        if line_id.startswith(("STRICT_HIGH_", "MACD_ONLY_")):
            assert line_id not in report
        elif line_id != "TAIEX_TOTAL_RETURN_SUPPLEMENT":
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

    conclusion = report.split("## 01｜", 1)[1].split("## 02｜", 1)[0]
    for label in (
        "策略 A（次一交易日開盤成交）",
        "策略 B（次一交易日開盤成交）",
    ):
        assert conclusion.count(label) >= 3
    assert conclusion.count("並列") >= 3
    assert "期末資產較高者為 策略 A" not in conclusion

    assert len(_table_data_rows(
        report, "#### 完整逐年績效表", "![逐年報酬]"
    )) == 70
    assert len(_table_data_rows(
        report, "#### 完整成交時點與成本壓力表", "![成本壓力]"
    )) == 12
    assert len(_table_data_rows(report, "#### 固定期間表", "#### 3／5／10 年滾動摘要表")) == 21
    assert len(_table_data_rows(report, "#### 3／5／10 年滾動摘要表", "#### 高低價格區間表")) == 21
    assert len(_table_data_rows(report, "#### 高低價格區間表", "![滾動窗口]")) == 8
    assert len(_table_data_rows(report, "#### 交易穩定度表", "#### 移除最佳交易敏感度表")) == 4
    assert len(_table_data_rows(report, "#### 移除最佳交易敏感度表", "![逐筆交易淨損益]")) == 16
    user_prose = re.sub(
        r"\[[^\]\n]+\]\([^()\n]+\.(?:svg|csv|json|md)\)", "", report
    )
    for old_phrase in (
        "FORMAL PIPELINE", "regime", " session", " stale", "top1", "top3",
        " PnL", " sizing", "replay", "receipts",
    ):
        assert old_phrase not in user_prose

    readable_trades = (left / formal.TRADE_REPORT_NAME).read_text(encoding="utf-8")
    assert readable_trades.startswith(
        "# 00635U 因果回測｜合成測試逐筆明細（非正式績效）"
    )
    assert "SYNTHETIC TEST ONLY" in readable_trades
    assert "[返回主報告](README.md)" in readable_trades
    assert "交易筆數刻意縮小，只驗證管線與規則" in readable_trades
    for phrase in fixture_scope_phrases:
        assert phrase in readable_trades
    assert "進場來源／出場理由" in readable_trades
    assert any(
        label in readable_trades
        for label in (
            "高價格區間 MA21 新突破（`HIGH_REGIME_MA21_BREAKOUT`）",
            "低價格區間 MACD 綠翻紅（`LOW_REGIME_MACD_GREEN_TO_RED`）",
        )
    )
    assert "MACD 紅翻綠（`MACD_RED_TO_GREEN`）" in readable_trades
    assert not re.search(
        r"\| (?:HIGH_REGIME_MA21_BREAKOUT|LOW_REGIME_MACD_GREEN_TO_RED|"
        r"INITIAL_PROTECTIVE_STOP|MACD_RED_TO_GREEN|KD_2R_DEATH_CROSS_EXIT) \|",
        readable_trades,
    )
    assert not re.search(r"(?<![\d-])-?\d+\.\d{3,}", readable_trades)
    raw_summary = (left / "formal-summary.csv").read_text(encoding="utf-8-sig")
    assert re.search(r"-?\d+\.\d{6,}", raw_summary)

    with (left / "trade-details.csv").open(encoding="utf-8-sig", newline="") as handle:
        trades = list(csv.DictReader(handle))
    assert len(trades) == 32
    line_ids = {row["line_id"] for row in trades}
    assert len(line_ids) == 4
    for line_id in line_ids:
        line_trades = [row for row in trades if row["line_id"] == line_id]
        assert sum(row["role"] == "ENTRY" for row in line_trades) == 4
        assert sum(row["role"] == "EXIT" for row in line_trades) == 4
    assert all(
        row["entry_type"] == "LOW_REGIME_MACD_GREEN_TO_RED"
        for row in trades if row["role"] == "ENTRY"
    )
    assert all(
        row["exit_reason"] == "MACD_RED_TO_GREEN"
        for row in trades if row["role"] == "EXIT"
    )
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

    source_data = formal.load_formal_snapshot(snapshot)
    with (left / "rolling-windows.csv").open(encoding="utf-8-sig", newline="") as handle:
        rolling_rows = [
            row for row in csv.DictReader(handle)
            if row["line_id"] == "STRATEGY_A_SIGNAL_CLOSE"
            and row["years"] == "5" and row["status"] == "OK"
        ]
    with (left / "trade-details.csv").open(encoding="utf-8-sig", newline="") as handle:
        trade_rows_for_ticks = list(csv.DictReader(handle))
    with (left / "formal-summary.csv").open(encoding="utf-8-sig", newline="") as handle:
        summary_rows_for_charts = list(csv.DictReader(handle))
    hold_summary = next(
        row for row in summary_rows_for_charts
        if row["line_id"] == "00635U_BUY_HOLD_CASH"
    )
    hold_chart_label = "00635U 買進持有（現金保留／分配再投入，本輪重合）"
    max_exit_count = max(
        sum(row["line_id"] == line_id and row["role"] == "EXIT" for row in trade_rows_for_ticks)
        for line_id in {row["line_id"] for row in trade_rows_for_ticks}
    )
    expected_x_values = {
        "equity-chart.svg": {
            source_data.indicator_rows[0].day.isoformat(),
            source_data.indicator_rows[-1].day.isoformat(),
        },
        "drawdown-chart.svg": {
            source_data.indicator_rows[0].day.isoformat(),
            source_data.indicator_rows[-1].day.isoformat(),
        },
        "annual-chart.svg": {
            str(source_data.indicator_rows[0].day.year),
            str(source_data.indicator_rows[-1].day.year),
        },
        "rolling-window-chart.svg": {
            f"{rolling_rows[0]['start'][:4]}–{rolling_rows[0]['end'][:4]}",
            f"{rolling_rows[-1]['start'][:4]}–{rolling_rows[-1]['end'][:4]}",
        },
        "regime-chart.svg": {"高", "低"},
        "trade-pnl-chart.svg": {"1", str(max_exit_count)},
        "cost-pressure-chart.svg": {"20", "40", "60"},
        "core-allocation-chart.svg": {"0%", "20%", "40%", "60%"},
        "parameter-grid-chart.svg": {"1", "243"},
    }
    chart_kinds = set()
    chart_hashes = set()
    for chart in left.glob("*-chart.svg"):
        svg = chart.read_text(encoding="utf-8")
        chart_kinds.add(re.search(r'data-chart-kind="([^"]+)"', svg).group(1))
        chart_hashes.add(re.search(r'data-source-sha256="([0-9a-f]{64})"', svg).group(1))
        assert "STRATEGY_" not in svg
        assert "策略 A" in svg or "策略 B" in svg
        x_ticks = re.findall(r'<text data-axis="x-tick"[^>]*>([^<]+)</text>', svg)
        y_ticks = re.findall(r'<text data-axis="y-tick"[^>]*>([^<]+)</text>', svg)
        assert len(set(x_ticks)) >= 2
        assert len(set(y_ticks)) >= 3
        assert expected_x_values[chart.name] <= set(x_ticks)
        if chart.name == "equity-chart.svg":
            match = re.search(
                rf'<polyline data-series-name="{re.escape(hold_chart_label)}" '
                r'data-first-value="[^"]+" data-last-value="([^"]+)"',
                svg,
            )
            assert match is not None
            assert Decimal(match.group(1)) == Decimal(hold_summary["ending_equity"])
        if chart.name == "drawdown-chart.svg":
            match = re.search(
                rf'<polyline data-series-name="{re.escape(hold_chart_label)}" '
                r'data-first-value="[^"]+" data-last-value="[^"]+" '
                r'data-min-value="([^"]+)"',
                svg,
            )
            assert match is not None
            assert Decimal(match.group(1)) == Decimal(hold_summary["max_drawdown"])
        if chart.name in {"equity-chart.svg", "drawdown-chart.svg", "annual-chart.svg"}:
            assert "合成測試" in svg
            assert "正式資產曲線" not in svg
    assert len(chart_kinds) == 9
    assert len(chart_hashes) >= 7

    output_manifest = json.loads((left / "manifest.json").read_text(encoding="utf-8"))
    output_names = {item["path"] for item in output_manifest["files"]}
    assert {formal.REPORT_NAME, formal.TRADE_REPORT_NAME} <= output_names
    assert output_manifest["input_data_classification"] == "SYNTHETIC_TEST_ONLY"
    assert output_manifest["schema"] == "00635U_SYNTHETIC_FIXTURE_RUN_V1"
    assert output_manifest["artifact_classification"] == "SYNTHETIC_TEST_PACK"
    assert output_manifest["publication_eligible"] is False
    assert output_manifest["intended_use"] == "PIPELINE_AND_RULE_VALIDATION_ONLY"
    readback = json.loads((left / "readback.json").read_text(encoding="utf-8"))
    assert readback["stop_code"] == formal.STOP_CODE
    assert readback["stop_code"] == "STOP_WAIT_00_00635U_REPORT_QA"
    assert readback["formal"] is False
    assert readback["artifact_classification"] == "SYNTHETIC_TEST_PACK"
    assert readback["publication_eligible"] is False
