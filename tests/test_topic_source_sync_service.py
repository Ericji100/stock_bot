from __future__ import annotations

import unittest
from unittest.mock import patch

from requests.exceptions import SSLError

from research_center.topic_source_sync_service import (
    apply_topic_source_caches_to_formal_library,
    TPEX_URL,
    UDN_INDUSTRY_URL,
    parse_tpex_industry_chain,
    parse_udn_industry_topics,
    sync_topic_sources,
)


class TopicSourceSyncServiceTests(unittest.TestCase):
    def test_parse_tpex_industry_chain_links_and_table_rows(self):
        html = """
        <html><body>
          <a href="/industry/semiconductor">半導體</a>
          <table>
            <tr><th>產業</th><th>階段</th><th>角色</th><th>代號</th><th>公司</th></tr>
            <tr><td>半導體</td><td>設計</td><td>ASIC設計</td><td>3443</td><td>創意</td></tr>
          </table>
        </body></html>
        """

        data = parse_tpex_industry_chain(html)
        items = data["items"]

        self.assertEqual(data["source"], "tpex_industry_chain")
        self.assertTrue(any(item["name"] == "半導體" for item in items))
        self.assertTrue(any(item["company_code"] == "3443" and item["company_name"] == "創意" for item in items))

    def test_parse_udn_industry_topics_links(self):
        html = """
        <html><body>
          <a href="/industry/semiconductor">半導體</a>
          <a href="/topic/ai-server">AI伺服器</a>
          <a href="/topic/cowos">CoWoS先進封裝</a>
        </body></html>
        """

        data = parse_udn_industry_topics(html)

        self.assertEqual(data["source"], "udn_industry_topics")
        self.assertTrue(any(item["name"] == "半導體" for item in data["industries"]))
        self.assertTrue(any(item["name"] == "AI伺服器" for item in data["topics"]))
        self.assertTrue(any(item["category"] == "科技" for item in data["topics"]))

    def test_sync_topic_sources_uses_fetcher_and_saves_both_caches(self):
        def fetcher(url: str) -> str:
            if url == TPEX_URL:
                return '<a href="/industry/semiconductor">半導體</a>'
            if url == UDN_INDUSTRY_URL:
                return '<a href="/topic/ai-server">AI伺服器</a>'
            raise AssertionError(url)

        with patch("research_center.topic_source_sync_service.save_tpex_industry_chain") as save_tpex, patch(
            "research_center.topic_source_sync_service.save_udn_industry_topics"
        ) as save_udn, patch("research_center.topic_source_sync_service.apply_topic_source_caches_to_formal_library") as apply_formal:
            apply_formal.return_value = {
                "profiles_created": 1,
                "profiles_updated": 0,
                "company_relations_updated": 0,
                "supply_chain_nodes_updated": 0,
            }
            result = sync_topic_sources(fetcher=fetcher)

        self.assertTrue(result.success)
        self.assertIn("tpex", result.synced_sources)
        self.assertIn("udn", result.synced_sources)
        self.assertGreaterEqual(result.tpex_items, 1)
        self.assertGreaterEqual(result.udn_topics, 1)
        save_tpex.assert_called_once()
        save_udn.assert_called_once()
        apply_formal.assert_called_once()
        self.assertEqual(result.formal_profiles_created, 1)

    def test_sync_topic_sources_can_sync_only_udn(self):
        calls: list[str] = []

        def fetcher(url: str) -> str:
            calls.append(url)
            return '<a href="/topic/ai-server">AI伺服器</a>'

        with patch("research_center.topic_source_sync_service.save_udn_industry_topics"), patch(
            "research_center.topic_source_sync_service.apply_topic_source_caches_to_formal_library",
            return_value={
                "profiles_created": 1,
                "profiles_updated": 0,
                "company_relations_updated": 0,
                "supply_chain_nodes_updated": 0,
            },
        ):
            result = sync_topic_sources(include_tpex=False, include_udn=True, fetcher=fetcher)

        self.assertTrue(result.success)
        self.assertEqual(calls, [UDN_INDUSTRY_URL])
        self.assertEqual(result.synced_sources, ["udn"])

    def test_sync_topic_sources_tpex_ssl_fallback_is_limited_to_tpex(self):
        calls: list[dict] = []

        class FakeResponse:
            encoding = "utf-8"
            text = '<a href="/industry/semiconductor">半導體</a>'

            def raise_for_status(self):
                return None

        def fake_get(url: str, **kwargs):
            calls.append({"url": url, "verify": kwargs.get("verify")})
            if url == TPEX_URL and kwargs.get("verify") is True:
                raise SSLError("certificate verify failed: Missing Subject Key Identifier")
            return FakeResponse()

        with patch("research_center.topic_source_sync_service.requests.get", side_effect=fake_get), patch(
            "research_center.topic_source_sync_service.save_tpex_industry_chain"
        ) as save_tpex, patch("research_center.topic_source_sync_service.save_udn_industry_topics"), patch(
            "research_center.topic_source_sync_service.apply_topic_source_caches_to_formal_library",
            return_value={
                "profiles_created": 1,
                "profiles_updated": 0,
                "company_relations_updated": 0,
                "supply_chain_nodes_updated": 0,
            },
        ):
            result = sync_topic_sources(include_tpex=True, include_udn=False)

        self.assertTrue(result.success)
        self.assertEqual([call["verify"] for call in calls], [True, False])
        saved = save_tpex.call_args.args[0]
        self.assertFalse(saved["metadata"]["ssl_verify"])
        self.assertTrue(saved["metadata"]["ssl_fallback"])
        self.assertIn("Missing Subject Key Identifier", saved["metadata"]["ssl_fallback_reason"])

    def test_apply_topic_source_caches_writes_formal_library(self):
        tpex_data = {
            "items": [
                {
                    "industry": "半導體",
                    "chain_stage": "IC設計",
                    "role": "ASIC設計",
                    "company_code": "3443",
                    "company_name": "創意",
                    "source_url": "https://ic.tpex.org.tw/semiconductor",
                }
            ]
        }
        udn_data = {
            "industries": [{"name": "半導體", "url": "https://money.udn.com/industry/semiconductor"}],
            "topics": [{"name": "AI伺服器", "url": "https://money.udn.com/topic/ai-server", "category": "科技"}],
        }

        saved: dict[str, object] = {}
        with patch("research_center.topic_source_sync_service.load_topic_profiles", return_value=[]), patch(
            "research_center.topic_source_sync_service.load_company_topic_map", return_value={}
        ), patch("research_center.topic_source_sync_service.load_supply_chain_nodes", return_value=[]), patch(
            "research_center.topic_source_sync_service.save_topic_profiles",
            side_effect=lambda profiles: saved.setdefault("profiles", profiles),
        ), patch(
            "research_center.topic_source_sync_service.save_company_topic_map",
            side_effect=lambda mapping: saved.setdefault("company_map", mapping),
        ), patch(
            "research_center.topic_source_sync_service.save_supply_chain_nodes",
            side_effect=lambda nodes: saved.setdefault("nodes", nodes),
        ):
            stats = apply_topic_source_caches_to_formal_library(tpex_data=tpex_data, udn_data=udn_data)

        self.assertGreaterEqual(stats["profiles_created"], 2)
        self.assertEqual(stats["company_relations_updated"], 1)
        self.assertEqual(stats["supply_chain_nodes_updated"], 1)
        profiles = saved["profiles"]
        self.assertTrue(any(p.theme_name == "AI伺服器" and p.extra.get("source_sync") for p in profiles))
        company_map = saved["company_map"]
        self.assertIn("3443", company_map)
        self.assertEqual(company_map["3443"].extra["source_sync_status"], "verified")
        nodes = saved["nodes"]
        self.assertEqual(nodes[0].extra["source_sync_method"], "topic_source_sync")


if __name__ == "__main__":
    unittest.main()
