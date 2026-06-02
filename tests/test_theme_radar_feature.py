from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

from research_center.command_parser import parse_command_text
from research_center.config import ROOT_DIR
from research_center.data_services import collect_structured_data
from research_center.prompt_registry import build_prompt_from_request
from research_center.theme_radar_service import (
    _attach_theme_matches,
    _build_theme_rankings,
    build_sector_strength_data,
    build_theme_flow_data,
    collect_theme_radar_data,
)


def _stock(code: str, name: str, industry: str) -> SimpleNamespace:
    return SimpleNamespace(code=code, symbol=f"{code}.TW", name=name, industry=industry)


def _library() -> dict:
    return {
        "profiles": [
            {
                "theme_id": "ai_server",
                "theme_name": "AI伺服器",
                "keywords": ["AI", "伺服器"],
                "industries": ["半導體", "電源"],
                "risk_notes": ["供應鏈漲幅擴散後要驗證訂單與毛利"],
            },
            {
                "theme_id": "power",
                "theme_name": "電源與功率半導體",
                "keywords": ["電源", "功率"],
                "industries": ["電源"],
                "risk_notes": ["注意報價與庫存循環"],
            },
        ],
        "profile_by_id": {},
        "company_theme_map": {
            "2330": {"primary_theme": "ai_server", "themes": ["ai_server"]},
            "3661": {"primary_theme": "ai_server", "themes": ["ai_server"]},
            "6415": {"primary_theme": "power", "themes": ["power", "ai_server"]},
        },
        "supply_chain_nodes": [
            {"node_id": "ai_server_chip", "theme_id": "ai_server", "company_code": "2330", "role": "晶片"},
            {"node_id": "ai_server_memory", "theme_id": "ai_server", "company_code": "3661", "role": "記憶體"},
            {"node_id": "ai_server_power", "theme_id": "ai_server", "company_code": "6415", "role": "電源"},
        ],
        "legacy_theme_references": {},
    }


def _prepared_library() -> dict:
    library = _library()
    library["profile_by_id"] = {item["theme_id"]: item for item in library["profiles"]}
    return library


def _metrics() -> dict:
    return {
        "2330.TW": {"price": 900, "previous_close": 870, "volume": 90000, "avg_volume_20d": 60000, "new_high_days": 20, "price_date": "2026-05-22"},
        "3661.TW": {"price": 120, "previous_close": 110, "volume": 50000, "avg_volume_20d": 22000, "price_date": "2026-05-22"},
        "6415.TW": {"price": 560, "previous_close": 540, "volume": 25000, "avg_volume_20d": 15000, "price_date": "2026-05-22"},
    }


def test_theme_radar_collects_rankings_flow_and_sector_strength(monkeypatch):
    stocks = [
        _stock("2330", "台積電", "半導體"),
        _stock("3661", "世芯-KY", "半導體"),
        _stock("6415", "矽力-KY", "電源"),
    ]
    monkeypatch.setattr("research_center.theme_radar_service.load_stock_universe", lambda _: stocks)
    monkeypatch.setattr("research_center.theme_radar_service._safe_price_metrics", lambda universe: _metrics())
    monkeypatch.setattr("research_center.theme_radar_service.load_recent_scan_results", lambda limit=30: [{"scan_type": "Radar", "selected_codes": []}])
    monkeypatch.setattr("research_center.theme_radar_service._load_topic_library", _prepared_library)
    monkeypatch.setattr(
        "research_center.theme_radar_service._build_news_theme_stats",
        lambda library, lookback_days: [
            {
                "theme_id": "ai_server",
                "theme_name": "AI伺服器",
                "news_count_24h": 2,
                "news_count_7d": 5,
                "matched_keywords": ["AI", "伺服器"],
                "mentioned_stocks": ["2330", "3661"],
                "trend_direction": "rising",
            }
        ],
    )

    data = collect_theme_radar_data(date(2026, 5, 22), lookback_days=7)

    assert data["command_role"] == "market_theme_radar"
    assert data["theme_rankings"][0]["theme_name"] == "AI伺服器"
    assert data["theme_flow_summaries"][0]["theme"]["theme_id"] == "ai_server"
    assert data["sector_strength"]["sector_rankings"]
    assert data["market_movers"]["top_gainers"]
    assert data["strong_stock_policy"]["status"] == "market_movers"
    assert "不套用 /scan" in data["market_movers"]["hard_filter_policy"]
    assert data["data_quality"]["theme_mapped_stock_rows"] == 3


