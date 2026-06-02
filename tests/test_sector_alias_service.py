from __future__ import annotations

from research_center.sector_alias_service import (
    build_subsector_rankings,
    build_topic_maintain_sector_queries,
    canonical_sector_name,
    load_sector_alias_map,
    rerating_label_for_industry,
    topic_search_terms_for_stock,
)


def test_sector_alias_map_covers_key_non_ai_groups():
    data = load_sector_alias_map()
    sectors = data["sectors"]

    assert "電子零組件業" in sectors
    assert "汽車工業" in sectors
    assert "電器電纜" in sectors
    assert canonical_sector_name("電線電纜") == "電器電纜"
    assert canonical_sector_name("汽車材料") == "汽車工業"


def test_subsector_rankings_split_passive_components_and_cables():
    rows = [
        {"code": "2327", "name": "國巨", "industry": "電子零組件業", "change_pct": 9.8, "volume_ratio": 2.1, "avg_volume_20d": 18000, "new_high_days": 20},
        {"code": "2492", "name": "華新科", "industry": "電子零組件業", "change_pct": 8.2, "volume_ratio": 1.8, "avg_volume_20d": 9000, "new_high_days": 10},
        {"code": "1605", "name": "華新", "industry": "電器電纜", "change_pct": 6.3, "volume_ratio": 1.7, "avg_volume_20d": 12000, "new_high_days": 5},
    ]

    rankings = build_subsector_rankings(rows)
    names = {(row["sector"], row["subsector"]) for row in rankings}

    assert ("電子零組件業", "被動元件") in names
    assert ("電器電纜", "電線電纜") in names


def test_topic_maintain_queries_are_dynamic_not_fixed_ai_memory_bundle():
    stock = {"code": "2327", "name": "國巨", "industry": "電子零組件業"}

    terms = topic_search_terms_for_stock(stock)
    queries = build_topic_maintain_sector_queries([stock], limit=4)
    text = " ".join(item["query"] for item in queries)

    assert "被動元件" in terms
    assert "被動元件" in text
    assert "AI 伺服器 電源 記憶體" not in text


def test_rerating_labels_are_not_ai_default_for_all_electronics():
    _, label = rerating_label_for_industry("電子零組件業")

    assert "被動元件" in label
    assert label != "AI 伺服器零組件/高速傳輸"
