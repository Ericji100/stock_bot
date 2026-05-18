from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import curated_scan_service
import research_center.recent_scans as recent_scans
from research_center.command_parser import parse_command_text
from research_center.config import ResearchCenterConfig
from research_center.database import ResearchDatabase
from research_center.date_guard import filter_sources_for_report_date
from research_center.data_services import _theme_profile, _value_rerating_score, collect_research_data
from research_center.knowledge_base import enrich_company_rows, theme_knowledge_summary
from research_center.macro_indicators import _fear_greed_zone
from research_center.official_connectors import parse_taifex_vix_html, parse_twse_institutional_json
from research_center.value_validation import build_value_cross_validation
from research_center.mops_sources import financial_detail_snapshot
from research_center.models import CommandRequest, ReportArtifacts
from research_center.models import CommandRequest, SourceItem
from research_center.orchestrator import ResearchCenter
from research_center.report_builder import fallback_markdown, write_report_artifacts
from research_center.source_rank import rank_source


class CommandParserTests(unittest.TestCase):
    def test_research_deep_date(self):
        request = parse_command_text('/research 6217 --date 2026-01-07 --deep', user_id='u1')
        self.assertEqual(request.command, 'research')
        self.assertEqual(request.target, '6217')
        self.assertEqual(request.mode, 'deep')
        self.assertEqual(request.report_date, date(2026, 1, 7))

    def test_research_supports_deepseek_model_flag(self):
        request = parse_command_text('/research 2330 --deep --model deepseek')
        self.assertEqual(request.ai_model, 'deepseek')

    def test_macro_scope(self):
        request = parse_command_text('/macro 台股 AI')
        self.assertEqual(request.market_scope, '台股')
        self.assertEqual(request.theme_scope, 'AI')
        self.assertEqual(request.region_scope, '台灣')

    def test_conflict_source_only_score(self):
        with self.assertRaises(ValueError):
            parse_command_text('/research 2330 --source-only --score')

    def test_top_not_allowed_for_research(self):
        with self.assertRaises(ValueError):
            parse_command_text('/research 2330 --top 10')

    def test_parse_keeps_deepseek_model_after_output_format_merge(self):
        config = ResearchCenterConfig(
            api_key=None,
            minimax_api_key=None,
            serper_api_key=None,
            jina_api_key=None,
            opencode_api_key=None,
        )
        center = ResearchCenter(config)
        request = center.parse("/research 5425 --deep --model deepseek")
        self.assertEqual(request.ai_model, "deepseek")


class SourceRankTests(unittest.TestCase):
    def test_official_source_is_level_1(self):
        self.assertEqual(rank_source('https://mops.twse.com.tw/server-java/t05st10'), 'Level 1')

    def test_forum_source_is_level_4(self):
        self.assertEqual(rank_source('https://www.ptt.cc/bbs/Stock/index.html'), 'Level 4')


