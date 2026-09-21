from copy import deepcopy
from datetime import date

import pandas as pd
import pytest

from scripts import course_corporate_action_backtest as bt


def frames(rows):
    return pd.DataFrame([{'date': pd.Timestamp(d), 'open': o, 'high': h, 'low': l,
        'close': c, 'volume': 1000.} for d, o, h, l, c in rows])


def action(day='2026-06-02', cash=2., ratio=1.):
    return {'date': day, 'cash': cash, 'ratio': ratio, 'pay_date': '2026-06-20', 'kind': 'DIVIDEND'}


def plan_snapshot(frame, thresholds):
    return {'armed': True, 'structural_invalid': False,
        'setup': {'trigger_price': 10., 'pattern_code': 'RECLAIM'},
        'risk': {'chase_cap': 10.5, 'defense': 9.}, 'structural_lens': 'QUADRANT_PRIMARY',
        'taiji': {'sequence': 'NONE', 'state': 'UNDEFINED'}}


def selection(day='2026-06-01'):
    return {'date': date.fromisoformat(day), 'code': '1234', 'name': '測試',
            'strategy_codes': ['B'], 'source_labels': ['radar']}


def test_future_action_never_enters_signal_history():
    f = frames([('2026-06-01',10,11,9,10),('2026-06-02',8,9,7,8)])
    a = [action()]
    pd.testing.assert_frame_equal(bt.adjusted_history(f,a,'2026-06-01'),
                                  bt.adjusted_history(f,[],'2026-06-01'))
    h = bt.adjusted_history(f,a,'2026-06-02')
    assert list(h.close) == [8.,8.]
    assert list(h.high) == [9.,9.]
    assert h.iloc[-1].TR == 2.


def test_share_normalization_is_undone_before_analysis():
    f = frames([('2026-06-01',50,51,49,50),('2026-06-02',50,51,49,50)])
    e = action(cash=0.,ratio=2.)
    raw = bt.restore_raw(f,[e])
    assert list(raw.close) == [100.,50.]
    h = bt.adjusted_history(raw,[e],'2026-06-02')
    assert list(h.close) == [50.,50.]
    assert list(h.volume) == [2000.,1000.]


def test_ex_action_preserves_economic_wealth_and_fixed_risk():
    t = bt.open_trade('1234','測試','2026-06-01',100.,90.,{'armed_date':'2026-05-29'},
                      {'sequence':1,'added_date':'2026-05-29'})
    bt.book_action(t,action(cash=10.,ratio=1.1))
    assert t['defense'] == pytest.approx(80/1.1)
    assert bt.wealth(t,90/1.1) == pytest.approx(100.)
    assert bt.wealth(t,t['defense']) == pytest.approx(90.)
    assert t['cash_entitlements'][0]['amount'] == 10.


def test_cash_is_not_reinvested_and_only_applies_to_prior_holder():
    t = bt.open_trade('1234','測試','2026-06-01',100.,90.,{'armed_date':'2026-05-29'},
                      {'sequence':1,'added_date':'2026-05-29'})
    bt.book_action(t,action(cash=5.))
    assert t['units'] == 1.
    assert bt.wealth(t,95) == 100.
    fresh = bt.open_trade('1234','測試','2026-06-02',95.,85.,{'armed_date':'2026-06-01'},
                          {'sequence':1,'added_date':'2026-06-01'})
    assert not fresh['cash_entitlements']


def test_cash_and_bonus_multiple_actions_do_not_double_count():
    t = bt.open_trade('1234','測試','2026-06-01',100.,90.,{'armed_date':'2026-05-29'},
                      {'sequence':1,'added_date':'2026-05-29'})
    bt.book_action(t,action(cash=4.,ratio=1.2))
    bt.book_action(t,action(day='2026-07-01',cash=2.,ratio=1.1))
    assert t['units'] == pytest.approx(1.32)
    assert sum(x['amount'] for x in t['cash_entitlements']) == pytest.approx(6.4)
    assert bt.wealth(t,t['defense']) == pytest.approx(90.)


