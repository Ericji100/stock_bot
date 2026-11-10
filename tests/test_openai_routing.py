from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import sqlite3
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from uuid import uuid4

import httpx

from research_center.config import ResearchCenterConfig
from research_center.deepseek_service import DeepSeekResult, DeepSeekUnavailable, ai_task
from research_center.openai_credit_service import OpenAICreditLedger, OpenAICreditsUnavailable, OpenAIUnavailable
from research_center.openai_service import OpenAIService, MODEL, price, billable_search_calls
from research_center.ai_provider_service import RoutedAIService, create_ai_client


def grant(name='a', amount='100', org=None, end='2026-12-01T00:00:00+00:00'):
    return dict(grant_id=name, account_id=name, organization_id=org or name,
        starts_at='2026-11-01T00:00:00+00:00', expires_at=end,
        verified_at='2026-11-01T00:00:00+00:00', evidence='offline synthetic fixture', allowance_usd=amount)


def response(text='完整正式分析', **kwargs):
    return dict(id='resp_fixture', model=MODEL, status='completed',
        output=[{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}],
        usage={'input_tokens': 1000, 'output_tokens': 100,
               'output_tokens_details': {'reasoning_tokens': 75}}, **kwargs)


class OpenAIRoutingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ.get('CODEX_TASK_TEST_ROOT', '.cache/test_tmp')) / uuid4().hex
        self.now = datetime(2026, 11, 4, tzinfo=timezone.utc)
        self.calls = []
        self.ledger = OpenAICreditLedger(self.root, [grant()], job_limit=0, now=lambda: self.now)
        def handler(request):
            self.calls.append(request)
            return httpx.Response(200, json=response())
        self.client = OpenAIService(self.ledger, {'a': 'synthetic-key'}, transport=httpx.MockTransport(handler))

    def fallback(self):
        f = Mock(model='deepseek-flash', organizer=False, timeout_seconds=180, max_output_tokens=32768)
        f.generate_report.return_value = DeepSeekResult('備援正式分析', {}, {'provider': 'deepseek', 'actual_model': 'deepseek-flash'})
        f.generate_json.return_value = DeepSeekResult('{}', {}, {'provider': 'deepseek', 'actual_model': 'deepseek-flash'})
        f.prompt_capacity.return_value = {'fits': True}
        return f

    def test_all_ten_command_roles_are_fixed_terra_xhigh(self):
        fallback = self.fallback()
        router = RoutedAIService(self.client, fallback)
        for command in ['research', 'value_scan', 'macro', 'theme', 'theme_flow', 'theme_radar',
                        'sector_strength', 'radar', 'news', 'topic_maintain']:
            with self.subTest(command=command), ai_task(command):
                result = router.generate_report('完整資料與來源：' + command)
                self.assertEqual(result.diagnostics['provider'], 'openai')
                body = json.loads(self.calls[-1].content)
                self.assertEqual(body['model'], MODEL)
                self.assertEqual(body['reasoning'], {'effort': 'xhigh'})
                self.assertFalse(body['store'])
                self.assertEqual(body['truncation'], 'disabled')
                self.assertIn('完整資料與來源', body['input'])
        fallback.generate_report.assert_not_called()

    def test_live_acceptance_rejects_core_data_only_in_appendix(self):
        from tools.terra_live_validation import report_quality_blockers
        metadata = {'high_model_input_package': {'command_specific_data': {'core_input_audit': {
            'sections': [{'section': 'ai_candidate_evidence_pack', 'raw_count': 2, 'status': 'appendix_only'}]}}}}
        self.assertTrue(report_quality_blockers(metadata, [object()]))
        metadata['high_model_input_package']['command_specific_data']['core_input_audit']['sections'][0]['status'] = 'direct'
        self.assertEqual(report_quality_blockers(metadata, [object()]), [])
        self.assertTrue(report_quality_blockers(metadata, []))

    def test_radar_case_exception_needs_receipt_and_cannot_expand_other_limits(self):
        from tools.terra_live_validation import authorized_case_limit, ROUND
        self.assertEqual(authorized_case_limit('radar', self.root), 5)
        path = self.root/'radar-limit-approval.json'
        self.root.mkdir(parents=True, exist_ok=True)
        approval = {'round_id': ROUND, 'case': 'radar', 'case_limit_usd': '7',
                    'round_limit_usd': '25', 'approved': True, 'user_confirmation': 'synthetic approval'}
        path.write_text(json.dumps(approval), encoding='utf-8')
        self.assertEqual(authorized_case_limit('radar', self.root), 7)
        self.assertEqual(authorized_case_limit('research', self.root), 5)
        for invalid in ({'round_limit_usd': '26'}, {'case_limit_usd': '8'}, {'approved': False}, {'case': 'news'}):
            path.write_text(json.dumps({**approval, **invalid}), encoding='utf-8')
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                authorized_case_limit('radar', self.root)

    def test_live_secondary_grant_requires_independent_verification_receipt(self):
        from tools.terra_live_validation import live_grants
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root/'grant-verification.json').write_text(json.dumps(grant()), encoding='utf-8')
        self.assertEqual(len(live_grants(self.root)), 1)
        row = {**grant('secondary'), 'key_project': 'synthetic-project',
               'verification_basis': 'user_supplied_billing_screenshot_and_ids'}
        path = self.root/'secondary-grant-verification.json'
        path.write_text(json.dumps(row), encoding='utf-8')
        self.assertEqual([item['account_id'] for item in live_grants(self.root)], ['a', 'secondary'])
        for change in ({'account_id': 'a'}, {'key_project': ''}, {'verification_basis': 'assumed'}):
            with self.subTest(change=change):
                path.write_text(json.dumps({**row, **change}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    live_grants(self.root)

    def test_news_case_exception_needs_receipt_and_cannot_change_round_or_research(self):
        from tools.terra_live_validation import authorized_case_limit, ROUND
        self.assertEqual(authorized_case_limit('news', self.root), 5)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root/'news-limit-approval.json'
        approval = {'round_id': ROUND, 'case': 'news', 'case_limit_usd': '6', 'round_limit_usd': '25',
                    'approved': True, 'user_confirmation': 'synthetic user approval'}
        path.write_text(json.dumps(approval), encoding='utf-8')
        self.assertEqual(authorized_case_limit('news', self.root), 6)
        self.assertEqual(authorized_case_limit('research', self.root), 5)
        for change in ({'case': 'radar'}, {'case_limit_usd': '7'}, {'round_limit_usd': '26'}, {'approved': False}):
            with self.subTest(change=change):
                path.write_text(json.dumps({**approval, **change}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    authorized_case_limit('news', self.root)

    def test_topic_case_exception_requires_exact_seven_dollar_receipt(self):
        from tools.terra_live_validation import authorized_case_limit, ROUND
        self.assertEqual(authorized_case_limit('topic_maintain', self.root), 5)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root/'topic_maintain-limit-approval.json'
        approval = {'round_id': ROUND, 'case': 'topic_maintain', 'case_limit_usd': '7',
                    'round_limit_usd': '25', 'approved': True, 'user_confirmation': 'synthetic approval'}
        path.write_text(json.dumps(approval), encoding='utf-8')
        self.assertEqual(authorized_case_limit('topic_maintain', self.root), 7)
        self.assertEqual(authorized_case_limit('research', self.root), 5)
        for change in ({'case_limit_usd': '8'}, {'round_limit_usd': '26'}, {'approved': False}):
            with self.subTest(change=change):
                path.write_text(json.dumps({**approval, **change}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    authorized_case_limit('topic_maintain', self.root)

    def test_live_operator_pause_cannot_be_bypassed_by_second_account(self):
        from tools.terra_live_validation import assert_no_operator_pause
        self.ledger = OpenAICreditLedger(self.root, [grant('a'), grant('b')], now=lambda: self.now)
        self.ledger.block(self.ledger.grants[0], 'user_pause_reboot_fixture')
        with self.assertRaises(OpenAIUnavailable) as caught:
            assert_no_operator_pause(self.ledger)
        self.assertEqual(caught.exception.status, 'authorized_live_operator_pause')

    def test_live_limit_latch_blocks_all_registered_accounts(self):
        from tools.terra_live_validation import latch_live_case_cap, assert_no_operator_pause, ROUND
        ledger = OpenAICreditLedger(self.root, [grant('a'), grant('b')], now=lambda: self.now,
            development=True, round_id=ROUND, round_limit=25, case_id='news')
        error = OpenAICreditsUnavailable('fixture', 'openai_job_budget_exceeded')
        self.assertTrue(latch_live_case_cap(ledger, ledger.grants[0], 'news', error, self.root/'proof'))
        with ledger.db() as conn:
            self.assertEqual({row[0] for row in conn.execute('select account_id from account_blocks')}, {'a', 'b'})
        with self.assertRaises(OpenAIUnavailable):
            assert_no_operator_pause(ledger)

    def test_round_case_caps_accumulate_across_accounts(self):
        from tools.terra_live_validation import ROUND
        ledger = OpenAICreditLedger(self.root, [grant('a'), grant('b')], job_limit=5, now=lambda: self.now,
            development=True, round_id=ROUND, round_limit=25, case_id='research')
        with ai_task('research'):
            _, ident = ledger.choose({'a'}, Decimal('4.9'), reserve=True)
            ledger.finish(ident, 'success', Decimal('4.9'), {})
            with self.assertRaises(OpenAICreditsUnavailable) as caught:
                ledger.choose({'b'}, Decimal('.11'), reserve=True)
            self.assertEqual(caught.exception.status, 'openai_job_budget_exceeded')
        other = OpenAICreditLedger(self.root, ledger.grants, job_limit=5, now=lambda: self.now,
            development=True, round_id=ROUND, round_limit=25, case_id='news')
        with other.db() as conn:
            conn.execute("UPDATE requests SET actual='24.9'")
        with ai_task('news'), self.assertRaises(OpenAIUnavailable) as caught:
            other.choose({'b'}, Decimal('.11'), reserve=True)
        self.assertEqual(caught.exception.status, 'openai_round_budget_exceeded')

    def test_live_case_cap_latch_preserves_fees_and_blocks_later_generation(self):
        from tools.terra_live_validation import latch_live_case_cap, ROUND
        ledger = OpenAICreditLedger(self.root, [grant()], job_limit=1, now=lambda: self.now,
            development=True, round_id=ROUND, round_limit=25, case_id='news')
        with ai_task('news'):
            chosen, ident = ledger.choose({'a'}, Decimal('.2'), reserve=True)
            ledger.finish(ident, 'success', Decimal('.2'), {})
            error = OpenAICreditsUnavailable('合計預留過高', 'openai_job_budget_exceeded')
            self.assertTrue(latch_live_case_cap(ledger, chosen, 'news', error, self.root/'evidence'))
            with self.assertRaises(OpenAICreditsUnavailable):
                ledger.choose({'a'}, Decimal('.01'), reserve=True)
        with ledger.db() as conn:
            self.assertEqual(conn.execute('SELECT actual FROM requests').fetchall(), [('0.2',)])
        self.assertEqual(ledger.job_limit, Decimal('1'))
        self.assertFalse(latch_live_case_cap(self.ledger, chosen, 'news', error, self.root/'formal-proof'))
        self.assertFalse((self.root/'formal-proof').exists())

    def test_radar_checkpoint_preserves_input_and_uses_native_remaining_stages(self):
        from tools.terra_live_validation import resume_radar_checkpoint
        from stock_ai_bot.monitoring import radar_service as radar
        path = self.root/'radar/attempt/result.json'
        path.parent.mkdir(parents=True)
        candidates = [radar.RadarCandidate(code=str(1000+i), name='合成候選', total_score=i) for i in range(15)]
        codes = [item.code for item in candidates]
        original = json.dumps({'report_date': '2026-11-04', 'request': {'source': 'combined', 'ai_top': 15,
            'report_date': '2026-11-04', 'model': 'deepseek', 'ai_comment_enabled': True, 'scoring_version': 'v3'},
            'candidates': [asdict(item) for item in candidates], 'ai_enriched_codes': codes, 'diagnostics': {}}, default=list)
        path.write_text(original, encoding='utf-8')
        with patch.object(radar, 'run_radar', side_effect=AssertionError('不得冒稱重新全市場收集')), \
                patch.object(radar, '_attach_research_center_sources') as sources, \
                patch.object(radar, '_ensure_radar_source_sufficiency') as sufficient, \
                patch.object(radar, '_attach_research_evidence_packs') as evidence, \
                patch.object(radar, '_attach_ai_comments', return_value={'comment_count': 15}) as analysis, \
                patch.object(radar, '_score_candidates') as scoring, \
                patch.object(radar, '_attach_base_evidence_packs'), patch.object(radar, 'save_radar_result'):
            result, proof = resume_radar_checkpoint(path, lambda message: None, self.root)
        self.assertEqual({item.code for item in result.candidates}, set(codes))
        self.assertFalse(proof['new_full_market_collection'])
        self.assertEqual(proof['candidate_count'], 15)
        self.assertEqual(path.read_text(encoding='utf-8'), original)
        for call in (sources, sufficient, evidence, analysis, scoring):
            call.assert_called_once()
        with self.assertRaises(ValueError):
            resume_radar_checkpoint(self.root/'other/result.json', lambda message: None, self.root)

    def test_radar_checkpoint_restores_legacy_set_literal_without_character_splitting(self):
        from tools.terra_live_validation import restore_radar_candidates
        from stock_ai_bot.monitoring import radar_service as radar
        for value in ("{'A', 'B'}", ['A', 'B']):
            restored = restore_radar_candidates([{'code': '1000', 'strategy_codes': value}])[0]
            self.assertEqual(restored.strategy_codes, {'A', 'B'})
            encoded = radar._json_safe(asdict(restored))
            self.assertIsInstance(encoded['strategy_codes'], list)
            self.assertEqual(set(encoded['strategy_codes']), {'A', 'B'})
        with self.assertRaises(ValueError):
            restore_radar_candidates([{'code': '1000', 'strategy_codes': {'A': True}}])

    def test_live_health_guard_is_read_only_and_rejects_stale_or_stopped_production(self):
        from tools.terra_live_validation import validate_production_health
        restored = {'bot_pid': 101, 'watchdog_pid': 102}
        healthy = {'pid': 101, 'updated_at': '2026-11-04T00:00:00+00:00', 'job_queue_available': True}
        check = Mock(return_value=True)
        validate_production_health(restored, healthy, now=self.now, process_alive=check)
        self.assertEqual(check.call_args_list, [unittest.mock.call(101), unittest.mock.call(102)])
        for heartbeat, alive in [(dict(healthy, updated_at='2026-11-03T23:00:00+00:00'), True),
                                 (dict(healthy, pid=103), True),
                                 (dict(healthy, job_queue_available=False), True), (healthy, False)]:
            with self.subTest(heartbeat=heartbeat, alive=alive), self.assertRaises(ValueError):
                validate_production_health(restored, heartbeat, now=self.now, process_alive=lambda pid: alive)

    def test_70_20_10_limits_are_independent_and_buffer_not_spent(self):
        for bg, total in [(True, '70'), (False, '20')]:
            with ai_task('fixture', background=bg):
                _, ident = self.ledger.choose({'a'}, Decimal(total), reserve=True)
                self.ledger.finish(ident, 'success', Decimal(total), {})
                with self.assertRaises(OpenAICreditsUnavailable):
                    self.ledger.choose({'a'}, Decimal('.01'))
        self.assertEqual(self.ledger.summary()['grants'][0]['used_or_reserved'], '90')

    def test_expiry_does_not_invent_next_month_grant(self):
        self.now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        with self.assertRaises(OpenAICreditsUnavailable):
            self.ledger.choose({'a'}, Decimal('.01'))

    def test_duplicate_org_and_overlapping_cycles_rejected(self):
        with self.assertRaises(ValueError):
            OpenAICreditLedger(self.root, [grant('a', org='same'), grant('b', org='same')])

    def test_checkpoint_cannot_be_changed_to_reset_spending(self):
        self.ledger.choose({'a'})
        other = OpenAICreditLedger(self.root, [grant(amount='200')], now=lambda: self.now)
        with self.assertRaises(ValueError):
            other.choose({'a'})

    def test_earlier_expiring_account_is_used_first(self):
        ledger = OpenAICreditLedger(self.root/'two', [grant('a'), grant('b', end='2026-11-21T00:00:00Z')], now=lambda: self.now)
        self.assertEqual(ledger.choose({'a', 'b'})[0]['account_id'], 'b')

    def test_proven_credit_exhaustion_uses_second_account(self):
        self.ledger = OpenAICreditLedger(self.root, [grant('a'), grant('b')], now=lambda: self.now)
        def handler(req):
            self.calls.append(req)
            return httpx.Response(429, json={'error': {'code': 'credit_balance_exhausted'}}) if req.headers['Authorization'].endswith('first') else httpx.Response(200, json=response())
        client = OpenAIService(self.ledger, {'a': 'first', 'b': 'second'}, transport=httpx.MockTransport(handler))
        client.generate_report('資料')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.ledger.summary()['grants'][0]['used_or_reserved'], '0')

    def test_exhausted_allowance_falls_back_with_original_prompt(self):
        self.client.accounts = {}
        fallback = self.fallback()
        with ai_task('research'):
            result = RoutedAIService(self.client, fallback).generate_report('不可刪的完整資料')
        fallback.generate_report.assert_called_once_with('不可刪的完整資料')
        self.assertEqual(result.diagnostics['fallback_reason'], 'openai_not_configured')
        self.assertEqual(result.diagnostics['actual_model'], 'deepseek-flash')

    def test_timeout_retains_unknown_fee_and_does_not_call_fallback(self):
        def timeout(req):
            raise httpx.ReadTimeout('fixture')
        self.client.transport = httpx.MockTransport(timeout)
        fallback = self.fallback()
        with ai_task('research'):
            with self.assertRaises(DeepSeekUnavailable):
                RoutedAIService(self.client, fallback).generate_report('資料')
            with self.assertRaises(DeepSeekUnavailable):
                self.ledger.choose({'a'})
        fallback.generate_report.assert_not_called()
        self.assertEqual(self.ledger.summary()['grants'][0]['unknown_or_pending'], 1)

    def test_generic_429_and_auth_never_rotate_or_call_fallback(self):
        for code in [429, 401, 403, 500]:
            with self.subTest(code=code), ai_task('research'):
                client = self.client._clone()
                client.transport = httpx.MockTransport(lambda r: httpx.Response(code, json={'error': {'code': 'rate_limit_exceeded'}}))
                fallback = self.fallback()
                with self.assertRaises(DeepSeekUnavailable):
                    RoutedAIService(client, fallback).generate_report('資料')
                fallback.generate_report.assert_not_called()

    def test_price_counts_reasoning_once_and_long_context(self):
        self.assertEqual(price(response()['usage']), Decimal('.0015'))
        self.assertEqual(price({'input_tokens': 300000, 'output_tokens': 1000,
                                'input_tokens_details': {'cached_tokens': 100000}}), Decimal('.4175'))

    def test_cache_write_price_not_counted_as_ordinary_input(self):
        self.assertEqual(price({'input_tokens': 12000, 'output_tokens': 0,
                                'input_tokens_details': {'cache_write_tokens': 12000}}), Decimal('.015'))
        self.assertEqual(price({'input_tokens': 12000, 'output_tokens': 0,
            'input_tokens_details': {'cache_write_tokens': 12000, 'cache_write_tokens_details':
                {'ttl_30m_tokens': 10000, 'ttl_12h_tokens': 2000}}}), Decimal('.0175'))
        with self.assertRaises(ValueError):
            price({'input_tokens': 10, 'output_tokens': 0,
                   'input_tokens_details': {'cached_tokens': 5, 'cache_write_tokens': 6}})

    def test_round_limit_spans_commands_and_cannot_reset(self):
        ledger = OpenAICreditLedger(self.root, [grant()], job_limit=5, development=True,
                now=lambda: self.now, round_id='fixed-round', round_limit=25)
        for i in range(5):
            with ai_task('case' + str(i)):
                _, ident = ledger.choose({'a'}, 5, reserve=True)
                ledger.finish(ident, 'success', 5, {})
        with ai_task('sixth'), self.assertRaises(DeepSeekUnavailable):
            ledger.choose({'a'}, Decimal('.01'), reserve=True)
        other = OpenAICreditLedger(self.root, [grant()], development=True,
                now=lambda: self.now, round_id='fixed-round', round_limit=26)
        with self.assertRaises(ValueError):
            other.choose({'a'})

    def test_round_unknown_cost_blocks_other_command(self):
        ledger = OpenAICreditLedger(self.root, [grant()], development=True,
                now=lambda: self.now, round_id='fixed-round', round_limit=25)
        with ai_task('first'):
            _, ident = ledger.choose({'a'}, 1, reserve=True)
            ledger.finish(ident, 'unknown', None, {})
        with ai_task('different'), self.assertRaises(DeepSeekUnavailable):
            ledger.choose({'a'}, 1, reserve=True)

    def test_case_cap_cannot_reset_by_changing_run_id(self):
        ledger = OpenAICreditLedger(self.root, [grant()], development=True, job_limit=5,
                now=lambda: self.now, round_id='fixed-round', round_limit=25, case_id='research')
        with ai_task('first'):
            _, ident = ledger.choose({'a'}, 4, reserve=True)
            ledger.finish(ident, 'success', 4, {})
        with ai_task('new-run'), self.assertRaises(OpenAICreditsUnavailable):
            ledger.choose({'a'}, 2, reserve=True)

    def test_acknowledged_unknown_keeps_full_reserve_and_case_cap(self):
        ledger = OpenAICreditLedger(self.root, [grant()], development=True, job_limit=5,
                now=lambda: self.now, round_id='fixed-round', round_limit=25, case_id='research')
        with ai_task('first'):
            _, ident = ledger.choose({'a'}, Decimal('2.8'), reserve=True)
            ledger.finish(ident, 'unknown', None, {'reason': 'crash'})
        with ai_task('next'):
            ledger.choose({'a'}, 2, acknowledged_unknown_ids=(ident,))
            with self.assertRaises(OpenAICreditsUnavailable):
                ledger.choose({'a'}, Decimal('2.3'), acknowledged_unknown_ids=(ident,))
        with ledger.db() as conn:
            self.assertEqual(conn.execute('SELECT status,estimate,actual FROM requests WHERE id=?', (ident,)).fetchone(),
                             ('unknown', '2.8', None))

    def test_acknowledged_unknown_still_counts_round_cap_and_new_unknown_stops(self):
        ledger = OpenAICreditLedger(self.root, [grant()], development=True, job_limit=0,
                now=lambda: self.now, round_id='fixed-round', round_limit=3)
        with ai_task('first'):
            _, first = ledger.choose({'a'}, Decimal('2.8'), reserve=True)
            ledger.finish(first, 'unknown', None, {})
        with ai_task('next'):
            with self.assertRaises(OpenAIUnavailable) as error:
                ledger.choose({'a'}, Decimal('.3'), acknowledged_unknown_ids=(first,))
            self.assertEqual(error.exception.status, 'openai_round_budget_exceeded')
            _, second = ledger.choose({'a'}, Decimal('.1'), reserve=True, acknowledged_unknown_ids=(first,))
            ledger.finish(second, 'unknown', None, {})
        with ai_task('third'), self.assertRaises(OpenAIUnavailable) as error:
            ledger.choose({'a'}, 0, acknowledged_unknown_ids=(first,))
        self.assertEqual(error.exception.status, 'transport_unknown')

    def test_unknown_acknowledgement_cannot_skip_production_pending_or_wrong_id(self):
        with self.assertRaises(ValueError):
            self.ledger.choose({'a'}, acknowledged_unknown_ids=('not-authorized',))
        ledger = OpenAICreditLedger(self.root, [grant()], development=True,
                now=lambda: self.now, round_id='fixed-round', round_limit=25)
        with ai_task('first'):
            _, ident = ledger.choose({'a'}, 1, reserve=True)
        for ids in ((ident,), ('not-in-this-round',)):
            with self.subTest(ids=ids), ai_task('next'), self.assertRaises(ValueError):
                ledger.choose({'a'}, acknowledged_unknown_ids=ids)

    def test_live_unknown_resume_requires_exact_receipt_and_original_reserve(self):
        from tools.terra_live_validation import authorized_unknown_reservations, ROUND
        ledger = OpenAICreditLedger(self.root, [grant()], development=True,
                now=lambda: self.now, round_id=ROUND, round_limit=25, case_id='topic_maintain')
        with ai_task('first'):
            _, ident = ledger.choose({'a'}, Decimal('2.79884'), reserve=True)
            ledger.finish(ident, 'unknown', None, {})
        proof = self.root/'proof'
        proof.mkdir()
        path = proof/'crash-reserved-unknown-approval.json'
        self.assertEqual(authorized_unknown_reservations(ledger, proof), ())
        receipt = {'approved': True, 'round_id': ROUND, 'round_limit_usd': '25',
            'question_id': 'call_f00523a211eb41f68c5a8f2cfd2119f3', 'user_confirmation': 'synthetic approval',
            'request_id': ident, 'reserved_unknown_usd': '2.79884'}
        path.write_text(json.dumps(receipt), encoding='utf-8')
        self.assertEqual(authorized_unknown_reservations(ledger, proof), (ident,))
        path.write_text(json.dumps({**receipt, 'reserved_unknown_usd': '0'}), encoding='utf-8')
        with self.assertRaises(ValueError):
            authorized_unknown_reservations(ledger, proof)

    def test_round_reservation_is_atomic_across_instances(self):
        accepted, errors = [], []
        barrier = threading.Barrier(4)
        def reserve():
            ledger = OpenAICreditLedger(self.root, [grant()], development=True,
                    now=lambda: self.now, round_id='fixed-round', round_limit=25)
            with ai_task('parallel'):
                try:
                    barrier.wait(timeout=10)
                    accepted.append(ledger.choose({'a'}, 4, reserve=True))
                except DeepSeekUnavailable:
                    pass
                except Exception as exc:
                    errors.append(exc)
        threads = [threading.Thread(target=reserve) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(accepted), 1)

    def test_incomplete_json_missing_usage_not_successfully_cached(self):
        for case in ['incomplete', 'json', 'usage']:
            with self.subTest(case=case), ai_task('research'):
                data = response('not-json')
                if case == 'incomplete':
                    data.update(status='incomplete', incomplete_details={'reason': 'max_output_tokens'})
                if case == 'usage':
                    data.pop('usage')
                self.client.transport = httpx.MockTransport(lambda r: httpx.Response(200, json=data))
                with self.assertRaises(Exception):
                    self.client.generate_json('資料'+case)
        with self.ledger.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM cache').fetchone()[0], 0)

    def test_json_compatibility_keeps_existing_classifiers(self):
        self.client.transport = httpx.MockTransport(lambda r: httpx.Response(200, json=response('{"facts":[]}')))
        result = self.client.generate_json('資料')
        self.assertEqual(result.raw['choices'][0]['message']['content'], '{"facts":[]}')

    def test_model_cache_no_second_charge(self):
        self.client.generate_report('同一資料')
        result = self.client.generate_report('同一資料')
        self.assertTrue(result.diagnostics['cache_hit'])
        self.assertEqual(len(self.calls), 1)

    def test_both_accounts_atomic_no_overspend(self):
        results, errors = [], []
        barrier = threading.Barrier(4)
        def reserve():
            with ai_task('fixture'):
                try:
                    barrier.wait(timeout=10)
                    results.append(self.ledger.choose({'a'}, Decimal('15'), reserve=True))
                except OpenAICreditsUnavailable:
                    pass
                except Exception as exc:
                    errors.append(exc)
        threads = [threading.Thread(target=reserve) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)

    def test_concurrent_legacy_schema_upgrade_keeps_original_accounting(self):
        self.root.mkdir(parents=True)
        with sqlite3.connect(self.root / 'openai-credits.sqlite3') as conn:
            conn.execute('CREATE TABLE requests (id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, '
                         'background INTEGER NOT NULL, command TEXT, run_id TEXT, operation TEXT, '
                         'status TEXT NOT NULL, estimate TEXT NOT NULL, actual TEXT, detail TEXT)')
            original = ('old', 'a', 0, 'research', 'original-run', 'model', 'success', '1', '0.5', '{}')
            conn.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?)', original)
        barrier, errors, snapshots = threading.Barrier(4), [], []
        def upgrade():
            ledger = OpenAICreditLedger(self.root, [grant()], now=lambda: self.now)
            try:
                barrier.wait(timeout=10)
                with ledger.db() as conn:
                    snapshots.append(conn.execute('SELECT * FROM requests WHERE id=?', ('old',)).fetchone())
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=upgrade) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(snapshots), 4)
        self.assertTrue(all(row == original + ('production', '', '') for row in snapshots))

    def test_search_accepts_only_tool_cited_urls_not_generated_urls(self):
        data = response(json.dumps({'results': [{'url': 'https://example.com/official', 'summary': '摘要'}, {'url': 'https://invented.invalid', 'summary': '虛構'}]}))
        data['output'].insert(0, {'type': 'web_search_call', 'status': 'completed', 'action': {'type': 'search', 'sources': [{'url': 'https://example.com/official', 'title': '官方來源'}]}})
        self.client.transport = httpx.MockTransport(lambda r: httpx.Response(200, json=data))
        result = self.client.search('查詢', '2026-11-04')
        request_path = next((self.root/'requests').glob('*-request.json'))
        body = json.loads(request_path.read_text(encoding='utf-8'))['body']
        self.assertNotIn('text', body)
        self.assertEqual(body['tool_choice'], {'type': 'web_search'})
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['results'][0]['content'], '')
        self.assertIsNone(result['results'][0]['date'])
        self.assertEqual(result['results'][0]['summary'], '摘要')

    def test_notices_deduplicate_and_delivery_unknown_not_resent(self):
        with ai_task('news', background=True):
            self.ledger.route('deepseek', 'openai_credits_unavailable')
            self.ledger.route('deepseek', 'openai_credits_unavailable')
        notice = self.ledger.claim_notice()
        self.assertIn('不是自動加值', notice[1])
        self.ledger.notice_result(notice[0], False)
        self.assertIsNone(self.ledger.claim_notice())

    def test_search_tracking_urls_match_without_trusting_uncited_summary(self):
        data = response(json.dumps({'results': [
            {'url': 'https://official.example/report?id=10', 'title': '官方財報', 'summary': '有來源的摘要'},
            {'url': 'https://official.example/report?id=11', 'summary': '未獲引用，不可採用'}]}))
        data['output'].insert(0, {'type': 'web_search_call', 'status': 'completed', 'action': {'type': 'search', 'sources': [
            {'url': 'https://official.example/report?id=10&trk=ref&utm_source=openai'}]}})
        self.client.transport = httpx.MockTransport(lambda r: httpx.Response(200, json=data))
        result = self.client.search('官方財報')
        self.assertEqual(len(result['results']), 1)
        row = result['results'][0]
        self.assertEqual(row['url'], 'https://official.example/report?id=10')
        self.assertEqual(row['summary'], '有來源的摘要')
        self.assertIn('utm_source', row['search_evidence_url'])
        self.assertIsNone(row['date'])

    def test_only_completed_search_actions_incur_search_tool_fee(self):
        data = {'output': [
            {'type': 'web_search_call', 'status': 'completed', 'action': {'type': 'search'}},
            {'type': 'web_search_call', 'status': 'completed', 'action': {'type': 'open_page'}},
            {'type': 'web_search_call', 'status': 'searching', 'action': {'type': 'search'}},
            {'type': 'web_search_call', 'status': 'completed', 'action': {'type': 'find_in_page'}}]}
        self.assertEqual(billable_search_calls(data), 1)

    def test_round_search_snapshot_reuses_successful_raw_response_without_reset(self):
        ledger = OpenAICreditLedger(self.root/'snapshot', [grant()], development=True,
            now=lambda: self.now, round_id='fixed-round', round_limit=25, case_id='research')
        data = response(json.dumps({'results': [{'url':'https://official.example/report','summary':'官方摘要'}]}))
        data['output'].insert(0, {'type':'web_search_call','status':'completed',
            'action':{'type':'search','sources':[{'url':'https://official.example/report'}]}})
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200,json=data)
        client = OpenAIService(ledger, {'a':'synthetic-key'}, validation_snapshot=True,
                               transport=httpx.MockTransport(handler))
        with ai_task('research'):
            client.search('官方證據','2026-11-04')
        before = ledger.summary()['grants'][0]['used_or_reserved']
        with ledger.db() as conn:
            conn.execute('UPDATE cache SET expires=0')
        with ai_task('research'):
            result = client.search('官方證據','2026-11-04')
        self.assertEqual(len(calls),1)
        self.assertEqual(result['diagnostics']['cache_scope'],'authorized_validation_round_snapshot')
        self.assertEqual(ledger.summary()['grants'][0]['used_or_reserved'],before)
        with self.assertRaises(ValueError):
            OpenAIService(self.ledger, {'a':'synthetic-key'}, validation_snapshot=True)

    def test_date_recovery_uses_fetched_metadata_without_dropping_sources(self):
        from research_center.models import SourceItem
        from research_center.web_fetch_enrichment import recover_search_publication_dates
        sources = [SourceItem('S001', '官方財報', 'https://official.example/report', 'L1_official',
                              snippet='模型摘要：2026年10月15日'),
                   SourceItem('S002', '未知來源', 'https://example.com/unknown', 'L2_media')]
        def fetch(request, candidates, diagnostics, progress, *, prefer_official=False):
            self.assertTrue(prefer_official)
            candidates[0] = replace(candidates[0], published_date='2026-10-16', date_status='explicit',
                                     fetch_status='success', fetch_provider='requests_bs4')
        diagnostics = {}
        with patch('research_center.web_fetch_enrichment._enrich_sources_with_web_fetch', side_effect=fetch):
            recover_search_publication_dates(SimpleNamespace(command='research'), sources, diagnostics)
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0].published_date, '2026-10-16')
        self.assertIsNone(sources[1].published_date)
        self.assertEqual(diagnostics['publication_date_recovery']['explicit_dates_recovered'], 1)

    def test_date_recovery_can_fetch_official_indexed_link_without_model_summary(self):
        from research_center.models import SourceItem
        from research_center.web_fetch_enrichment import recover_search_publication_dates
        sources = [SourceItem('S001', '官方財報', 'https://official.example/news/report', 'L1_official')]
        def fetch(request, candidates, diagnostics, progress, *, prefer_official=False):
            self.assertTrue(prefer_official)
            self.assertEqual(len(candidates), 1)
            candidates[0] = replace(candidates[0], published_date='2026-10-16', date_status='explicit',
                                    snippet='原文財報', fetch_status='success')
        with patch('research_center.web_fetch_enrichment._enrich_sources_with_web_fetch', side_effect=fetch):
            recover_search_publication_dates(SimpleNamespace(command='research'), sources, {})
        self.assertEqual(sources[0].snippet, '原文財報')
        self.assertEqual(sources[0].date_status, 'explicit')

    def test_date_recovery_reserves_official_without_displacing_all_media(self):
        from research_center.models import SourceItem
        from research_center.web_fetch_enrichment import _enrich_sources_with_web_fetch
        sources = [SourceItem(f'S{i:03}', '財報', f'https://official.example/news/{i}', 'L1_official') for i in range(8)]
        sources += [SourceItem(f'M{i:03}', '反證', f'https://money.udn.com/money/story/1/{i}', 'L2_media') for i in range(8)]
        service = Mock()
        service.fetch_many.return_value = SimpleNamespace(results=[], diagnostics={})
        with patch('research_center.web_fetch_enrichment.WebFetchService', return_value=service):
            _enrich_sources_with_web_fetch(SimpleNamespace(command='research', mode='deep', target='2330',
                market_scope=None, theme_scope=None, candidate_pool=None), sources, {}, prefer_official=True)
        urls = service.fetch_many.call_args.args[0]
        self.assertEqual(len(urls), 8)
        self.assertEqual(sum('official.example' in url for url in urls), 2)
        self.assertEqual(sum('money.udn.com' in url for url in urls), 6)

    def test_worktree_factory_isolated_no_key_and_unchanged_deepseek_limits(self):
        config = ResearchCenterConfig(openai_primary_enabled=True)
        with patch('research_center.ai_provider_service.account_options', return_value={
                'budget_root': self.root/'dev-budget', 'usage_root': self.root/'dev-account', 'development': True}):
            client = create_ai_client(config, self.root)
        self.assertEqual(client.provider, 'openai_router')
        self.assertEqual(client.model, MODEL)
        self.assertEqual(client.primary.accounts, {})
        self.assertNotEqual(client.account.root, client.fallback.account.root)
        self.assertEqual(client.fallback.account.budget.total, Decimal('10'))
        self.assertEqual(client.fallback.account.budget.job_limit, Decimal('.3'))

    def test_capacity_refusal_does_not_drop_data_or_send(self):
        with self.assertRaises(Exception):
            self.client.generate_report('完整資料' * 100000)
        self.assertEqual([request.url.path for request in self.calls], ['/v1/responses/input_tokens'])
        with self.ledger.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM requests').fetchone()[0], 0)

    def test_development_does_not_use_formal_bucket_but_consumes_shared_grant(self):
        dev = OpenAICreditLedger(self.root, [grant()], development=True, job_limit=0, now=lambda: self.now)
        with ai_task('research'):
            _, request_id = dev.choose({'a'}, Decimal('21'), reserve=True)
            dev.finish(request_id, 'success', Decimal('21'), {})
        with ai_task('research'):
            self.ledger.choose({'a'}, Decimal('20'))
        with ai_task('news', background=True):
            with self.assertRaises(OpenAICreditsUnavailable):
                self.ledger.choose({'a'}, Decimal('70'))
        report = self.ledger.summary()['grants'][0]
        self.assertEqual(report['development_used'], '21')
        self.assertEqual(report['production_used'], '0')

    def test_new_verified_cycle_does_not_erase_old_history(self):
        with ai_task('research'):
            _, request_id = self.ledger.choose({'a'}, Decimal('20'), reserve=True)
            self.ledger.finish(request_id, 'success', Decimal('20'), {})
        newer = grant('next')
        newer.update(account_id='a', organization_id='a', starts_at='2026-12-01T00:00:00Z',
                     verified_at='2026-12-01T00:00:00Z', expires_at='2027-01-01T00:00:00Z')
        ledger = OpenAICreditLedger(self.root, [grant(), newer], now=lambda: datetime(2026, 12, 2, tzinfo=timezone.utc))
        self.assertEqual(ledger.choose({'a'})[0]['grant_id'], 'next')
        self.assertEqual(ledger.summary()['grants'][0]['production_used'], '20')

    def test_all_openai_accounts_exhausted_then_deepseek(self):
        self.client.transport = httpx.MockTransport(lambda r: httpx.Response(429, json={'error': {'code': 'credit_balance_exhausted'}}))
        fallback = self.fallback()
        result = RoutedAIService(self.client, fallback).generate_report('完整資料')
        self.assertEqual(result.markdown, '備援正式分析')
        fallback.generate_report.assert_called_once()

    def test_deepseek_exhaustion_is_not_falsely_successful(self):
        self.client.accounts = {}
        fallback = self.fallback()
        fallback.generate_report.side_effect = DeepSeekUnavailable('餘額不足', 'insufficient_balance')
        with self.assertRaises(DeepSeekUnavailable):
            RoutedAIService(self.client, fallback).generate_report('資料')

    def test_secrets_not_written_to_request_or_result_files(self):
        self.client.generate_report('資料')
        for path in (self.root/'requests').glob('*.json'):
            self.assertNotIn('synthetic-key', path.read_text(encoding='utf-8'))

    def test_clone_keeps_fixed_model_effort_and_timeout(self):
        client = self.client.with_timeout(900).with_output_limit(65536)
        client.generate_report('資料')
        body = json.loads(self.calls[0].content)
        self.assertEqual(body['reasoning']['effort'], 'xhigh')
        self.assertEqual(body['max_output_tokens'], 65536)
        self.assertEqual(client.timeout_seconds, 900)

    def test_known_stage_estimate_precedes_paid_calls(self):
        self.client.accounts = {}
        fallback = self.fallback()
        with ai_task('research'):
            router = RoutedAIService(self.client, fallback)
            plan = router.preflight_workflow(['全部資料一', '全部資料二'])
            self.assertEqual(plan['provider'], 'deepseek')
            fallback.account.preflight.assert_called_once()
            router.generate_report('全部資料一')
            router.generate_report('全部資料二')
            self.assertEqual(fallback.generate_report.call_count, 2)
        self.assertEqual(self.calls, [])

    def test_count_only_resolves_byte_estimate_without_raising_cap_or_generating(self):
        self.ledger.job_limit = Decimal('1')
        def counted(request):
            self.calls.append(request)
            self.assertEqual(request.url.path, '/v1/responses/input_tokens')
            self.assertEqual(json.loads(request.content)['input'], '核心' * 100000)
            return httpx.Response(200, json={'object': 'response.input_tokens', 'input_tokens': 10000})
        self.client.transport = httpx.MockTransport(counted)
        fallback = self.fallback()
        plan = RoutedAIService(self.client, fallback).preflight_workflow(['核心' * 100000])
        self.assertEqual(plan['provider'], 'openai')
        self.assertEqual(plan['estimate_basis'], 'official_input_count_plus_full_output_reserve')
        self.assertLess(Decimal(plan['estimated_usd']), self.ledger.job_limit)
        self.assertEqual(self.ledger.job_limit, Decimal('1'))
        fallback.account.preflight.assert_not_called()
        self.assertEqual(self.client.with_output_limit(65536).count_input_tokens('核心' * 100000), 10000)
        self.assertEqual(len(self.calls), 1)
        with self.ledger.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM requests').fetchone()[0], 0)
        receipt = next((self.root/'token-counts').glob('*.json')).read_text(encoding='utf-8')
        self.assertNotIn('synthetic-key', receipt)
        self.assertEqual(json.loads(receipt)['cost_usd'], '0')

    def test_count_failure_stops_before_paid_generation_or_fallback(self):
        self.ledger.job_limit = Decimal('.01')
        self.client.transport = httpx.MockTransport(lambda r: httpx.Response(200, json={'input_tokens': True}))
        fallback = self.fallback()
        with self.assertRaises(OpenAIUnavailable) as caught:
            RoutedAIService(self.client, fallback).preflight_workflow(['核心' * 100000])
        self.assertEqual(caught.exception.status, 'input_count_unavailable')
        fallback.account.preflight.assert_not_called()
        fallback.generate_report.assert_not_called()
        with self.ledger.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM requests').fetchone()[0], 0)

    def test_true_cap_failure_preserves_primary_reason_when_fallback_missing(self):
        self.ledger.job_limit = Decimal('.01')
        self.client.transport = httpx.MockTransport(lambda r: httpx.Response(200,
            json={'object': 'response.input_tokens', 'input_tokens': 500000}))
        fallback = self.fallback()
        fallback.account.preflight.side_effect = DeepSeekUnavailable('未設定備援金鑰', 'not_configured')
        with self.assertRaises(DeepSeekUnavailable) as caught:
            RoutedAIService(self.client, fallback).preflight_workflow(['核心' * 100000])
        self.assertEqual(caught.exception.diagnostics['primary_preflight_status'], 'openai_job_budget_exceeded')
        self.assertIn('單次費用護欄', str(caught.exception))
        self.assertIn('未設定備援金鑰', str(caught.exception))
        self.assertEqual(self.ledger.job_limit, Decimal('.01'))
        fallback.generate_report.assert_not_called()

    def test_search_and_account_errors_preserve_primary_and_missing_fallback_reasons(self):
        self.client.accounts = {}
        fallback = self.fallback()
        fallback._post.side_effect = DeepSeekUnavailable('未設定備援金鑰', 'not_configured')
        fallback.account.preflight.side_effect = DeepSeekUnavailable('未設定備援金鑰', 'not_configured')
        router = RoutedAIService(self.client, fallback)
        for call in (lambda: router._post('search', {'query': '原始查詢'}, 0), router.ensure_ready):
            with self.subTest(call=call), self.assertRaises(DeepSeekUnavailable) as caught:
                call()
            self.assertEqual(caught.exception.diagnostics['primary_status'], 'openai_not_configured')
            self.assertIn('OpenAI', str(caught.exception))
            self.assertIn('未設定備援金鑰', str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_partial_radar_preserves_actual_model_for_already_completed_comments(self):
        from stock_ai_bot.monitoring import radar_service as radar
        candidates = [radar.RadarCandidate(code=str(1000+i), name='合成測試') for i in range(6)]
        codes = [item.code for item in candidates]
        comments = json.dumps({'comments': [{'code': code, 'reason': '已完成的合成短評'} for code in codes[:5]]})
        with patch.object(radar, 'load_research_config', return_value=SimpleNamespace(deepseek_only=True)), \
                patch.object(radar, '_attach_radar_low_model_digest', return_value={}), \
                patch.object(radar, 'RADAR_AI_CHUNK_SIZE', 5), \
                patch.object(radar, '_build_ai_comment_prompt_jobs', side_effect=lambda chunk, *a, **kw:
                    [{'codes': [item.code for item in chunk], 'profile': 'normal', 'prompt': '合成資料'}]), \
                patch.object(radar, '_call_ai_comment_model', side_effect=[comments, DeepSeekUnavailable('護欄停止', 'budget')]), \
                patch('research_center.ai_provider_service.current_routing_metadata',
                    return_value={'actual_model': MODEL, 'routing_history': []}):
            diagnostics = radar._attach_ai_comments(candidates, codes, 'deepseek', self.now.date(), None)
        self.assertEqual(diagnostics['status'], 'partial_success')
        self.assertTrue(all(item.ai_comment['model'] == MODEL for item in candidates[:5]))
        self.assertEqual(candidates[-1].ai_comment['status'], 'failed')
        result = radar.RadarResult(radar.RadarRequest(model='deepseek', ai_top=6), self.now.date(), candidates, codes, {})
        rendered = radar.format_radar_report(result)
        self.assertIn('AI短評：' + MODEL, rendered)

    def test_real_search_is_required_not_just_model_text(self):
        with self.assertRaises(Exception):
            self.client.search('查詢')
        with self.ledger.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM cache').fetchone()[0], 0)

    def test_organizer_still_uses_xhigh_not_disabled_thinking(self):
        self.client.organizer = True
        self.client.transport = httpx.MockTransport(lambda r: (self.calls.append(r) or httpx.Response(200, json=response('{}'))))
        self.client.generate_json('請整理全部事實')
        self.assertEqual(json.loads(self.calls[0].content)['reasoning']['effort'], 'xhigh')

    def test_scheduled_account_gate_does_not_precheck_deepseek_when_openai_ready(self):
        fallback = self.fallback()
        router = RoutedAIService(self.client, fallback)
        router.ensure_ready()
        fallback.account.preflight.assert_not_called()

    def test_fallback_preflight_uses_real_zero_argument_contract_and_checks_estimate(self):
        self.client.accounts = {}
        fallback = self.fallback()
        fallback.account.preflight = Mock(side_effect=lambda: {'ready': True})
        router = RoutedAIService(self.client, fallback)
        with ai_task('research'):
            router.ensure_ready()
            plan = router.preflight_workflow(['完整來源'])
        self.assertEqual(plan['provider'], 'deepseek')
        self.assertEqual(fallback.account.preflight.call_args_list, [unittest.mock.call(), unittest.mock.call()])
        self.assertGreater(fallback.account.budget.check.call_args.kwargs['estimate'], 0)

    def test_research_center_roles_share_route_even_without_legacy_flag(self):
        from research_center.orchestrator import ResearchCenter
        config = ResearchCenterConfig(openai_primary_enabled=True, database_path=self.root/'db.sqlite3')
        router = RoutedAIService(self.client, self.fallback())
        with patch('research_center.ai_provider_service.create_ai_client', return_value=router):
            center = ResearchCenter(config)
        try:
            self.assertTrue(center.config.deepseek_only)
            self.assertIs(center.deepseek, center.opencode)
            self.assertIs(center.deepseek_organizer, center.low_model_minimax)
            self.assertIs(center.deepseek_search.client, router)
            self.assertIs(center._segmented_ai_client_for('deepseek'), router)
            for text in ['/research 2330 --deep', '/value_scan 2330', '/macro', '/theme AI電源',
                         '/theme_flow AI電源', '/theme_radar', '/sector_strength', '/news refresh', '/topic_maintain']:
                self.assertIsNotNone(center.parse(text))
        finally:
            center.database.close()

    def test_news_classifier_calls_terra_json_adapter(self):
        from research_center.news_service import _call_news_classifier
        self.client.transport = httpx.MockTransport(lambda r: httpx.Response(200, json=response('{"items":[]}')))
        router = RoutedAIService(self.client, self.fallback())
        center = SimpleNamespace(config=SimpleNamespace(deepseek_only=True), deepseek_organizer=router)
        result = _call_news_classifier(center, 'deepseek', '新聞資料', 180)
        self.assertEqual(result.diagnostics['actual_model'], MODEL)

    def test_radar_native_json_entry_calls_terra(self):
        from stock_ai_bot.monitoring.radar_service import _call_ai_comment_model_once
        self.client.transport = httpx.MockTransport(lambda r: httpx.Response(200, json=response('{"comments":[]}')))
        center = SimpleNamespace(config=SimpleNamespace(deepseek_only=True), deepseek=RoutedAIService(self.client, self.fallback()))
        with patch('stock_ai_bot.monitoring.radar_service.ResearchCenter', return_value=center):
            self.assertEqual(_call_ai_comment_model_once('deepseek', '完整候選資料'), '{"comments":[]}')

    def test_search_error_reason_is_openai_not_mislabeled_deepseek(self):
        self.client.transport = httpx.MockTransport(lambda r: httpx.Response(401, json={'error': {'code': 'invalid_api_key'}}))
        with ai_task('news', background=True), self.assertRaises(DeepSeekUnavailable) as error:
            self.client.search('查詢')
        self.assertEqual(error.exception.diagnostics['provider'], 'openai')
        notice = self.ledger.claim_notice()
        self.assertIn('OpenAI 工作暫停', notice[1])

    def test_telegram_failure_label_prefers_actual_report_model(self):
        from research_center.telegram_handlers import _fallback_model_label
        result = SimpleNamespace(ai_model=None, request=SimpleNamespace(ai_model='deepseek'),
                                 report_json={'metadata': {'analysis_model': MODEL}})
        self.assertEqual(_fallback_model_label(result), MODEL)