class ReportAndDatabaseTests(unittest.TestCase):
    def test_write_report_and_db(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("research_center/test_write_report_and_db")
        db = None
        try:
            root = tmp
            db = ResearchDatabase(root / 'stock_research.db')
            request = CommandRequest(command='research', raw_text='/research 2330 --source-only', target='2330', target_type='stock', source_only=True, mode='source_only')
            sources = [SourceItem(source_id='S001', title='TWSE', url='https://www.twse.com.tw/', source_level='Level 1')]
            markdown = fallback_markdown(request, {'stock': {'code': '2330'}}, sources)
            artifacts, report_json = write_report_artifacts(root / 'reports', request, markdown, 'summary', sources, False, None)
            db.save_report(request, artifacts, 'summary', sources, False, None)
            self.assertTrue(artifacts.markdown_path.exists())
            self.assertTrue(artifacts.html_path.exists())
            self.assertTrue(artifacts.json_path.exists())
            row = db.latest_report(target='2330')
            self.assertIsNotNone(row)
            self.assertEqual(row['report_id'], artifacts.report_id)
            self.assertEqual(report_json['report_type'], 'research')
        finally:
            if db is not None:
                db.close()
            safe_remove_test_cache("research_center/test_write_report_and_db")

    def test_database_saves_events(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("research_center/test_database_saves_events")
        db = ResearchDatabase(tmp / 'stock_research.db')
        try:
            db.save_events([
                {
                    'event_type': 'mops',
                    'target': 'TEST_DB_SAVE_EVENTS_TARGET',
                    'title': 'material event',
                    'source_url': 'https://mops.twse.com.tw/',
                    'source_level': 'Level 1',
                    'published_date': '2026-01-01',
                    'payload': {'ok': True},
                }
            ])
            rows = db.query_events_before('TEST_DB_SAVE_EVENTS_TARGET', '2026-01-07')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['event_type'], 'mops')
        finally:
            db.close()
            safe_remove_test_cache("research_center/test_database_saves_events")

    def test_database_close_is_idempotent(self):
        """Verify ResearchDatabase.close() can be called multiple times without error."""
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("research_center/test_close_idempotent")
        db = None
        try:
            db = ResearchDatabase(tmp / 'stock_research.db')
            db.close()  # First close
            db.close()  # Second close - must not raise
        finally:
            if db is not None:
                db.close()
            safe_remove_test_cache("research_center/test_close_idempotent")


class DateGuardTests(unittest.TestCase):
    def test_date_guard_drops_undated_and_future_sources(self):
        sources = [
            SourceItem(source_id='S001', title='old', url='https://example.com/old', source_level='Level 3', published_date='2026-01-01'),
            SourceItem(source_id='S002', title='future', url='https://example.com/future', source_level='Level 3', published_date='2026-01-08'),
            SourceItem(source_id='S003', title='unknown', url='https://example.com/unknown', source_level='Level 3'),
        ]
        kept, dropped = filter_sources_for_report_date(sources, date(2026, 1, 7))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].title, 'old')
        self.assertEqual(len(dropped), 2)


class ThemeAndValueScanTests(unittest.TestCase):
    def test_theme_profile_known_theme(self):
        profile = _theme_profile('AI伺服器')
        self.assertIn('supply_chain', profile)
        self.assertIn('AI', profile['keywords'])

    def test_value_rerating_score_has_labels_and_components(self):
        score = _value_rerating_score('半導體業', 80, 2500, 35)
        self.assertGreater(score['score'], 50)
        self.assertIn('old_market_label', score)
        self.assertIn('new_market_label', score)
        self.assertIn('revenue_turnaround', score['components'])

    def test_curated_scan_cache_uses_report_date_and_structured_codes(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("research_center/test_curated_scan_cache")
        try:
            original_recent_path = recent_scans.RECENT_SCAN_PATH
            original_curated_recent_path = curated_scan_service.RECENT_SCAN_PATH if hasattr(curated_scan_service, "RECENT_SCAN_PATH") else None
            cache_path = tmp / "recent_scan_results.json"
            recent_scans.RECENT_SCAN_PATH = cache_path
            curated_scan_service.RECENT_SCAN_PATH = cache_path
            try:
                # Create a valid backfill marker so _is_backfill_ready_for_scan returns True
                marker_root = tmp / ".cache" / "backfill"
                marker_dir = marker_root / "2026-05-14"
                marker_dir.mkdir(parents=True, exist_ok=True)
                marker_file = marker_dir / "complete.json"
                marker_file.write_text(json.dumps({
                    "schema_version": 2,
                    "backfill_ready_for_scan": True,
                    "universe_count": 1500,
                    "candidate_count": 100,
                    "chip_candidate_count": 90,
                    "curated_scan_count": 20,
                }, ensure_ascii=False), encoding="utf-8")
                # Patch ROOT_DIR so _is_backfill_ready_for_scan looks in our test cache
                with patch.object(curated_scan_service, 'ROOT_DIR', tmp):
                    recent_scans.save_recent_scan_result(
                        "精選選股",
                        date(2026, 5, 14),
                        "⭐ 精選選股交叉命中報告\n📅 日期：2026-05-14\n2330 台積電",
                        ["2330", "5425"],
                    )
                    record = curated_scan_service.find_cached_curated_scan(date(2026, 5, 14))
                    self.assertIsNotNone(record)
                    self.assertEqual(record["codes"], ["2330", "5425"])
                    self.assertIsNone(curated_scan_service.find_cached_curated_scan(date(2026, 5, 13)))
            finally:
                recent_scans.RECENT_SCAN_PATH = original_recent_path
                if original_curated_recent_path is None:
                    delattr(curated_scan_service, "RECENT_SCAN_PATH")
                else:
                    curated_scan_service.RECENT_SCAN_PATH = original_curated_recent_path
        finally:
            safe_remove_test_cache("research_center/test_curated_scan_cache")

class KnowledgeAndValidationTests(unittest.TestCase):
    def test_company_knowledge_enrichment_marks_covered_and_missing(self):
        rows = [{"code": "2330", "name": "TSMC"}, {"code": "9999", "name": "Missing"}]
        enriched = enrich_company_rows(rows, {"companies": {"2330": {"product_lines": ["晶圓代工"], "customers": ["AI 客戶"]}}})
        self.assertEqual(enriched[0]["company_knowledge"]["status"], "covered")
        self.assertEqual(enriched[1]["company_knowledge"]["status"], "missing")
        summary = theme_knowledge_summary(enriched)
        self.assertEqual(summary["covered_companies"], 1)

    def test_value_cross_validation_scores_missing_evidence_conservatively(self):
        row = {"latest_monthly_revenue": 100, "revenue_yoy": 12, "company_knowledge": {"customers": [], "product_lines": []}}
        validation = build_value_cross_validation(row)
        self.assertLess(validation["verification_score"], 50)
        self.assertTrue(validation["risk_flags"])

    def test_fear_greed_zone(self):
        self.assertEqual(_fear_greed_zone(80), "greed")
        self.assertEqual(_fear_greed_zone(20), "fear")


class OfficialConnectorTests(unittest.TestCase):
    def test_parse_taifex_vix_html(self):
        html = """
        <table><tr><th>交易日期</th><th>臺指選擇權波動率指數</th></tr>
        <tr><td>2026/01/05</td><td>18.25</td></tr>
        <tr><td>2026/01/08</td><td>20.00</td></tr></table>
        """
        rows = parse_taifex_vix_html(html, date(2026, 1, 7))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 18.25)

    def test_parse_twse_institutional_json(self):
        payload = {
            "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"],
            "data": [["外資及陸資", "1,000", "500", "500"]],
        }
        rows = parse_twse_institutional_json(payload)
        self.assertEqual(rows[0]["net_amount"], 500)

    def test_financial_detail_snapshot(self):
        snapshot = financial_detail_snapshot([{"Quarter": "2025Q4", "EPS": 3.1, "gross_margin": 52.0}])
        self.assertEqual(snapshot["status"], "covered")
        self.assertGreater(snapshot["score_points"], 0)

