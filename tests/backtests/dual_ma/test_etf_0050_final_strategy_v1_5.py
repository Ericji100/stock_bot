from pathlib import Path

from backtests.dual_ma import etf_0050_final_strategy_v1_5 as report


def _source_report() -> str:
    sections = []
    for _number, heading in report.SECTION_NAV[:-1]:
        sections.append(
            f"## {heading}\n\n"
            "### 測試目的\n\na\n\n"
            "### 測試方法\n\nb\n\n"
            "### 觀察結果\n\nc\n\n"
            "### 白話結論\n\nd\n\n"
            "### 對最終決策的影響\n\ne\n"
        )
    return "\n".join(
        [
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
            *sections,
            "## 完整附件目錄",
            "",
            "- [old.csv](old.csv)",
            "",
            "候選狀態為 `STOP_WAIT_00_QA`，等待 00 獨立品質驗收。",
            "只有 00 明確驗收後才能宣稱正式通過；候選產物不得覆寫 v1.2。",
        ]
    )


def test_v15_is_an_isolated_presentation_revision() -> None:
    assert report.VERSION.endswith("v1.5")
    assert report.RUN_ROOT_DEFAULT.name.endswith("v1-5")
    assert report.REPORT_ROOT_DEFAULT.name.endswith("v1-5")
    assert report.SOURCE_RUN.name.endswith("v1-3-r3")
    assert report.SOURCE_REPORT.name.endswith("v1-3-r3")


def test_navigation_is_pure_markdown_without_raw_html() -> None:
    rendered = report._render_report(
        _source_report(), "2026-10-10 00:00:00（台北）", ("report.md",)
    )
    assert '<a id="' not in rendered
    for number, heading in report.SECTION_NAV:
        assert f"[{number}｜{heading}](#{number})" in rendered
        assert f"## {number}\n\n### {heading}" in rendered
    assert report._contract_errors(rendered) == []


def test_numeric_markdown_anchor_link_validator(tmp_path: Path) -> None:
    (tmp_path / "chart.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "## 02\n\n### 完整績效總表\n\n"
        "[績效](#02)\n\n![圖](chart.svg)\n",
        encoding="utf-8",
    )
    assert report._broken_links(tmp_path) == []


def test_chart_inventory_contains_five_views() -> None:
    assert report.CHARTS == (
        "main-comparison-equity.svg",
        "main-comparison-drawdown.svg",
        "calendar-year-returns.svg",
        "execution-cost-sensitivity.svg",
        "parameter-robustness.svg",
    )


def test_chart_language_validator_rejects_internal_english_ids(tmp_path: Path) -> None:
    for name in report.CHARTS:
        (tmp_path / name).write_text("<svg><text>中文圖表</text></svg>", encoding="utf-8")
    assert report._chart_language_errors(tmp_path) == []
    (tmp_path / report.CHARTS[0]).write_text(
        "<svg><text>FINAL_SIGNAL_CLOSE_20BPS</text></svg>", encoding="utf-8"
    )
    assert report._chart_language_errors(tmp_path)