def test_theme_flow_returns_extension_layers_and_candidates():
    library = _prepared_library()
    rows = [
        {
            "code": "2330",
            "name": "台積電",
            "industry": "半導體",
            "change_pct": 3.2,
            "volume_ratio": 1.6,
            "avg_volume_20d": 60000,
            "price_date": "2026-05-22",
            "theme_matches": [{"theme_id": "ai_server", "theme_name": "AI伺服器", "supply_chain_role": "晶片"}],
        },
        {
            "code": "6415",
            "name": "矽力-KY",
            "industry": "電源",
            "change_pct": 0.2,
            "avg_volume_20d": 15000,
            "price_date": "2026-05-22",
            "theme_matches": [{"theme_id": "ai_server", "theme_name": "AI伺服器", "supply_chain_role": "電源"}],
        },
    ]

    data = build_theme_flow_data(
        "AI伺服器",
        date(2026, 5, 22),
        preloaded={
            "topic_library": library,
            "stock_rows": rows,
            "news_stats": [],
            "market_movers": {
                "market_data_date": "2026-05-22",
            },
        },
    )

    assert data["theme"]["theme_id"] == "ai_server"
    assert data["related_stock_count"] == 2
    assert len(data["layers"]) == 4
    assert "next_layer_candidates" in data
    assert data["layer_market_validation"][0]["status"] == "盤面已驗證"
    assert data["layer_market_validation"][0]["market_validated"] is True


def test_sector_strength_ranks_by_breadth_and_theme_hits():
    rows = [
        {"code": "2330", "industry": "半導體", "avg_volume_20d": 60000, "theme_matches": [{"theme_id": "ai_server", "verification_status": "verified"}]},
        {"code": "3661", "industry": "半導體", "avg_volume_20d": 22000, "theme_matches": [{"theme_id": "ai_server", "verification_status": "candidate"}]},
        {"code": "6415", "industry": "電源", "avg_volume_20d": 15000, "theme_matches": [{"theme_id": "power"}]},
    ]

    data = build_sector_strength_data(date(2026, 5, 22), strong_rows=rows, price_metrics={})

    assert data["command_role"] == "sector_strength"
    assert data["sector_rankings"][0]["sector"] == "半導體業"
    assert data["sector_rankings"][0]["strong_stock_count"] == 2
    assert "market_movers_data_quality" in data["data_quality"]


def test_sector_strength_uses_market_movers_not_recent_scan_cache(monkeypatch):
    stocks = [
        _stock("1111", "低價漲幅股", "電子零組件"),
        _stock("2222", "量增股", "電子零組件"),
        _stock("3333", "下跌股", "航運"),
    ]
    metrics = {
        "1111.TW": {"price": 4.0, "previous_close": 3.5, "volume": 20000, "avg_volume_20d": 2000},
        "2222.TW": {"price": 20.0, "previous_close": 19.5, "volume": 12000, "avg_volume_20d": 3000},
        "3333.TW": {"price": 50.0, "previous_close": 55.0, "volume": 5000, "avg_volume_20d": 4500},
    }
    monkeypatch.setattr("research_center.theme_radar_service.load_stock_universe", lambda _: stocks)
    monkeypatch.setattr("research_center.theme_radar_service._safe_price_metrics", lambda universe: metrics)
    monkeypatch.setattr("research_center.theme_radar_service.load_recent_scan_results", lambda limit=30: [{"scan_type": "Radar", "selected_codes": ["3333"]}])

    data = build_sector_strength_data(date(2026, 5, 22))

    assert data["market_movers"]["top_gainers"][0]["code"] == "1111"
    assert data["sector_rankings"][0]["sector"] == "電子零組件業"
    assert "不套用 /scan" in data["market_movers"]["hard_filter_policy"]


