from backtests.dual_ma import etf_0050_2006_fixed_methods_v1 as accepted
from backtests.dual_ma import etf_0050_2006_seven_methods_complete_report_v1 as subject
from backtests.dual_ma import etf_0050_2006_seven_methods_v1 as runner
from backtests.dual_ma import etf_0050_standard_report_v3 as standard


def _configured_rows():
    root = subject.SOURCE_ROOT
    metrics = subject._read(root / "scenario-summary.csv")
    details = subject._read(root / "trade-details.csv")
    transactions = subject._read(root / "transactions.csv")
    curves = subject._read(root / "daily-equity.csv")
    actions = subject._read(root / "corporate-actions.csv")
    benchmark = subject._read(root / "benchmark.csv")
    labels = {row["strategy_id"]: row["strategy_label"] for row in metrics}
    previous = (standard.YEARS, standard.METHOD_ORDER, standard.TIMING_PREFIXES)
    standard.YEARS, standard.METHOD_ORDER = accepted.YEARS, runner.METHOD_ORDER
    standard.TIMING_PREFIXES = ("A_", "MA21_")
    annual = standard.annual_summaries(curves, transactions, details, actions, benchmark, labels)
    summaries = standard.overall_summaries(metrics, annual)
    standard.YEARS, standard.METHOD_ORDER, standard.TIMING_PREFIXES = previous
    return metrics, details, transactions, curves, actions, annual, summaries


def test_sealed_values_and_all_147_carry_rows_reconcile():
    metrics, details, transactions, curves, actions, annual, _summaries = _configured_rows()
    result = subject._reconcile(
        metrics=metrics, annual=annual, details=details,
        transactions=transactions, curves=curves, actions=actions,
    )
    assert result["status"] == "PASS"
    assert len(annual) == 147
    assert all(item["annual_rows"] == 21 for item in result["strategy_checks"])
    assert all(item["carry_position_matches_last_daily_shares"] for item in result["strategy_checks"])


def test_complete_tables_include_required_fields_and_cutoff_label():
    *_unused, annual, summaries = _configured_rows()
    overall = subject._overall_full(summaries)
    capital = subject._capital_full(summaries)
    yearly = subject._annual_full(annual)
    concentration = subject._concentration(summaries)
    assert all(text in overall for text in ("獲利年度", "虧損年度", "平盤年度", "進場／出場／未平倉", "最大投入"))
    assert all(text in capital for text in ("累計買進", "已實現淨損益", "股息收入", "手續費＋證交稅", "滑價成本"))
    assert "2026（至 9/30）" in yearly
    assert all(text in yearly for text in ("大盤報酬", "大盤最大回落", "年底／截止仍持有"))
    assert "後段占總獲利" in concentration


def test_dynamic_report_lineage_distinguishes_source_replay_from_report_render():
    text = subject._lineage_text(
        dynamic_exit=True,
        source_run_name="02-etf-0050-2006-2026-seven-methods-v1-5",
    )
    assert "兩個 MA21/MA144 分流策略" in text
    assert "正式重播" in text
    assert "渲染階段沒有再次重播策略" in text
    assert "原五組" in text and "PASS_EXACT" in text
    assert "sealed v1-3 並不是兩個新策略數值的來源" in text
