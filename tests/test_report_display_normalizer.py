from __future__ import annotations

from research_center.command_parser import parse_command_text
from research_center.models import SourceItem
from research_center.report_builder import build_report_json, render_html, summarize_for_telegram, write_report_artifacts
from research_center.report_display_normalizer import display_term, normalize_report_text
from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache


def test_display_terms_translate_common_internal_codes():
    assert display_term("verified") == "已驗證"
    assert display_term("L2_media") == "媒體來源"
    assert display_term("ai_power_semiconductor_mosfet") == "AI 電源與功率半導體 MOSFET"


def test_normalize_report_text_rewrites_key_value_and_snake_case():
    raw = (
        "`ai_power_semiconductor_mosfet`\n"
        "market_validated = false\n"
        "news_stats.news_count_24h = 0\n"
        "source_level=L2_media\n"
        "trend_pullback / active_breakout / verified / missing"
    )

    text = normalize_report_text(raw)

    assert "AI 電源與功率半導體 MOSFET" in text
    assert "盤面尚未明確驗證" in text
    assert "近 24 小時新聞熱度不足" in text
    assert "來源層級：媒體來源" in text
    assert "近期強勢後回檔整理" in text
    assert "放量突破或轉強" in text
    assert "已驗證" in text
    assert "資料缺口" in text
    assert "market_validated = false" not in text


def test_report_artifacts_normalize_markdown_html_summary_but_keep_json_structured_data():
    tmp_path = ensure_test_cache_dir("report_display_normalizer/basic_artifacts")
    request = parse_command_text("/theme_radar --no-json")
    markdown = """# 題材雷達

`ai_power_semiconductor_mosfet`

- market_validated = false
- news_stats.news_count_24h = 0
- trend_pullback
- L2_media
"""
    structured_data = {
        "market_validated": False,
        "theme_id": "ai_power_semiconductor_mosfet",
        "news_stats": {"news_count_24h": 0},
    }

    try:
        artifacts, report_json = write_report_artifacts(
            tmp_path,
            request,
            markdown,
            summarize_for_telegram(markdown),
            [SourceItem("S001", "媒體", "https://example.com/a_b", "L2_media")],
            True,
            None,
            structured_data,
        )

        md_text = artifacts.markdown_path.read_text(encoding="utf-8")
        html_text = artifacts.html_path.read_text(encoding="utf-8")

        assert "AI 電源與功率半導體 MOSFET" in md_text
        assert "盤面尚未明確驗證" in md_text
        assert "近 24 小時新聞熱度不足" in html_text
        assert "trend_pullback" not in md_text
        assert "market_validated = false" not in html_text
        assert structured_data["market_validated"] is False
        assert structured_data["theme_id"] == "ai_power_semiconductor_mosfet"
        assert report_json["sources"][0]["source_level"] == "L2_media"
    finally:
        safe_remove_test_cache("report_display_normalizer/basic_artifacts")


def test_summarize_for_telegram_uses_display_normalizer():
    summary = summarize_for_telegram("# 測試\n\nmarket_validated = false\ntrend_pullback\nL2_media")

    assert "盤面尚未明確驗證" in summary
    assert "媒體來源" in summary
    assert "market_validated" not in summary


def test_html_auxiliary_tabs_use_readable_labels_instead_of_raw_keys():
    request = parse_command_text("/theme_radar --model minimax")
    sources = [
        SourceItem(
            "S001",
            "測試來源",
            "https://example.com/report",
            "L2_media",
            snippet="source_quality_score and trend_pullback should not leak",
            provider="minimax_mcp_search",
            provider_detail="query=台股 題材 輪動; task=熱門題材與資金輪動",
        )
    ]
    structured_data = {
        "analysis_model": "MiniMax",
        "market_movers": {"top_gainers": [{"code": "2330"}], "source_mode": "fresh"},
        "theme_rankings": [{"theme": "AI 電源", "subsector_score": 88}],
        "feature_pack": {"market_validated": False},
        "data_coverage": {"missing_fields": ["financial_validation"]},
        "local_scoring": {
            "scores": [
                {
                    "score_name": "盤面驗證",
                    "score_value": 70,
                    "score_max": 100,
                    "score_reason": "trend_pullback",
                    "deduction_reason": "market_validated=false",
                }
            ]
        },
    }
    report_json = build_report_json(
        request,
        "# 題材雷達\n\nmarket_validated=false\n\n## 完整資料來源清單\n- [S001] 測試來源",
        "summary",
        sources,
        True,
        None,
        structured_data,
    )
    report_json["metadata"]["qa_validation"] = {
        "passed": False,
        "warnings": ["rejected_sources:1"],
        "missing_sections": ["watch_items"],
        "schema_errors": ["source_quality_score"],
    }

    html = render_html(report_json, "# 題材雷達\n\nmarket_validated=false")

    assert "技術附錄" in html
    assert "全市場強弱排行" in html
    assert "題材排行" in html
    assert "MiniMax 網路搜尋" in html
    assert "搜尋詞：台股 題材 輪動" in html
    assert "搜尋任務：熱門題材與資金輪動" in html
    assert "來源層級：媒體來源" in html
    assert "品質分數" in html
    for raw in [
        "market_movers",
        "theme_rankings",
        "feature_pack",
        "source_quality_score",
        "provider_detail",
        "query=",
        "task=",
        "quality=",
        "trend_pullback",
        "market_validated=false",
        "missing_sections",
        "schema_errors",
    ]:
        assert raw not in html


def test_report_artifacts_normalize_programmatic_appendices_for_all_report_commands():
    tmp_path = ensure_test_cache_dir("report_display_normalizer/all_commands")
    commands = [
        "/research 2330",
        "/macro 台股",
        "/theme AI電源",
        "/theme_radar",
        "/theme_flow AI電源",
        "/sector_strength",
        "/value_scan",
    ]
    try:
        for command in commands:
            request = parse_command_text(command)
            artifacts, _report_json = write_report_artifacts(
                tmp_path,
                request,
                "# 測試報告\n\nmarket_validated=false\ntrend_pullback\n\n## 完整資料來源清單\n- [S001] 測試",
                "summary",
                [
                    SourceItem(
                        "S001",
                        "測試",
                        "https://example.com/source",
                        "L3_community",
                        provider="minimax_mcp_search",
                        provider_detail="query=台股 題材; task=測試",
                    )
                ],
                True,
                None,
                {
                    "analysis_model": "test",
                    "market_movers": {"top_gainers": []},
                    "theme_rankings": [],
                    "local_scoring": {"scores": []},
                },
            )
            md_text = artifacts.markdown_path.read_text(encoding="utf-8")
            html_text = artifacts.html_path.read_text(encoding="utf-8")
            assert "market_validated=false" not in md_text
            assert "trend_pullback" not in md_text
            assert "market_movers" not in html_text
            assert "query=" not in html_text
            assert "task=" not in html_text
    finally:
        safe_remove_test_cache("report_display_normalizer/all_commands")
