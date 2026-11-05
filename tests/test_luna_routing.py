from concurrent.futures import ThreadPoolExecutor
import asyncio
from decimal import Decimal
import json
from unittest.mock import Mock
import unittest

import httpx

from research_center.ai_provider_service import RoutedAIService
from research_center.deepseek_service import ai_task, task_context
from research_center.openai_credit_service import OpenAIUnavailable
from research_center.openai_service import BACKGROUND_MODEL, MODEL, OpenAIService, price, reserve_price
from tests import test_openai_routing as fixtures


class LunaRoutingTests(unittest.TestCase):
    setUp = fixtures.OpenAIRoutingTests.setUp
    fallback = fixtures.OpenAIRoutingTests.fallback

    def make_client(self):
        def handler(request):
            self.calls.append(request)
            body = json.loads(request.content)
            result = fixtures.response('{}')
            result['model'] = body['model']
            if body.get('tools'):
                result['output'].insert(0, {'type': 'web_search_call', 'status': 'completed',
                    'action': {'type': 'search', 'sources': []}})
            return httpx.Response(200, json=result)
        return OpenAIService(self.ledger, {'a': 'synthetic-key'}, transport=httpx.MockTransport(handler))

    def test_all_ten_commands_follow_stage_and_billing_role(self):
        router = RoutedAIService(self.make_client(), self.fallback())
        for command in ('research','value_scan','macro','theme','theme_flow','theme_radar',
                        'sector_strength','radar','news','topic_maintain'):
            for background in (True, False):
                with self.subTest(command=command,background=background), ai_task(command,background=background):
                    result = router.generate_report('完整核心資料、風險與反證 ' + command)
                    expected = BACKGROUND_MODEL if background and command != 'radar' else MODEL
                    body = json.loads(self.calls[-1].content)
                    self.assertEqual(body['model'], expected)
                    self.assertEqual(body['reasoning']['effort'], 'xhigh')
                    self.assertEqual(body['truncation'], 'disabled')
                    self.assertEqual(result.diagnostics['requested_model'], expected)
                    self.assertEqual(router.actual_model, expected)
                    self.assertEqual(router.routing_history[-1]['model'], expected)

    def test_nested_native_context_and_clones_preserve_background(self):
        router = RoutedAIService(self.make_client(), self.fallback()).with_timeout(60).with_output_limit(12000)
        @task_context('news')
        def native():
            return router.generate_json('完整新聞內容')
        with ai_task('scheduled_news',background=True):
            result = native()
        self.assertEqual(result.diagnostics['actual_model'], BACKGROUND_MODEL)
        body = json.loads(self.calls[-1].content)
        self.assertEqual(body['max_output_tokens'],12000)
        self.assertEqual(body['text']['format']['type'],'json_object')
        self.assertEqual(router.model,MODEL)

    def test_search_model_and_full_context_reserve_follow_role(self):
        client = self.make_client()
        expected = {False:Decimal('.288884'),True:Decimal('.288884')}
        for bg in (False,True):
            with ai_task('news',background=bg):
                result = client.search('公司公告 '+str(bg),'2026-11-04')
                ident = result['diagnostics']['request_id']
            with self.ledger.db() as c:
                estimate = c.execute('SELECT estimate FROM requests WHERE id=?',(ident,)).fetchone()[0]
            self.assertEqual(Decimal(estimate),expected[bg])
            self.assertEqual(result['diagnostics']['actual_model'],BACKGROUND_MODEL)
            self.assertEqual(result['diagnostics']['cost_estimate_usd'],str(Decimal('.01015')))

    def test_model_fee_including_cache_write_long_context_and_reasoning(self):
        usage = {'input_tokens':1000000,'output_tokens':10000,
            'input_tokens_details':{'cached_tokens':100000,'cache_write_tokens':200000,
                'cache_write_tokens_details':{'ttl_30m_tokens':150000,'ttl_12h_tokens':50000}},
            'output_tokens_details':{'reasoning_tokens':8000}}
        self.assertEqual(price(usage,MODEL),Decimal('2.11'))
        self.assertEqual(price(usage,BACKGROUND_MODEL),Decimal('.212'))
        self.assertEqual(price(usage,BACKGROUND_MODEL+'-2026-09-22'),Decimal('.212'))
        with self.assertRaises(ValueError): price(usage,'unpriced-model')

    def test_long_context_boundary_prices_and_reserves(self):
        for count in (272000,272001):
            with self.subTest(count=count):
                usage={'input_tokens':count,'output_tokens':1000}
                multiplier=Decimal('2') if count>272000 else Decimal('1')
                out=Decimal('.75') if count>272000 else Decimal('.5')
                self.assertEqual(price(usage,BACKGROUND_MODEL),
                    (Decimal(count)*Decimal('.1')*multiplier+Decimal(1000)*out)/1000000)
                self.assertEqual(reserve_price(count,1000,BACKGROUND_MODEL),
                    (Decimal(count)*Decimal('.125')*multiplier+Decimal(1000)*out)/1000000)

    def test_model_and_prompt_count_caches_do_not_cross_roles(self):
        client=self.make_client()
        for bg in (True,False,True,False):
            with ai_task('same-task',background=bg): client.generate_report('同一份完整資料')
        self.assertEqual(len(self.calls),2)
        self.assertEqual({json.loads(call.content)['model']for call in self.calls},{MODEL,BACKGROUND_MODEL})
        def count(request):
            self.calls.append(request)
            return httpx.Response(200,json={'object':'response.input_tokens','input_tokens':500})
        client.transport=httpx.MockTransport(count)
        for bg in (True,False,True,False):
            with ai_task('same-task',background=bg): self.assertEqual(client.count_input_tokens('相同資料'),500)
        self.assertEqual(len(self.calls),4)

    def test_estimate_and_prompt_capacity_track_selected_model(self):
        client=self.make_client()
        with ai_task('research'):
            terra=client.estimate_request('完整資料')
        with ai_task('research',background=True):
            luna=client.estimate_request('完整資料')
            self.assertTrue(client.prompt_capacity('完整資料')['reference'].endswith(BACKGROUND_MODEL))
        self.assertEqual(luna,terra/10)

    def test_shared_client_concurrent_tasks_do_not_change_each_other(self):
        client=self.make_client()
        def run(bg):
            with ai_task('concurrent',background=bg):
                return client.generate_report('並行完整資料 '+str(bg)).diagnostics['actual_model']
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(run,[True,False])),[BACKGROUND_MODEL,MODEL])

    def test_async_scheduled_boundary_preserves_model_in_native_worker(self):
        client=self.make_client()
        @task_context('news',background=True)
        async def scheduled():
            return await asyncio.to_thread(client.generate_report,'排程新聞完整資料')
        result=asyncio.run(scheduled())
        self.assertEqual(result.diagnostics['actual_model'],BACKGROUND_MODEL)
        self.assertEqual(client.model,MODEL)

    def test_background_credit_refusal_uses_existing_deepseek_fallback(self):
        client=self.make_client()
        fallback=self.fallback()
        client.ledger.grants=[]
        with ai_task('news',background=True):
            result=RoutedAIService(client,fallback).generate_report('完整資料不刪除')
        self.assertEqual(result.diagnostics['requested_model'],BACKGROUND_MODEL)
        self.assertEqual(result.diagnostics['actual_model'],'deepseek-flash')
        fallback.generate_report.assert_called_once_with('完整資料不刪除')
        self.assertEqual(self.calls,[])

    def test_luna_model_mismatch_keeps_unknown_and_never_retries(self):
        with ai_task('news',background=True), self.assertRaises(OpenAIUnavailable):
            self.client.generate_report('故意回傳Terra的合成測試')
        self.assertEqual(len(self.calls),1)
        with self.ledger.db() as c:
            row=c.execute('SELECT status,actual FROM requests').fetchone()
        self.assertEqual(row,('unknown',None))
