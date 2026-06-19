from __future__ import annotations

import unittest

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


def test_normalize_report_text_rewrites_internal_evidence_paths():
    raw = (
        "除息資訊來自 [S023][unified_evidence_pack.news[0]]。\n"
        "source_id=S001，source_ids: [S001, S002]"
    )

    text = normalize_report_text(raw)

    assert "共用證據包第 1 筆" in text
    assert "來源編號：S001" in text
    assert "來源編號：" in text
    for token in ["unified_evidence_pack", "source_id=", "source_ids:"]:
        assert token not in text


def test_normalize_sector_strength_internal_labels_to_chinese():
    raw = (
        "| strong stocks | direct，但 stock codes 為空 |\n"
        "| local scoring | scores=[]、buy rating=null |\n"
        "report confidence v1 score=81，sector strong samples 僅作觀察。"
    )

    text = normalize_report_text(raw)

    assert "強勢股主表" in text
    assert "直接取得" in text
    assert "股票代號" in text
    assert "本地評分" in text
    assert "評分明細" in text
    assert "買賣評等" in text
    assert "報告信賴度" in text
    assert "族群強勢樣本" in text
    for raw_token in ["strong stocks", "stock codes", "local scoring", "scores", "buy rating", "report confidence v1", "sector strong samples"]:
        assert raw_token not in text


class ThemeRadarDisplayNormalizerTests(unittest.TestCase):
    def test_normalize_theme_radar_internal_codes_to_readable_chinese(self):
        raw = (
            "market theme radar\n"
            "market sector movers / subsector strength\n"
            "| state | V / C / I | theme_id | financial validation coverage |\n"
            "Tier A：V 8 / C 8 / I 2，V 占比偏低，C ≥ V × 2。\n"
            "代表股清單為 <list truncated>，financial validation coverage = 0%`。\n"
            "trend score、volume surge、sector score 都需要重新檢查。\n"
            "24h / 7d / A/B/C / A/B / 強勢題材 vs 弱勢題材"
        )

        text = normalize_report_text(raw)

        self.assertIn("全市場題材雷達", text)
        self.assertIn("全市場族群異動", text)
        self.assertIn("子族群強弱", text)
        self.assertIn("狀態", text)
        self.assertIn("已驗證／候選／推論", text)
        self.assertIn("題材代碼", text)
        self.assertIn("財務驗證覆蓋率", text)
        self.assertIn("A級", text)
        self.assertIn("已驗證 8 / 候選 8 / 推論 2", text)
        self.assertIn("已驗證占比", text)
        self.assertIn("候選數量至少是已驗證的兩倍", text)
        self.assertIn("清單已精簡，完整明細保存在 JSON 附錄", text)
        self.assertIn("近24小時", text)
        self.assertIn("近7日", text)
        self.assertIn("A級／B級／C級", text)
        self.assertIn("A級／B級", text)
        self.assertIn("強勢題材 相對於 弱勢題材", text)
        self.assertNotIn("覆蓋率 = 0%", text)
        for raw_token in ["market sector movers", "subsector strength", "V / C / I", "<list truncated>", "financial validation coverage"]:
            self.assertNotIn(raw_token, text)

    def test_html_main_report_normalizes_theme_radar_title_and_h4(self):
        request = parse_command_text("/theme_radar --model minimax")
        report_json = build_report_json(
            request,
            "# market theme radar\n\n#### Tier A 子族群\n\n| state | V / C / I |\n|---|---|\n| active_breakout | V 3 / C 2 / I 1 |",
            "summary",
            [],
            True,
            None,
            {"analysis_model": "MiniMax"},
        )

        html = render_html(
            report_json,
            "# market theme radar\n\n#### Tier A 子族群\n\n| state | V / C / I |\n|---|---|\n| active_breakout | V 3 / C 2 / I 1 |",
        )

        self.assertIn("全市場題材雷達報告", html)
        self.assertIn('<h4 class="report-subsection-title">A級 子族群</h4>', html)
        self.assertIn("狀態", html)
        self.assertIn("已驗證／候選／推論", html)
        self.assertIn("已驗證 3 / 候選 2 / 推論 1", html)
        for raw in ["market theme radar", "####", "V / C / I", "active_breakout"]:
            self.assertNotIn(raw, html)

    def test_normalize_theme_radar_relation_phrases(self):
        raw = (
            "C / (V+C)\n"
            "為 B 級中 V 數最高\n"
            "無高 V 主題對應\n"
            "V 仍須以 L1 補強\n"
            "候選股（C）顯著多於已驗證股（V）\n"
            "待補 L1 證據，L1 級驗證與 L2 級市場訊號，[L1 official 證交所]"
        )

        text = normalize_report_text(raw)

        self.assertIn("候選占比", text)
        self.assertIn("B級中已驗證數量最高", text)
        self.assertIn("沒有高已驗證度題材對應", text)
        self.assertIn("已驗證關聯仍須以官方或一級來源補強", text)
        self.assertIn("候選股顯著多於已驗證股", text)
        self.assertIn("待補官方一級來源證據", text)
        self.assertIn("官方一級來源驗證", text)
        self.assertIn("媒體或市場二級來源市場訊號", text)
        self.assertIn("[官方來源 證交所]", text)
        for raw_token in ["C / (V+C)", "V 數", "高 V", "V 仍須以 L1", "候選股（C）", "已驗證股（V）", "L1 official"]:
            self.assertNotIn(raw_token, text)


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
    report_json["metadata"]["ai_input_audit"] = {
        "context_size": {"prompt_context_chars": 1234},
        "ai_received": {"selected_source_count": 2},
        "ai_not_received_directly": {"omitted_source_count": 3, "omitted_reason_counts": {"oversized": 3}},
        "source_coverage": {"official_sources": 1, "media_sources": 1, "risk_or_counter_sources": 1, "dated_sources": 2},
        "structured_coverage": {
            "available_sections": ["sources", "theme"],
            "missing_sections": ["matched_companies", "topic_context", "supply_chain_profile", "sector_rankings"],
        },
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
    assert "高階模型已直接收到的資料類型" in html
    assert "高階模型未直接收到的資料類型" in html
    assert "完整資料仍在 JSON / HTML 附錄" in html
    assert "命中公司" in html
    assert "題材脈絡" in html
    assert "供應鏈輪廓" in html
    assert "族群排行" in html
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
        "缺少資料類型",
        "matched_companies",
        "topic_context",
        "supply_chain_profile",
        "missing_sections",
        "schema_errors",
    ]:
        assert raw not in html


