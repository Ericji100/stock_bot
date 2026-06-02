from __future__ import annotations

import json
from pathlib import Path

from research_center.sector_alias_service import (
    build_subsector_rankings,
    build_topic_maintain_sector_queries,
    canonical_sector_name,
    topic_search_terms_for_stock,
)


ROOT = Path(__file__).resolve().parents[1]


def _json(path: str):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_sector_aliases_cover_non_ai_rotation_groups():
    assert canonical_sector_name("電線電纜") == "電器電纜"
    assert canonical_sector_name("電纜線材") == "電器電纜"
    assert canonical_sector_name("MLCC") == "電子零組件業"
    assert canonical_sector_name("車用電子") == "汽車工業"
    assert canonical_sector_name("汽車材料") == "汽車工業"
    assert canonical_sector_name("重電") == "電機機械"
    assert canonical_sector_name("金控") == "金融保險"


def test_subsector_rankings_split_cable_auto_passive_and_heavy_power():
    rows = [
        {"code": "1609", "name": "大亞", "industry": "電器電纜", "change_pct": 8.1, "volume_ratio": 2.2, "avg_volume_20d": 18000, "new_high_days": 20},
        {"code": "6605", "name": "帝寶", "industry": "汽車工業", "change_pct": 7.3, "volume_ratio": 1.9, "avg_volume_20d": 12000, "new_high_days": 10},
        {"code": "2327", "name": "國巨", "industry": "電子零組件業", "change_pct": 6.8, "volume_ratio": 1.7, "avg_volume_20d": 22000, "new_high_days": 5},
        {"code": "1519", "name": "華城", "industry": "電機機械", "change_pct": 5.9, "volume_ratio": 1.6, "avg_volume_20d": 16000, "new_high_days": 3},
    ]

    rankings = build_subsector_rankings(rows)
    names = {(row["sector"], row["subsector"]) for row in rankings}

    assert ("電器電纜", "電線電纜") in names
    assert ("汽車工業", "汽車零組件/AM") in names
    assert ("電子零組件業", "被動元件") in names
    assert ("電機機械", "重電/電力設備") in names


def test_topic_maintain_queries_follow_dynamic_strong_groups():
    stocks = [
        {"code": "1609", "name": "大亞", "industry": "電器電纜"},
        {"code": "6605", "name": "帝寶", "industry": "汽車工業"},
        {"code": "2327", "name": "國巨", "industry": "電子零組件業"},
    ]

    terms = topic_search_terms_for_stock(stocks[0])
    queries = build_topic_maintain_sector_queries(stocks, limit=8)
    text = " ".join(item["query"] for item in queries)

    assert "電線電纜" in terms
    assert "電線電纜" in text
    assert "汽車零組件" in text
    assert "被動元件" in text


def test_topic_seed_data_contains_cable_auto_and_passive_evidence_nodes():
    profiles = {item["theme_id"]: item for item in _json("config/theme_profiles.json")}
    company_map = _json("config/company_theme_map.json")
    nodes = {(item["theme_id"], item["company_code"]) for item in _json("config/supply_chain_nodes.json")}

    assert "electric_wire_cable_grid" in profiles
    assert "smart_ev_electronics" in profiles
    assert "passive_components" in profiles
    assert "electric_wire_cable_grid" in company_map["1609"]["themes"]
    assert company_map["6605"]["primary_theme"] == "smart_ev_electronics"
    assert ("electric_wire_cable_grid", "1609") in nodes
    assert ("smart_ev_electronics", "6605") in nodes
    assert ("passive_components", "2327") in nodes


def test_theme_profiles_do_not_contain_question_mark_mojibake_values():
    profiles = _json("config/theme_profiles.json")
    bad_values: list[tuple[str, str]] = []

    def walk(value, path: str = ""):
        if isinstance(value, str):
            stripped = value.strip()
            if stripped and set(stripped) == {"?"}:
                bad_values.append((path, value))
            elif "????" in stripped:
                bad_values.append((path, value))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}" if path else str(key))

    walk(profiles)
    assert bad_values == []


def test_topic_source_caches_do_not_contain_mojibake_text_values():
    markers = ("ç", "æ", "å", "è", "é", "ä", "¶", "�", "????")
    bad_values: list[tuple[str, str]] = []

    def walk(value, path: str = ""):
        if isinstance(value, str):
            if path.endswith(".source_url") or path.endswith(".url"):
                return
            if any(marker in value for marker in markers):
                bad_values.append((path, value))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}" if path else str(key))

    walk(_json("config/theme_profiles.json"), "theme_profiles")
    walk(_json("config/tpex_industry_chain.json"), "tpex_industry_chain")
    assert bad_values == []


def test_tpex_industry_cache_excludes_navigation_rows():
    names = {item.get("name") for item in _json("config/tpex_industry_chain.json").get("items", [])}

    assert "半導體" in names
    assert "人工智慧" in names
    assert "產業價值鏈資訊平台 企業籌資更便捷 大眾投資更穩當" not in names
    assert "使用條款" not in names
    assert "隱私權保護說明" not in names
    assert "網站地圖" not in names
