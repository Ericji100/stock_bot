from __future__ import annotations

import unittest
from pathlib import Path

from research_center.command_parser import parse_command_text
from research_center.database import ResearchDatabase
from research_center.gemini_service import build_prompt
from research_center.models import CommandRequest, ReportArtifacts, SourceItem
from research_center.prompt_registry import _prompt_structured_data, prompt_metadata


class PromptContractTests(unittest.TestCase):
    def test_research_score_prompt_loads_original_scoring_drafts(self):
        request = parse_command_text('/research 2330 --score')
        prompt = build_prompt(
            request,
            structured_data={'stock': {'code': '2330', 'name': 'TSMC'}, 'strategy_summary': []},
            source_list=[SourceItem(source_id='S001', title='MOPS', url='https://mops.twse.com.tw/', source_level='Level 1')],
        )
        self.assertIn('股票量化評分標準原稿', prompt)
        self.assertIn('股票標籤重估模型原稿', prompt)
        self.assertIn('/research', prompt)
        self.assertIn('資料來源', prompt)
        # 新版 base.md 簡化規則，無「不得捏造」但有「不得直接給出保證獲利」
        self.assertIn('不得直接給出保證獲利', prompt)
        self.assertIn('本地量化底稿', prompt)
        self.assertIn('不是最終評分', prompt)
        self.assertIn('AI 最終投研評分', prompt)
        metadata = prompt_metadata(request)
        self.assertEqual(metadata['template'], 'research_score.md')
        self.assertIn('股票量化評分標準.md', metadata['scoring_files'])

    def test_value_scan_prompt_uses_top_10_default_and_rerating_rules(self):
        request = parse_command_text('/value_scan')
        prompt = build_prompt(
            request,
            structured_data={'candidate_pool': '精選選股', 'candidates': []},
            source_list=[],
        )
        self.assertIn('前 10 名', prompt)
        self.assertIn('股票標籤重估模型原稿', prompt)
        self.assertIn('財務硬指標', prompt)
        self.assertIn('價值重估掃描報告', prompt)
        # 新結構：source_quality_rules.md 的規則（非舊版 base.md 的 rule）
        self.assertIn('資料來源可信度', prompt)

    def test_macro_and_report_defaults_follow_latest_spec(self):
        macro = parse_command_text('/macro')
        self.assertEqual(macro.market_scope, '全球')
        self.assertEqual(macro.region_scope, 'global')

        recent = parse_command_text('/report')
        self.assertEqual(recent.target, '__recent__')

        latest = parse_command_text('/report latest')
        self.assertEqual(latest.target, 'latest')

        latest_stock = parse_command_text('/report 6217 latest')
        self.assertEqual(latest_stock.target, '6217')

        latest_theme = parse_command_text('/report theme AI伺服器 latest')
        self.assertEqual(latest_theme.target, 'theme AI伺服器')


    def test_output_format_flags_are_parsed(self):
        request = parse_command_text('/research 2330 --no-html --no-json')
        self.assertEqual(request.output_formats, ('md',))
        with self.assertRaises(Exception):
            parse_command_text('/research 2330 --no-html --no-json --no-md')

    def test_database_latest_report_can_filter_by_date(self):
        from tests.test_cache_utils import ensure_test_cache_dir, safe_remove_test_cache
        tmp = ensure_test_cache_dir("prompt_contracts/test_database_latest_report")
        try:
            db = ResearchDatabase(tmp / 'stock_research.db')
            sources: list[SourceItem] = []
            old_request = CommandRequest(command='research', raw_text='/research 2330 --date 2026-01-01', target='2330', report_date=__import__('datetime').date(2026, 1, 1))
            new_request = CommandRequest(command='research', raw_text='/research 2330 --date 2026-01-02', target='2330', report_date=__import__('datetime').date(2026, 1, 2))
            old_artifacts = ReportArtifacts('old', 'research', Path('old.md'), Path('old.html'), Path('old.json'), Path('old.sources.json'))
            new_artifacts = ReportArtifacts('new', 'research', Path('new.md'), Path('new.html'), Path('new.json'), Path('new.sources.json'))
            db.save_report(old_request, old_artifacts, 'old summary', sources, False, None)
            db.save_report(new_request, new_artifacts, 'new summary', sources, False, None)
            row = db.latest_report(target='2330', report_type='research', report_date='2026-01-01')
            self.assertIsNotNone(row)
            self.assertEqual(row['report_id'], 'old')
        finally:
            safe_remove_test_cache("prompt_contracts/test_database_latest_report")

    def test_research_deep_prompt_requires_ai_final_scoring_headings(self):
        request = parse_command_text('/research 2330 --deep')
        prompt = build_prompt(
            request,
            structured_data={'stock': {'code': '2330', 'name': 'TSMC'}},
            source_list=[],
        )
        self.assertIn('AI 最終推薦買入評分 1～5 分', prompt)
        self.assertIn('AI 最終財務與題材評分', prompt)
        self.assertIn('AI 最終飆股基因評分', prompt)
        self.assertIn('AI 最終價值重估評分', prompt)

    def test_prompts_define_inference_bonus_categories(self):
        request = parse_command_text('/research 2330 --score')
        prompt = build_prompt(
            request,
            structured_data={'stock': {'code': '2330', 'name': 'TSMC'}, 'strategy_summary': []},
            source_list=[],
        )
        required_terms = [
            '已驗證加分',
            '推論型加分',
            '情緒型參考',
            '財務硬指標',
            '題材想像空間',
            '價值重估潛力',
            '飆股基因觀察',
            '尚待驗證',
            '推論型加分比重偏高',
        ]
        for term in required_terms:
            self.assertIn(term, prompt)

    def test_value_scan_prompt_defines_inference_bonus_limits(self):
        request = parse_command_text('/value_scan 精選選股 --deep --top 30')
        prompt = build_prompt(
            request,
            structured_data={'candidate_pool': '精選選股', 'candidates': []},
            source_list=[],
        )
        self.assertIn('AI 最終重估判斷', prompt)
        self.assertIn('推論型加分', prompt)
        self.assertIn('情緒型參考', prompt)
        self.assertIn('財務硬指標', prompt)
        self.assertIn('尚待驗證', prompt)


    def test_research_prompt_contains_topic_library_rules(self):
        """Research prompt must contain topic library usage restrictions."""
        request = parse_command_text('/research 2330 --score')
        prompt = build_prompt(
            request,
            structured_data={'stock': {'code': '2330', 'name': 'TSMC'}, 'strategy_summary': []},
            source_list=[],
        )
        # Topic library rules must appear in the prompt
        self.assertIn('topic_context', prompt)
        self.assertIn('題材庫資料僅供背景參考', prompt)
        self.assertIn('不得僅因題材庫標記', prompt)
        self.assertIn('重新驗證', prompt)
        self.assertIn('以最新證據為準', prompt)

    def test_value_scan_prompt_contains_topic_library_rules(self):
        """Value scan prompt must contain topic library usage restrictions."""
        request = parse_command_text('/value_scan --deep')
        prompt = build_prompt(
            request,
            structured_data={'candidate_pool': '精選選股', 'candidates': []},
            source_list=[],
        )
        # Topic library rules must appear in the prompt
        self.assertIn('topic_context', prompt)
        self.assertIn('不得只因某股票命中熱門題材就給高分', prompt)
        self.assertIn('重估分數仍需依財報', prompt)

    def test_theme_prompt_contains_topic_library_rules(self):
        """Theme prompt must contain topic library usage restrictions."""
        request = parse_command_text('/theme AI伺服器 --deep')
        prompt = build_prompt(
            request,
            structured_data={'theme': 'AI伺服器', 'topic_context': {'matched_topics': []}},
            source_list=[],
        )
        self.assertIn('topic_context', prompt)
        self.assertIn('題材庫資料僅供背景參考', prompt)
        self.assertIn('不得只因題材庫已有相近題材', prompt)
        self.assertIn('重新驗證', prompt)
        self.assertIn('以最新證據為準', prompt)

    def test_research_structured_data_includes_topic_context(self):
        """Research structured prompt data should include topic_context if present."""
        request = parse_command_text('/research 2330 --deep')
        structured = _prompt_structured_data(
            request,
            {'stock': {'code': '2330'}, 'topic_context': {'matched_topics': []}},
        )
        data = __import__('json').loads(structured)
        self.assertIn('topic_context', data)

    def test_value_scan_structured_data_includes_topic_context(self):
        """Value scan structured prompt data should include topic_context if present."""
        request = parse_command_text('/value_scan --deep')
        structured = _prompt_structured_data(
            request,
            {
                'ai_candidate_evidence_pack': [],
                'topic_context': {'candidate_topic_map': []},
            },
        )
        data = __import__('json').loads(structured)
        self.assertIn('topic_context', data)

    def test_theme_structured_data_includes_topic_context(self):
        """Theme structured prompt data should include topic_context if present."""
        request = parse_command_text('/theme AI伺服器 --deep')
        structured = _prompt_structured_data(
            request,
            {'theme': 'AI伺服器', 'topic_context': {'matched_topics': []}},
        )
        data = __import__('json').loads(structured)
        self.assertIn('topic_context', data)


