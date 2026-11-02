from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo
import asyncio
import os
import unittest

import httpx

from research_center.ai_budget_service import MonthlyBudget, BudgetBlocked, shared_budget_root
from research_center.deepseek_service import DeepSeekAccount, DeepSeekService, ai_task, DeepSeekUnavailable


class MonthlyBudgetTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ.get("CODEX_TASK_TEST_ROOT", ".cache/test_tmp/monthly_budget")) / uuid4().hex
        self.now = datetime(2026,11,2,10,tzinfo=ZoneInfo("Asia/Taipei"))
        self.budget = MonthlyBudget(self.root, job_limit=0, pacing=False)

    def put(self, amount, background=False, status="success", actual=True, now=None):
        token = uuid4().hex
        value = Decimal(str(amount))
        self.budget.reserve_request(token,"test",background,token,value,now or self.now)
        self.budget.finish(token,status,value if actual else None)
        return token

    def test_changed_news_body_updates_existing_classification_without_duplicate(self):
        from research_center.news_repository import NewsRepository
        from research_center.news_models import NewsItem
        repo=NewsRepository(self.root/'news.sqlite3')
        original=NewsItem(url='https://example.test/article',title='原新聞',full_text='第一版',summary='舊摘要',published_at='2026-11-01T10:00:00')
        repo.save_many([original])
        updated=NewsItem(url=original.url,title='更新新聞',full_text='第二版新增公告',summary='新摘要',published_at=original.published_at)
        repo.save_many([updated])
        rows=repo.query_all_recent()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0].full_text,updated.full_text)
        self.assertEqual(rows[0].summary,'新摘要')

    def test_manual_monthly_limit_does_not_consume_background_share(self):
        self.put(4)
        with self.assertRaises(BudgetBlocked) as c:
            self.budget.check(estimate=Decimal('.01'),now=self.now)
        self.assertEqual(c.exception.reason,'manual_budget_exceeded')
        self.budget.check(estimate=Decimal('1'),background=True,now=self.now)

    def test_total_usable_budget_preserves_safety_buffer(self):
        self.put(4)
        self.put(5,True)
        with self.assertRaises(BudgetBlocked) as c:
            self.budget.check(estimate=Decimal('.01'),now=self.now)
        self.assertEqual(c.exception.reason,'monthly_budget_exceeded')

    def test_background_monthly_limit_preserves_manual_share(self):
        self.put(5,True)
        with self.assertRaises(BudgetBlocked) as c:
            self.budget.check(estimate=Decimal('.01'),background=True,now=self.now)
        self.assertEqual(c.exception.reason,'background_budget_exceeded')
        self.budget.check(estimate=Decimal('1'),now=self.now)

    def test_pending_and_unknown_cost_count_conservatively(self):
        self.put(3,status='unknown',actual=False)
        self.budget.reserve_request('pending','test',False,'job',Decimal('1'),self.now)
        with self.assertRaises(BudgetBlocked):
            self.budget.check(estimate=Decimal('.01'),now=self.now)
        self.assertEqual(self.budget.summary(self.now)['unknown_or_pending'],2)

    def test_failed_unbilled_request_does_not_consume_budget(self):
        self.put(3,status='failed',actual=False)
        self.budget.check(estimate=Decimal('4'),now=self.now)

    def test_new_month_resets_window_without_deleting_history(self):
        self.put(4)
        self.budget.check(estimate=Decimal('4'),now=self.now.replace(month=12))
        self.assertEqual(self.budget.summary(self.now)['manual_used'],'4')

    def test_restart_uses_persistent_ledger(self):
        self.put(4)
        other=MonthlyBudget(self.root,job_limit=0,pacing=False)
        with self.assertRaises(BudgetBlocked):
            other.check(estimate=Decimal('.01'),now=self.now)

    def test_atomic_parallel_reservations_share_limit(self):
        def reserve(i):
            try:
                MonthlyBudget(self.root,job_limit=0,pacing=False).reserve_request(str(i),'test',False,str(i),Decimal('1'),self.now)
                return True
            except BudgetBlocked:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            result=list(pool.map(reserve,range(8)))
        self.assertEqual(sum(result),4)

    def test_job_limit_requires_explicit_fee_confirmation(self):
        self.budget=MonthlyBudget(self.root,job_limit=.30,pacing=False)
        self.budget.reserve_request('one','test',False,'same',Decimal('.25'),self.now)
        with self.assertRaises(BudgetBlocked) as c:
            self.budget.reserve_request('two','test',False,'same',Decimal('.10'),self.now)
        self.assertEqual(c.exception.reason,'job_budget_exceeded')

    def test_schedule_pacing_does_not_spend_entire_month_on_day_one(self):
        self.budget=MonthlyBudget(self.root,job_limit=0,pacing=True)
        with self.assertRaises(BudgetBlocked) as c:
            self.budget.check(estimate=Decimal('2'),background=True,now=self.now)
        self.assertEqual(c.exception.reason,'schedule_pacing')

    def test_incident_notice_once_and_recovery_once(self):
        for _ in range(3): self.budget.incident('insufficient_balance',background=True)
        first=self.budget.claim_notice()
        self.assertIn('官方餘額不足',first[1])
        self.budget.notice_result(first[0],True)
        self.assertIsNone(self.budget.claim_notice())
        self.budget.incident(None,background=True)
        recovery=self.budget.claim_notice()
        self.assertIn('恢復',recovery[1])
        self.budget.notice_result(recovery[0],True)
        self.budget.incident(None,background=True)
        self.assertIsNone(self.budget.claim_notice())

    def test_unknown_notification_delivery_is_not_blindly_retried(self):
        self.budget.incident('authentication_failed',background=True)
        note=self.budget.claim_notice()
        self.budget.notice_result(note[0],False)
        self.assertIsNone(self.budget.claim_notice())

    def test_real_worktree_and_main_resolve_same_budget_location(self):
        from research_center.config import ROOT_DIR
        self.assertEqual(shared_budget_root(ROOT_DIR),shared_budget_root(Path('D:/code/stock_ai_bot')))

    def test_daily_limit_is_not_default_config(self):
        from research_center.config import load_research_config
        config=load_research_config()
        self.assertEqual(config.deepseek_daily_limit_usd,0)
        self.assertEqual(config.deepseek_monthly_limit_usd,10)

    def test_scheduled_quota_failure_notifies_once_and_recovers(self):
        import main
        available=[False]
        requests=[]
        def respond(request):
            requests.append(request)
            return httpx.Response(200,json={'is_available':available[0],'balance_infos':[{'currency':'USD','total_balance':'10' if available[0] else '0'}]})
        account=DeepSeekAccount('fake-key',self.root/'account',transport=httpx.MockTransport(respond))
        client=DeepSeekService(account)
        context=SimpleNamespace(bot=SimpleNamespace())
        with patch.object(main,'safe_send_bot_message',new=AsyncMock()) as send:
            with ai_task('news',True):
                first=asyncio.run(main._scheduled_ai_gate(context,client,123))
                second=asyncio.run(main._scheduled_ai_gate(context,client,123))
                self.assertEqual(first,'insufficient_balance')
                self.assertEqual(second,first)
                self.assertEqual(send.await_count,1)
                self.assertFalse(any(r.method=='POST' for r in requests))
                available[0]=True
                account.balance_checked_at-=61
                self.assertEqual(asyncio.run(main._scheduled_ai_gate(context,client,123)),'')
                self.assertEqual(send.await_count,2)

    def test_news_job_budget_skip_does_not_search_or_send_news(self):
        import main
        center=SimpleNamespace(deepseek=object())
        context=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.object(main,'load_config',return_value={'chat_id':123}), \
             patch('research_center.orchestrator.ResearchCenter',return_value=center), \
             patch.object(main,'_scheduled_ai_gate',new=AsyncMock(return_value='background_budget_exceeded')), \
             patch.object(main,'run_news_refresh') as refresh:
            outcome=asyncio.run(main._scheduled_news_refresh(context))
        self.assertEqual(outcome['status'],'skipped_budget')
        refresh.assert_not_called()
        context.bot.send_message.assert_not_awaited()

    def test_news_mid_search_quota_failure_notifies_immediately_once(self):
        import main
        account=DeepSeekAccount('fake-key',self.root/'client')
        center=SimpleNamespace(deepseek=DeepSeekService(account),config=SimpleNamespace(database_path=self.root/'news.sqlite3'))
        def fail(*args,**kwargs): account.block(402)
        with patch.object(main,'load_config',return_value={'chat_id':123}), \
             patch('research_center.orchestrator.ResearchCenter',return_value=center), \
             patch.object(main,'_scheduled_ai_gate',new=AsyncMock(return_value='')), \
             patch.object(main,'run_news_refresh',side_effect=fail), \
             patch.object(main,'safe_send_bot_message',new=AsyncMock()) as send:
            for _ in range(2):
                with self.assertRaises(DeepSeekUnavailable):
                    asyncio.run(main._scheduled_news_refresh(SimpleNamespace(bot=object())))
            self.assertEqual(send.await_count,1)
            self.assertIn('官方餘額不足',send.await_args.args[2])

    def test_exhausted_monthly_scope_notifies_without_any_http(self):
        import main
        self.put(5,True)
        requests=[]
        def forbidden(request):
            requests.append(request)
            raise AssertionError('budget exhaustion must stop before HTTP')
        account=DeepSeekAccount('fake-key',self.root/'client',budget_root=self.root,transport=httpx.MockTransport(forbidden))
        with patch.object(main,'safe_send_bot_message',new=AsyncMock()) as send, ai_task('news',True):
            reason=asyncio.run(main._scheduled_ai_gate(SimpleNamespace(bot=object()),DeepSeekService(account),123))
            self.assertEqual(reason,'background_budget_exceeded')
            self.assertEqual(send.await_count,1)
            self.assertIn('排程預算',send.await_args.args[2])
        self.assertEqual(requests,[])

    def test_transport_incident_not_cleared_by_balance_probe_alone(self):
        self.budget.incident('transport_unknown',background=True)
        first=self.budget.claim_notice()
        self.budget.notice_result(first[0],True)
        self.budget.incident(None,background=True,preflight=True)
        self.assertIsNone(self.budget.claim_notice())
        self.budget.incident(None,background=True)
        self.assertIn('恢復',self.budget.claim_notice()[1])

    def test_queue_budget_skip_is_not_completed_and_next_job_runs(self):
        from research_center.scheduled_task_service import ScheduledTaskService, ScheduledTaskSpec
        from research_center.command_runtime_service import CommandRuntimeService
        audit=[]
        service=ScheduledTaskService(runtime=CommandRuntimeService(),sink=lambda s:None,audit_sink=lambda spec,status,event:audit.append((spec.task_id,status)))
        async def blocked(): return {'status':'skipped_budget','reason':'預算不足'}
        async def normal(): return None
        async def run():
            await service._run_one(ScheduledTaskSpec('budget','額度不足測試'),blocked)
            await service._run_one(ScheduledTaskSpec('next','後續正常任務'),normal)
        asyncio.run(run())
        self.assertIn(('budget','skipped_budget'),audit)
        self.assertNotIn(('budget','completed'),audit)
        self.assertIn(('next','completed'),audit)
