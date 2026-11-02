from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock
from uuid import uuid4

import httpx

from research_center.deepseek_service import DeepSeekAccount, DeepSeekService, DeepSeekUnavailable, DeepSeekError, ai_task
from research_center.deepseek_search_service import DeepSeekSearchService, _safe_result_title, _stock_table_excerpt
from research_center.command_parser import parse_command_text, validate_deepseek_request


class DeepSeekServiceTests(unittest.TestCase):
    def test_stock_final_summary_does_not_invent_market_ranking_requirements(self):
        from research_center.segmented_analysis_service import _final_local_summary
        for command in ('research', 'value_scan'):
            with self.subTest(command=command):
                data = {'high_model_input_package': {'command': command, 'command_specific_data': {
                    'payload': {'stock': {'code': '2330'}, 'candidate_pool': '2330'}}},
                    'feature_pack': {'candidates': [{'code': '2330'}]}, 'local_scoring': {'scores': []}}
                summary = _final_local_summary(data)
                self.assertNotIn('sector_rankings', summary)
                self.assertEqual(summary['command_role'], command)
                if command == 'value_scan':
                    self.assertEqual(summary['candidate_count'], 1)
                    self.assertEqual(summary['candidate_pool'], '2330')

    def test_official_final_notes_preserve_evidence_after_the_legacy_excerpt(self):
        from research_center.segmented_analysis_service import _build_final_prompt
        note = '完整證據。' * 1200 + '德微完整產品與反證不能消失'
        prompt = _build_final_prompt(parse_command_text('/theme AI電源'), {},
            [{'label': 'companies', 'markdown': note, 'status': 'success'}], [], complete_segment_notes=True)
        self.assertIn('德微完整產品與反證不能消失', prompt)
        self.assertNotIn('intermediate note truncated', prompt)

    def test_official_final_failure_preserves_segments_without_a_compact_request(self):
        from types import SimpleNamespace
        from research_center import segmented_analysis_service as service
        plan = {'label': 'test', 'title': '完整分段', 'payload': {'facts': ['原始反證']}}
        result = SimpleNamespace(markdown='已完成分段，含原始反證', raw={}, diagnostics={})
        with patch.object(service, '_segment_plans', return_value=[plan]), \
                patch.object(service, 'write_prompt_log', return_value=Path('unused-test-prompt')), \
                patch.object(service, '_build_final_prompt', return_value='完整整合資料'), \
                patch.object(service, '_build_compact_final_retry_prompt') as compact, \
                patch.object(service, '_call_ai_with_timeout_setting', side_effect=[result, DeepSeekError('仍未完整輸出', 'incomplete_output')]) as call:
            report = service.run_segmented_theme_analysis(request=parse_command_text('/theme AI電源'),
                structured_data={}, sources=[], ai_client=self.client, model_name='deepseek-flash')
        self.assertEqual(call.call_count, 2)
        compact.assert_not_called()
        self.assertEqual(report.diagnostics['final_status'], 'fallback')
        self.assertIn('原始反證', report.markdown)
        self.assertEqual(report.segment_runs[0].status, 'success')

    def test_final_report_reserves_existing_output_headroom_without_forcing_usage(self):
        from research_center.segmented_analysis_service import _call_ai_with_timeout_setting
        result = _call_ai_with_timeout_setting(self.client, '完整正式報告與來源', 180, final_output=True)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(json.loads(self.posts()[0].content)['max_tokens'], 65536)
        self.assertEqual(self.client.max_output_tokens, 32768)
        self.assertLess(result.diagnostics['usage']['completion_tokens'], 65536)

    def test_final_report_does_not_repeat_the_same_maximum_output_request(self):
        from research_center.segmented_analysis_service import _call_ai_with_timeout_setting
        self.finish_reason = 'length'
        with self.assertRaises(DeepSeekError):
            _call_ai_with_timeout_setting(self.client, '完整來源', 180, final_output=True)
        self.assertEqual(len(self.posts()), 1)

    def test_topic_candidate_output_headroom_is_a_cap_not_a_forced_completion(self):
        from research_center.topic_maintain_service import TOPIC_CANDIDATE_OUTPUT_TOKENS
        self.assertEqual(TOPIC_CANDIDATE_OUTPUT_TOKENS,32768)
        self.answer='{"candidates":[]}'
        result=self.client.with_output_limit(TOPIC_CANDIDATE_OUTPUT_TOKENS).generate_json('完整來源不截斷')
        self.assertEqual(len(self.posts()),1)
        self.assertEqual(json.loads(self.posts()[0].content)['max_tokens'],32768)
        self.assertLess(result.diagnostics['usage']['completion_tokens'],32768)

    def test_reservation_uses_off_peak_and_peak_window_without_lowering_job_limit(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from research_center.deepseek_service import _reserve_model_price
        off_peak=datetime(2026,11,3,0,30,tzinfo=ZoneInfo('Asia/Taipei'))
        off=_reserve_model_price(100,20,390,off_peak)
        self.assertEqual(off,Decimal('0.000027'))
        self.assertEqual(_reserve_model_price(100,20,390,off_peak.replace(hour=10)),off*2)
        self.assertEqual(_reserve_model_price(100,20,390,off_peak.replace(hour=8,minute=59)),off*2)
        self.assertEqual(self.account.budget.job_limit,Decimal('0.3'))

    def test_fetch_downstream_error_does_not_disable_model_analysis(self):
        self.post_status=504
        with patch('research_center.deepseek_service.time.sleep'), self.assertRaises(DeepSeekError) as caught:
            self.client._post('v1/tools/fetch',{'url':'https://example.com/unavailable'},Decimal('0.00075'))
        self.assertEqual(caught.exception.status,'source_unavailable')
        self.assertNotIsInstance(caught.exception,DeepSeekUnavailable)
        self.post_status=200
        self.client.generate_report('依已有資料分析，揭露正文缺口')
        self.assertEqual(len(self.posts()),3)

    def test_json_output_length_retry_keeps_prompt_and_is_bounded(self):
        self.answer='{"actions":[]}'
        self.finish_reason='length'
        self.client.max_output_tokens=8192
        with self.assertRaises(DeepSeekError):
            self.client.generate_json('完整來源不得刪減')
        bodies=[json.loads(request.content) for request in self.posts()]
        self.assertEqual([body['max_tokens'] for body in bodies],[8192,16384])
        self.assertEqual(bodies[0]['messages'],bodies[1]['messages'])

    def test_invalid_json_preserves_original_response_without_caching_success(self):
        self.answer='{"actions":['
        with self.assertRaises(DeepSeekError) as caught:
            self.client.generate_json('回傳合法題材變更 JSON')
        self.assertEqual(caught.exception.status,'invalid_json')
        saved=json.loads(Path(caught.exception.diagnostics['incomplete_output_path']).read_text(encoding='utf-8'))
        self.assertEqual(saved['content'],self.answer)
        self.assertNotIn('test-key',json.dumps(saved))
        from research_center.topic_pipeline_service import _should_retry_detail_error
        self.assertTrue(_should_retry_detail_error(caught.exception))
        self.assertFalse(_should_retry_detail_error(DeepSeekUnavailable('連線結果不明','transport_unknown')))

    def test_segment_length_retry_preserves_prompt_and_increases_only_output_limit(self):
        from research_center.segmented_analysis_service import _call_ai_with_timeout_setting
        original=self.account.transport
        def respond(request):
            if request.method!='POST':
                return original.handle_request(request)
            self.requests.append(request)
            body=json.loads(request.content)
            return httpx.Response(200,json={'choices':[{'message':{'content':'完整保留來源與反證'},'finish_reason':'length' if body['max_tokens']==32768 else 'stop'}],
                'usage':{'prompt_tokens':20,'completion_tokens':10}})
        self.account.transport=httpx.MockTransport(respond)
        result=_call_ai_with_timeout_setting(self.client,'完整核心與反證',180)
        bodies=[json.loads(r.content) for r in self.posts()]
        self.assertEqual([b['max_tokens'] for b in bodies],[32768,65536])
        self.assertEqual(bodies[0]['messages'],bodies[1]['messages'])
        self.assertTrue(result.diagnostics['output_limit_retry'])

    def test_segment_length_retry_is_bounded(self):
        from research_center.segmented_analysis_service import _call_ai_with_timeout_setting
        self.finish_reason='length'
        with self.assertRaises(DeepSeekError):
            _call_ai_with_timeout_setting(self.client,'完整核心',180)
        self.assertEqual(len(self.posts()),2)

    def test_content_filter_does_not_trigger_larger_output_retry(self):
        from research_center.segmented_analysis_service import _call_ai_with_timeout_setting
        self.finish_reason='content_filter'
        with self.assertRaises(DeepSeekError):
            _call_ai_with_timeout_setting(self.client,'完整核心',180)
        self.assertEqual(len(self.posts()),1)

    def test_transport_unknown_stops_following_paid_segments_during_cooldown(self):
        self.transport_error=True
        for prompt in ['第一段','下一段不得繼續付費']:
            with self.assertRaises(DeepSeekUnavailable) as error:
                self.client.generate_report(prompt)
            self.assertEqual(error.exception.status,'transport_unknown')
        self.assertEqual(len(self.posts()),1)

    def test_official_table_date_not_replaced_by_search_crawl_date(self):
        from unittest.mock import MagicMock
        adapter=DeepSeekSearchService(MagicMock())
        row={'title':'法人日表','url':'https://www.twse.com.tw/test','date':'2026-11-02','content':json.dumps({'date':'20190716','fields':['證券代號','外資買賣超'],'data':[['2330','10']]})}
        with patch.object(adapter,'search',return_value={'results':[row]}):
            result=adapter.discover(parse_command_text('/research 2330'),[{'queries':['2330 法人']}])
        self.assertEqual(result.sources[0].published_date,'2019-07-16')

    def test_official_table_without_requested_code_is_not_target_evidence(self):
        from unittest.mock import MagicMock
        adapter=DeepSeekSearchService(MagicMock())
        row={'title':'法人日表','url':'https://www.twse.com.tw/test','content':json.dumps({'fields':['證券代號'],'data':[['2345']]})}
        with patch.object(adapter,'search',return_value={'results':[row]}):
            result=adapter.discover(parse_command_text('/research 2330'),[{'queries':['2330 法人']}])
        self.assertEqual(result.sources,[])
        self.assertEqual(result.diagnostics['runs'][0]['nonmatching_table_count'],1)

    def test_stock_table_projection_preserves_target_fields_and_raw_input(self):
        raw=json.dumps({'date':'20261102','fields':['證券代號','外資買賣超'],'data':[['2330','15000'],['2345','999']]})
        text,projected=_stock_table_excerpt(raw,parse_command_text('/research 2330'))
        self.assertTrue(projected)
        self.assertEqual(json.loads(text)['data'],[['2330','15000']])
        self.assertEqual(len(json.loads(raw)['data']),2)

    def test_multi_stock_commands_do_not_apply_single_stock_projection(self):
        text='{"fields":["證券代號"],"data":[["2330"],["2345"]]}'
        self.assertEqual(_stock_table_excerpt(text,parse_command_text('/theme_radar')),(text,False))
    def setUp(self):
        self.root = Path(os.environ.get("CODEX_TASK_TEST_ROOT", ".cache/test_tmp/deepseek")) / uuid4().hex
        self.requests = []
        self.balance = "10.00"
        self.post_status = 200
        self.answer = "完整研究內容"
        self.transport_error = False
        self.finish_reason = "stop"

        def handler(request):
            self.requests.append(request)
            if request.url.path == "/user/balance":
                return httpx.Response(200, json={"is_available": Decimal(self.balance) > 0,
                    "balance_infos": [{"currency": "USD", "total_balance": self.balance}]})
            if self.transport_error:
                raise httpx.ReadTimeout("private provider message", request=request)
            if self.post_status != 200:
                return httpx.Response(self.post_status, json={"error": {"message": "private message"}})
            if request.url.path.endswith("search"):
                return httpx.Response(200, json={"results": [{"url": "https://www.twse.com.tw/news", "title": "公告", "date": "2026-10-30", "summary": "公司公告與反證", "authority": 4}]})
            if request.url.path.endswith("fetch"):
                return httpx.Response(200, json={"title": "公告", "content": "完整原文"})
            return httpx.Response(200, json={"model": "deepseek-flash", "choices": [{"finish_reason": self.finish_reason, "message": {"content": self.answer}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 400, "prompt_cache_hit_tokens": 200}})

        self.account = DeepSeekAccount("test-key-never-log", self.root, transport=httpx.MockTransport(handler))
        self.client = DeepSeekService(self.account)

    def posts(self):
        return [r for r in self.requests if r.method == "POST"]

    def ledger(self):
        with self.account._db() as db:
            return db.execute("SELECT command, background, status, actual_usd, detail FROM usage").fetchall()

    def test_report_uses_official_flash_and_saves_usage(self):
        with ai_task("research", report_date="2026-10-30"):
            result = self.client.generate_report("完整資料，不可省略核心資料")
        self.assertEqual(result.markdown, self.answer)
        self.assertEqual(str(self.posts()[0].url), "https://api.deepseek.com/chat/completions")
        body = json.loads(self.posts()[0].content)
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["thinking"]["type"], "enabled")
        self.assertEqual(self.ledger()[0][:3], ("research", 0, "success"))
        self.assertNotIn("test-key", str(self.ledger()))

    def test_organizer_uses_non_thinking_json(self):
        self.answer = '{"facts": []}'
        result = DeepSeekService(self.account, organizer=True).generate_json("新聞資料")
        body = json.loads(self.posts()[0].content)
        self.assertEqual(body["thinking"]["type"], "disabled")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(json.loads(result.markdown), {"facts": []})

    def test_exact_successful_prompt_is_reused_without_new_charge(self):
        self.client.generate_report("同一版本來源與提示詞")
        result = self.client.generate_report("同一版本來源與提示詞")
        self.assertTrue(result.diagnostics["cache_hit"])
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(len(self.ledger()), 1)

    def test_changed_prompt_or_analysis_date_invalidates_cache(self):
        with ai_task("research", report_date="2026-10-30"):
            self.client.generate_report("來源版本一")
            self.client.generate_report("來源版本二")
        with ai_task("research", report_date="2026-10-29"):
            self.client.generate_report("來源版本一")
        self.assertEqual(len(self.posts()), 3)

    def test_402_blocks_later_calls_without_retries(self):
        self.post_status = 402
        for _ in range(3):
            with self.assertRaises(DeepSeekUnavailable):
                self.client.generate_report("資料")
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.ledger()[0][2], "failed")

    def test_zero_balance_starts_no_paid_call(self):
        self.balance = "0"
        with self.assertRaises(DeepSeekUnavailable) as cm:
            self.client.generate_report("資料")
        self.assertEqual(cm.exception.status, "insufficient_balance")
        self.assertEqual(len(self.posts()), 0)

    def test_topup_restores_after_balance_cache_expires(self):
        self.balance = "0"
        with self.assertRaises(DeepSeekUnavailable):
            self.client.generate_report("資料")
        self.account.balance_checked_at -= 61
        self.balance = "10"
        self.client.generate_report("資料")
        self.assertEqual(len(self.posts()), 1)

    def test_auth_failure_does_not_retry(self):
        self.post_status = 401
        with self.assertRaises(DeepSeekUnavailable) as cm:
            self.client.generate_report("資料")
        self.assertEqual(cm.exception.status, "authentication_failed")
        self.assertEqual(len(self.posts()), 1)

    def test_timeout_is_unknown_cost_and_not_retried(self):
        self.transport_error = True
        with self.assertRaises(DeepSeekError):
            self.client.generate_report("資料")
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.ledger()[0][2:4], ("unknown", None))

    def test_rate_limit_has_bounded_retry_and_shared_cooldown(self):
        self.post_status = 429
        with patch("research_center.deepseek_service.time.sleep"):
            with self.assertRaises(DeepSeekError):
                self.client.generate_report("資料")
        self.assertEqual(len(self.posts()), 2)
        with self.assertRaises(DeepSeekUnavailable):
            self.client.generate_report("其他資料")
        self.assertEqual(len(self.posts()), 2)

    def test_daily_budget_blocks_before_paid_request(self):
        self.account.daily_limit = Decimal("0.00001")
        with self.assertRaises(DeepSeekUnavailable) as cm:
            self.client.generate_report("資料")
        self.assertEqual(cm.exception.status, "daily_budget_exceeded")
        self.assertFalse(self.posts())

    def test_background_reserves_balance_for_manual_tasks(self):
        self.balance = "0.21"
        with ai_task("news", background=True):
            with self.assertRaises(DeepSeekUnavailable):
                self.client.generate_report("資料")
        self.client.generate_report("資料")
        self.assertEqual(len(self.posts()), 1)

    def test_concurrent_reservations_cannot_overspend(self):
        self.balance = "0.10"
        def reserve(_):
            try:
                return self.account.reserve("test", Decimal("0.07"))
            except DeepSeekUnavailable:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        self.assertEqual(sum(bool(r) for r in results), 1)

    def test_truncated_model_output_is_not_cached_as_success(self):
        self.finish_reason = "length"
        for _ in range(2):
            with self.assertRaises(DeepSeekError):
                self.client.generate_report("完整資料")
        self.assertEqual(len(self.posts()), 2)

    def test_search_deduplicates_queries_and_uses_existing_source_ranking(self):
        search = DeepSeekSearchService(self.client)
        request = parse_command_text("/research 2330 --date 2026-10-30")
        result = search.discover(request, [{"label": "官方", "queries": ["公司公告", "公司公告"]}])
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(result.sources[0].source_level, "L1_official")
        self.assertEqual(result.sources[0].provider, "deepseek_search")
        search.discover(request, [{"queries": ["公司公告"]}])
        self.assertEqual(len(self.posts()), 1)

    def test_fetch_reuses_complete_page_cache(self):
        search = DeepSeekSearchService(self.client)
        first = search.fetch("https://www.twse.com.tw/news")
        second = search.fetch("https://www.twse.com.tw/news")
        self.assertEqual(first["content"], "完整原文")
        self.assertTrue(second["cache_hit"])
        self.assertEqual(len(self.posts()), 1)

    def test_old_model_flags_are_rejected(self):
        for model in ["gemini", "minimax", "opencode", "deepseek-v4-pro"]:
            with self.subTest(model=model), self.assertRaises(ValueError):
                validate_deepseek_request(parse_command_text("/research 2330 --model " + model))

    def test_report_command_defaults_all_use_deepseek(self):
        for command in ["/research 2330", "/value_scan 我的持股", "/macro 全球", "/theme AI電源", "/theme_flow AI電源", "/theme_radar", "/sector_strength", "/news refresh", "/topic_maintain"]:
            with self.subTest(command=command):
                self.assertEqual(parse_command_text(command).ai_model, "deepseek")
        from stock_ai_bot.monitoring.radar_service import parse_radar_args
        self.assertEqual(parse_radar_args([]).model, "deepseek")

    def test_official_center_does_not_construct_legacy_clients(self):
        import research_center.orchestrator as module
        from research_center.config import ResearchCenterConfig
        config = ResearchCenterConfig(deepseek_only=True, deepseek_api_key="test-key",
            database_path=self.root / "research.db", report_root=self.root / "reports")
        with patch.object(module, "ROOT_DIR", self.root), \
             patch.object(module, "GeminiService", side_effect=AssertionError("legacy client")), \
             patch.object(module, "MiniMaxService", side_effect=AssertionError("legacy client")), \
             patch.object(module, "MiniMaxSearchService", side_effect=AssertionError("legacy client")), \
             patch.object(module, "OpenCodeService", side_effect=AssertionError("legacy client")), \
             patch.object(module, "TavilySearchService", side_effect=AssertionError("legacy client")):
            center = module.ResearchCenter(config)
            try:
                self.assertFalse(center.gemini.is_configured())
                self.assertFalse(center.minimax.is_configured())
                self.assertIs(center.opencode, center.deepseek)
                with self.assertRaises(ValueError):
                    center.parse("/research 2330 --model minimax")
            finally:
                center.database.close()

    def test_all_seven_report_routes_use_official_service_and_save_metadata(self):
        import research_center.orchestrator as module
        from research_center.config import ResearchCenterConfig
        from research_center.models import SourceItem
        config = ResearchCenterConfig(deepseek_only=True, deepseek_api_key="test-key",
            enable_low_model_digest=False, database_path=self.root / "research.db", report_root=self.root / "reports")
        with patch.object(module, "ROOT_DIR", self.root):
            center = module.ResearchCenter(config)
        center.deepseek = self.client
        center.opencode = self.client
        source = SourceItem("S001", "交易所公告", "https://www.twse.com.tw/news", "Level 1", snippet="公告內容")
        try:
            with patch.object(module, "collect_structured_data", side_effect=lambda *a, **k: ({}, [source])), \
                 patch.object(center._gemini_discovery_runner, "run_discovery_flow", return_value=([source], False)), \
                 patch.object(module, "_enrich_sources_with_web_fetch"), \
                 patch.object(module, "persist_search_sources_to_news"), \
                 patch.object(module, "attach_news_events"):
                for command in ["/research 2330", "/value_scan 我的持股", "/macro 全球", "/theme AI電源", "/theme_flow AI電源", "/theme_radar", "/sector_strength"]:
                    with self.subTest(command=command):
                        result = center.run_text_command(command)
                        self.assertTrue(result.ai_used)
                        self.assertEqual(result.report_json["metadata"]["analysis_provider"], "deepseek")
                        self.assertEqual(result.ai_model, "deepseek-flash")
                        self.assertTrue(result.artifacts.json_path.exists())
        finally:
            center.database.close()

    def test_news_classification_reused_and_pending_database_row_updated(self):
        from research_center.news_service import _batch_classify_news
        from research_center.news_repository import NewsRepository
        from research_center.news_models import NewsItem
        self.answer = '{"0":{"category":"company_news","summary":"新訂單經公告確認","related_symbols":["2330"],"related_topics":["AI"]}}'
        center = SimpleNamespace(config=SimpleNamespace(deepseek_only=True, deepseek_model="deepseek-flash"),
            deepseek_organizer=DeepSeekService(self.account, organizer=True))
        repo = NewsRepository(self.root / "news.db")
        item = NewsItem(url="https://www.twse.com.tw/news", title="台積電新訂單", full_text="公司正式公告",
            tags=["local_rule_fallback"])
        repo.save_many([item])
        pending = repo.query_pending_classification()
        classified = _batch_classify_news(pending, center, lambda *a: None, ai_model="deepseek")
        repo.save_many(classified)
        self.assertEqual(repo.query_pending_classification(), [])
        _batch_classify_news([NewsItem(url=item.url, title=item.title, full_text=item.full_text)], center, lambda *a: None, ai_model="deepseek")
        self.assertEqual(len(self.posts()), 1)

    def test_scheduled_news_updates_without_any_telegram_message(self):
        import main
        center = SimpleNamespace(config=SimpleNamespace(database_path=self.root / "news.db", scheduled_news_push_enabled=False))
        bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(bot=bot)
        diagnostics = {"primary24": 8, "display_categories": {k: 1 for k in ("market_focus", "sector_rotation", "ai_semiconductor", "supply_chain", "macro_policy", "company_news")}}
        with patch.object(main, "load_config", return_value={"chat_id": "test-only"}), \
             patch("research_center.orchestrator.ResearchCenter", return_value=center), \
             patch.object(main, "NewsRepository"), \
             patch.object(main, "run_news_refresh", return_value=([], {"saved": 3})) as refresh, \
             patch.object(main, "run_news_scheduled_latest", return_value=[]), \
             patch.object(main, "build_scheduled_news_diagnostics", return_value=diagnostics), \
             patch.object(main, "format_news_digest", return_value="測試新聞"):
            asyncio.run(main._scheduled_news_refresh(context))
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.kwargs["ai_model"], "deepseek")
        bot.send_message.assert_not_awaited()

    def test_topic_incomplete_ai_review_cannot_be_applied(self):
        from research_center.topic_apply_service import confirm_change_pack
        pack = SimpleNamespace(extra={"ai_review_complete": False})
        with patch("research_center.topic_apply_service.load_change_pack", return_value=pack), \
             patch("research_center.topic_apply_service.backup_topic_files") as backup:
            result = confirm_change_pack("test-change")
        self.assertFalse(result.success)
        backup.assert_not_called()

    def test_radar_balanced_batches_preserve_all_candidates(self):
        import stock_ai_bot.monitoring.radar_service as radar
        from datetime import date
        candidates = [radar.RadarCandidate(code=str(2300+i), name="測試") for i in range(5)]
        with patch.object(radar, "RADAR_AI_PROMPT_MAX_CHARS", 30), \
             patch.object(radar, "_build_ai_comment_prompt", side_effect=lambda batch, *a, **k: "x" * (10*len(batch))):
            jobs = radar._build_ai_comment_prompt_jobs(candidates, date(2026, 10, 30))
        self.assertEqual(len(jobs), 2)
        self.assertEqual([code for job in jobs for code in job["codes"]], [c.code for c in candidates])
        self.assertTrue(all(j["profile"] == "normal" for j in jobs))

    def test_web_fetch_workers_keep_background_budget_context(self):
        from research_center.deepseek_service import TASK
        from research_center.web_fetch_service import WebFetchService, WebFetchResult
        observed = []
        def fake_fetch(url, **kwargs):
            observed.append(TASK.get())
            return WebFetchResult(url, "測試", "完整正文")
        with patch("research_center.web_fetch_service.fetch_web_content", side_effect=fake_fetch), ai_task("news", True, "2026-10-30"):
            WebFetchService().fetch_many(["https://example.com/1", "https://example.com/2"])
        self.assertEqual(observed, [("news", True, "2026-10-30")]*2)

    def test_usage_records_retry_count_without_secret(self):
        self.post_status = 503
        with patch("research_center.deepseek_service.time.sleep"), self.assertRaises(DeepSeekError):
            self.client.generate_report("測試")
        detail = json.loads(self.ledger()[0][-1])
        self.assertEqual(detail["attempts"], 2)
        self.assertEqual(detail["retry_count"], 1)
        self.assertGreater(detail["prompt_chars"], 0)
        self.assertNotIn("test-key", str(detail))

    def test_missing_news_classification_is_not_formal_ai_success(self):
        from research_center.news_service import _apply_classification_meta
        from research_center.news_models import NewsItem
        items = _apply_classification_meta([NewsItem(url="https://example.com", title="新訂單")], {})
        self.assertIn("local_rule_fallback", items[0].tags)

    def test_concurrent_identical_prompts_only_make_one_paid_request(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.client.generate_report("同一份證據"), range(4)))
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(sum(not r.diagnostics["cache_hit"] for r in results), 1)

    def test_cny_balance_uses_explicit_budget_rate(self):
        self.account.available = True
        import time
        self.account.balance_checked_at = time.monotonic()
        self.account.balances = [{"currency": "CNY", "total_balance": "1.50"}]
        with ai_task("news", True), self.assertRaises(DeepSeekUnavailable):
            self.account.reserve("chat/completions", Decimal("0.001"))
        self.assertEqual(self.posts(), [])

    def test_offline_live_tool_check_never_connects_without_key(self):
        from tools.deepseek_live_validation import main
        with patch("tools.deepseek_live_validation.load_research_config", return_value=SimpleNamespace(deepseek_only=True, deepseek_api_key=None)), \
             patch("tools.deepseek_live_validation.ResearchCenter") as center:
            self.assertEqual(main(["--check-config"]), 2)
        center.assert_not_called()

    def test_radar_search_uses_official_adapter_not_tavily(self):
        import stock_ai_bot.monitoring.radar_service as radar
        from datetime import date
        config = SimpleNamespace(deepseek_only=True)
        with patch.object(radar, "load_research_config", return_value=config), \
             patch.object(radar, "TavilySearchService", side_effect=AssertionError("retired search")), \
             patch("research_center.deepseek_service.configured_client", return_value=self.client):
            candidate = radar.RadarCandidate(code="2330", name="台積電")
            radar._attach_web_sources([candidate], ["2330"], date(2026, 10, 30), None)
        self.assertTrue(any(r.url.path.endswith("search") for r in self.posts()))

    def test_search_quality_gap_is_checked_even_when_source_count_is_high(self):
        import research_center.orchestrator as module
        from research_center.config import ResearchCenterConfig
        from research_center.models import SourceItem
        from research_center.deepseek_search_service import DiscoveryResult
        from unittest.mock import Mock
        config = ResearchCenterConfig(deepseek_only=True)
        search = Mock()
        weak = [SourceItem(f"S{i}", "網友討論", f"https://www.ptt.cc/bbs/Stock/M.{i}.html", "L4_forum") for i in range(40)]
        search.discover.return_value = DiscoveryResult(weak, {"provider": "deepseek_search"})
        runner = module._GeminiDiscoveryRunner(SimpleNamespace(config=config, deepseek_search=search))
        data = {}
        with patch.object(module, "build_grounding_discovery_prompts", return_value=[{"queries": ["台股"]}]), \
             patch.object(module, "build_required_data_gap_summary", return_value={}), \
             patch.object(module, "build_required_gap_fill_tasks", return_value=[]):
            runner.run_discovery_flow(parse_command_text("/macro 台股"), [], data, False, None)
        self.assertEqual(search.discover.call_count, 2)
        self.assertIn("social_or_video_ratio_too_high", data["deepseek_source_quality_before"]["gap_reasons"])

    def test_short_or_irrelevant_fetched_content_is_not_marked_healthy(self):
        from research_center.web_fetch_service import fetch_web_content
        with patch("research_center.config.load_research_config", return_value=SimpleNamespace(deepseek_only=True)), \
             patch("research_center.deepseek_service.configured_client", return_value=self.client), \
             patch("research_center.web_fetch_service._fetch_with_requests", side_effect=RuntimeError("network unavailable")):
            result = fetch_web_content("https://example.com/article/1", expected_terms=["台積電"])
        self.assertEqual(result.content_status, "partial")
        self.assertEqual(result.fetch_quality, "low")
        self.assertIn("no_keyword_match", result.failure_reason)

    def test_provider_title_code_is_isolated_without_losing_evidence(self):
        title = "window['dataLayer']=[];function(){font-family:arial;box-sizing:border-box;}" * 100
        row = {"title": title, "url": "https://example.com/article", "content": "公司正式公告與營收數據"}
        normalized = _safe_result_title(row)
        self.assertEqual(normalized["content"], row["content"])
        self.assertEqual(normalized["title"], "來源網頁（example.com）")
        self.assertEqual(row["title"], title)
        self.assertEqual(normalized["title_normalization"], "code_isolated")

    def test_normal_financial_text_is_not_truncated_as_a_title(self):
        title = "公司本季營收成長，毛利率與自由現金流皆改善。" * 150
        row = {"title": title, "url": "https://example.com/article"}
        self.assertEqual(_safe_result_title(row), row)

    def test_cached_search_is_normalized_without_mutating_raw_response(self):
        title = "window['dataLayer']=[];function(){font-family:arial;}" * 100
        body = {"results": [{"title": title, "url": "https://example.com/article", "summary": "證據"}]}
        with patch.object(self.account, "cached", return_value=body):
            result = DeepSeekSearchService(self.client).search("公司公告")
        self.assertEqual(result["results"][0]["title"], "來源網頁（example.com）")
        self.assertEqual(body["results"][0]["title"], title)
        self.assertEqual(self.posts(), [])

    def test_shared_source_ingestion_blocks_forum_code_title(self):
        from research_center.source_rank import make_source_items
        title = "window['dataLayer']=[];function(){font-family:arial;}" * 100
        sources = make_source_items([{"title": title, "url": "https://www.cmoney.tw/forum/", "snippet": "保留論壇來源"}])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].snippet, "保留論壇來源")
        self.assertEqual(sources[0].title, "來源網頁（www.cmoney.tw）")
        self.assertIn("source_title:code_isolated", sources[0].found_by)

    def test_existing_source_objects_are_normalized_before_model_input(self):
        from research_center.search_source_normalizer import normalize_source_items
        from research_center.models import SourceItem
        title = "window['dataLayer']=[];function(){font-family:arial;}" * 100
        original = SourceItem("S140", title, "https://www.cmoney.tw/forum/", "Level 4", snippet="原始內容")
        normalized = normalize_source_items([original], parse_command_text("/research 2330"))
        self.assertEqual(normalized[0].source_id, "S140")
        self.assertEqual(normalized[0].snippet, original.snippet)
        self.assertEqual(original.title, title)
        self.assertEqual(normalized[0].title, "來源網頁（www.cmoney.tw）")
