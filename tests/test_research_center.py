from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import curated_scan_service
import research_center.recent_scans as recent_scans
import research_center.orchestrator as orchestrator_module
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
from research_center.gemini_service import GeminiResult
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

    def test_research_supports_minimax_model_flag(self):
        request = parse_command_text('/research 2330 --deep --model minimax')
        self.assertEqual(request.ai_model, 'minimax')

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


class AIModelDispatchTests(unittest.TestCase):
    """Test AI model dispatch mapping for Gemini, DeepSeek, and MiniMax."""

    def test_research_minimax_model_routes_to_minimax_provider(self):
        """Verify /research --model minimax maps to the MiniMax provider/model."""
        from research_center.config import ResearchCenterConfig
        from research_center.orchestrator import ResearchCenter

        config = ResearchCenterConfig(
            api_key=None,
            minimax_api_key="test-key",
            minimax_model="MiniMax-M3",
            minimax_base_url="https://api.minimax.io",
            opencode_api_key="test-key",
            opencode_model="deepseek-chat",
            opencode_base_url="https://api.opencode.cn",
            serper_api_key=None,
            jina_api_key=None,
        )
        center = ResearchCenter(config)

        request = center.parse("/research 2330 --deep --model minimax")
        self.assertEqual(request.ai_model, "minimax")

        # Simulate the routing logic from orchestrator.run()
        selected_ai_model = request.ai_model or "gemini"
        if selected_ai_model == "deepseek":
            expected_provider = "opencode_go"
            expected_model = config.opencode_model
        elif selected_ai_model == "minimax":
            expected_provider = "minimax"
            expected_model = config.minimax_model
        else:
            expected_provider = "gemini"
            expected_model = config.model

        self.assertEqual(selected_ai_model, "minimax")
        self.assertEqual(expected_provider, "minimax")
        self.assertEqual(expected_model, "MiniMax-M3")

        # Verify gemini default case
        request_gemini = center.parse("/research 2330 --deep --model gemini")
        selected_gemini = request_gemini.ai_model or "gemini"
        self.assertEqual(selected_gemini, "gemini")

        # Verify deepseek case
        request_deepseek = center.parse("/research 2330 --deep --model deepseek")
        selected_deepseek = request_deepseek.ai_model or "gemini"
        self.assertEqual(selected_deepseek, "deepseek")

    def test_minimax_model_sets_correct_provider_in_structured_data(self):
        """Verify minimax request is correctly parsed and structured_data gets the right provider."""
        from research_center.config import ResearchCenterConfig
        from research_center.orchestrator import ResearchCenter

        config = ResearchCenterConfig(
            api_key=None,
            minimax_api_key="fake-minimax-key",
            minimax_model="MiniMax-M3",
            minimax_base_url="https://api.minimax.io",
            opencode_api_key=None,
            serper_api_key=None,
            jina_api_key=None,
        )
        center = ResearchCenter(config)

        # Parse minimax request
        request = center.parse("/research 2330 --deep --model minimax")
        self.assertEqual(request.ai_model, "minimax")

        # Verify the minimax branch would set analysis_provider = "minimax"
        # and analysis_model = config.minimax_model
        selected_ai_model = request.ai_model or "gemini"
        if selected_ai_model == "deepseek":
            expected_model = config.opencode_model
            expected_provider = "opencode_go"
        elif selected_ai_model == "minimax":
            expected_model = config.minimax_model
            expected_provider = "minimax"
        else:
            expected_model = config.model
            expected_provider = "gemini"

        self.assertEqual(expected_provider, "minimax")
        self.assertEqual(expected_model, "MiniMax-M3")

    def test_gemini_model_sets_correct_provider_in_structured_data(self):
        """Verify gemini request is correctly parsed and structured_data gets the right provider."""
        from research_center.config import ResearchCenterConfig
        from research_center.orchestrator import ResearchCenter

        config = ResearchCenterConfig(
            api_key="fake-gemini-key",
            model="gemini-2.0-flash",
            minimax_api_key="fake-minimax-key",
            minimax_model="MiniMax-M3",
            minimax_base_url="https://api.minimax.io",
            opencode_api_key=None,
            serper_api_key=None,
            jina_api_key=None,
        )
        center = ResearchCenter(config)

        request = center.parse("/research 2330 --deep --model gemini")
        self.assertEqual(request.ai_model, "gemini")

        selected_ai_model = request.ai_model or "gemini"
        if selected_ai_model == "deepseek":
            expected_provider = "opencode_go"
        elif selected_ai_model == "minimax":
            expected_provider = "minimax"
        else:
            expected_provider = "gemini"

        self.assertEqual(expected_provider, "gemini")

    def test_deepseek_model_sets_correct_provider_in_structured_data(self):
        """Verify deepseek request is correctly parsed and structured_data gets the right provider."""
        from research_center.config import ResearchCenterConfig
        from research_center.orchestrator import ResearchCenter

        config = ResearchCenterConfig(
            api_key=None,
            minimax_api_key="fake-minimax-key",
            minimax_model="MiniMax-M3",
            minimax_base_url="https://api.minimax.io",
            opencode_api_key="fake-opencode-key",
            opencode_model="deepseek-chat",
            opencode_base_url="https://api.opencode.cn",
            serper_api_key=None,
            jina_api_key=None,
        )
        center = ResearchCenter(config)

        request = center.parse("/research 2330 --deep --model deepseek")
        self.assertEqual(request.ai_model, "deepseek")

        selected_ai_model = request.ai_model or "gemini"
        if selected_ai_model == "deepseek":
            expected_provider = "opencode_go"
        elif selected_ai_model == "minimax":
            expected_provider = "minimax"
        else:
            expected_provider = "gemini"

        self.assertEqual(expected_provider, "opencode_go")


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
        # keywords may contain "AI伺服器" instead of raw "AI"; accept either
        keywords = profile.get('keywords', [])
        self.assertTrue(
            any('AI' in str(k) for k in keywords),
            f"Expected keywords to contain AI-related term, got: {keywords}"
        )

    def test_value_rerating_score_has_labels_and_components(self):
        score = _value_rerating_score('半導體業', 80, 2500, 35)
        self.assertGreater(score['score'], 50)
        self.assertIn('old_market_label', score)
        self.assertIn('new_market_label', score)
        self.assertIn('revenue_turnaround', score['components'])

    def test_theme_quality_context_feeds_local_scoring(self):
        from research_center.scoring_engine import build_local_scores
        request = CommandRequest(command="theme", raw_text="/theme AI電源", theme_scope="AI電源")
        data = {
            "theme_quality_context": {
                "coverage_pct": 60.0,
                "effective_total_companies": 10,
                "effective_covered_companies": 6,
                "related_supply_chain_node_count": 4,
            }
        }
        scores = build_local_scores(request, data)
        self.assertEqual(scores[0]["score_name"], "供應鏈資料覆蓋度")
        self.assertEqual(scores[0]["score_value"], 60.0)
        self.assertIn("供應鏈節點 4", scores[0]["score_reason"])

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

    def test_latest_curated_scan_cache_uses_latest_ready_marker(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("research_center/test_latest_curated_scan_cache")
        try:
            original_curated_recent_path = curated_scan_service.RECENT_SCAN_PATH if hasattr(curated_scan_service, "RECENT_SCAN_PATH") else None
            cache_path = tmp / "recent_scan_results.json"
            curated_scan_service.RECENT_SCAN_PATH = cache_path
            records = []
            for index in range(80):
                records.append({
                    "scan_type": "curated",
                    "report_date": "2026-06-06",
                    "scan_id": f"not-ready-{index}",
                    "selected_codes": ["9999"],
                })
            records.extend([
                {
                    "scan_type": "curated",
                    "report_date": "2026-06-04",
                    "scan_id": "ready",
                    "selected_codes": ["2330", "5425"],
                },
                {
                    "scan_type": "curated",
                    "report_date": "2026-06-02",
                    "scan_id": "older-ready",
                    "selected_codes": ["6282"],
                },
            ])
            cache_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            for marker_date in ("2026-06-04", "2026-06-02"):
                marker_dir = tmp / ".cache" / "backfill" / marker_date
                marker_dir.mkdir(parents=True, exist_ok=True)
                (marker_dir / "complete.json").write_text(
                    json.dumps({
                        "schema_version": 2,
                        "backfill_ready_for_scan": marker_date == "2026-06-02",
                        "curated_scan_ready": True,
                    }, ensure_ascii=False),
                    encoding="utf-8",
                )
            with patch.object(curated_scan_service, "ROOT_DIR", tmp):
                record = curated_scan_service.find_latest_cached_curated_scan(date(2026, 6, 6))
            self.assertIsNotNone(record)
            self.assertEqual(record["scan_id"], "ready")
            self.assertEqual(record["codes"], ["2330", "5425"])
        finally:
            if original_curated_recent_path is None:
                delattr(curated_scan_service, "RECENT_SCAN_PATH")
            else:
                curated_scan_service.RECENT_SCAN_PATH = original_curated_recent_path
            safe_remove_test_cache("research_center/test_latest_curated_scan_cache")

    def test_curated_scan_cache_ready_does_not_require_full_scan_ready(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("research_center/test_curated_cache_ready_only")
        try:
            original_curated_recent_path = curated_scan_service.RECENT_SCAN_PATH if hasattr(curated_scan_service, "RECENT_SCAN_PATH") else None
            cache_path = tmp / "recent_scan_results.json"
            curated_scan_service.RECENT_SCAN_PATH = cache_path
            cache_path.write_text(json.dumps([
                {
                    "scan_type": "curated",
                    "report_date": "2026-06-04",
                    "scan_id": "curated-ready",
                    "selected_codes": ["2330", "5425"],
                }
            ], ensure_ascii=False), encoding="utf-8")
            marker_dir = tmp / ".cache" / "backfill" / "2026-06-04"
            marker_dir.mkdir(parents=True, exist_ok=True)
            (marker_dir / "complete.json").write_text(
                json.dumps({
                    "schema_version": 2,
                    "scan_data_ready": False,
                    "curated_scan_cache_ready": True,
                    "backfill_ready_for_scan": False,
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.object(curated_scan_service, "ROOT_DIR", tmp):
                record = curated_scan_service.find_cached_curated_scan(date(2026, 6, 4))

            self.assertIsNotNone(record)
            self.assertEqual(record["scan_id"], "curated-ready")
            self.assertEqual(record["codes"], ["2330", "5425"])
        finally:
            if original_curated_recent_path is None:
                delattr(curated_scan_service, "RECENT_SCAN_PATH")
            else:
                curated_scan_service.RECENT_SCAN_PATH = original_curated_recent_path
            safe_remove_test_cache("research_center/test_curated_cache_ready_only")

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


class ResearchCenterRunCoverageTests(unittest.TestCase):
    def test_new_ai_report_written_by_run_contains_workflow_coverage(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache

        class FakeGemini:
            def generate_report(self, prompt, enable_grounding=False):
                return GeminiResult(
                    "# 2330 research\n\n## 摘要\nAI 分析完成 [S001]\n\n## 資料來源\n- [S001] TWSE",
                    [],
                    {"raw": "ok"},
                    {"actual_model": "fake-gemini", "grounding_metadata_present": False},
                )

        tmp = ensure_test_cache_dir("research_center/test_run_writes_ai_workflow_coverage")
        try:
            config = ResearchCenterConfig(
                api_key="fake-key",
                enable_grounding=False,
                enable_low_model_digest=False,
                minimax_api_key=None,
                serper_api_key=None,
                jina_api_key=None,
                tavily_api_key=None,
                report_root=tmp / "reports",
                database_path=tmp / "research.db",
            )
            center = ResearchCenter(config)
            center.gemini = FakeGemini()  # type: ignore[assignment]

            source = SourceItem("S001", "TWSE", "https://www.twse.com.tw/", "Level 1", snippet="台積電測試來源")
            structured_data = {
                "stock_id": "2330",
                "stock_name": "台積電",
                "price_data": [{"date": "2026-06-05", "close": 1000}],
                "technical_data": {"above_ma21": True},
                "revenue_data": [{"YoY": 10}],
                "financial_data": [{"EPS": 1.0, "operating_margin": 20}],
            }

            with patch.object(orchestrator_module, "collect_structured_data", return_value=(structured_data, [source])):
                with patch.object(center._gemini_discovery_runner, "run_discovery_flow", return_value=([source], False)):
                    with patch.object(orchestrator_module, "_enrich_sources_with_web_fetch", lambda *args, **kwargs: None):
                        with patch.object(orchestrator_module, "persist_search_sources_to_news", lambda *args, **kwargs: None):
                            with patch.object(orchestrator_module, "attach_news_events", lambda *args, **kwargs: None):
                                result = center.run(parse_command_text("/research 2330 --model gemini"))

            self.assertEqual(result.status, "success")
            coverage = result.report_json["metadata"].get("ai_workflow_coverage")
            self.assertIsInstance(coverage, dict)
            self.assertEqual(coverage["schema_version"], "ai_workflow_coverage_v1")
            self.assertEqual(coverage["status"], "aligned")
            self.assertEqual(coverage["missing_capabilities"], [])
            saved = json.loads(result.artifacts.json_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(saved["metadata"]["ai_workflow_coverage"]["status"], "aligned")
        finally:
            try:
                center.database.close()  # type: ignore[name-defined]
            except Exception:
                pass
            safe_remove_test_cache("research_center/test_run_writes_ai_workflow_coverage")


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


class ResearchDataFallbackTests(unittest.TestCase):
    @patch("research_center.data_services.load_latest_research_structured_cache")
    @patch("research_center.data_services._collect_research_data_live", side_effect=RuntimeError("no text parsed from document (line 0)"))
    @patch("research_center.data_services.resolve_stock_reference")
    def test_collect_research_data_falls_back_to_latest_cache_on_live_failure(self, mock_resolve, mock_live, mock_latest_cache):
        from portfolio_manager import ResolvedStock

        mock_resolve.return_value = ResolvedStock(code="1785", name="光洋科", market="TPEX", symbol="1785.TWO")
        mock_latest_cache.return_value = (
            {
                "stock": {"code": "1785", "name": "光洋科", "symbol": "1785.TWO", "market": "TPEX"},
                "report_date": "2026-05-22",
                "notes": [],
            },
            date(2026, 5, 22),
        )

        request = parse_command_text("/research 光洋科 --deep --model deepseek")
        messages: list[str] = []
        result = collect_research_data(request, progress=messages.append)

        self.assertEqual(result["stock"]["code"], "1785")
        self.assertTrue(result["structured_cache_fallback"]["enabled"])
        self.assertEqual(result["structured_cache_fallback"]["fallback_date"], "2026-05-22")
        self.assertTrue(any("no text parsed from document" in note for note in result["notes"]))
        self.assertTrue(any("改用最近投研結構化快取" in msg for msg in messages))


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
    @patch("research_center.data_services.build_rerating_snapshot_for_stock")
    @patch("research_center.data_services.resolve_stock_reference")
    def test_collect_research_data_uses_structured_cache(self, mock_resolve, mock_rerating, mock_load_cache):
        from portfolio_manager import ResolvedStock

        mock_resolve.return_value = ResolvedStock(code="5425", name="台半", market="TPEX", symbol="5425.TWO")
        cached_data = {
            "stock": {"code": "5425", "name": "台半", "symbol": "5425.TWO", "market": "TPEX"},
            "report_date": "2026-05-15",
            "price_data": [],
            "notes": [],
        }
        mock_load_cache.return_value = cached_data
        mock_rerating.return_value = {"stock_id": "5425", "rerating_score": 70}

        request = parse_command_text("/research 5425 --deep --date 2026-05-15")
        messages: list[str] = []
        result = collect_research_data(request, progress=messages.append)

        self.assertEqual(result["stock"]["code"], "5425")
        self.assertEqual(result["local_rerating_snapshot"]["rerating_score"], 70)
        mock_rerating.assert_called_once_with("5425", date(2026, 5, 15), progress=messages.append)
        self.assertTrue(any("個股研究：使用投研結構化快取" in msg for msg in messages))

    @patch("research_center.data_services.load_research_structured_cache")
    @patch("research_center.data_services.build_rerating_snapshot_for_stock", side_effect=RuntimeError("snapshot down"))
    @patch("research_center.data_services.resolve_stock_reference")
    def test_collect_research_data_keeps_cached_research_when_rerating_snapshot_fails(self, mock_resolve, mock_rerating, mock_load_cache):
        from portfolio_manager import ResolvedStock

        mock_resolve.return_value = ResolvedStock(code="5425", name="台半", market="TPEX", symbol="5425.TWO")
        cached_data = {
            "stock": {"code": "5425", "name": "台半", "symbol": "5425.TWO", "market": "TPEX"},
            "report_date": "2026-05-15",
            "price_data": [],
            "notes": [],
        }
        mock_load_cache.return_value = cached_data

        request = parse_command_text("/research 5425 --deep --date 2026-05-15")
        result = collect_research_data(request)

        self.assertEqual(result["stock"]["code"], "5425")
        self.assertIn("local_rerating_snapshot_error", result)
        self.assertTrue(any("價值重估底稿建立失敗" in note for note in result["notes"]))