class NewsDiscoveryTests(unittest.TestCase):
    """Test that /news uses Taiwan-finance-specific discovery queries."""

    def test_news_command_uses_taiwan_finance_discovery_tasks(self):
        """news command must use Taiwan-finance queries, not generic 'news latest'."""
        from research_center.prompt_registry import _grounding_discovery_tasks
        from research_center.models import CommandRequest

        request = CommandRequest(
            command="news", raw_text="/news refresh", target="台股財經新聞",
            target_type="news", mode="normal", source_only=False, score=False,
            brief=False, top=None, ai_model="gemini", report_date=None,
            output_formats=("md",), user_id="", created_at=None,
        )
        tasks = _grounding_discovery_tasks(request, {})

        # Flatten all query items
        all_items: list[str] = []
        for task in tasks:
            for q in task.get("queries", []):
                if isinstance(q, dict):
                    all_items.extend(q.get("items", []))
                elif isinstance(q, str):
                    all_items.append(q)

        items_lower = [it.lower() for it in all_items]

        # Must contain Taiwan keywords
        taiwan_keywords = {"台股", "台灣", "taiwan", "財經", "半導體", "股票"}
        has_taiwan = any(any(kw in it for kw in taiwan_keywords) for it in items_lower)
        self.assertTrue(has_taiwan, f"Discovery tasks must contain Taiwan keywords. Items: {all_items}")

        # Must NOT contain generic English news phrases (without Taiwan indicator)
        generic_phrases = ["latest news", "breaking news", "world news", "today news"]
        taiwan_indicators = {"taiwan", "台股", "台灣"}
        for item in items_lower:
            has_taiwan_ind = any(ind in item for ind in taiwan_indicators)
            for phrase in generic_phrases:
                self.assertFalse(
                    phrase in item and not has_taiwan_ind,
                    f"Query '{item}' contains generic phrase '{phrase}' without Taiwan indicator",
                )

    def test_news_discovery_tasks_not_empty(self):
        """news command must return non-empty discovery tasks."""
        from research_center.prompt_registry import _grounding_discovery_tasks
        from research_center.models import CommandRequest

        request = CommandRequest(
            command="news", raw_text="/news refresh", target="台股財經新聞",
            target_type="news", mode="normal", source_only=False, score=False,
            brief=False, top=None, ai_model="gemini", report_date=None,
            output_formats=("md",), user_id="", created_at=None,
        )
        tasks = _grounding_discovery_tasks(request, {})
        self.assertTrue(len(tasks) > 0, "news discovery tasks must not be empty")


