from backtests.dual_ma import etf_0050_final_strategy_v1_6 as report


def _row(timing: str, *, closed: bool) -> dict[str, str]:
    return {
        "execution_timing": timing,
        "status_zh": "已賣出" if closed else "截至截止日仍持有（按收盤價估算）",
        "entry_date": "2026-01-02",
        "entry_fill_price": "100.2",
        "entry_shares": "1000",
        "exit_date": "2026-02-03" if closed else "",
        "exit_reason": "MACD_RED_TO_GREEN" if closed else "",
        "exit_fill_or_mark_price": "110.5",
        "buy_total": "100240",
        "sell_commission": "44",
        "sell_tax": "110",
        "cash_dividends": "500",
        "net_pnl_or_estimate": "10106",
        "return_rate_or_estimate": "0.10082",
        "mfe": "0.15",
        "mae": "-0.03",
        "initial_stop": "95",
    }


def test_v16_is_an_isolated_trade_detail_revision() -> None:
    assert report.VERSION.endswith("v1.6")
    assert report.RUN_ROOT_DEFAULT.name.endswith("v1-6")
    assert report.REPORT_ROOT_DEFAULT.name.endswith("v1-6")
    assert report.SOURCE_RUN.name.endswith("v1-5")
    assert report.SOURCE_REPORT.name.endswith("v1-5")


def test_chinese_trade_detail_contains_both_execution_versions() -> None:
    rendered = report._render_trade_details(
        [
            _row("SIGNAL_CLOSE", closed=True),
            _row("NEXT_SESSION_OPEN", closed=False),
        ],
        "2026-10-10 00:00:00（台北）",
    )
    assert "## 訊號當日收盤成交" in rendered
    assert "## 次一交易日開盤成交" in rendered
    assert "MACD 柱狀值跌破 0 出場" in rendered
    assert "截止日仍持有" in rendered
    assert rendered.count("|1|") == 2


def test_trade_detail_links_are_prominent_in_section_06() -> None:
    source = report.SOURCE_REPORT.joinpath("README.md").read_text(encoding="utf-8")
    rendered = report._render_report(source, "2026-10-10 00:00:00（台北）", ("report.md", "trade-details.md"))
    section = rendered[rendered.index("## 06\n"):rendered.index("## 07\n")]
    assert "### 逐筆交易明細" in section
    for label, target in report.TRADE_LINKS:
        assert f"[{label}]({target})" in section
    assert "# 0050 最終策略定版報告｜正式 v1.6" in rendered
