from research_center.command_parser import parse_command_text
from research_center.models import SourceItem
from research_center.orchestrator import _source_quality_summary
from research_center.required_data_gap_service import (
    build_required_data_gap_summary,
    build_required_gap_fill_tasks,
)
from research_center.web_fetch_service import _assess_fetch_quality


def test_macro_required_gap_detects_listed_and_otc_markets_separately():
    request = parse_command_text("/macro 台股 --model minimax")
    sources = [
        SourceItem("S001", "VIX 美股波動率", "https://example.com/vix", "Level 2", snippet="VIX 全球風險"),
        SourceItem("S002", "美債殖利率 Fed 美元指數", "https://example.com/rates", "Level 2"),
        SourceItem("S003", "加權指數 TWSE 上市成交量 漲跌家數", "https://www.twse.com.tw/a", "Level 1"),
        SourceItem("S004", "櫃買指數 TPEx 上櫃成交量 漲跌家數", "https://www.tpex.org.tw/a", "Level 1"),
        SourceItem("S005", "三大法人 外資 投信 自營商 買賣超", "https://example.com/inst", "Level 2"),
        SourceItem("S006", "TAIFEX 台指期 台指選擇權 Put Call 未平倉", "https://example.com/taifex", "Level 1"),
        SourceItem("S007", "油價 黃金 關稅 地緣政治", "https://example.com/geo", "Level 2"),
    ]

    summary = build_required_data_gap_summary(request, sources, {})

    missing_fields = {item["field"] for item in summary["missing"]}
    assert "twse_market" not in missing_fields
    assert "tpex_market" not in missing_fields
    assert not summary["hard_missing"]


def test_macro_required_gap_creates_focused_backfill_tasks():
    request = parse_command_text("/macro 台股 --model minimax")
    sources = [
        SourceItem("S001", "VIX 美股波動率", "https://example.com/vix", "Level 2", snippet="VIX 全球風險"),
    ]

    summary = build_required_data_gap_summary(request, sources, {})
    tasks = build_required_gap_fill_tasks(request, summary)
    query_text = "\n".join(query for task in tasks for query in task["queries"])

    assert summary["backfill_recommended"] is True
    assert "櫃買指數" in query_text
    assert "台指選擇權" in query_text
    assert all(task["label"].startswith("required_gap:") for task in tasks)


def test_required_gap_summary_triggers_gemini_fallback_reason():
    request = parse_command_text("/macro 台股 --model minimax")
    sources = [SourceItem("S001", "VIX", "https://example.com/vix", "Level 2", snippet="VIX")]
    gap_summary = build_required_data_gap_summary(request, sources, {})

    quality = _source_quality_summary(sources, request, {"required_data_gap_summary": gap_summary})

    assert "required_hard_data_gap" in quality["fallback_reasons"]


def test_research_required_gap_covers_official_revenue_financial_and_risk():
    request = parse_command_text("/research 2330 --deep --model minimax")
    sources = [
        SourceItem("S001", "公開資訊觀測站 重大訊息", "https://mops.twse.com.tw/a", "Level 1"),
        SourceItem("S002", "台積電 月營收 年增率", "https://example.com/revenue", "Level 2"),
        SourceItem("S003", "台積電 財報 EPS 毛利率", "https://example.com/financials", "Level 2"),
        SourceItem("S004", "台積電 庫存 風險 毛利 下滑", "https://example.com/risk", "Level 2"),
    ]

    summary = build_required_data_gap_summary(request, sources, {})

    missing_fields = {item["field"] for item in summary["missing"]}
    assert "official_mops" not in missing_fields
    assert "monthly_revenue" not in missing_fields
    assert "financial_report" not in missing_fields
    assert "counter_risk" not in missing_fields


def test_web_fetch_quality_flags_non_article_and_keyword_mismatch():
    quality, reason = _assess_fetch_quality(
        "https://www.youtube.com/watch?v=abc",
        "短內容",
        "影片",
        None,
        ["台積電"],
    )

    assert quality == "low"
    assert "non_article_page" in (reason or "")
    assert "no_keyword_match" in (reason or "")


def test_web_fetch_quality_accepts_article_like_matching_content():
    content = "\n".join([
        "台積電今日公布法說會重點，市場關注先進製程與資本支出。",
        "公司說明 AI 需求仍然強勁，並提到供應鏈與客戶拉貨狀況。",
        "法人認為毛利率與匯率仍是後續觀察重點。",
    ] * 8)

    quality, reason = _assess_fetch_quality(
        "https://news.example.com/article/123",
        content,
        "台積電法說會",
        "2026-06-19",
        ["台積電"],
    )

    assert quality == "high"
    assert reason is None