class SearchQueryOptimizationTests(unittest.TestCase):
    def test_theme_radar_has_dedicated_discovery_tasks(self):
        from research_center.prompt_registry import _grounding_discovery_tasks

        request = parse_command_text("/theme_radar --days 7")
        tasks = _grounding_discovery_tasks(request, {})
        labels = {str(task.get("label")) for task in tasks}
        self.assertIn("熱門題材與資金輪動", labels)
        self.assertIn("題材催化與新聞爆量", labels)
        self.assertIn("退燒題材與反證", labels)
        items: list[str] = []
        excludes: list[str] = []
        for task in tasks:
            excludes.extend(str(item) for item in task.get("exclude", []))
            for group in task.get("queries", []):
                if isinstance(group, dict):
                    items.extend(group.get("items", []))
                elif isinstance(group, str):
                    items.append(group)
        flat = "\n".join(items)
        self.assertIn("site:", flat)
        self.assertIn("上市櫃 今日 漲停 量增 題材 族群 輪動", flat)
        self.assertIn("TWSE TPEx", flat)
        self.assertNotIn("market 2026", flat.lower())
        self.assertIn("farmers market", "\n".join(excludes))

    def test_sector_strength_queries_are_taiwan_stock_specific(self):
        from research_center.prompt_registry import _grounding_discovery_tasks

        request = parse_command_text("/sector_strength --source radar")
        tasks = _grounding_discovery_tasks(
            request,
            {"sector_rankings": [{"sector": "半導體業"}, {"sector": "電子零組件業"}]},
        )
        all_items: list[str] = []
        for task in tasks:
            for group in task.get("queries", []):
                if isinstance(group, dict):
                    all_items.extend(group.get("items", []))
                elif isinstance(group, str):
                    all_items.append(group)

        joined = "\n".join(all_items)
        self.assertIn("台股", joined)
        self.assertIn("半導體業 電子零組件業", joined)
        self.assertIn("TWSE TPEx", joined)
        self.assertNotIn("market 2026", joined.lower())

    def test_value_scan_queries_are_batched_by_candidates(self):
        from research_center.prompt_registry import _grounding_discovery_tasks

        request = parse_command_text("/value_scan 精選選股 --deep --top 30")
        candidates = [{"code": f"23{i:02d}", "name": f"測試{i}"} for i in range(12)]
        tasks = _grounding_discovery_tasks(request, {"ai_candidates": candidates})
        all_items: list[str] = []
        for task in tasks:
            for group in task.get("queries", []):
                if isinstance(group, dict):
                    all_items.extend(group.get("items", []))
        batched_items = [item for item in all_items if "2300" in item and "2304" not in item]
        self.assertTrue(batched_items)
        self.assertTrue(any("2304" in item for item in all_items))
        self.assertFalse(any("2311" in item and "2300" in item for item in all_items))

    def test_news_queries_include_recency_and_event_terms(self):
        from research_center.news_service import build_news_discovery_queries

        tasks = build_news_discovery_queries("latest")
        all_items: list[str] = []
        for task in tasks:
            for group in task.get("queries", []):
                if isinstance(group, dict):
                    all_items.extend(group.get("items", []))
        joined = "\n".join(all_items)
        self.assertIn("近24小時", joined)
        self.assertIn("法說會", joined)
        self.assertIn("月營收", joined)