class ReportLookupTests(unittest.TestCase):
    def test_report_lookup_restores_analysis_model_from_report_json(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("research_center/test_report_lookup")
        db = None
        center = None
        try:
            root = tmp
            database_path = root / 'research.db'
            report_root = root / 'reports'

            request = parse_command_text('/research 5425 --deep --model deepseek')
            sources = [SourceItem('S001', 'TWSE', 'https://www.twse.com.tw/', 'Level 1')]
            structured_data = {
                'analysis_model': 'deepseek-v4-pro',
                'analysis_model_choice': 'deepseek',
                'analysis_provider': 'opencode_go',
                'local_scoring': {'scores': []},
            }

            artifacts, report_json = write_report_artifacts(
                report_root,
                request,
                '# 5425 深度研究\n\n## AI 最終推薦買入評分 1～5 分\n- 3\n\n## 資料來源列表\n- [S001] TWSE',
                'summary',
                sources,
                True,
                None,
                structured_data,
            )

            db = ResearchDatabase(database_path)
            db.save_report(request, artifacts, 'summary', sources, True, None)

            # Verify DB write succeeded and DB storage mode
            self.assertTrue(database_path.exists() or getattr(db, "_memory_uri", None) is not None,
                            "DB file must exist or DB must use memory fallback")
            row = db.latest_report(target="5425", report_type="research")
            self.assertIsNotNone(row, "DB should have the saved report")
            self.assertEqual(row["target"], "5425")
            self.assertEqual(row["report_type"], "research")

            # Close file DB before creating center to avoid Windows file lock issues.
            # Keep memory fallback alive so the second DB instance can read the shared in-memory data.
            if getattr(db, "_memory_uri", None) is None:
                db.close()
                db = None

            config = ResearchCenterConfig(
                api_key=None,
                database_path=database_path,
                report_root=report_root,
                minimax_api_key=None,
                serper_api_key=None,
                jina_api_key=None,
                opencode_api_key=None,
            )
            center = ResearchCenter(config)

            # Verify config and DB path consistency
            self.assertEqual(center.config.database_path, database_path,
                             "center.config.database_path must match test database_path")
            self.assertEqual(center.database.path, database_path,
                             "center.database.path must match test database_path")

            # Verify memory URI consistency. File DB uses None; memory fallback must share the same URI.
            db_uri = getattr(db, "_memory_uri", None) if db is not None else None
            center_uri = getattr(center.database, "_memory_uri", None)
            self.assertEqual(db_uri, center_uri,
                             "db and center.database must use the same memory URI")

            # Verify parser resolves correctly
            lookup_request = center.parse("/report 5425 latest")
            self.assertEqual(lookup_request.command, "report")
            self.assertEqual(lookup_request.target, "5425")

            # Verify center.database can find the report
            center_row = center.database.latest_report(target="5425", report_type="research")
            self.assertIsNotNone(center_row, "center.database should find the report")
            self.assertEqual(center_row["target"], "5425")

            result = center.run_text_command('/report 5425 latest')

            self.assertEqual(result.ai_model, 'deepseek-v4-pro')
            self.assertEqual(result.report_json['metadata']['analysis_model'], 'deepseek-v4-pro')
            self.assertEqual(result.report_json['metadata']['analysis_model_choice'], 'deepseek')
            self.assertEqual(result.report_json['metadata']['analysis_provider'], 'opencode_go')
        finally:
            if db is not None:
                db.close()
            if center is not None and hasattr(center, 'database'):
                center.database.close()
            safe_remove_test_cache("research_center/test_report_lookup")


class MemoryFallbackIsolationTests(unittest.TestCase):
    """Test that ResearchDatabase memory fallback uses unique URIs per path."""

    def test_repeated_use_memory_fallback_closes_previous_anchor(self):
        """Verify repeated _use_memory_fallback() closes the previous anchor, not leak."""
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        from research_center.database import ResearchDatabase
        tmp = ensure_test_cache_dir("research_center/test_repeated_memory_fallback")
        db = None
        try:
            db_path = tmp / "test_repeated.db"
            db = ResearchDatabase(db_path)
            db._use_memory_fallback()
            first_anchor = db._memory_anchor
            self.assertIsNotNone(first_anchor, "First call should create anchor")

            # Second call should close previous anchor and create new one
            db._use_memory_fallback()
            second_anchor = db._memory_anchor
            self.assertIsNot(second_anchor, first_anchor, "Second call should replace anchor")
            self.assertIsNotNone(second_anchor, "New anchor must exist")

            # Old anchor should be closed (cannot execute)
            with self.assertRaises(sqlite3.ProgrammingError):
                first_anchor.execute("SELECT 1")
        finally:
            if db is not None:
                db.close()
            safe_remove_test_cache("research_center/test_repeated_memory_fallback")

    def test_same_path_shares_memory_fallback_data(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        from research_center.database import ResearchDatabase
        from research_center.models import CommandRequest, ReportArtifacts
        from datetime import date
        tmp = ensure_test_cache_dir("research_center/test_memory_fallback_same")
        db1 = None
        db2 = None
        try:
            db_path = tmp / "test_shared.db"
            # Force both instances into memory fallback
            db1 = ResearchDatabase(db_path)
            db1._use_memory_fallback()
            db1.init_schema()
            db1.save_report(
                CommandRequest(command='research', raw_text='/research 2330', target='2330', report_date=date(2026, 5, 15)),
                ReportArtifacts('mem_test_1', 'research', Path('test.md'), Path('test.html'), Path('test.json'), Path('test.sources.json')),
                'summary', [], True, None,
            )
            # Second instance with same path should see the same data
            db2 = ResearchDatabase(db_path)
            db2._use_memory_fallback()
            db2.init_schema()
            # Same path must produce same memory URI
            self.assertEqual(db1._memory_uri, db2._memory_uri,
                             "Same path must produce same memory URI")
            row = db2.latest_report(target='2330', report_type='research')
            self.assertIsNotNone(row, "Second instance must see data written by first")
            self.assertEqual(row['target'], '2330')
        finally:
            if db1 is not None:
                db1.close()
            if db2 is not None:
                db2.close()
            safe_remove_test_cache("research_center/test_memory_fallback_same")

    def test_different_paths_isolate_memory_fallback_data(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        from research_center.database import ResearchDatabase
        from research_center.models import CommandRequest, ReportArtifacts
        from datetime import date
        tmp1 = ensure_test_cache_dir("research_center/test_memory_fallback_a")
        tmp2 = ensure_test_cache_dir("research_center/test_memory_fallback_b")
        db1 = None
        db2 = None
        try:
            db_path1 = tmp1 / "test_a.db"
            db_path2 = tmp2 / "test_b.db"
            db1 = ResearchDatabase(db_path1)
            db1._use_memory_fallback()
            db1.init_schema()
            db1.save_report(
                CommandRequest(command='research', raw_text='/research 2330', target='2330', report_date=date(2026, 5, 15)),
                ReportArtifacts('mem_test_2', 'research', Path('test.md'), Path('test.html'), Path('test.json'), Path('test.sources.json')),
                'summary', [], True, None,
            )
            db2 = ResearchDatabase(db_path2)
            db2._use_memory_fallback()
            db2.init_schema()
            # Different paths must produce different memory URIs
            self.assertNotEqual(db1._memory_uri, db2._memory_uri,
                                "Different paths must produce different memory URIs")
            row = db2.latest_report(target='2330', report_type='research')
            self.assertIsNone(row, "Different path must not see data from other path")
        finally:
            if db1 is not None:
                db1.close()
            if db2 is not None:
                db2.close()
            safe_remove_test_cache("research_center/test_memory_fallback_a")
            safe_remove_test_cache("research_center/test_memory_fallback_b")


if __name__ == '__main__':
    unittest.main()


class StructuredCacheIntegrationTests(unittest.TestCase):
    """Test collect_research_data() cache read/write integration."""

    @patch("research_center.data_services.save_research_structured_cache")
    @patch("research_center.data_services.load_research_structured_cache", return_value=None)
    @patch("research_center.data_services.StockDataFetcher")
    @patch("research_center.data_services.build_free_research_sources", return_value={"valuation": {}, "tdcc": {}, "gross_margin_cache": {}, "mops_documents": {}})
    @patch("research_center.data_services.build_chip_backup_snapshot", return_value={})
    @patch("research_center.data_services.build_chip_backup_events", return_value=[])
    @patch("research_center.data_services.resolve_stock_reference")
    def test_collect_research_data_saves_structured_cache(
        self, mock_resolve, mock_chip_events, mock_chip_backup, mock_free_sources, mock_fetcher_cls, mock_load_cache, mock_save_cache,
    ):
        from data_fetcher import StockMeta
        import pandas as pd

        meta = StockMeta(code="5425", symbol="5425.TWO", market="TPEX", name="台半")
        mock_resolve.return_value = meta
        mock_fetcher = MagicMock()
        mock_fetcher.__enter__ = MagicMock(return_value=mock_fetcher)
        mock_fetcher.__exit__ = MagicMock(return_value=False)
        mock_fetcher_cls.return_value = mock_fetcher
        mock_fetcher.resolve_stock.return_value = meta
        mock_fetcher.fetch_price_history.return_value = pd.DataFrame()
        mock_fetcher.fetch_monthly_revenue.return_value = pd.DataFrame()
        mock_fetcher.fetch_quarterly_financials.return_value = pd.DataFrame()
        mock_fetcher.merge_daily_frames.return_value = pd.DataFrame()
        mock_fetcher.build_strategy_summary.return_value = pd.DataFrame()

        request = parse_command_text("/research 5425 --date 2026-05-15")
        result = collect_research_data(request)

        mock_save_cache.assert_called_once()
        call_args = mock_save_cache.call_args
        self.assertEqual(call_args[0][0], "5425")  # stock_code
        self.assertEqual(call_args[0][1], date(2026, 5, 15))  # cache_date
        self.assertIn("stock", call_args[0][2])  # result dict has "stock" key

    @patch("research_center.data_services.load_research_structured_cache")
    @patch("research_center.data_services.resolve_stock_reference")
    def test_collect_research_data_uses_structured_cache(self, mock_resolve, mock_load_cache):
        cached_data = {
            "stock": {"code": "5425", "name": "台半", "symbol": "5425.TWO", "market": "TPEX"},
            "report_date": "2026-05-15",
            "price_data": [],
            "notes": [],
        }
        mock_load_cache.return_value = cached_data

        request = parse_command_text("/research 5425 --date 2026-05-15")
        messages: list[str] = []
        result = collect_research_data(request, progress=messages.append)

        self.assertEqual(result["stock"]["code"], "5425")
        self.assertTrue(any("個股研究：使用投研結構化快取" in msg for msg in messages))