def test_sector_strength_exposes_subsector_rankings_for_passive_components():
    rows = [
        {"code": "2327", "name": "國巨", "industry": "電子零組件業", "change_pct": 9.5, "volume_ratio": 2.2, "avg_volume_20d": 18000, "new_high_days": 20, "theme_matches": []},
        {"code": "2492", "name": "華新科", "industry": "電子零組件業", "change_pct": 8.0, "volume_ratio": 1.9, "avg_volume_20d": 9000, "new_high_days": 10, "theme_matches": []},
        {"code": "1605", "name": "華新", "industry": "電器電纜", "change_pct": 6.0, "volume_ratio": 1.8, "avg_volume_20d": 12000, "new_high_days": 5, "theme_matches": []},
    ]

    data = build_sector_strength_data(date(2026, 5, 29), strong_rows=rows, price_metrics={})

    subsectors = {(row["sector"], row["subsector"]) for row in data["subsector_rankings"]}
    assert ("電子零組件業", "被動元件") in subsectors
    assert ("電器電纜", "電線電纜") in subsectors
    assert data["sector_rankings"][0]["top_subsectors"]


def test_sector_strength_marks_recent_strong_single_day_pullback_as_pullback():
    rows = [
        {
            "code": "2327",
            "name": "國巨",
            "industry": "電子零組件業",
            "change_pct": -0.54,
            "change_pct_5d": 8.0,
            "change_pct_10d": 16.0,
            "change_pct_20d": 31.0,
            "trend_score": 62.0,
            "trend_state": "trend_pullback",
            "trend_summary": "今日回檔，但近期趨勢仍強或接近高點",
            "near_high_20d": True,
            "volume_ratio": 0.5,
            "avg_volume_20d": 55170,
            "new_high_days": None,
            "theme_matches": [{"theme_id": "passive_components", "verification_status": "inferred"}],
        }
    ]

    data = build_sector_strength_data(date(2026, 5, 29), strong_rows=rows, price_metrics={})
    sector = data["sector_rankings"][0]
    subsector = data["subsector_rankings"][0]

    assert sector["sector_state"] == "trend_pullback"
    assert sector["trend_pullback_count"] == 1
    assert subsector["subsector_state"] == "trend_pullback"
    assert subsector["trend_pullback_count"] == 1


def test_theme_rankings_keep_pullback_lifecycle_when_recent_trend_is_strong():
    rows = [
        {
            "code": "2327",
            "name": "國巨",
            "industry": "電子零組件業",
            "change_pct": -0.54,
            "trend_score": 62.0,
            "trend_state": "trend_pullback",
            "avg_volume_20d": 55170,
            "theme_matches": [
                {
                    "theme_id": "passive_components",
                    "theme_name": "被動元件漲價與庫存回補",
                    "match_method": "direct_map",
                    "verification_status": "inferred",
                    "supply_chain_role": "被動元件供應商",
                }
            ],
        }
    ]

    rankings = _build_theme_rankings(
        rows,
        {"profile_by_id": {"passive_components": {"risk_notes": []}}},
        [{"theme_id": "passive_components", "news_count_7d": 20, "news_count_24h": 4, "trend_direction": "rising"}],
    )

    assert rankings[0]["theme_state"] == "trend_pullback"
    assert rankings[0]["lifecycle"] == "強勢後整理"
    assert rankings[0]["trend_pullback_count"] == 1


def test_sector_strength_keeps_strong_samples_out_of_representatives():
    rows = [
        {"code": "2330", "industry": "半導體", "avg_volume_20d": 60000, "theme_matches": [{"theme_id": "ai_server", "verification_status": "verified"}]},
        {"code": "3661", "industry": "半導體", "avg_volume_20d": 50000, "theme_matches": [{"theme_id": "ai_server", "verification_status": "inferred"}]},
        {"code": "2436", "industry": "半導體", "avg_volume_20d": 40000, "theme_matches": [{"theme_id": "memory_recovery", "verification_status": "candidate"}]},
        {"code": "3588", "industry": "半導體", "avg_volume_20d": 30000, "theme_matches": []},
    ]

    data = build_sector_strength_data(date(2026, 5, 22), strong_rows=rows, price_metrics={})
    sector = data["sector_rankings"][0]

    assert [row["code"] for row in sector["sector_strong_samples"]] == ["2330", "3661", "2436", "3588"]
    assert [row["code"] for row in sector["representative_stocks"]] == ["2330", "3661"]
    assert [row["code"] for row in sector["candidate_stocks"]] == ["2436"]
    assert sector["theme_relation_status_counts"] == {"verified": 1, "inferred": 1, "candidate": 1, "missing": 1}
    assert sector["display_stock_groups"]["sector_sample_label"].startswith("類股強勢樣本")
    assert "不得稱為代表股" in sector["display_stock_groups"]["candidate_label"]
    assert sector["representative_policy"].startswith("sector_strong_samples are price/volume strong")


