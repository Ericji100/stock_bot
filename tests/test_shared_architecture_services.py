from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from research_center.artifact_registry import (
    build_artifact_inventory,
    build_artifact_record,
    is_artifact_usable,
    register_artifact,
    summarize_artifact_inventory,
)
from research_center.backfill_dag_service import (
    build_backfill_dag,
    create_backfill_dag_event,
    summarize_backfill_dag,
    summarize_backfill_events,
)
from research_center.data_source_gateway import run_provider_chain
from research_center.entity_resolver import (
    format_tw_symbol,
    resolve_entity,
    resolve_sector_alias,
    resolve_supply_chain_nodes,
    resolve_topic_alias,
    summarize_supply_chain_nodes,
)
from research_center.error_classification_service import classify_error
from research_center.event_context_service import build_event_context, summarize_event_context
from research_center.models import CommandRequest
from research_center.prompt_manifest_service import (
    PROMPT_BUNDLE_SCHEMA_VERSION,
    build_prompt_manifest,
    prompt_bundle_for_request,
)
from research_center.stock_feature_pack_service import attach_feature_pack
from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache


class SharedArchitectureServicesTests(unittest.TestCase):
    def tearDown(self):
        safe_remove_test_cache("shared_architecture_services")

    def test_artifact_registry_registers_and_checks_usable_record(self):
        root = ensure_test_cache_dir("shared_architecture_services/artifact")
        artifact = root / "sample.json"
        artifact.write_text("{}", encoding="utf-8")
        record = build_artifact_record(
            artifact_type="test_artifact",
            path=artifact,
            schema_version="sample_v1",
            data_date="2026-06-19",
            completeness=0.8,
            ttl_days=1,
        )
        registry_path = register_artifact(record, registry_root=root / "registry")

        self.assertTrue(registry_path.exists())
        usable, reason = is_artifact_usable(record)
        self.assertTrue(usable)
        self.assertEqual(reason, "usable")

    def test_artifact_registry_rejects_expired_record(self):
        record = build_artifact_record(
            artifact_type="test",
            path="x.json",
            schema_version="v1",
            completeness=1,
            ttl_days=1,
        )
        usable, reason = is_artifact_usable(record, now=datetime.now().astimezone() + timedelta(days=2))
        self.assertFalse(usable)
        self.assertEqual(reason, "expired")

    def test_artifact_inventory_summarizes_existing_data_targets(self):
        root = ensure_test_cache_dir("shared_architecture_services/inventory")
        (root / ".cache").mkdir()
        (root / ".cache" / "price_metrics.json").write_text("{}", encoding="utf-8")
        (root / "reports" / "stock").mkdir(parents=True)
        (root / "reports" / "stock" / "sample.json").write_text("{}", encoding="utf-8")
        (root / "manual_20260619_check").mkdir()
        (root / "manual_20260619_check" / "note.txt").write_text("ok", encoding="utf-8")

        inventory = build_artifact_inventory(root_dir=root, targets=(".cache", "reports"), include_manual_dirs=True)
        summary = summarize_artifact_inventory(inventory)

        self.assertEqual(inventory["schema_version"], "artifact_inventory_v1")
        self.assertEqual(summary["target_count"], 3)
        self.assertEqual(summary["usable_count"], 3)
        self.assertEqual(summary["by_type"]["cache_directory"], 1)
        self.assertEqual(summary["by_type"]["report_directory"], 1)
        self.assertEqual(summary["by_type"]["manual_artifact"], 1)

    def test_entity_resolver_resolves_stock_code_and_topic_alias(self):
        root = ensure_test_cache_dir("shared_architecture_services/entity")
        (root / "config").mkdir()
        (root / "stock_list.json").write_text(
            json.dumps([{"code": "2330", "name": "台積電", "industry": "半導體", "market": "TWSE"}], ensure_ascii=False),
            encoding="utf-8",
        )
        (root / "config" / "theme_profiles.json").write_text(
            json.dumps([{"theme": "AI伺服器", "aliases": ["AI Server"]}], ensure_ascii=False),
            encoding="utf-8",
        )
        (root / "config" / "sector_alias_map.json").write_text(
            json.dumps(
                {
                    "schema_version": "sector_alias_map_v1",
                    "sectors": {
                        "半導體": {
                            "display_name": "半導體",
                            "aliases": ["半導體業", "IC設計"],
                            "rerating_label": "半導體重估",
                            "rerating_bonus": 10,
                            "subsectors": [{"name": "晶圓代工"}],
                        }
                    },
                    "alias_redirects": {"晶片": "半導體"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (root / "config" / "supply_chain_nodes.json").write_text(
            json.dumps(
                [
                    {
                        "company_code": "2330",
                        "company_name": "台積電",
                        "theme_id": "ai_server",
                        "layer": "upstream",
                        "role": "晶圓代工",
                    },
                    {
                        "company_code": "2317",
                        "company_name": "鴻海",
                        "theme_id": "ai_server",
                        "layer": "assembly",
                        "role": "系統組裝",
                    },
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        entity = resolve_entity("2330", root_dir=root)
        self.assertEqual(entity.code, "2330")
        self.assertEqual(entity.name, "台積電")
        self.assertEqual(entity.symbol, "2330.TW")
        self.assertEqual(format_tw_symbol("2330", "TWSE"), "2330.TW")
        self.assertEqual(resolve_topic_alias("AI Server", root_dir=root)["canonical"], "AI伺服器")
        self.assertEqual(entity.sector["canonical"], "半導體")
        self.assertIn("半導體業", entity.aliases)
        self.assertEqual(entity.supply_chain_summary["node_count"], 1)
        self.assertEqual(entity.supply_chain_summary["theme_ids"], ["ai_server"])

        sector = resolve_sector_alias("晶片", root_dir=root)
        nodes = resolve_supply_chain_nodes(company_code="2330", root_dir=root)
        self.assertEqual(sector["canonical"], "半導體")
        self.assertEqual(nodes[0]["role"], "晶圓代工")
        self.assertEqual(summarize_supply_chain_nodes(nodes)["roles"], ["晶圓代工"])

    def test_error_classification_covers_quota_timeout_and_cache(self):
        self.assertEqual(classify_error(RuntimeError("HTTP 429 quota exceeded")).error_type, "quota_exhausted")
        self.assertEqual(classify_error(TimeoutError("timeout")).error_type, "ai_timeout")
        self.assertEqual(classify_error(ValueError("invalid json parse")).error_type, "parse_failed")

    def test_data_source_gateway_falls_back_to_second_provider(self):
        def failing():
            raise RuntimeError("network timeout")

        result = run_provider_chain(
            [("first", failing), ("second", lambda: {"ok": True})],
            operation="load_price",
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.provider, "second")
        self.assertEqual(result.attempts[0].status, "failed")
        self.assertEqual(result.attempts[1].status, "success")

    def test_backfill_dag_summary_reports_ready_nodes(self):
        dag = build_backfill_dag(marker={"universe_count": 1800, "health": {"technical_cache_ok": True, "chip_coverage_ok": False}})
        summary = summarize_backfill_dag(dag)

        self.assertEqual(summary["schema_version"], "backfill_dag_v1")
        self.assertGreaterEqual(summary["node_count"], 7)
        self.assertIn("technical_cache", summary["ready_nodes"])
        self.assertIn("chip_cache", summary["pending_nodes"])

    def test_backfill_dag_events_override_node_status_and_summarize(self):
        events = [
            create_backfill_dag_event("market_universe", "started", message="load"),
            create_backfill_dag_event("market_universe", "completed", metadata={"universe_count": 1800}),
            create_backfill_dag_event("chip_cache", "failed", failure_reason="quota_exhausted"),
        ]

        dag = build_backfill_dag(marker={"universe_count": 1800, "backfill_dag_events": events})
        summary = summarize_backfill_dag(dag)
        event_summary = summarize_backfill_events(events)

        self.assertIn("market_universe", summary["ready_nodes"])
        self.assertIn("chip_cache", summary["blocked_nodes"])
        self.assertEqual(event_summary["event_count"], 3)
        self.assertEqual(event_summary["latest_status_by_node"]["chip_cache"], "failed")
        self.assertIn("chip_cache", event_summary["failed_nodes"])

    def test_prompt_manifest_builds_file_status_and_request_bundle(self):
        root = ensure_test_cache_dir("shared_architecture_services/prompt")
        (root / "prompt" / "base").mkdir(parents=True)
        (root / "prompt" / "report").mkdir(parents=True)
        (root / "prompt" / "base" / "base.md").write_text("base", encoding="utf-8")
        (root / "prompt" / "report" / "research_summary.md").write_text("research", encoding="utf-8")

        manifest = build_prompt_manifest(root)
        request = CommandRequest(command="research", raw_text="/research 2330", target="2330")
        bundle = prompt_bundle_for_request(request, root_dir=root)

        self.assertEqual(manifest["schema_version"], PROMPT_BUNDLE_SCHEMA_VERSION)
        self.assertEqual(bundle["command"], "research")
        self.assertIn("files", bundle["bundle"])

    def test_event_context_filters_target_and_summarizes_counts(self):
        events = [
            {"target": "2330", "event_type": "news", "published_date": datetime.now().date().isoformat()},
            {"target": "2317", "event_type": "news", "published_date": datetime.now().date().isoformat()},
            {"target": "2330", "event_type": "old", "published_date": "2020-01-01"},
        ]
        context = build_event_context(target="2330", events=events, days=30)
        summary = summarize_event_context(context)

        self.assertEqual(context["event_count"], 1)
        self.assertEqual(summary["event_types"]["news"], 1)

    def test_feature_pack_registers_artifact_metadata(self):
        request = CommandRequest(command="research", raw_text="/research 2330", target="2330")
        data = {
            "stock": {"code": "2330", "name": "台積電"},
            "price_data": {"price": 100},
            "news_context": {"status": "ok", "items": [{"title": "sample"}]},
        }

        with patch("research_center.stock_feature_pack_service.register_artifact", return_value=Path("registry/feature_pack.json")):
            attach_feature_pack(request, data)

        artifact = data.get("feature_pack_artifact") or {}
        self.assertTrue(artifact.get("registered"))
        self.assertIn("artifact_id", artifact)


if __name__ == "__main__":
    unittest.main()
