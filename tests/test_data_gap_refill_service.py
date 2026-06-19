from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import Mock, patch

import pandas as pd

from research_center.data_gap_refill_service import refill_data_gaps
from research_center.models import CommandRequest


def _request(command: str, **kwargs) -> CommandRequest:
    return CommandRequest(
        command=command,
        raw_text=kwargs.pop("raw_text", f"/{command}"),
        report_date=kwargs.pop("report_date", date(2026, 6, 19)),
        **kwargs,
    )


def _free_sources() -> dict:
    return {
        "valuation": {"status": "official_public", "pe_ratio": 18.5},
        "tdcc": {"status": "covered", "large_holder_pct": 42.0},
        "gross_margin_cache": {"status": "covered", "latest": {"gross_margin": 51.2}},
        "mops_documents": {"status": "official_reference", "annual_report": {"url": "https://mops.example/2330"}},
    }


def _patch_fetcher(mock_fetcher_cls: Mock) -> None:
    fetcher = Mock()
    fetcher.resolve_stock.return_value = Mock(code="2330", name="台積電")
    fetcher.fetch_quarterly_financials.return_value = pd.DataFrame(
        [
            {"Quarter": "2026Q1", "EPS": 8.1, "gross_margin": 51.2},
            {"Quarter": "2026Q2", "EPS": 8.8, "gross_margin": 52.0},
        ]
    )
    mock_fetcher_cls.return_value.__enter__.return_value = fetcher
    mock_fetcher_cls.return_value.__exit__.return_value = False