class PromptTemplateStructureTests(unittest.TestCase):
    """Test that all prompt templates exist in the new prompt/ directory."""

    PROMPT_ROOT = Path(__file__).parent.parent / "prompt"

    def test_prompt_base_exists(self):
        self.assertTrue(
            (self.PROMPT_ROOT / "base" / "base.md").exists(),
            "prompt/base/base.md must exist",
        )

    def test_necessary_templates_not_empty(self):
        """Check that all necessary templates exist and have non-empty content."""
        necessary_templates = [
            "base/base.md",
            "report/research_summary.md",
            "report/research_score.md",
            "report/research_deep.md",
            "report/macro.md",
            "report/theme.md",
            "report/theme_deep.md",
            "report/value_scan.md",
            "report/source_only_summary.md",
            "discovery/discovery_task.md",
            "rules/report_context.md",
            "rules/local_scoring_and_ai_final_scoring.md",
            "rules/historical_rules.md",
            "rules/discovery_research.md",
            "rules/discovery_macro.md",
            "rules/discovery_theme.md",
            "rules/discovery_value_scan.md",
        ]
        for path_str in necessary_templates:
            with self.subTest(path=path_str):
                full_path = self.PROMPT_ROOT / path_str
                self.assertTrue(
                    full_path.exists(),
                    f"Template {path_str} must exist",
                )
                content = full_path.read_text(encoding="utf-8-sig")
                self.assertTrue(
                    content.strip(),
                    f"Template {path_str} must not be empty",
                )

    def test_prompt_report_templates_exist(self):
        for name in [
            "research_summary.md",
            "research_score.md",
            "research_deep.md",
            "macro.md",
            "theme.md",
            "theme_deep.md",
            "value_scan.md",
            "source_only_summary.md",
        ]:
            with self.subTest(name=name):
                self.assertTrue(
                    (self.PROMPT_ROOT / "report" / name).exists(),
                    f"prompt/report/{name} not found",
                )

    def test_prompt_discovery_task_exists(self):
        self.assertTrue(
            (self.PROMPT_ROOT / "discovery" / "discovery_task.md").exists(),
        )

    def test_prompt_rules_templates_exist(self):
        for name in [
            "report_context.md",
            "local_scoring_and_ai_final_scoring.md",
            "historical_rules.md",
            "discovery_research.md",
            "discovery_macro.md",
            "discovery_theme.md",
            "discovery_value_scan.md",
        ]:
            with self.subTest(name=name):
                self.assertTrue(
                    (self.PROMPT_ROOT / "rules" / name).exists(),
                    f"prompt/rules/{name} not found",
                )


