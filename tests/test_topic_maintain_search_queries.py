from __future__ import annotations

from research_center.data_services import _topic_search_query_plan


def test_topic_maintain_search_plan_has_full_market_buckets():
    plan = _topic_search_query_plan(
        themes=[],
        stocks=[],
        gaps={},
        maintenance_mode="full_market_maintenance",
    )
    buckets = {item.get("bucket") for item in plan if item.get("type") == "full_market_bucket"}

    assert "ai_semiconductor" in buckets
    assert "power_energy_grid" in buckets
    assert "financial_dividend" in buckets
    assert "biotech_healthcare" in buckets
    assert "shipping_commodities_cycle" in buckets
    assert "domestic_consumption" in buckets


def test_topic_maintain_search_plan_is_not_ai_only():
    plan = _topic_search_query_plan(
        themes=[],
        stocks=[],
        gaps={},
        maintenance_mode="full_market_maintenance",
    )
    bucket_items = [item for item in plan if item.get("type") == "full_market_bucket"]
    ai_items = [item for item in bucket_items if item.get("bucket") == "ai_semiconductor"]

    assert len(bucket_items) >= 20
    assert len(ai_items) < len(bucket_items) / 3


def test_topic_maintain_company_queries_are_added_after_full_market_buckets():
    plan = _topic_search_query_plan(
        themes=[],
        stocks=[{"code": "2327", "name": "國巨", "industry": "電子零組件業"}],
        gaps={},
        maintenance_mode="full_market_maintenance",
    )
    types = [item.get("type") for item in plan]

    assert "full_market_bucket" in types
    assert any(item.get("type") == "sector_subsector_discovery" for item in plan)