class DataGapRefillServiceTests(unittest.TestCase):
    @patch("research_center.data_gap_refill_service.attach_news_context")
    @patch("research_center.data_gap_refill_service.build_stock_topic_context")
    @patch("research_center.data_gap_refill_service.StockDataFetcher")
    @patch("research_center.data_gap_refill_service.build_chip_backup_events")
    @patch("research_center.data_gap_refill_service.build_chip_backup_snapshot")
    @patch("research_center.data_gap_refill_service.build_free_research_sources")
    @patch("research_center.data_gap_refill_service.load_price_metrics_with_fallback")
    def test_research_refills_target_only_and_records_attempts(
        self,
        mock_price,
        mock_free,
        mock_chip,
        mock_chip_events,
        mock_fetcher_cls,
        mock_topic,
        mock_news,
    ):
        _patch_fetcher(mock_fetcher_cls)
        mock_price.return_value = ({"2330.TW": {"price": 100}}, {"status": "ok"})
        mock_free.return_value = _free_sources()
        mock_chip.return_value = {"status": "covered", "summary": {"foreign_net_buy": 1200}}
        mock_chip_events.return_value = [{"event_type": "chip", "target": "2330"}]
        mock_topic.return_value = {"status": "covered", "matched_topics": ["AI"]}
        mock_news.side_effect = lambda request, data, progress=None: data.setdefault(
            "news_context", {"status": "covered", "usable_count": 1, "items": [{"title": "news"}]}
        )
        request = _request("research", target="2330", mode="deep")
        data = {"stock": {"code": "2330", "symbol": "2330.TW", "name": "台積電"}}

        refill_data_gaps(request, data)

        mock_free.assert_called_once()
        self.assertEqual(mock_free.call_args.args[0], "2330")
        self.assertIn("data_gap_refill", data)
        self.assertEqual(data["gross_margin_cache"]["status"], "covered")
        self.assertEqual(data["tdcc_data"]["status"], "covered")
        self.assertEqual(data["chip_backup_data"]["summary"]["foreign_net_buy"], 1200)
        self.assertTrue(data["source_events"])
        self.assertNotIn("2345", str(data))

    @patch("research_center.data_gap_refill_service.attach_news_context")
    @patch("research_center.data_gap_refill_service.build_candidates_topic_context")
    @patch("research_center.data_gap_refill_service.StockDataFetcher")
    @patch("research_center.data_gap_refill_service.build_chip_backup_events")
    @patch("research_center.data_gap_refill_service.build_mops_reference_events")
    @patch("research_center.data_gap_refill_service.build_chip_backup_snapshot")
    @patch("research_center.data_gap_refill_service.build_free_research_sources")
    def test_value_scan_deep_refills_only_ai_candidates_not_local_ranking(
        self,
        mock_free,
        mock_chip,
        mock_mops_events,
        mock_chip_events,
        mock_fetcher_cls,
        mock_topic,
        mock_news,
    ):
        _patch_fetcher(mock_fetcher_cls)
        mock_free.return_value = _free_sources()
        mock_chip.return_value = {"status": "covered", "summary": {"foreign_net_buy": 100}}
        mock_mops_events.side_effect = lambda code, report_date=None: [{"event_type": "mops", "target": code}]
        mock_chip_events.side_effect = lambda code, report_date=None: [{"event_type": "chip", "target": code}]
        mock_topic.return_value = {"status": "covered"}
        mock_news.side_effect = lambda request, data, progress=None: data.setdefault("news_context", {"usable_count": 0})
        request = _request("value_scan", candidate_pool="精選選股", mode="deep")
        data = {
            "ai_candidates": [{"code": "2330", "name": "台積電"}, {"code": "2317", "name": "鴻海"}],
            "local_ranking": [{"code": "9999", "name": "不應補抓"}],
            "ai_candidate_evidence_pack": [{"code": "2330", "name": "台積電"}, {"code": "2317", "name": "鴻海"}],
        }

        refill_data_gaps(request, data)

        called_codes = [call.args[0] for call in mock_free.call_args_list]
        self.assertEqual(called_codes, ["2330", "2317"])
        pack_codes = [item["code"] for item in data["ai_candidate_evidence_pack"]]
        self.assertEqual(pack_codes, ["2330", "2317"])
        self.assertNotIn("9999", pack_codes)
        for item in data["ai_candidate_evidence_pack"]:
            self.assertTrue(item["source_events"])
            self.assertNotIn("source_events", item["missing_data_status"] or [])
            self.assertNotIn("financial_detail", item["missing_data_status"] or [])
            self.assertNotIn("gross_margin_cache", item["missing_data_status"] or [])
            self.assertNotIn("chip_backup_summary", item["missing_data_status"] or [])
            self.assertIn("company_knowledge", item["missing_data_status"] or [])

    @patch("research_center.data_gap_refill_service.attach_news_context")
    @patch("research_center.data_gap_refill_service.StockDataFetcher")
    @patch("research_center.data_gap_refill_service.build_chip_backup_events")
    @patch("research_center.data_gap_refill_service.build_mops_reference_events")
    @patch("research_center.data_gap_refill_service.build_chip_backup_snapshot")
    @patch("research_center.data_gap_refill_service.build_free_research_sources")
    def test_value_scan_single_stock_uses_numeric_target_without_candidates(
        self,
        mock_free,
        mock_chip,
        mock_mops_events,
        mock_chip_events,
        mock_fetcher_cls,
        mock_news,
    ):
        _patch_fetcher(mock_fetcher_cls)
        mock_free.return_value = _free_sources()
        mock_chip.return_value = {"status": "covered", "summary": {"foreign_net_buy": 50}}
        mock_mops_events.return_value = []
        mock_chip_events.return_value = [{"event_type": "chip", "target": "6217"}]
        mock_news.side_effect = lambda request, data, progress=None: data.setdefault("news_context", {"usable_count": 0})
        request = _request("value_scan", target="6217", target_type="stock")
        data = {}

        refill_data_gaps(request, data)

        self.assertEqual(data["ai_candidate_evidence_pack"][0]["code"], "6217")
        self.assertEqual(mock_free.call_args.args[0], "6217")

    @patch("research_center.data_gap_refill_service.attach_news_context")
    @patch("research_center.data_gap_refill_service.build_free_macro_sources")
    def test_macro_refills_macro_context_and_skips_stock_specific_fields(self, mock_macro_sources, mock_news):
        mock_macro_sources.return_value = {"twse_industry_index": {"status": "official_public"}}
        mock_news.side_effect = lambda request, data, progress=None: data.setdefault("news_context", {"usable_count": 0})
        request = _request("macro", market_scope="台股")
        data = {"quantitative_market": {"status": "covered"}}

        refill_data_gaps(request, data)

        self.assertEqual(data["global_public_macro"]["twse_industry_index"]["status"], "official_public")
        fields = [item["field"] for item in data["data_gap_refill"]["attempts"]]
        self.assertIn("macro_stock_specific_refill", fields)
        self.assertNotIn("gross_margin_cache", data)
        self.assertNotIn("chip_backup_data", data)

    @patch("research_center.data_gap_refill_service.attach_news_context")
    @patch("research_center.data_gap_refill_service.build_stock_topic_context")
    @patch("research_center.data_gap_refill_service.build_chip_backup_snapshot")
    @patch("research_center.data_gap_refill_service.build_free_research_sources")
    @patch("research_center.data_gap_refill_service.load_price_metrics_with_fallback")
    def test_failed_refill_records_error_and_does_not_interrupt_report_flow(
        self,
        mock_price,
        mock_free,
        mock_chip,
        mock_topic,
        mock_news,
    ):
        mock_price.side_effect = RuntimeError("quota exhausted")
        mock_free.side_effect = RuntimeError("source cooldown")
        mock_chip.side_effect = RuntimeError("network down")
        mock_topic.side_effect = RuntimeError("topic cache missing")
        mock_news.side_effect = lambda request, data, progress=None: data.setdefault("news_context", {"usable_count": 0})
        request = _request("research", target="2330")
        data = {"stock": {"code": "2330", "symbol": "2330.TW"}}

        refill_data_gaps(request, data)

        attempts = data["data_gap_refill"]["attempts"]
        self.assertTrue(any(item["status"] == "skipped" and item.get("reason") == "source_quota_or_cooldown" for item in attempts))
        self.assertTrue(any(item["status"] == "failed" and "network down" in item.get("error", "") for item in attempts))

    @patch("research_center.data_gap_refill_service.attach_news_context")
    def test_news_search_is_deferred_to_existing_discovery_when_local_context_insufficient(self, mock_news):
        mock_news.side_effect = lambda request, data, progress=None: data.update(
            {"news_context": {"usable_count": 0, "search_recommended": True}}
        )
        request = _request("theme", theme_scope="AI")
        data = {"theme": "AI", "topic_context": {"status": "covered"}}

        refill_data_gaps(request, data)

        attempts = data["data_gap_refill"]["attempts"]
        deferred = [item for item in attempts if item["field"] == "news_external_search"]
        self.assertEqual(deferred[0]["status"], "skipped")
        self.assertIn("existing_discovery", deferred[0]["reason"])

    @patch("research_center.data_services.fetch_forum_sources")
    @patch("research_center.data_services.attach_unified_evidence_pack")
    @patch("research_center.data_services.attach_data_gap_summary")
    @patch("research_center.data_services.attach_feature_pack")
    @patch("research_center.data_services.attach_data_inventory")
    @patch("research_center.data_services.attach_company_knowledge_autofill")
    @patch("research_center.data_services.attach_shared_event_context")
    @patch("research_center.data_services.attach_shared_entity_context")
    @patch("research_center.data_services.attach_news_events")
    @patch("research_center.data_services.attach_news_context")
    @patch("research_center.data_services.attach_date_aware_context")
    @patch("research_center.data_services.refill_data_gaps")
    @patch("research_center.data_services.collect_research_data")
    def test_collect_structured_data_calls_refill_before_prompt_bundle(
        self,
        mock_collect,
        mock_refill,
        mock_date,
        mock_news,
        mock_news_events,
        mock_entity,
        mock_event,
        mock_knowledge,
        mock_inventory,
        mock_feature,
        mock_gap,
        mock_evidence,
        mock_forum,
    ):
        from research_center.data_services import collect_structured_data

        def no_op(_request, data, *args, **kwargs):
            return data

        for item in (
            mock_date,
            mock_news,
            mock_news_events,
            mock_entity,
            mock_event,
            mock_knowledge,
            mock_inventory,
            mock_feature,
            mock_gap,
            mock_evidence,
        ):
            item.side_effect = no_op
        mock_collect.return_value = {"stock": {"code": "2330", "symbol": "2330.TW", "name": "台積電"}}
        mock_refill.side_effect = lambda request, data, progress=None: data.setdefault("data_gap_refill", {"called": True})
        mock_forum.return_value = Mock(sources=[], failure_count=0, notes=[])
        request = _request("research", target="2330")

        data, sources = collect_structured_data(request)

        self.assertGreaterEqual(len(sources), 3)
        self.assertEqual(data["data_gap_refill"], {"called": True})
        mock_refill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