class PromptBuildIntegrationTests(unittest.TestCase):
    """Test that build_prompt_from_request produces valid prompts with new templates."""

    def test_research_2330_prompt_contains_required_sections(self):
        request = parse_command_text("/research 2330")
        prompt = build_prompt(
            request,
            structured_data={"stock": {"code": "2330", "name": "TSMC"}},
            source_list=[SourceItem(source_id="S001", title="MOPS", url="https://mops.twse.com.tw/", source_level="Level 1", published_date="2026-05-01")],
        )
        self.assertIn("指令 JSON", prompt)
        self.assertIn("結構化資料", prompt)
        self.assertIn("來源清單", prompt)
        self.assertIn("S001", prompt)

    def test_research_2330_deep_prompt_contains_local_scoring_rules(self):
        request = parse_command_text("/research 2330 --deep")
        prompt = build_prompt(
            request,
            structured_data={"stock": {"code": "2330", "name": "TSMC"}},
            source_list=[],
        )
        self.assertIn("本地量化底稿", prompt)
        self.assertIn("AI 最終投研評分", prompt)
        self.assertIn("推論型加分", prompt)

    def test_macro_global_prompt_builds(self):
        request = parse_command_text("/macro 全球")
        prompt = build_prompt(
            request,
            structured_data={"market_score": {"total": 50}},
            source_list=[],
        )
        self.assertIn("指令 JSON", prompt)
        self.assertIn("結構化資料", prompt)

    def test_theme_ai_prompt_builds(self):
        request = parse_command_text("/theme AI伺服器")
        prompt = build_prompt(
            request,
            structured_data={"matched_companies": []},
            source_list=[],
        )
        self.assertIn("AI伺服器", prompt)
        self.assertIn("指令 JSON", prompt)
        self.assertIn("來源資料可能已由程式做題材相關性精選", prompt)
        self.assertIn("每家公司至少要標示", prompt)

    def test_value_scan_deep_prompt_builds(self):
        request = parse_command_text("/value_scan 精選選股 --deep --top 10")
        prompt = build_prompt(
            request,
            structured_data={"candidate_pool": "精選選股", "candidates": [], "top_n": 10},
            source_list=[],
        )
        self.assertIn("指令 JSON", prompt)
        self.assertIn("結構化資料", prompt)

    def test_value_scan_prompt_contains_ai_candidate_evidence_pack(self):
        """驗證 value_scan prompt 包含 ai_candidate_evidence_pack 完整欄位（不做 [:22000] 截斷）。"""
        from research_center.prompt_registry import build_prompt_from_request

        request = parse_command_text("/value_scan 精選選股 --deep")
        structured_data = {
            "candidate_pool": "精選選股",
            "candidates": [{"code": "2330", "name": "台積電"}],
            "ai_candidates": [{"code": "2330", "name": "台積電", "rerating_score": 85.0}],
            "ai_candidate_evidence_pack": [
                {
                    "code": "2330",
                    "name": "台積電",
                    "old_market_label": "低價",
                    "new_market_label": "中高價",
                    "rerating_score": 85.0,
                    "verification_score": 80.0,
                    "financial_detail": {"Q1": 100, "Q2": 110},
                    "gross_margin_cache": {"gross_margin": 50.0},
                    "chip_backup_summary": {"top3_holders": ["A", "B", "C"], "holding_ratio": 60.0},
                    "source_events": [{"source": "mops", "event": "法人報告"}],
                    "cross_validation": {"verification_score": 80.0, "tdcc_score": 75.0},
                    "missing_data_status": None,
                }
            ],
            "top_n": 30,
            "total_candidate_count": 1,
            "ai_candidate_limit": 30,
        }
        prompt = build_prompt_from_request(
            request,
            structured_data,
            [SourceItem(source_id="S001", title="TWSE", url="https://www.twse.com.tw/", source_level="Level 1")],
        )
        # 驗證關鍵欄位都在 prompt 中
        self.assertIn("ai_candidate_evidence_pack", prompt)
        self.assertIn("financial_detail", prompt)
        self.assertIn("gross_margin_cache", prompt)
        self.assertIn("chip_backup_summary", prompt)
        self.assertIn("old_market_label", prompt)
        self.assertIn("new_market_label", prompt)
        self.assertIn("source_events", prompt)
        self.assertIn("company_knowledge_update_status", prompt)
        self.assertIn("公司知識庫自動補全規則", prompt)

    def test_research_deep_prompt_contains_local_rerating_snapshot(self):
        """驗證 /research --deep prompt 包含 local_rerating_snapshot（research 專用 pack）。"""
        from research_center.prompt_registry import build_prompt_from_request

        request = parse_command_text("/research 2330 --deep")
        structured_data = {
            "stock": {"code": "2330", "name": "台積電"},
            "local_rerating_snapshot": {"score": 85, "label": "中高價"},
            "local_scoring": {"total": 80, "components": []},
            "technical_data": {},
            "revenue_data": [],
        }
        prompt = build_prompt_from_request(request, structured_data, [])
        self.assertIn("local_rerating_snapshot", prompt)
        self.assertIn("local_scoring", prompt)
        self.assertIn("company_knowledge", prompt)
        self.assertIn("公司知識庫自動補全規則", prompt)

    def test_macro_deep_prompt_contains_quantitative_market_and_fear_greed(self):
        """驗證 /macro --deep prompt 包含 quantitative_market、fear_greed（macro 專用 pack）。"""
        from research_center.prompt_registry import build_prompt_from_request

        request = parse_command_text("/macro 全球 --deep")
        structured_data = {
            "market_scope": "全球",
            "quantitative_market": {"VIX": 18.5},
            "fear_greed": {"score": 65, "label": "中立"},
            "industry_flow": {},
            "market_score": {},
        }
        prompt = build_prompt_from_request(request, structured_data, [])
        self.assertIn("quantitative_market", prompt)
        self.assertIn("fear_greed", prompt)

    def test_value_scan_discovery_uses_ai_candidates_not_candidates(self):
        """驗證 /value_scan discovery tasks 使用 ai_candidates（不只是 candidates）。"""
        from research_center.prompt_registry import build_grounding_discovery_prompts

        request = parse_command_text("/value_scan 精選選股 --deep")
        structured_data = {
            "candidate_pool": "精選選股",
            # candidates only has 5 entries with code "fake1"..."fake5"
            "candidates": [{"code": f"fake{i}"} for i in range(1, 6)],
            # ai_candidates has 15 entries, first 10 have codes "real0"..."real9"
            "ai_candidates": [{"code": f"real{i}"} for i in range(15)],
        }
        prompts = build_grounding_discovery_prompts(request, structured_data, [])
        focus_text = prompts[0]["prompt"] if prompts else ""
        # Should use ai_candidates (15 entries), so focus codes should include "real0"..."real9"
        # Should NOT use candidates (5 entries), so "fake1"..."fake5" should not appear
        self.assertIn("real0", focus_text)
        self.assertNotIn("fake1", focus_text)

    def test_theme_discovery_local_brief_uses_matched_companies_alias(self):
        """驗證 /theme discovery local_brief 同時支援 matched_companies 與 matched_universe。"""
        from research_center.prompt_registry import _grounding_local_brief

        request = parse_command_text("/theme AI伺服器 --deep")
        # Only matched_universe set (no matched_companies)
        structured_data = {
            "matched_universe": [{"code": "2330", "name": "台積電"}, {"code": "2317", "name": "鴻海"}],
        }
        brief = _grounding_local_brief(request, structured_data)
        self.assertEqual(brief["matched_count"], 2)
        self.assertEqual(len(brief["top_companies"]), 2)


