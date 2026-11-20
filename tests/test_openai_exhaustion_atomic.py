from decimal import Decimal

import pytest

from research_center.deepseek_service import ai_task
from research_center.openai_credit_service import OpenAICreditLedger, OpenAIUnavailable
from tests.test_openai_routing import grant


def test_rejected_settlement_and_exhaustion_block_roll_back_together(tmp_path, monkeypatch):
    ledger = OpenAICreditLedger(tmp_path, [grant('secondary')])
    with ai_task('news', background=True):
        selected, identifier = ledger.choose({'secondary'}, Decimal('.2'), reserve=True)
        with ledger.db() as conn:
            before = conn.execute('SELECT * FROM requests').fetchall()
        original = ledger._block

        def fail_after_block(*args):
            original(*args)
            raise RuntimeError('synthetic notification persistence failure')

        monkeypatch.setattr(ledger, '_block', fail_after_block)
        with pytest.raises(RuntimeError):
            ledger.reject_exhausted(identifier, selected, {'error_code': 'insufficient_quota'})
        with ledger.db() as conn:
            assert conn.execute('SELECT * FROM requests').fetchall() == before
            assert conn.execute('SELECT count(*) FROM account_blocks').fetchone()[0] == 0
            assert conn.execute('SELECT count(*) FROM notices').fetchone()[0] == 0
        with pytest.raises(OpenAIUnavailable) as caught:
            ledger.choose({'secondary', 'primary'})
        assert caught.value.status == 'transport_unknown'


def test_late_exhaustion_cannot_replace_stronger_stop_or_reset_cost(tmp_path):
    ledger = OpenAICreditLedger(tmp_path, [grant('secondary'), grant('primary')])
    with ai_task('research'):
        selected, identifier = ledger.choose({'secondary', 'primary'}, Decimal('.2'), reserve=True)
        ledger.block(selected, 'user_pause_existing')
        ledger.reject_exhausted(identifier, selected, {'error_code': 'credit_balance_exhausted'})
        with ledger.db() as conn:
            assert conn.execute('SELECT reason FROM account_blocks').fetchone()[0] == 'user_pause_existing'
            assert conn.execute('SELECT status,estimate,actual FROM requests').fetchone() == ('rejected', '0.2', '0')
        with pytest.raises(OpenAIUnavailable) as caught:
            ledger.choose({'secondary', 'primary'})
        assert caught.value.status == 'openai_account_blocked'


def test_pause_precedes_job_limit_so_it_cannot_fall_back(tmp_path):
    ledger = OpenAICreditLedger(tmp_path, [grant('secondary'), grant('primary')], job_limit='.1')
    ledger.block(ledger.grants[1], 'user_pause_existing')
    with ai_task('research'), pytest.raises(OpenAIUnavailable) as caught:
        ledger.choose({'secondary', 'primary'}, Decimal('1'))
    assert caught.value.status == 'openai_account_blocked'