def test_sector_strength_prompt_has_candidate_naming_rules():
    prompt_text = (ROOT_DIR / "prompt" / "report" / "sector_strength.md").read_text(encoding="utf-8")

    assert "類股強勢樣本" in prompt_text
    assert "不得稱為代表股" in prompt_text
    assert "candidate 代表股" in prompt_text
    assert "market_movers" in prompt_text
    assert "不得以 `/scan`、`/radar`" in prompt_text


def test_theme_commands_parse_collect_and_build_prompt(monkeypatch):
    monkeypatch.setattr(
        "research_center.data_services.collect_theme_radar_data",
        lambda *args, **kwargs: {
            "command_role": "market_theme_radar",
            "report_date": "2026-05-22",
            "theme_rankings": [{"theme_id": "ai_server", "theme_name": "AI伺服器", "theme_score": 88}],
            "theme_flow_summaries": [],
            "sector_strength": {"sector_rankings": []},
            "market_movers": {"top_gainers": [], "data_quality": {}},
            "news_theme_stats": [],
            "data_quality": {},
            "analysis_policy": {"no_trading_advice": True},
        },
    )
    request = parse_command_text("/theme_radar --days 7 --source radar --model minimax")

    structured, sources = collect_structured_data(request)
    prompt = build_prompt_from_request(request, structured, sources)

    assert request.command == "theme_radar"
    assert request.lookback_days == 7
    assert request.source == "radar"
    assert "市場題材雷達" in prompt
    assert "theme_rankings" in prompt
    assert "不得輸出買進" in prompt


def test_theme_flow_and_sector_strength_commands_parse_and_register():
    flow = parse_command_text("/theme_flow AI伺服器 --days 20")
    sector = parse_command_text("/sector_strength --source radar")

    assert flow.command == "theme_flow"
    assert flow.target == "AI伺服器"
    assert flow.lookback_days == 20
    assert sector.command == "sector_strength"
    assert sector.source == "radar"

    from research_center.telegram_handlers import build_research_handlers

    handlers = build_research_handlers(lambda *args, **kwargs: None, lambda *args, **kwargs: None, None, lambda _name, fn: fn)
    assert {"theme_radar", "theme_flow", "sector_strength"}.issubset(handlers)


def test_theme_radar_uses_enriched_topic_library_fields(monkeypatch):
    stocks = [_stock("2308", "台達電", "電源")]
    library = _prepared_library()
    library["company_theme_map"] = {
        "2308": {
            "company_name": "台達電",
            "themes": ["power"],
            "primary_theme": "power",
            "relation_strength": "high",
            "relation_type": "direct",
            "products": ["BBU"],
            "customers": ["CSP"],
            "revenue_exposure": {"level": "high", "description": "AI power"},
            "benefit_logic": "AI rack power demand",
            "evidence": [{"source": "IR", "content": "power"}],
        }
    }
    library["supply_chain_nodes"] = [
        {
            "node_id": "power_2308_power",
            "theme_id": "power",
            "company_code": "2308",
            "company_name": "台達電",
            "layer": 3,
            "role": "電源",
            "product_keywords": ["power supply"],
            "customers": ["CSP"],
        }
    ]
    monkeypatch.setattr("research_center.theme_radar_service.load_stock_universe", lambda _: stocks)
    monkeypatch.setattr("research_center.theme_radar_service._safe_price_metrics", lambda universe: {"2308.TW": {"price": 300, "avg_volume_20d": 30000}})
    monkeypatch.setattr("research_center.theme_radar_service.load_recent_scan_results", lambda limit=30: [{"scan_type": "Radar", "selected_codes": ["2308"]}])
    monkeypatch.setattr("research_center.theme_radar_service._load_topic_library", lambda: library)
    monkeypatch.setattr("research_center.theme_radar_service._build_news_theme_stats", lambda library, lookback_days: [])

    data = collect_theme_radar_data(date(2026, 5, 22), lookback_days=7)

    match = data["strong_stocks"][0]["theme_matches"][0]
    assert match["relation_type"] == "direct"
    assert match["verification_status"] == "inferred"
    assert match["revenue_exposure"]["level"] == "high"
    assert match["benefit_logic"] == "AI rack power demand"
    assert match["evidence"][0]["source"] == "IR"
    assert data["data_quality"]["company_relation_evidence_coverage_pct"] == 100.0
    assert data["data_quality"]["relation_status_counts"]["inferred"] == 1


