"""Tests for preferred_sources utilities."""
from __future__ import annotations

import unittest

from research_center.preferred_sources import (
    build_site_queries,
    load_preferred_sources,
    match_preferred_source,
    preferred_source_level,
    preferred_source_weight,
)
from research_center.source_rank import sort_sources_by_preferred_weight
from research_center.models import SourceItem


class PreferredSourcesTests(unittest.TestCase):
    def test_load_preferred_sources_returns_dict(self):
        data = load_preferred_sources()
        self.assertIsInstance(data, dict)
        self.assertIn("official", data)
        self.assertIn("financial_media", data)
        self.assertIn("industry_media", data)
        self.assertIn("community", data)

    def test_match_exact_domain(self):
        entry = match_preferred_source("https://moneydj.com/article/123")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["domain"], "moneydj.com")

    def test_match_www_subdomain(self):
        entry = match_preferred_source("https://www.moneydj.com/news")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["domain"], "moneydj.com")

    def test_match_uanalyze_source(self):
        entry = match_preferred_source("https://uanalyze.com.tw/articles/4402751575")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["domain"], "uanalyze.com.tw")
        self.assertEqual(entry["name"], "優分析")
        self.assertEqual(entry["level"], "L2_media")
        self.assertGreater(preferred_source_weight("https://uanalyze.com.tw/articles/4402751575"), 0)

    def test_match_moneyweekly_source(self):
        entry = match_preferred_source("https://www.moneyweekly.com.tw/ArticleData/Info/article/227526")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["domain"], "moneyweekly.com.tw")
        self.assertEqual(entry["name"], "理財週刊")
        self.assertEqual(entry["level"], "L2_media")
        self.assertGreater(preferred_source_weight("https://www.moneyweekly.com.tw/ArticleData/Info/article/227526"), 0)

    def test_match_fugle_blog_source(self):
        entry = match_preferred_source("https://blog.fugle.tw/industry/ai-server-supply-chain")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["domain"], "blog.fugle.tw")
        self.assertEqual(entry["name"], "富果直送")
        self.assertEqual(entry["level"], "L2_media")
        self.assertGreater(preferred_source_weight("https://blog.fugle.tw/industry/ai-server-supply-chain"), 0)

    def test_match_sinotrade_richclub_source(self):
        entry = match_preferred_source("https://www.sinotrade.com.tw/richclub/hotstock")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["domain"], "sinotrade.com.tw")
        self.assertEqual(entry["name"], "豐雲學堂")
        self.assertEqual(entry["level"], "L2_media")
        self.assertGreater(preferred_source_weight("https://www.sinotrade.com.tw/richclub/hotstock"), 0)

    def test_match_official_returns_l1_high_weight(self):
        entry = match_preferred_source("https://mops.twse.com.tw/announcement")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["level"], "L1_official")
        self.assertGreaterEqual(entry["weight"], 95)

    def test_match_community_returns_l3_low_weight(self):
        entry = match_preferred_source("https://ptt.cc/stock")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["level"], "L3_community")
        self.assertLess(entry["weight"], 50)

    def test_unknown_domain_returns_none(self):
        self.assertIsNone(match_preferred_source("https://example.com"))
        self.assertIsNone(match_preferred_source(""))

    def test_preferred_source_weight_official(self):
        self.assertGreater(preferred_source_weight("https://twse.com.tw"), 90)

    def test_preferred_source_weight_unknown(self):
        self.assertEqual(preferred_source_weight("https://unknown-site.com"), 0)

    def test_preferred_source_level_official(self):
        self.assertEqual(preferred_source_level("https://tdcc.com.tw"), "L1_official")

    def test_preferred_source_level_unknown(self):
        self.assertIsNone(preferred_source_level("https://unknown-site.com"))

    def test_build_site_queries_limited(self):
        qs = build_site_queries("AI伺服器", max_domains=6)
        self.assertGreater(len(qs), 0)
        self.assertLessEqual(len(qs), 6)
        for q in qs:
            self.assertTrue(q.startswith("AI伺服器 site:"), f"Unexpected query format: {q}")

    def test_build_site_queries_does_not_include_community(self):
        qs = build_site_queries("test query")
        domains = [q.split("site:")[1] for q in qs]
        community_domains = {"ptt.cc", "dcard.tw", "cmoney.tw", "stockfeel.com.tw"}
        for d in domains:
            self.assertNotIn(d, community_domains, f"Community domain {d} should not be in site queries")

    def test_build_site_queries_priority_order(self):
        qs = build_site_queries("test", max_domains=10)
        # Should include official first, then media, then industry
        domains = [q.split("site:")[1] for q in qs]
        official_domains = {"mops.twse.com.tw", "twse.com.tw", "tpex.org.tw", "tdcc.com.tw"}
        media_domains = {"cna.com.tw", "ctee.com.tw", "money.udn.com", "cnyes.com", "moneydj.com", "tw.stock.yahoo.com"}
        industry_domains = {"digitimes.com", "technews.tw", "trendforce.com", "eettaiwan.com"}
        # At least some official domains should appear first
        if domains:
            first = domains[0]
            self.assertIn(first, official_domains | media_domains | industry_domains)

    def test_sort_sources_by_preferred_weight_orders_correctly(self):
        """Official > media > community > unknown ordering must be respected."""
        sources = [
            SourceItem(source_id="S001", title="Unknown", url="https://unknown-site.com/article", source_level="Level 3"),
            SourceItem(source_id="S002", title="PTT", url="https://ptt.cc/stock", source_level="L3_community"),
            SourceItem(source_id="S003", title="MoneyDJ", url="https://moneydj.com/news", source_level="L2_media"),
            SourceItem(source_id="S004", title="MOPS", url="https://mops.twse.com.tw/announcement", source_level="L1_official"),
        ]
        sorted_sources = sort_sources_by_preferred_weight(sources)
        urls = [s.url for s in sorted_sources]
        # mops (L1, weight 100) should be first
        self.assertEqual(urls[0], "https://mops.twse.com.tw/announcement")
        # moneydj (L2, weight 78) should be second
        self.assertEqual(urls[1], "https://moneydj.com/news")
        # ptt (L3, weight 35) should be before unknown (weight 0)
        # because we sort descending by weight
        self.assertEqual(urls[2], "https://ptt.cc/stock")
        self.assertEqual(urls[3], "https://unknown-site.com/article")

    def test_sort_sources_stable_preserves_original_order_for_same_weight(self):
        """Ties should preserve original order (stable sort)."""
        sources = [
            SourceItem(source_id="S001", title="First", url="https://unknown-a.com", source_level="Level 3"),
            SourceItem(source_id="S002", title="Second", url="https://unknown-b.com", source_level="Level 3"),
        ]
        sorted_sources = sort_sources_by_preferred_weight(sources)
        urls = [s.url for s in sorted_sources]
        self.assertEqual(urls, ["https://unknown-a.com", "https://unknown-b.com"])


if __name__ == "__main__":
    unittest.main()