class DiscoveryPromptTests(unittest.TestCase):
    """Test that build_grounding_discovery_prompts produces valid discovery prompts."""

    def test_discovery_prompts_return_list_with_required_keys(self):
        from research_center.prompt_registry import build_grounding_discovery_prompts

        request = parse_command_text("/research 2330")
        prompts = build_grounding_discovery_prompts(
            request,
            structured_data={"stock": {"code": "2330", "name": "TSMC"}},
            source_list=[SourceItem(source_id="S001", title="MOPS", url="https://mops.twse.com.tw/", source_level="Level 1", published_date="2026-05-01")],
        )
        self.assertIsInstance(prompts, list)
        self.assertGreater(len(prompts), 0)
        for item in prompts:
            self.assertIn("label", item)
            self.assertIn("prompt", item)
            self.assertIn("queries", item)
            self.assertIn("objective", item)
            self.assertIn("JSON", item["prompt"])
            self.assertNotIn("最終買賣建議", item["prompt"])

    def test_discovery_prompts_for_macro_command(self):
        from research_center.prompt_registry import build_grounding_discovery_prompts

        request = parse_command_text("/macro 全球")
        prompts = build_grounding_discovery_prompts(
            request,
            structured_data={"market_score": {"total": 50}},
            source_list=[],
        )
        self.assertIsInstance(prompts, list)
        self.assertGreater(len(prompts), 0)

    def test_topic_maintain_discovery_contains_original_and_site_queries(self):
        """/topic_maintain discovery tasks contain both original queries and site: queries."""
        from research_center.prompt_registry import build_grounding_discovery_prompts

        request = parse_command_text("/topic_maintain --deep")
        prompts = build_grounding_discovery_prompts(
            request,
            structured_data={"topic_maintain_mode_hint": "update"},
            source_list=[],
        )
        self.assertIsInstance(prompts, list)
        self.assertGreater(len(prompts), 0)

        all_queries: list[str] = []
        for item in prompts:
            all_queries.extend(item.get("queries", []))

        # Must have some original queries (no site: prefix)
        site_queries = [q for q in all_queries if "site:" in q]
        original_queries = [q for q in all_queries if "site:" not in q]
        self.assertGreater(len(original_queries), 0, f"Should have original queries, got: {all_queries[:20]}")

        # Must have site: queries
        self.assertGreater(len(site_queries), 0, f"Should have site: queries, got: {all_queries[:20]}")

        # Query count should not explode
        self.assertLessEqual(len(all_queries), 120, f"Too many queries: {len(all_queries)}")

    def test_topic_maintain_site_queries_per_task_capped_at_four(self):
        """Each /topic_maintain discovery task should have at most 4 site: queries."""
        from research_center.prompt_registry import _grounding_discovery_tasks

        request = parse_command_text("/topic_maintain --deep --model minimax")
        tasks = _grounding_discovery_tasks(request, {})
        self.assertIsInstance(tasks, list)
        self.assertGreater(len(tasks), 0)

        total_site = 0
        for task in tasks:
            queries = task.get("queries", [])
            site_count = sum(1 for q in queries if isinstance(q, str) and "site:" in q)
            self.assertLessEqual(site_count, 4, f"Task '{task.get('label')}' has {site_count} site queries")
            total_site += site_count

        self.assertGreater(total_site, 0, "Should have at least some site queries across tasks")
        # Total should be much lower than the unbounded 78+ we had before
        self.assertLessEqual(total_site, len(tasks) * 4)

    def test_topic_maintain_prompt_contains_source_level_rules(self):
        """topic_maintain.md must contain L1/L2/L3 source level rules."""
        from research_center.topic_maintain_service import _load_prompt

        prompt_text = _load_prompt("topic_maintain")
        self.assertIn("L1_official", prompt_text)
        self.assertIn("L2_media", prompt_text)
        self.assertIn("L3_community", prompt_text)
        self.assertIn("不得單獨支撐", prompt_text)

    def test_topic_maintain_prompt_contains_external_source_cache(self):
        """topic_maintain.md should include external topic source cache input."""
        from research_center.topic_maintain_service import _load_prompt

        prompt_text = _load_prompt("topic_maintain")
        self.assertIn("external_topic_source_caches_json", prompt_text)
        self.assertIn("TPEx", prompt_text)
        self.assertIn("UDN", prompt_text)