def test_theme_radar_keyword_match_is_candidate(monkeypatch):
    stocks = [_stock("1111", "AI Keyword Co", "?餅?")]
    library = _prepared_library()
    library["company_theme_map"] = {}
    library["supply_chain_nodes"] = []
    monkeypatch.setattr("research_center.theme_radar_service.load_stock_universe", lambda _: stocks)
    monkeypatch.setattr("research_center.theme_radar_service._safe_price_metrics", lambda universe: {"1111.TW": {"price": 30, "avg_volume_20d": 5000}})
    monkeypatch.setattr("research_center.theme_radar_service.load_recent_scan_results", lambda limit=30: [{"scan_type": "Radar", "selected_codes": ["1111"]}])
    monkeypatch.setattr("research_center.theme_radar_service._load_topic_library", lambda: library)
    monkeypatch.setattr("research_center.theme_radar_service._build_news_theme_stats", lambda library, lookback_days: [])

    data = collect_theme_radar_data(date(2026, 5, 22), lookback_days=7)

    match = data["strong_stocks"][0]["theme_matches"][0]
    assert match["match_method"] == "keyword_or_industry"
    assert match["verification_status"] == "candidate"
    assert data["analysis_policy"]["keyword_or_industry_match_is_candidate_only"] is True


def test_memory_theme_does_not_match_broad_semiconductor_industry():
    library = {
        "profiles": [
            {
                "theme_id": "memory_recovery",
                "theme_name": "記憶體景氣復甦",
                "keywords": ["DRAM", "NAND Flash", "記憶體模組"],
                "industries": ["半導體業"],
            }
        ],
        "profile_by_id": {
            "memory_recovery": {
                "theme_id": "memory_recovery",
                "theme_name": "記憶體景氣復甦",
                "keywords": ["DRAM", "NAND Flash", "記憶體模組"],
                "industries": ["半導體業"],
            }
        },
        "company_theme_map": {},
        "supply_chain_nodes": [],
    }
    rows = [
        {"code": "2436", "name": "偉詮電", "industry": "半導體業"},
        {"code": "3588", "name": "通嘉", "industry": "半導體業"},
        {"code": "3041", "name": "揚智", "industry": "半導體業"},
    ]

    mapped = _attach_theme_matches(rows, library)

    assert all(not row["theme_matches"] for row in mapped)


def test_candidate_only_stocks_are_not_theme_representatives(monkeypatch):
    stocks = [
        _stock("1111", "AI Keyword Co", "?擗?"),
        _stock("2222", "AI Candidate Co", "?擗?"),
    ]
    library = _prepared_library()
    library["company_theme_map"] = {}
    library["supply_chain_nodes"] = []
    monkeypatch.setattr("research_center.theme_radar_service.load_stock_universe", lambda _: stocks)
    monkeypatch.setattr(
        "research_center.theme_radar_service._safe_price_metrics",
        lambda universe: {
            "1111.TW": {"price": 30, "avg_volume_20d": 5000},
            "2222.TW": {"price": 40, "avg_volume_20d": 6000},
        },
    )
    monkeypatch.setattr("research_center.theme_radar_service.load_recent_scan_results", lambda limit=30: [{"scan_type": "Radar", "selected_codes": ["1111", "2222"]}])
    monkeypatch.setattr("research_center.theme_radar_service._load_topic_library", lambda: library)
    monkeypatch.setattr("research_center.theme_radar_service._build_news_theme_stats", lambda library, lookback_days: [])

    data = collect_theme_radar_data(date(2026, 5, 22), lookback_days=7)
    ai_server = next(item for item in data["theme_rankings"] if item["theme_id"] == "ai_server")

    assert ai_server["representative_stocks"] == []
    assert {row["code"] for row in ai_server["candidate_stocks"]} == {"1111", "2222"}
    assert {row["code"] for row in ai_server["display_stock_groups"]["candidate_watchlist"]} == {"1111", "2222"}
    assert ai_server["display_stock_groups"]["candidate_label"] == "待驗證候選股，不得稱為代表股或核心受惠股"
    assert ai_server["representative_policy"].startswith("representative_stocks excludes candidate-only")


