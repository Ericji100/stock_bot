from dataclasses import asdict
from decimal import Decimal
import json
from unittest.mock import Mock, patch
import unittest
from datetime import date

import httpx

from research_center.ai_provider_service import RoutedAIService
from research_center.deepseek_service import ai_task, TASK, DeepSeekUnavailable
from research_center.deepseek_search_service import DeepSeekSearchService
from research_center.models import CommandRequest
from research_center.openai_service import MODEL, BACKGROUND_MODEL
from research_center.search_routing import search_route, important_search_gaps, discovery_command
from tests import test_luna_routing as fixtures


class HybridSearchRoutingTests(unittest.TestCase):
    setUp = fixtures.LunaRoutingTests.setUp
    fallback = fixtures.LunaRoutingTests.fallback
    make_client = fixtures.LunaRoutingTests.make_client

    def router(self):
        router = RoutedAIService(self.make_client(), self.fallback())
        cache = {}
        router.account.cached = lambda key: cache.get(key)
        router.account.cache = lambda key, value, ttl: cache.__setitem__(key, value)
        router.account.activity = Mock()
        return router

    def test_initial_gap_final_preserve_pool_date_account_and_full_prompt(self):
        for command in ('news', 'topic_maintain', 'radar'):
            for bg in (False, True):
                with self.subTest(command=command, background=bg):
                    router = self.router()
                    with ai_task(command, background=bg, report_date='2026-11-05'):
                        for stage, expected in [('initial_search', BACKGROUND_MODEL), ('critical_gap_search', MODEL)]:
                            with search_route(command, stage, 'official_document'):
                                result = router._post('v1/tools/search', {'query':command+stage+str(bg)}, Decimal('.00075'))
                                self.assertEqual(result['diagnostics']['actual_model'], expected)
                                self.assertEqual(result['diagnostics']['workflow_stage'], stage)
                                self.assertEqual(TASK.get(), (command, bg, '2026-11-05'))
                        result = router.generate_report('完整核心資料、風險、反證 '+command+str(bg))
                        self.assertEqual(result.diagnostics['actual_model'], BACKGROUND_MODEL if bg and command != 'radar' else MODEL)
                        self.assertIn('完整核心資料、風險、反證', json.loads(self.calls[-1].content)['input'])
                        with self.ledger.db() as conn:
                            self.assertEqual(conn.execute('SELECT background FROM requests WHERE id=?',
                                (result.diagnostics['request_id'],)).fetchone()[0], int(bg))

    def test_cache_never_crosses_initial_and_critical_search(self):
        router = self.router()
        service = DeepSeekSearchService(router)
        with ai_task('radar', background=True):
            for stage in ('initial_search', 'critical_gap_search', 'initial_search', 'critical_gap_search'):
                with search_route('radar', stage, 'same_reason'):
                    service.search('同一公司官方公告', report_date='2026-11-05')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([json.loads(call.content)['model'] for call in self.calls], [BACKGROUND_MODEL, MODEL])
        self.assertEqual(router.account.activity.call_args.args[2]['incremental_cost_usd'], '0')

    def test_gap_checks_ignore_large_counts_wrong_company_future_and_reference_links(self):
        request = CommandRequest(command='research', target='1234 測試公司', raw_text='/radar')
        rows = [dict(title='9999 其他公司 官方財報', snippet='風險 '*80, url='https://mops.twse.com.tw/STR/9999',
                     published_date='2026-11-06', document_kind='reference_link')] * 30
        gaps = important_search_gaps(request, {}, rows, date(2026,11,5), 'radar')
        self.assertIn('publication_date', gaps)
        self.assertIn('official_document', gaps)
        self.assertIn('company_mapping', gaps)
        self.assertEqual(discovery_command(request), 'radar')
        rows = [dict(title='1234 官方財報', snippet='1234 測試公司營收數值、風險與反證。',
            url='https://mops.twse.com.tw/STR/1234', published_date='2026-11-04',
            date_status='explicit', document_kind='document_body', fetch_status='success')]
        self.assertEqual(important_search_gaps(request, {}, rows, date(2026,11,5), 'radar'), [])

    def test_single_bounded_followup_preserves_same_url_events_and_unverified_notes(self):
        service = DeepSeekSearchService(self.router(), radar_critical_gap_search_enabled=True)
        seen = []
        def search(query, report_date=''):
            from research_center.search_routing import SEARCH_ROUTE
            stage = SEARCH_ROUTE.get()['stage']
            seen.append(stage)
            return {'results':[{'title': '1234 測試公司', 'url':'https://example.test/news',
                'summary':stage, 'summary_events':[{'summary':stage}]}],
                'search_context_notes':[{'text':'未確認客戶 '+stage, 'verified':False}]}
        request = CommandRequest(command='research', raw_text='/radar', target='1234 測試公司', report_date=date(2026,11,5))
        with patch.object(service, 'search', side_effect=search), patch('research_center.web_fetch_enrichment.recover_search_publication_dates'):
            result = service.discover(request, [{'label':'營收與官方反證', 'queries':['第一個問題']}])
        self.assertEqual(seen, ['initial_search', 'critical_gap_search'])
        self.assertEqual(len(result.sources), 1)
        self.assertEqual(len(result.sources[0].search_summary_events), 2)
        notes = result.diagnostics['search_context_notes']
        self.assertEqual(len([note for note in notes if '未確認客戶' in note.get('text', '')]), 2)
        self.assertTrue(any(note.get('kind') == 'unresolved_critical_search_gaps' for note in notes))
        self.assertFalse(result.diagnostics['complete'])
        self.assertEqual(result.diagnostics['query_count'], 2)

    def test_sufficient_evidence_does_not_call_terra_and_core_data_unchanged(self):
        service = DeepSeekSearchService(self.router())
        request = CommandRequest(command='research', raw_text='/radar', target='1234 測試公司', report_date=date(2026,11,5))
        def enrich(req, items, diagnostics, progress):
            from dataclasses import replace
            items[:] = [replace(item, published_date='2026-11-04', date_status='explicit',
                document_kind='document_body', fetch_status='success') for item in items]
        data = {'results':[{'title':'1234 官方公告', 'url':'https://mops.twse.com.tw/STR/1234',
                           'summary':'1234 完整營收與風險原文內容'}]}
        with patch.object(service, 'search', return_value=data) as search, patch(
                'research_center.web_fetch_enrichment.recover_search_publication_dates', side_effect=enrich):
            result = service.discover(request, [{'label':'完整資料', 'queries':['公司公告']}])
        search.assert_called_once()
        self.assertTrue(result.diagnostics['complete'])
        self.assertIn('完整營收與風險原文內容', result.sources[0].snippet)

    def test_unknown_fee_stops_followup_retaining_initial_sources(self):
        service = DeepSeekSearchService(self.router(), radar_critical_gap_search_enabled=True)
        data = {'results':[{'title':'1234 線索', 'url':'https://example.test/news', 'summary':'第一批資料'}]}
        request = CommandRequest(command='research', raw_text='/radar', target='1234', report_date=date(2026,11,5))
        with patch.object(service, 'search', side_effect=[data, DeepSeekUnavailable('未知費用', 'transport_unknown')]), patch(
                'research_center.web_fetch_enrichment.recover_search_publication_dates'), self.assertRaises(DeepSeekUnavailable) as caught:
            service.discover(request, [{'label':'營收', 'queries':['公司公告']}])
        self.assertEqual(caught.exception.partial_discovery_sources[0].snippet, '第一批資料')
        self.assertEqual(caught.exception.partial_discovery_diagnostics['stop_reason'], 'transport_unknown')

    def test_gap_budget_defers_not_erases_unresolved_requirements(self):
        service = DeepSeekSearchService(self.router())
        request = CommandRequest(command='topic_maintain', raw_text='/topic_maintain', target='供應鏈', report_date=date(2026,11,5))
        tasks = [{'label':str(i), 'queries':['不同問題'+str(i)]} for i in range(6)]
        with patch.object(service, 'search', return_value={'results':[]}) as search, patch(
                'research_center.web_fetch_enrichment.recover_search_publication_dates'):
            result = service.discover(request, tasks)
        self.assertLessEqual(search.call_count, 9)
        self.assertEqual(len(result.diagnostics['critical_gap_checks']), 6)
        queries = [call.args[0] for call in search.call_args_list]
        self.assertEqual(len(queries), len(set(queries)))
        self.assertTrue(all(row['remaining'] for row in result.diagnostics['critical_gap_checks']))
        self.assertFalse(result.diagnostics['complete'])

    def test_unrelated_search_role_unchanged(self):
        router = self.router()
        with ai_task('research'):
            result = router._post('v1/tools/search', {'query':'非指定指令'}, Decimal('.00075'))
        self.assertEqual(result['diagnostics']['actual_model'], MODEL)

    def test_existing_radar_followup_is_terra_once_not_initial_plus_followup(self):
        service = DeepSeekSearchService(self.router(), radar_critical_gap_search_enabled=True)
        request = CommandRequest(command='research', raw_text='/radar', target='1234', report_date=date(2026,11,5))
        with patch.object(service, 'search', return_value={'results':[]}) as search, patch(
                'research_center.web_fetch_enrichment.recover_search_publication_dates'):
            result = service.discover(request, [{'label':'radar_evidence_gap', 'queries':['官方正文']}])
        search.assert_called_once()
        self.assertEqual(result.diagnostics['runs'][0]['workflow_stage'], 'critical_gap_search')
        self.assertFalse(result.diagnostics['complete'])

    def test_critical_role_counting_clones_and_exception_reset(self):
        router = self.router().with_timeout(60).with_output_limit(12000)
        with ai_task('news', background=True):
            with self.assertRaises(RuntimeError):
                with search_route('news', 'critical_gap_search', 'publication_date'):
                    self.assertEqual(router.model, MODEL)
                    self.assertTrue(router.prompt_capacity('完整資料')['reference'].endswith(MODEL))
                    router.primary.transport = httpx.MockTransport(lambda request: httpx.Response(200,
                        json={'object':'response.input_tokens', 'input_tokens':500}))
                    self.assertEqual(router.primary.count_input_tokens('完整資料'), 500)
                    raise RuntimeError('reset scope')
            self.assertEqual(router.model, BACKGROUND_MODEL)

    def test_readonly_report_unknown_and_pending_are_not_actual_spend(self):
        from tools.ai_stage_cost_report import cost_report
        with ai_task('news', background=True):
            _, request_id = self.ledger.choose({'a':'synthetic'}, Decimal('.2'), reserve=True, operation='web_search')
        report = cost_report(self.ledger.root/'openai-credits.sqlite3')
        self.assertEqual(report['settled_usd'], '0')
        self.assertEqual(report['pending_reserved_usd'], '0.2')
        self.ledger.finish(request_id, 'unknown', None, {'workflow_stage':'critical_gap_search', 'requested_model':MODEL})
        report = cost_report(self.ledger.root/'openai-credits.sqlite3')
        self.assertEqual(report['settled_usd'], '0')
        self.assertEqual(report['unknown_reserved_usd'], '0.2')
        self.assertEqual(report['pending_reserved_usd'], '0')

    def test_readonly_cost_report_separates_stages_tools_pools_and_unknown(self):
        from tools.ai_stage_cost_report import cost_report
        router = self.router()
        with ai_task('radar', background=True):
            with search_route('radar'):
                router._post('v1/tools/search', {'query':'成本測試'}, Decimal('.00075'))
            router.generate_report('完整最終分析')
        before = (self.ledger.root/'openai-credits.sqlite3').read_bytes()
        report = cost_report(self.ledger.root/'openai-credits.sqlite3')
        self.assertEqual(len(report['groups']), 2)
        self.assertEqual({g['stage'] for g in report['groups']}, {'initial_search', 'final_analysis'})
        self.assertEqual({g['pool'] for g in report['groups']}, {'background'})
        self.assertEqual(sum(Decimal(g['tool_usd']) for g in report['groups']), Decimal('.01'))
        self.assertEqual(before, (self.ledger.root/'openai-credits.sqlite3').read_bytes())

    def test_multiple_gap_rounds_keep_prior_notes_for_final_input(self):
        from research_center.orchestrator import _append_search_provider_log
        from research_center.search_transmission import collect_search_context
        data = {}
        first = {'kind':'unresolved_critical_search_gaps', 'verified':False, 'text':'第一輪缺公司官方證據'}
        second = {'kind':'unresolved_critical_search_gaps', 'verified':False, 'text':'第二輪日期仍有衝突'}
        for notes in ([first], [second, first]):
            _append_search_provider_log(data, provider='openai', source_count=0, diagnostics={'search_context_notes':notes})
        self.assertEqual(collect_search_context(data), [first, second])