def test_html_ai_audit_shows_not_required_core_sections_in_chinese():
    request = parse_command_text("/theme 功率半導體 --model gemini")
    report_json = build_report_json(
        request,
        "# 功率半導體題材研究報告",
        "summary",
        [],
        True,
        None,
        {
            "analysis_model": "gemini",
            "high_model_input_package": {
                "input_mode": "balanced",
                "command_specific_data": {
                    "core_input_audit": {
                        "sections": [
                            {"section": "theme", "status": "direct", "raw_count": 1, "sent_count": 1, "note": "已送入"},
                            {"section": "theme_rankings", "status": "not_required", "raw_count": 0, "sent_count": 0, "note": "本指令不是全市場排行型任務，這類資料本次不需要。"},
                        ],
                    }
                },
            },
            "ai_input_audit": {
                "context_size": {"prompt_context_chars": 100},
                "ai_received": {"selected_source_count": 0},
                "ai_not_received_directly": {"omitted_source_count": 0, "omitted_reason_counts": {}},
                "source_coverage": {},
                "structured_coverage": {},
            },
        },
    )

    html = render_html(report_json, "# 功率半導體題材研究報告")

    assert "題材排行" in html
    assert "本指令不需要" in html




def test_html_ai_audit_shows_macro_source_missing_in_chinese():
    request = parse_command_text("/macro 台股 --model gemini")
    report_json = build_report_json(
        request,
        "# 台股宏觀\n\n## 摘要\n測試",
        "summary",
        [],
        True,
        None,
        {
            "analysis_model": "gemini",
            "high_model_input_package": {
                "input_mode": "balanced",
                "command_specific_data": {
                    "core_input_audit": {
                        "sections": [
                            {"section": "volatility", "status": "direct", "raw_count": 2, "sent_count": 2, "note": "ok"},
                            {"section": "global_public_macro", "status": "source_missing", "raw_count": 0, "sent_count": 0, "note": "資料源不足或本次未取得。"},
                        ],
                    }
                },
            },
            "ai_input_audit": {
                "context_size": {"prompt_context_chars": 100},
                "ai_received": {"selected_source_count": 0},
                "ai_not_received_directly": {"omitted_source_count": 0, "omitted_reason_counts": {}},
                "source_coverage": {},
                "structured_coverage": {},
            },
        },
    )

    html = render_html(report_json, "# 台股宏觀\n\n## 摘要\n測試")

    assert "全球公開總經資料" in html
    assert "資料源不足" in html
    assert "global_public_macro" not in html
    assert "source_missing" not in html


def test_html_low_model_429_failure_is_explained_in_chinese():
    request = parse_command_text("/macro 台股 --model gemini")
    report_json = build_report_json(
        request,
        "# 台股宏觀報告\n\n## 摘要\n測試",
        "summary",
        [],
        True,
        None,
        {
            "analysis_model": "gemini",
            "low_model_model": "MiniMax-M3",
            "low_model_digest": {
                "status": "failed",
                "error": "MiniMax API request failed; status=429 weekly usage limit",
                "prompt_chars": 95187,
                "estimated_tokens": 23796,
                "source_count": 17,
            },
        },
    )

    html = render_html(report_json, "# 台股宏觀報告\n\n## 摘要\n測試")

    assert "MiniMax M3 資料整理底稿" in html
    assert "額度或速率限制" in html
    assert "本地 AI 資料中心與高階模型繼續產出報告" in html


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