def test_theme_flow_layers_keep_candidate_out_of_representatives():
    library = _prepared_library()
    rows = [
        {
            "code": "1111",
            "name": "AI Candidate Co",
            "industry": "?擗?",
            "avg_volume_20d": 5000,
            "theme_matches": [
                {
                    "theme_id": "ai_server",
                    "theme_name": "AI隡箸???",
                    "supply_chain_role": "候選節點",
                    "verification_status": "candidate",
                    "match_method": "keyword_or_industry",
                }
            ],
        }
    ]

    data = build_theme_flow_data(
        "AI隡箸???",
        date(2026, 5, 22),
        preloaded={"topic_library": library, "stock_rows": rows, "news_stats": []},
    )

    layer = data["layers"][0]
    assert layer["representative_stocks"] == []
    assert [row["code"] for row in layer["candidate_stocks"]] == ["1111"]
    assert layer["candidate_label"] == "待驗證候選股，不得稱為代表股"
    assert layer["current_strength"] == "價格強但關聯待驗證"
    assert layer["stage"] == "待驗證候選"


def test_theme_flow_layers_expose_display_stock_groups():
    library = _prepared_library()
    rows = [
        {
            "code": "1111",
            "name": "Verified AI Co",
            "industry": "電子",
            "avg_volume_20d": 7000,
            "theme_matches": [
                {
                    "theme_id": "ai_server",
                    "theme_name": "AI伺服器",
                    "supply_chain_role": "核心受惠",
                    "verification_status": "verified",
                    "match_method": "direct_map",
                }
            ],
        },
        {
            "code": "2222",
            "name": "Candidate AI Co",
            "industry": "電子",
            "avg_volume_20d": 6000,
            "theme_matches": [
                {
                    "theme_id": "ai_server",
                    "theme_name": "AI伺服器",
                    "supply_chain_role": "核心受惠",
                    "verification_status": "candidate",
                    "match_method": "keyword_or_industry",
                }
            ],
        },
    ]

    data = build_theme_flow_data(
        "AI伺服器",
        date(2026, 5, 22),
        preloaded={"topic_library": library, "stock_rows": rows, "news_stats": []},
    )

    layer = data["layers"][0]
    groups = layer["display_stock_groups"]
    assert [row["code"] for row in groups["verified_representatives"]] == ["1111"]
    assert groups["inferred_representatives"] == []
    assert [row["code"] for row in groups["candidate_watchlist"]] == ["2222"]
    assert "不得稱為代表股" in groups["candidate_label"]


def test_theme_radar_prompt_has_candidate_naming_rules():
    prompt_text = __import__("pathlib").Path("prompt/report/theme_radar.md").read_text(encoding="utf-8-sig")

    assert "candidate 狀態的代表股" in prompt_text
    assert "待驗證候選股" in prompt_text
    assert "不得稱為代表股" in prompt_text
    assert "market_movers" in prompt_text
    assert "不得用 `/scan`、`/radar`" in prompt_text
    assert "同產業上漲" in prompt_text
    assert "market_data_date" in prompt_text
    assert "HBM、AI ASIC、液冷、BBU" in prompt_text


def test_theme_flow_prompt_has_output_hard_rules():
    prompt_text = (ROOT_DIR / "prompt" / "report" / "theme_flow.md").read_text(encoding="utf-8")

    assert "不要把整份報告包在 ```markdown" in prompt_text
    assert "嚴禁輸出 `[S?]`" in prompt_text
    assert "已驗證代表股" in prompt_text
    assert "推論型代表股" in prompt_text
    assert "待驗證候選股" in prompt_text
    assert "題材庫待補清單" in prompt_text
    assert "layer_market_validation" in prompt_text
    assert "尚未從盤面驗證" in prompt_text
    assert "price-only" in prompt_text
    assert "market_data_date" in prompt_text
    assert "報告產生日" in prompt_text
    assert "本次盤面資料未直接驗證" in prompt_text
