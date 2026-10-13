from backtests.dual_ma import etf_0050_final_strategy_v1_7 as report


def test_v17_is_an_isolated_navigation_revision() -> None:
    assert report.VERSION.endswith("v1.7")
    assert report.RUN_ROOT_DEFAULT.name.endswith("v1-7")
    assert report.REPORT_ROOT_DEFAULT.name.endswith("v1-7")
    assert report.SOURCE_RUN.name.endswith("v1-6")
    assert report.SOURCE_REPORT.name.endswith("v1-6")


def test_trade_details_link_back_to_main_at_top_and_bottom() -> None:
    source = "# 0050 最終策略逐筆交易明細\n\n產出時間：old\n\n內容\n"
    rendered = report._render_trade_details(source, "2026-10-10 00:00:00（台北）")
    assert rendered.count(report.TRADES_TO_MAIN) == 2
    assert rendered.index(report.TRADES_TO_MAIN) < rendered.index("內容")
    assert rendered.rstrip().endswith(report.TRADES_TO_MAIN)


def test_main_report_retains_trade_detail_link() -> None:
    source = report.SOURCE_REPORT.joinpath("README.md").read_text(encoding="utf-8")
    rendered = report._render_main_report(
        source, "2026-10-10 00:00:00（台北）", ("report.md", "trade-details.md")
    )
    assert report.MAIN_TO_TRADES in rendered
    assert "# 0050 最終策略定版報告｜正式 v1.7" in rendered
