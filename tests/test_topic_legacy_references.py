from research_center.topic_legacy_references import build_legacy_theme_references


def test_build_legacy_theme_references_from_formal_topic_library():
    profiles = [
        {
            "theme_id": "ai_server",
            "theme_name": "AI伺服器",
            "keywords": ["AI", "伺服器"],
            "industries": ["電腦及週邊設備"],
            "supply_chain_role": "AI伺服器供應鏈",
            "confidence": "high",
        }
    ]
    nodes = [
        {
            "theme_id": "ai_server",
            "role": "散熱模組",
            "product_keywords": ["水冷板"],
            "upstream": ["銅材"],
            "downstream": ["雲端服務商"],
        }
    ]

    refs = build_legacy_theme_references(profiles, nodes)

    assert "AI伺服器" in refs
    assert "AI" in refs["AI伺服器"]["keywords"]
    assert "電腦及週邊設備" in refs["AI伺服器"]["industries"]
    assert "散熱模組" in refs["AI伺服器"]["supply_chain"]
    assert "水冷板" in refs["AI伺服器"]["supply_chain"]
    assert "新版題材庫" in refs["AI伺服器"]["rerating_labels"]


def test_build_legacy_theme_references_handles_missing_or_bad_inputs():
    assert build_legacy_theme_references(None, None) == {}
    assert build_legacy_theme_references([{"theme_id": "", "theme_name": ""}], []) == {}
