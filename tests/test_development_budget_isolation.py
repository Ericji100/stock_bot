from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from research_center.ai_budget_service import BudgetBlocked, MonthlyBudget
from research_center.deepseek_service import DeepSeekAccount, DeepSeekUnavailable, account_options, development_accounting
from tools.reconcile_development_spending import apply_plan, make_plan, development_usage_requests


class DevelopmentBudgetIsolationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ.get('CODEX_TASK_TEST_ROOT', '.cache/test_tmp')) / uuid4().hex
        self.root.mkdir(parents=True)
        self.now = datetime(2026, 11, 4, 10, tzinfo=ZoneInfo('Asia/Taipei'))
        self.budget = MonthlyBudget(self.root / 'budget', manual=2, pacing=False, job_limit=0)

    def charge(self, name, amount, *, background=False, status='success'):
        self.budget.reserve_request(name, 'research', background, name, Decimal(amount), self.now)
        self.budget.finish(name, status, Decimal(amount) if status != 'unknown' else None)
        with self.budget.db() as conn:
            return conn.execute('SELECT id,day,background,command,run_id,status,estimate,actual FROM spending WHERE id=?', (name,)).fetchone()

    def attribution(self, row):
        return {'request_id': row[0], 'fingerprint': self.budget.spending_fingerprint(row),
                'evidence': 'verified-test-manifest:sha256', 'reason': '已核對測試請求'}

    def test_verified_test_cost_is_excluded_but_original_charge_unchanged(self):
        row = self.charge('test', '2')
        with self.assertRaises(BudgetBlocked):
            self.budget.check(now=self.now)
        self.budget.attribute_development([self.attribution(row)])
        self.budget.check(estimate=Decimal('2'), now=self.now)
        with self.budget.db() as conn:
            self.assertEqual(conn.execute('SELECT * FROM spending').fetchone(), row)
        result = self.budget.summary(self.now)
        self.assertEqual(result['development_excluded'], '2')
        self.assertEqual(result['manual_used'], '0')
        self.assertEqual(result['manual_limit'], '2')

    def test_genuine_manual_charges_still_block_at_unchanged_limit(self):
        row = self.charge('test', '1')
        self.budget.attribute_development([self.attribution(row)])
        self.charge('normal', '2')
        with self.assertRaises(BudgetBlocked) as exc:
            self.budget.check(now=self.now)
        self.assertEqual(exc.exception.reason, 'manual_budget_exceeded')

    def test_original_costs_still_excluded_after_legacy_import_and_restart(self):
        row = self.charge('test', '2')
        self.budget.attribute_development([self.attribution(row)])
        old = self.root / 'legacy.sqlite3'
        with sqlite3.connect(old) as conn:
            conn.execute('CREATE TABLE usage (id,day,background,command,status,estimated_usd,actual_usd)')
            conn.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?)', (row[0], row[1], 0, 'research', 'success', '2', '2'))
        restarted = MonthlyBudget(self.budget.root)
        restarted.import_legacy(old)
        restarted.check(estimate=Decimal('1'), now=self.now)
        self.assertEqual(restarted.summary(self.now)['manual_used'], '0')

    def test_attribution_is_idempotent(self):
        row = self.charge('test', '1')
        entries = [self.attribution(row)]
        self.assertEqual(self.budget.attribute_development(entries), 1)
        self.assertEqual(self.budget.attribute_development(entries), 0)

    def test_changed_charge_rolls_back_entire_batch(self):
        a = self.charge('first', '1')
        b = self.charge('second', '1')
        entries = [self.attribution(a), self.attribution(b)]
        entries[1]['fingerprint'] = 'incorrect'
        with self.assertRaises(ValueError):
            self.budget.attribute_development(entries)
        self.assertEqual(self.budget.summary(self.now)['development_excluded'], '0')

    def test_unknown_cost_cannot_be_silently_freed(self):
        row = self.charge('unknown', '1', status='unknown')
        with self.assertRaises(ValueError):
            self.budget.attribute_development([self.attribution(row)])
        self.assertEqual(self.budget.summary(self.now)['unknown_or_pending'], 1)

    def test_missing_evidence_rejected(self):
        row = self.charge('test', '1')
        entry = self.attribution(row)
        entry['evidence'] = ''
        with self.assertRaises(ValueError):
            self.budget.attribute_development([entry])

    def test_background_exclusion_does_not_release_real_background_usage(self):
        row = self.charge('test', '1', background=True)
        self.budget.attribute_development([self.attribution(row)])
        self.charge('normal', '1', background=True)
        self.assertEqual(self.budget.summary(self.now)['background_used'], '1')

    def options(self, *, worktree=False, environment=None):
        from research_center.config import load_research_config
        project = self.root / ('worktree' if worktree else 'main')
        project.mkdir(exist_ok=True)
        if worktree:
            (project / '.git').write_text('gitdir: unused', encoding='utf-8')
        with patch.dict(os.environ, {'STOCK_AI_BOT_WORKDATA_PROFILE': 'prod', 'DEEPSEEK_BUDGET_TEST_ROOT': '', **(environment or {})}), \
             patch('research_center.ai_budget_service.shared_budget_root', return_value=self.root / 'main/.workdata/ai_budget'):
            return account_options(load_research_config(), project)

    def test_production_options_keep_formal_limits(self):
        options = self.options()
        self.assertFalse(options['development'])
        self.assertEqual(options['monthly_limit_usd'], 10)
        self.assertEqual(options['manual_monthly_usd'], 2)
        self.assertEqual(options['job_limit_usd'], .30)

    def test_worktree_uses_independent_account_and_budget(self):
        options = self.options(worktree=True)
        self.assertTrue(options['development'])
        self.assertIn('ai_development', options['budget_root'].parts)
        self.assertEqual(options['usage_root'], options['budget_root'] / 'account')

    def test_validation_scope_on_main_is_isolated_and_restored(self):
        self.assertFalse(self.options()['development'])
        with development_accounting():
            self.assertTrue(self.options()['development'])
        self.assertFalse(self.options()['development'])

    def test_development_scope_does_not_import_legacy_to_shared_budget(self):
        original = self.root / 'original'
        original.mkdir()
        with sqlite3.connect(original / 'usage.sqlite3') as conn:
            conn.execute('CREATE TABLE usage (id,day,background,command,status,estimated_usd,actual_usd)')
            conn.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?)', ('old', '2026-11-04', 0, 'research', 'success', '5', '5'))
        account = DeepSeekAccount('fake', original, budget_root=self.root / 'dev-budget',
                                  usage_root=self.root / 'dev-account', development=True)
        self.assertEqual(account.budget.summary(self.now)['used_or_reserved'], '0')
        self.assertEqual(account.root, self.root / 'dev-account')

    def test_explicit_test_root_isolates_both_ledgers(self):
        target = self.root / 'isolated'
        options = self.options(environment={'DEEPSEEK_BUDGET_TEST_ROOT': str(target)})
        self.assertEqual(options['usage_root'], target / 'account')
        self.assertTrue(options['development'])

    def test_test_root_cannot_alias_production(self):
        target = self.root / 'main/.workdata/ai_budget'
        with self.assertRaises(ValueError):
            self.options(environment={'DEEPSEEK_BUDGET_TEST_ROOT': str(target)})

    def test_development_has_no_local_cap_but_balance_guard_remains(self):
        options = self.options(worktree=True)
        account = DeepSeekAccount('fake', self.root / 'ignored', **options,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'is_available': False})))
        account.budget.check(estimate=Decimal('100'), background=True, run_id='test')
        self.assertFalse(account.budget.summary()['limits_enforced'])
        with self.assertRaises(DeepSeekUnavailable):
            account.refresh_balance()

    def test_shared_client_identity_separates_formal_and_test_ledgers(self):
        root = self.root / 'account'
        formal = DeepSeekAccount.shared('fake', root, budget_root=self.root / 'formal')
        development = DeepSeekAccount.shared('fake', root, budget_root=self.root / 'dev', usage_root=self.root / 'dev-account', development=True)
        self.assertIsNot(formal, development)

    def test_evidence_plan_requires_exact_hash_and_retains_raw_rows(self):
        row = self.charge('test', '1')
        proof = self.root / 'manifest.json'
        proof.write_text(json.dumps({'run_id': row[4]}), encoding='utf-8')
        with patch('tools.reconcile_development_spending.shared_budget_root', return_value=self.budget.root):
            plan = make_plan(self.root, [proof])
            self.assertEqual(len(plan['entries']), 1)
            target = self.root / 'plan.json'
            target.write_text(json.dumps(plan), encoding='utf-8')
            with self.assertRaises(ValueError):
                apply_plan(self.root, target, 'wrong')
            sha = hashlib.sha256(target.read_bytes()).hexdigest()
            self.assertEqual(apply_plan(self.root, target, sha), 1)
            self.assertEqual(apply_plan(self.root, target, sha), 0)

    def test_modified_evidence_stops_plan(self):
        row = self.charge('test', '1')
        proof = self.root / 'manifest.json'
        proof.write_text(json.dumps({'run_id': row[4]}), encoding='utf-8')
        with patch('tools.reconcile_development_spending.shared_budget_root', return_value=self.budget.root):
            plan = make_plan(self.root, [proof])
            target = self.root / 'plan.json'
            target.write_text(json.dumps(plan), encoding='utf-8')
            sha = hashlib.sha256(target.read_bytes()).hexdigest()
            proof.write_text('{}', encoding='utf-8')
            with self.assertRaises(ValueError):
                apply_plan(self.root, target, sha)

    def test_production_usage_cannot_be_used_as_development_proof(self):
        account = self.root / '.cache/deepseek/usage.sqlite3'
        account.parent.mkdir(parents=True)
        account.touch()
        with self.assertRaises(ValueError):
            development_usage_requests(self.root, account)

    def test_shared_request_in_production_usage_is_not_reclassified(self):
        project = self.root / 'repo'
        checkout = self.root / 'checkout'
        checkout.mkdir()
        (checkout / '.git').write_text('gitdir: unused', encoding='utf-8')
        paths = [checkout / '.cache/deepseek/usage.sqlite3', project / '.cache/deepseek/usage.sqlite3']
        for path in paths:
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE usage (id,day,background,command,status,estimated_usd,actual_usd)')
                conn.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?)', ('shared', '2026-11-04', 0, 'research', 'success', '1', '1'))
        with sqlite3.connect(paths[0]) as conn:
            conn.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?)', ('only-dev', '2026-11-04', 0, 'research', 'success', '1', '1'))
        with patch('tools.reconcile_development_spending.shared_budget_root', return_value=project / '.workdata/ai_budget'):
            self.assertEqual(set(development_usage_requests(project, paths[0])), {'only-dev'})