def test_pending_plan_converts_but_ex_date_buyer_has_no_dividend(monkeypatch):
    monkeypatch.setattr(bt,'enlightenment_snapshot',plan_snapshot)
    f = frames([('2026-06-01',9.5,9.9,9.4,9.8),('2026-06-02',8,8.6,7.8,8.4)])
    r = bt.replay_stock({'code':'1234','name':'測試'},[selection()],f,[action()])
    t = r['trades'][0]
    assert t['entry_price'] == 8.
    assert t['initial_defense'] == 7.
    assert not t['cash_entitlements']


def test_prior_holder_not_stopped_by_pure_ex_dividend_gap(monkeypatch):
    monkeypatch.setattr(bt,'enlightenment_snapshot',plan_snapshot)
    f = frames([('2026-06-01',9.5,9.9,9.4,9.8),('2026-06-02',10,10.5,9.8,10.2),
                ('2026-06-03',8.2,8.5,8.,8.3)])
    r = bt.replay_stock({'code':'1234','name':'測試'},[selection()],f,[action('2026-06-03')])
    t = r['trades'][0]
    assert t['status']=='OPEN' and t['defense']==7.
    assert t['performance_return_pct']==pytest.approx(3.)


def test_exit_next_open_on_ex_date_keeps_entitlement(monkeypatch):
    monkeypatch.setattr(bt,'enlightenment_snapshot',plan_snapshot)
    f = frames([('2026-06-01',9.5,9.9,9.4,9.8),('2026-06-02',10,10.1,8.7,8.8),
                ('2026-06-03',7.7,8,7.5,7.8)])
    r=bt.replay_stock({'code':'1234','name':'測試'},[selection()],f,[action('2026-06-03',cash=1.)])
    t=r['trades'][0]
    assert t['status']=='CLOSED' and t['exit_date']=='2026-06-03'
    assert t['cash_entitlement_per_original_share']==1.
    assert t['performance_return_pct']==pytest.approx(-13.)


def test_no_actions_matches_legacy_trade_semantics(monkeypatch):
    monkeypatch.setattr(bt,'enlightenment_snapshot',plan_snapshot)
    monkeypatch.setattr(bt.legacy,'enlightenment_snapshot',plan_snapshot)
    f = frames([('2026-06-01',9.5,9.9,9.4,9.8),('2026-06-02',10,10.5,9.8,10.2),
                ('2026-06-03',10,12,8.5,8.8),('2026-06-04',8.6,20,8,19)])
    monkeypatch.setattr(bt.legacy,'load_full_frame',lambda *a:(f,bt.ROOT/'test.csv'))
    stock={'code':'1234','name':'測試','symbol':'1234.TW'}
    new=bt.replay_stock(stock,[selection()],f,[])['trades'][0]
    old=bt.legacy.replay_stock(stock=stock,selections=[selection()],as_of=date(2026,6,4),max_monitor_bars=20)['trades'][0]
    for key in ('entry_date','entry_price','exit_trigger_date','exit_date','exit_price','status'):
        assert new[key]==old[key]
    assert new['performance_return_pct']==pytest.approx(old['performance_return_pct'],abs=.0001)
    assert new['mfe_pct']==pytest.approx(old['mfe_pct'],abs=.0001)


def test_subscription_adjustment_does_not_force_capital_injection():
    e={**action(cash=.5,ratio=1.05),'capital_issue_ratio':.2,'subscription_price':15.}
    assert bt.transform_price(20.,e)==pytest.approx(18.)
    t=bt.open_trade('1234','測試','2026-06-01',20.,18.,{'armed_date':'2026-05-29'},
                    {'sequence':1,'added_date':'2026-05-29'})
    bt.book_action(t,e)
    assert t['units']==1.05  # No invented subscription or new cash contribution.
    assert bt.wealth(t,18.)==pytest.approx(19.4)
