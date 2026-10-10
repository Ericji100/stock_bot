from pathlib import Path

from backtests.dual_ma import etf_0050_final_strategy_v1 as v12
from backtests.dual_ma import etf_0050_final_strategy_v1_4 as report


def test_v14_is_an_isolated_presentation_revision() -> None:
    assert report.VERSION.endswith("v1.4")
    assert report.RUN_ROOT_DEFAULT.name.endswith("v1-4")
    assert report.REPORT_ROOT_DEFAULT.name.endswith("v1-4")
    assert report.SOURCE_RUN.name.endswith("v1-3-r3")
    assert report.SOURCE_REPORT.name.endswith("v1-3-r3")


def test_every_table_of_contents_entry_uses_a_stable_ascii_anchor() -> None:
    source = "\n".join([
        "# 0050 最終策略定版報告｜候選 v1.3 修正版3",
        "",
        "產出時間：old",
        "",
        "候選狀態：等待 00 獨立品質驗收。",
        "",
        "## 閱讀順序",
        "",
        "- [old](#old)",
        "",
        *[
            f"## {heading}\n\n### 測試目的\n\na\n\n### 測試方法\n\nb\n\n### 觀察結果\n\nc\n\n### 白話結論\n\nd\n\n### 對最終決策的影響\n\ne\n"
            for heading, _anchor in report.SECTION_ANCHORS[:-1]
        ],
        "## 完整附件目錄",
        "",
        "- [old.csv](old.csv)",
        "",
        "候選狀態為 `STOP_WAIT_00_QA`，等待 00 獨立品質驗收。",
        "只有 00 明確驗收後才能宣稱正式通過；候選產物不得覆寫 v1.2。",
    ])
    rendered = report._render_report(source, "2026-10-10 00:00:00（台北）", ("report.md",))
    for heading, anchor in report.SECTION_ANCHORS:
        assert f'<a id="{anchor}"></a>' in rendered
        assert f'[{heading}](#{anchor})' in rendered
    for chart in report.CHARTS:
        assert f']({chart})' in rendered
    assert "狀態：`ACCEPT`" in rendered
    assert "候選狀態" not in rendered
    assert "STOP_WAIT_00_QA" not in rendered


def test_chart_inventory_has_one_existing_and_four_new_views() -> None:
    assert report.CHARTS == (
        "main-comparison-equity.svg",
        "main-comparison-drawdown.svg",
        "calendar-year-returns.svg",
        "execution-cost-sensitivity.svg",
        "parameter-robustness.svg",
    )


def test_explicit_anchor_link_validator(tmp_path: Path) -> None:
    (tmp_path / "chart.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        '<a id="performance"></a>\n\n## 完整績效總表\n\n'
        '[績效](#performance)\n\n![圖](chart.svg)\n',
        encoding="utf-8",
    )
    assert report._broken_links(tmp_path) == []


def test_final_comparison_still_has_exactly_five_lines() -> None:
    assert v12.MAIN_IDS == (
        v12.SIGNAL_CLOSE_ID,
        v12.NEXT_OPEN_ID,
        v12.BUY_HOLD_CASH_ID,
        v12.BUY_HOLD_REINVEST_ID,
        v12.BENCHMARK_ID,
    )
